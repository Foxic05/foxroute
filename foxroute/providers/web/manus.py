"""Manus — облачный агент по Socket.IO (manus.im).

**Это не чат-модель, а агент.** Каждое сообщение поднимает у них виртуальный
компьютер и гоняет агентный цикл — в потоке прямым текстом «Initializing the
computer». На простую просьбу цикл коротко замыкается на прямой ответ модели:
14–40 секунд и 8–16 кредитов из трёхсот в сутки. Отсюда роль запасного, а не
рабочей лошадки.

Авторизация обманчиво проста: кука ``session_id`` — это и есть JWT (HS256,
90 дней). Она уходит и в заголовок, и внутрь рукопожатия Socket.IO.

**REST у них только на чтение.** Отправка сообщения идёт исключительно по
сокету, кадром ``42["message", …]``.

**На пинг обязательно отвечать.** Движок шлёт ``2``, ждёт ``3``; без ответа
сервер рвёт соединение по своему pingTimeout, и это выглядит как оборвавшийся
ответ, а не как наша ошибка.

Ответ берём **самый длинный** ассистентский: в агентном режиме рядом с
готовым текстом приезжает короткая обёртка вроде «Вот готовый текст…», и по
длине она проигрывает.

В агентный цикл непригоден — он сам агент, и протокольные промпты трактует
по-своему.
"""
from __future__ import annotations

import json
import secrets
import time
from urllib.parse import quote
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

WS_URL = ("wss://api.manus.im/socket.io/?locale=en&tz=Europe%2FMoscow"
          "&clientType=web&branch=&EIO=4&transport=websocket")

#: Алфавит их идентификаторов: фронт генерит nanoid из 22 символов base62.
ALPHABET = ("abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

#: Сколько ждём кадр, прежде чем проверить, не пора ли заканчивать.
FRAME_TIMEOUT = 3
#: Сколько добираем после сигнала о завершении: финальный кадр с полным
#: текстом приходит уже ПОСЛЕ статуса.
TAIL_SECONDS = 4


def make_id(length: int = 22) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def extract_jwt(raw: str) -> str:
    """Достать JWT из ключа.

    Принимаем и голый токен, и всю строку кук «a=b; session_id=eyJ…» —
    второе удобнее копировать из инструментов разработчика.
    """
    raw = (raw or "").strip()
    if "session_id=" not in raw:
        return raw
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("session_id="):
            return part[len("session_id="):].strip()
    return raw


class ManusProvider(Provider):
    name = "manus"
    # Потоком отдавать НЕЛЬЗЯ. Текст здесь
    # приходит не приращениями, а конкурирующими вариантами: сперва короткая
    # обёртка «Конечно, я подготовлю…», следом настоящий ответ — который
    # обёртку НЕ продолжает. Отдав обёртку сразу, дописать к ней ответ уже
    # невозможно: получается склейка вида «…определение для вас., которая
    # значительно ускоряет поиск», где у ответа отрезано начало.
    #
    # Поэтому копим все варианты и в конце отдаём самый длинный. Manus и без
    # того тратит 14-40 секунд, так что потери от отсутствия потока здесь нет.
    #: Файлы — см. ``_upload``. Картинки он тоже читает.
    capabilities = Capabilities(text=True, streaming=False,
                                files_in=True, vision=True)

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self._jwt = extract_jwt(credential.value)
        if not self._jwt.startswith("eyJ"):
            # Не формат виноват, а доступ: сюда попадает и пустой ключ, и
            # обрезанный, и просто не тот. Тип должен быть тот же, что при
            # отказе сервера, иначе негодный ключ не отличить от поломки.
            raise AuthError(
                "ключ не похож на JWT — нужна кука session_id с manus.im",
                self.name)
        #: Остаток кредитов по данным сервиса. -1 — ещё не присылал.
        self.remaining = -1
        self.limit = -1
        self.reset_at = ""

    # ── разбор кадров ─────────────────────────────────────────────────

    def _absorb_credits(self, payload: dict) -> bool:
        """Запомнить остаток кредитов. Возвращает True, если они кончились.

        Остаток приходит пушем, без нашего запроса — редкая щедрость: у
        большинства сервисов квота не видна вовсе.
        """
        data = payload.get("data")
        if not isinstance(data, dict) or "freeCredits" not in data:
            return False

        available = ((data.get("freeCredits") or 0)
                     + (data.get("periodicCredits") or 0))
        self.remaining = available
        if data.get("maxRefreshCredits"):
            self.limit = int(data["maxRefreshCredits"])
        if data.get("nextRefreshTime"):
            self.reset_at = str(data["nextRefreshTime"])[:16].replace("T", " ")

        # consumeStatus отличает «кредитов нет» от «нам их ещё не считали».
        return available <= 0 and bool(data.get("consumeStatus", 0))

    @staticmethod
    def _assistant_text(payload: dict) -> str:
        """Накопленный текст ответа из кадра. Пусто — кадр не про текст."""
        event = payload.get("event") or {}
        kind = event.get("type") or payload.get("name")

        if kind == "chat" and event.get("sender") != "user":
            return event.get("content") or ""
        if kind == "chatDelta":
            return (event.get("delta") or {}).get("content") or ""
        return ""

    @staticmethod
    def _is_finished(payload: dict) -> bool:
        event = payload.get("event") or {}
        return (event.get("type") == "statusUpdate"
                and event.get("agentStatus") in ("stopped", "idle", "finished"))

    # ── протокол ──────────────────────────────────────────────────────

    def _connect(self):
        try:
            from websocket import create_connection
        except ImportError as exc:
            raise ProviderError(
                "нужен пакет websocket-client", self.name) from exc

        socket = create_connection(
            WS_URL, header=["Origin: https://manus.im"],
            suppress_origin=True, max_size=None, timeout=30)

        def receive():
            try:
                socket.settimeout(FRAME_TIMEOUT)
                return socket.recv()
            except Exception:  # noqa: BLE001 — таймаут это штатная пауза
                return None

        receive()  # 0{…} — движок открыл соединение
        socket.send("40" + json.dumps({"token": self._jwt}))
        acknowledgement = receive() or ""
        if acknowledgement.startswith("44"):
            # 44 — отказ рукопожатия. Единственный внятный сигнал о том,
            # что кука не принята: дальше сокет просто молчал бы.
            raise AuthError(
                "сессия отклонена — обновить куку session_id", self.name)
        return socket, receive

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Файл кладётся не к ним, а в их хранилище, и в три шага. Порядок снят
    # с живой страницы; имя приёмника — ``getPresignedUploadUrl``.
    #
    #   1. ``/api/chat/getPresignedUploadUrl`` — просим место, получаем
    #      подписанный адрес на запись и ``id`` загрузки;
    #   2. ``PUT`` байтов туда, без нашей авторизации;
    #   3. ``/api/chat/uploadComplete`` — подтверждаем, и в ответ приходит
    #      ПОДПИСАННЫЙ АДРЕС НА ЧТЕНИЕ, который и уходит в сообщение.
    #
    # Тонкость: файл с тем же содержимым сервис узнаёт и второй раз не
    # принимает — переиспользует прежнюю загрузку. Из-за этого повторная
    # отправка того же файла выглядит как «загрузка сломалась», хотя работает.

    API = "https://api.manus.im"

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    def _upload(self, session, item) -> dict:
        """Положить вложение в хранилище и собрать запись для сообщения."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        name = item.filename or "file.bin"
        mime = item.mime or "application/octet-stream"
        head = {
            "Authorization": f"Bearer {self._jwt}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-client-type": "web",
            "x-client-locale": "en",
            "Origin": "https://manus.im",
            "Referer": "https://manus.im/",
        }

        asked = _http.request(
            session, "POST", f"{self.API}/api/chat/getPresignedUploadUrl",
            provider=self.name, headers=head,
            json={"filename": name, "fileType": mime, "fileSize": len(raw)},
            timeout=60)
        _http.check(self.name, asked)
        place = ((asked.json() or {}).get("data") or {})
        target, upload_id = place.get("uploadUrl"), place.get("id")
        if not target or not upload_id:
            raise ProviderError(
                f"сервис не выдал место под файл: {str(place)[:200]}",
                self.name)

        put = _http.request(
            session, "PUT", target, provider=self.name,
            headers={
                "Content-Type": mime,
                # Имя файла хранилище берёт ОТСЮДА, а не из адреса.
                "Content-Disposition":
                    f"attachment; filename*=UTF-8''{quote(name)}",
            },
            data=raw, timeout=300)
        if put.status_code not in (200, 201, 204):
            raise ProviderError(
                f"хранилище отвергло файл: HTTP {put.status_code} "
                f"{(put.text or '')[:200]}", self.name)

        done = _http.request(
            session, "POST", f"{self.API}/api/chat/uploadComplete",
            provider=self.name, headers=head,
            json={"filename": name, "fileSize": len(raw), "id": upload_id},
            timeout=60)
        _http.check(self.name, done)
        link = (((done.json() or {}).get("data") or {}).get("fileUrl") or "")
        if not link:
            raise ProviderError(
                "сервис не подтвердил загрузку файла", self.name)

        return {
            "filename": name,
            "id": f"temp-{make_id()}",
            "type": "file",
            "url": link,
            "contentType": mime,
            "fileMetaData": {"tag": "upload", "uploadStatus": "success",
                             "uploadProgress": 100},
        }

    def _send_prompt(self, socket, prompt: str,
                     attachments: list[dict] | None = None) -> None:
        message = {
            "id": make_id(),
            "timestamp": int(time.time() * 1000),
            "messageStatus": "pending",
            "type": "user_message",
            "sessionId": make_id(),
            "content": "",
            "contents": [{"type": "text", "value": prompt}],
            "messageType": "text",
            "taskMode": "standard",
            "attachments": attachments or [],
        }
        socket.send("42" + json.dumps(["message", message],
                                      ensure_ascii=False))

    def _stream(self, req: Request) -> Iterator[str]:
        attachments = []
        if req.attachments:
            with _http.session() as session:
                attachments = [self._upload(session, item)
                               for item in req.attachments]
        socket, receive = self._connect()
        self._send_prompt(socket, req.prompt, attachments)

        # Копим лучший вариант, а не отдаём по ходу: см. заметку о streaming
        # у класса. Ответом считается самый длинный ассистентский текст.
        best = ""
        out_of_credits = False
        finished_at = 0.0
        deadline = time.time() + req.timeout

        try:
            while time.time() < deadline:
                frame = receive()

                if frame is None:
                    # Тишина. Если агент уже отчитался о завершении и хвост
                    # добран — уходим; иначе ждём дальше.
                    if finished_at and time.time() > finished_at:
                        break
                    continue

                if frame == "2":
                    socket.send("3")  # без понга сервер рвёт соединение
                    continue
                if not frame.startswith("42"):
                    continue

                try:
                    event = json.loads(frame[2:])
                except ValueError:
                    continue
                payload = event[1] if len(event) > 1 else {}
                if not isinstance(payload, dict):
                    continue

                if (payload.get("name") == "error"
                        or (payload.get("event") or {}).get("type") == "error"):
                    raise ProviderError(
                        json.dumps(payload, ensure_ascii=False)[:200], self.name)

                out_of_credits = self._absorb_credits(payload) or out_of_credits

                candidate = self._assistant_text(payload)
                if len(candidate) > len(best):
                    best = candidate

                if self._is_finished(payload) and not finished_at:
                    # Финальный кадр с полным текстом приходит ПОСЛЕ статуса,
                    # поэтому не обрываемся сразу, а добираем хвост.
                    finished_at = time.time() + TAIL_SECONDS
        finally:
            try:
                socket.close()
            except Exception:  # noqa: BLE001 — уборка не должна ронять ответ
                pass

        answer = best.strip()
        if not answer:
            if out_of_credits:
                raise RateLimited("кредиты кончились", self.name)
            raise ProviderError(
                "пустой ответ — задача не дала текста", self.name)
        yield answer
