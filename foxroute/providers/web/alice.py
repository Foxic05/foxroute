"""Алиса — YandexGPT Pro через WebSocket uniproxy.

Работает без браузера и без прокси. Доступ — кука ``Session_id`` с
``.yandex.ru``.

Особенность, отличающая её от прочих: **нужен шаблон кадра**, снятый один раз
с живой страницы. Протокол uniproxy несёт много служебных полей, которые
подделать вслепую нельзя, поэтому берётся настоящий отправленный кадр, а в
нём подменяются текст и идентификаторы.

**Серверные беседы.** Контекст держится через три поля:
- ``dialog_id`` — один на весь чат, появляется в двух местах (header и
  ``active_chat_dialog_context``); его нельзя менять между ходами.
- ``prev_req_id`` — ``request_id`` предыдущего хода (цепочка).
- ``seqNumber`` — инкрементируется (1, 2, 3…).
Без этого каждый ход начинал новый чат: regex ``_CAPTURED_ID`` затирал в
том числе ``dialog_id``, и сервер не видел продолжения.

Держит около 48 000 символов входа, медиана хода 1.4 секунды — самая
быстрая из веб-сессий.
"""
from __future__ import annotations

import json
import re
import ssl
import struct
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterator

from foxroute.errors import AuthError, ProviderError
from foxroute.paths import app_dir
from foxroute.providers._async import to_sync
from foxroute.providers._http import Accumulated
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)

#: Имя файла с захваченным кадром.
TEMPLATE_NAME = "alice_sent_frame.json"

#: Имя файла с кадром рукопожатия. Нужен только для вложений.
SYNC_NAME = "alice_sync_frame.json"

#: Кадр подписок. Страница шлёт его сразу за рукопожатием.
SUBS_NAME = "alice_subs_frame.json"

#: В шаблоне остаются идентификаторы той сессии, из которой он снят. Сервис
#: отвергает повторное использование, поэтому все такие подменяются свежими.
#:
#: Опознаются по началу: сервис выдаёт их как UUIDv7, где первые байты —
#: отметка времени, и все снятые в один день начинаются одинаково.
#:
#: Брать префикс одного конкретного дня (вроде ``019fcc``) нельзя: это
#: скрытая привязка к шаблону — при его замене префикс перестаёт
#: совпадать, чужие идентификаторы уходят на сервер как есть, и Алиса
#: замолкает, будто протухла кука. Берём общее начало: наши собственные
#: uuid4 под него практически не попадают.
_CAPTURED_ID = re.compile(r"019f[0-9a-f]{2}[0-9a-f-]{28,}")


class AliceProvider(Provider):
    name = "alice"
    #: Картинки — да, документы — НЕТ. Проверено: PNG разбирается и
    #: цитируется, а txt, csv и pdf через тот же путь уходят в молчание —
    #: сервис не отвечает вовсе. В их интерфейсе документы есть, но,
    #: похоже, идут другой дорогой; найти её пока не удалось.
    #: ``files_in=False`` при ``vision=True`` как раз и означает «картинки
    #: принимаю, файлы нет» — отказ придёт до траты запроса.
    #: Веб-поиска НЕТ. Мнимая свежесть обманчива: дату Яндекс кладёт в
    #: системный промпт сам, поиском это не является. В интерфейсе Алисы
    #: тумблера поиска нет вовсе (проверено осмотром композера и меню),
    #: управляемой функции нет — значит и кнопки быть не должно.
    #: «Рассуждать» есть, см. ``_frame`` и рукопожатие в ``_frames``.
    capabilities = Capabilities(text=True, images_out=True,
                                conversations=True, vision=True,
                                thinking=True)

    #: Домен именно ``.ru``. На ``.net`` тот же протокол и те же ответы на
    #: текст — но это ДРУГОЙ мир: загруженный туда файл сервисам аккаунта
    #: неизвестен, и вопрос с ним получает «не вижу картинку». Проверено
    #: сравнением: один и тот же файл, залитый на ``.net``, для
    #: ``is_job_finished`` неотличим от выдуманного идентификатора, а
    #: залитый на ``.ru`` — находится сразу.
    ENDPOINT = "wss://uniproxy.alice.yandex.ru/uni.ws"
    ORIGIN = "https://alice.yandex.ru"

    #: Сколько кадров ждём, прежде чем считать, что ответ не придёт.
    MAX_FRAMES = 40
    #: Пауза между кадрами, после которой считаем сервис замолчавшим.
    FRAME_TIMEOUT = 15

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # Кука НЕ обязательна: без неё Алиса отвечает анонимно. Но лимиты
        # там ниже и памяти чатов нет, поэтому основной путь — аккаунт,
        # а анонимный режим включается явно (см. providers.build).
        self.authorized = bool(credential.value)

    # ── шаблон ────────────────────────────────────────────────────────

    def _template(self) -> dict:
        path = Path(app_dir()) / TEMPLATE_NAME
        if not path.exists():
            raise ProviderError(
                f"нет шаблона кадра ({path}). Снять один раз: открыть "
                f"{self.ORIGIN} в браузере, отправить любое сообщение и "
                "сохранить отправленный кадр WebSocket", self.name)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(
                f"шаблон кадра не читается: {exc}", self.name) from exc

    #: «Рассуждать» в её меню — это пара полей в ``alice_2_settings``,
    #: и лежит оно В САМОМ payload, рядом с ``request``, а не внутри него.
    #: Видно сравнением двух живых отправок: один и тот же вопрос с режимом
    #: и без него отличается ровно этим. Эффект заметный — в ответ приходит
    #: 169 кадров против 29 обычных.
    THINKING_SETTINGS = {"mode": "External", "preset": "thinking"}
    PLAIN_SETTINGS = {"mode": "Pro", "preset": ""}

    def _frame(self, prompt: str, dialog_id: str = "",
               prev_req_id: str = "", seq: int = 1,
               request_id: str = "",
               guids: list[str] | None = None,
               thinking: bool = False) -> tuple[str, str]:
        """Подставить в шаблон текст и идентификаторы. Возвращает (кадр, request_id)."""
        template = self._template()
        if not request_id:
            request_id = str(uuid.uuid4())
        if not dialog_id:
            dialog_id = str(uuid.uuid4())
        try:
            payload = template["event"]["payload"]
            payload["request"]["event"]["text"] = prompt
            # Вложения — списком идентификаторов, полученных при загрузке.
            #
            # Пустой список ОБЯЗАТЕЛЬНО затираем. Шаблон снят с настоящей
            # отправки, и если она была с картинкой, в нём остаётся её
            # идентификатор. Отправить такой вопрос без файла — значит
            # сослаться на чужой мёртвый файл: сервис не отвечает вовсе, и
            # выглядит это как протухший шаблон.
            if guids:
                payload["request"]["event"]["attached_files"] = [
                    {"guid": guid} for guid in guids
                ]
            else:
                payload["request"]["event"].pop("attached_files", None)
            # Режим размышления. В нашем шаблоне этого поля НЕТ вовсе —
            # он снят раньше, чем Алиса обзавелась режимами, — поэтому при
            # надобности добавляем его сами. Когда режим не просят, поле
            # трогаем, только если оно в шаблоне уже есть: вдруг шаблон
            # сняли с включённым режимом, тогда обычный вопрос уехал бы
            # думать без спроса.
            if thinking:
                payload["alice_2_settings"] = dict(self.THINKING_SETTINGS)
            elif "alice_2_settings" in payload:
                payload["alice_2_settings"] = dict(self.PLAIN_SETTINGS)
            payload["header"]["request_id"] = request_id
            payload["header"]["prev_req_id"] = prev_req_id or request_id
            payload["header"]["dialog_id"] = dialog_id
            template["event"]["header"]["messageId"] = str(uuid.uuid4())
            template["event"]["header"]["seqNumber"] = seq
        except (KeyError, TypeError) as exc:
            raise ProviderError(
                f"шаблон кадра непривычной формы, нет поля {exc}", self.name
            ) from exc

        # dialog_id в глубоком active_chat_dialog_context
        try:
            endpoints = payload["request"]["environment_state"]["endpoints"]
            for ep in endpoints:
                for cap in ep.get("capabilities", []):
                    ctx = cap.get("state", {}).get("active_chat_dialog_context")
                    if ctx and "dialog_id" in ctx:
                        ctx["dialog_id"] = dialog_id
        except (KeyError, TypeError):
            pass

        text = json.dumps(template)
        for captured in set(_CAPTURED_ID.findall(text)):
            text = text.replace(captured, str(uuid.uuid4()))
        return text, request_id

    # ── протокол ──────────────────────────────────────────────────────

    @staticmethod
    def _card_text(card: dict) -> str:
        """Текст из карточки любого известного вида.

        Обычный ответ приходит в ``text_card``. А вот разбор картинки — в
        ``neuro_structured_blocks_card``, где текст лежит в ``plain_text``.
        Если читать только первый вид, ответы по вложениям пропадают
        целиком: кадр приходит на 39 килобайт, но выглядит пустым, будто
        Алиса картинку не разобрала.
        """
        plain = (card.get("text_card") or {}).get("text") or ""
        if plain:
            return plain
        blocks = card.get("neuro_structured_blocks_card") or {}
        return blocks.get("plain_text") or ""

    @classmethod
    def _longest_card(cls, response: dict) -> tuple[str, bool]:
        """Текст ответа из кадра и признак последнего.

        Алиса присылает ответ карточками, каждый раз ЦЕЛИКОМ и всё длиннее.
        Берём самую длинную: короткие — это промежуточные состояния того же
        ответа, а не отдельные куски.
        """
        directive = response.get("directive") or {}
        if (directive.get("header") or {}).get("name") != "DeferredAliceResponse":
            return "", False
        answer = (directive.get("payload") or {}).get("json_response") or {}
        cards = (answer.get("base_response") or {}).get("cards") or []
        best = ""
        for card in cards:
            text = cls._card_text(card)
            if len(text) > len(best):
                best = text
        return best, bool(answer.get("is_last"))

    def _begin(self, req: Request) -> tuple[str, str, int]:
        """dialog_id, prev_req_id, seq — из беседы или свежие."""
        conv = req.conversation
        if conv and conv.chat_id:
            prev = ""
            seq = 1
            if conv.last_message_id:
                parts = conv.last_message_id.rsplit(":", 1)
                prev = parts[0]
                if len(parts) == 2:
                    try:
                        seq = int(parts[1]) + 1
                    except ValueError:
                        seq = 2
                else:
                    seq = 2
            return conv.chat_id, prev, seq
        return str(uuid.uuid4()), "", 1

    def _remember(self, req: Request, dialog_id: str,
                  request_id: str, seq: int) -> None:
        """Сохранить беседу, чтобы сервер отдал её клиенту."""
        if req.conversation is None:
            req.conversation = Conversation(provider=self.name,
                                            chat_id=dialog_id)
        req.conversation.chat_id = dialog_id
        req.conversation.last_message_id = f"{request_id}:{seq}"

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Файл едет ТЕМ ЖЕ веб-сокетом, что и вопрос, — отдельной загрузки по
    # HTTP у них нет. Порядок снят с живой страницы:
    #
    #   1. ``File.Upload`` со своим ``streamId`` и описанием файла
    #      (``file_upload_start``: тип, размер, имя, origin «Chat»);
    #   2. сами байты — ДВОИЧНЫМИ кадрами сокета;
    #   3. ``File.Upload`` с ``refMessageId`` первого кадра и
    #      ``chunk_upload_finish: {is_last: true}``;
    #   4. ``streamcontrol`` — закрыть поток.
    #
    # В ответ сервис присылает ``guid`` файла, и уже он кладётся в вопрос
    # как ``attached_files``. Без этого ответа отправлять вопрос бесполезно:
    # ссылаться не на что.

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024
    #: Размер куска. Крупнее слать незачем: сервис читает поток.
    CHUNK = 256 * 1024
    #: Сколько ждать ответа с идентификатором файла.
    UPLOAD_TIMEOUT = 60

    @staticmethod
    def _upload_frames(item, stream_id: int, seq: int) -> tuple[dict, dict, dict]:
        """Три служебных кадра загрузки: начало, конец, закрытие потока."""
        start_id = str(uuid.uuid4())
        head = {"event": {
            "header": {"namespace": "File", "name": "Upload",
                       "streamId": stream_id, "messageId": start_id,
                       "seqNumber": seq},
            "payload": {"file_upload_start": {
                "mime_type": item.mime or "application/octet-stream",
                "size": len(item.data or b""),
                "title": item.filename or "file.bin",
                "origin": "Chat"}}}}
        tail = {"event": {
            "header": {"namespace": "File", "name": "Upload",
                       "streamId": stream_id, "refMessageId": start_id,
                       "messageId": str(uuid.uuid4()), "seqNumber": seq + 1},
            "payload": {"chunk_upload_finish": {"is_last": True}}}}
        close = {"streamcontrol": {"action": 0, "reason": 0,
                                   "streamId": stream_id,
                                   "messageId": start_id}}
        return head, tail, close

    @staticmethod
    def _file_guid(frame: dict) -> str:
        """Идентификатор загруженного файла из ответа сервиса.

        Приходит в директиве ``file_upload_finish`` полем ``file_guid``
        (рядом лежат размер, тип и ссылка на предпросмотр).
        """
        payload = (frame.get("directive") or {}).get("payload") or {}
        done = payload.get("file_upload_finish")
        if isinstance(done, dict):
            return str(done.get("file_guid") or "")
        return ""

    def _sync_frame(self, seq: int) -> str:
        """Кадр рукопожатия. Нужен ТОЛЬКО для загрузки файлов.

        Текстовый вопрос сервис принимает и без него — авторизации из куки
        в заголовках подключения хватает. А вот ``File.Upload`` без
        синхронизованной сессии обрывает соединение с кодом 1011, ничего не
        объясняя.

        Как и кадр вопроса, снят с живой страницы: в нём ``auth_token``,
        ``uuid`` устройства и ``icookie``, которых неоткуда взять.
        """
        path = Path(app_dir()) / SYNC_NAME
        if not path.exists():
            raise ProviderError(
                f"нет шаблона рукопожатия ({path}) — без него сервис не "
                "принимает файлы. Снимается один раз с живого клиента.",
                self.name)
        try:
            frame = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(
                f"шаблон рукопожатия не читается: {exc}", self.name) from exc
        frame["event"]["header"]["messageId"] = str(uuid.uuid4())
        frame["event"]["header"]["seqNumber"] = seq
        return json.dumps(frame)

    def _subs_frame(self, seq: int, dialog_id: str) -> str:
        """Кадр подписок, идущий сразу за рукопожатием.

        Подписываемся на СВОЙ диалог, а не на те, что остались в шаблоне.
        Иначе выходит нескладица: файл загружен в сессию, вопрос уходит в
        диалог, о котором сервис не знает, — и Алиса отвечает «не вижу
        картинку», не сообщая об ошибке.
        """
        path = Path(app_dir()) / SUBS_NAME
        if not path.exists():
            return ""
        try:
            frame = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        frame["event"]["payload"]["subscriptions"] = [
            {"id": dialog_id, "state": {"full_content": {}}}]
        frame["event"]["header"]["messageId"] = str(uuid.uuid4())
        frame["event"]["header"]["seqNumber"] = seq
        return json.dumps(frame)

    async def _settle(self, socket, seconds: float = 5) -> None:
        """Дать сессии устояться после рукопожатия.

        Сразу за ``SynchronizeState`` сервис отказывается принимать
        пользовательские сообщения — так и пишет: «Cannot handle message
        from user after System.SynchronizeState». Он успевает прислать свой
        ``Ping``, на который положено ответить, и только потом считает
        сессию готовой.
        """
        import asyncio

        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), 1.5)
            except asyncio.TimeoutError:
                continue
            if not isinstance(raw, str):
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            head = (frame.get("directive") or {}).get("header") or {}
            if head.get("name") == "Ping":
                # Ответ должен ссылаться на сам пинг: без ``refMessageId``
                # сервис считает кадр безголовым и рвёт соединение.
                await socket.send(json.dumps({"event": {
                    "header": {"namespace": "System", "name": "Pong",
                               "messageId": str(uuid.uuid4()),
                               "refMessageId": head.get("messageId", "")},
                    "payload": {}}}))

    async def _send_file(self, socket, item, stream_id: int,
                         seq: int) -> str:
        """Отправить файл и дождаться его идентификатора."""
        import asyncio

        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        head, tail, close = self._upload_frames(item, stream_id, seq)
        await socket.send(json.dumps(head))

        # Байты идут ТОЛЬКО после разрешения. Сервис отвечает на начало
        # загрузки директивой ``file_upload_start`` и в ней же диктует
        # размер куска. Отправить раньше — значит послать двоичный кадр в
        # ещё не открытый поток: uniproxy разберёт его как обычное
        # сообщение и оборвёт связь с «Condition violated: HasHeader()».
        # Догадаться по этой ошибке не о чем, поэтому здесь ждём явно.
        # Каждый двоичный кадр начинается с НОМЕРА ПОТОКА: четыре байта,
        # старшим вперёд. Без них сервис принимает кадр, но к потоку не
        # относит и на завершении отвечает «file_upload_chunk_finish before
        # all data uploaded» — жалуется на нехватку данных, хотя отправлено
        # всё. Порядок байтов именно старшим вперёд: с обратным ошибка та
        # же, что и без префикса вовсе (проверено обоими).
        chunk = await self._await_permission(socket)
        mark = struct.pack(">I", stream_id)
        for at in range(0, len(raw), chunk):
            await socket.send(mark + raw[at:at + chunk])
        await socket.send(json.dumps(tail))

        deadline = asyncio.get_event_loop().time() + self.UPLOAD_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            try:
                answer = await asyncio.wait_for(socket.recv(),
                                                self.FRAME_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if not isinstance(answer, str):
                continue
            try:
                frame = json.loads(answer)
            except ValueError:
                continue
            guid = self._file_guid(frame)
            if guid:
                # Поток закрываем ТОЛЬКО после подтверждения. Отправленный
                # раньше ``streamcontrol`` попадает в уже закрытый поток, и
                # сервис разбирает его как обычное сообщение — отсюда
                # «Condition violated: HasHeader()» и обрыв связи.
                await socket.send(json.dumps(close))
                return guid
        raise ProviderError(
            "сервис не подтвердил загрузку файла", self.name)

    async def _await_permission(self, socket) -> int:
        """Дождаться разрешения слать байты. Возвращает размер куска."""
        import asyncio

        deadline = asyncio.get_event_loop().time() + self.UPLOAD_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            try:
                answer = await asyncio.wait_for(socket.recv(),
                                                self.FRAME_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if not isinstance(answer, str):
                continue
            try:
                frame = json.loads(answer)
            except ValueError:
                continue
            payload = (frame.get("directive") or {}).get("payload") or {}
            allowed = payload.get("file_upload_start")
            if isinstance(allowed, dict):
                size = allowed.get("max_chunk_size")
                return int(size) if size else self.CHUNK
        raise ProviderError(
            "сервис не разрешил загрузку файла", self.name)

    async def _frames(self, prompt: str, dialog_id: str = "",
                      prev_req_id: str = "", seq: int = 1,
                      request_id: str = "",
                      attachments: list | None = None,
                      thinking: bool = False) -> AsyncIterator[str]:
        import websockets

        if not self.credential.header_safe:
            raise AuthError(
                "кука не пролезает в заголовок (туда можно только latin-1) — "
                "похоже, скопирована с лишним текстом", self.name)

        headers = {"Origin": self.ORIGIN}
        if self.credential.value:
            headers["Cookie"] = f"Session_id={self.credential.value}"
        try:
            connection = websockets.connect(
                self.ENDPOINT, additional_headers=headers,
                ssl=ssl.create_default_context())
        except TypeError:
            connection = websockets.connect(
                self.ENDPOINT, extra_headers=headers,
                ssl=ssl.create_default_context())

        async with connection as socket:
            # Файлы уходят ПЕРЕД вопросом и в том же сокете: вопрос
            # ссылается на их идентификаторы, значит их надо получить
            # раньше. Номера потоков и seqNumber идут подряд.
            guids = []
            step = seq
            if attachments or thinking:
                # Рукопожатие идёт первым. Загрузке оно нужно всегда — без
                # него сокет рвётся. Режиму размышления, похоже, тоже: в
                # рукопожатии страница перечисляет, что умеет принимать, и
                # без этого списка Алиса режим узнаёт, но работу не делает.
                await socket.send(self._sync_frame(step))
                step += 1
                await self._settle(socket)
                subs = self._subs_frame(step, dialog_id or "")
                if subs:
                    await socket.send(subs)
                    step += 1
                for number, item in enumerate(attachments, start=1):
                    guids.append(await self._send_file(socket, item, number,
                                                       step))
                    step += 2

            frame_text, _ = self._frame(prompt, dialog_id, prev_req_id,
                                        step if guids else seq,
                                        request_id, guids, thinking)
            await socket.send(frame_text)

            grown = Accumulated()
            import asyncio

            for _ in range(self.MAX_FRAMES):
                try:
                    raw = await asyncio.wait_for(socket.recv(),
                                                 self.FRAME_TIMEOUT)
                except asyncio.TimeoutError:
                    return
                if not isinstance(raw, str):
                    continue
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue

                whole, last = self._longest_card(frame)
                if whole:
                    delta = grown.feed(whole)
                    if delta:
                        yield delta
                if last:
                    return

    def _stream(self, req: Request) -> Iterator[str]:
        dialog_id, prev_req_id, seq = self._begin(req)
        request_id = str(uuid.uuid4())

        produced = False
        for piece in to_sync(
                lambda: self._frames(req.prompt, dialog_id, prev_req_id, seq,
                                     request_id, req.attachments,
                                     req.thinking),
                timeout=req.timeout):
            produced = True
            yield piece

        if produced:
            self._remember(req, dialog_id, request_id, seq)
        else:
            raise ProviderError(
                "ответа не пришло — обычно это устаревший шаблон кадра, "
                f"реже отвергнутая кука Session_id ({TEMPLATE_NAME})",
                self.name)

    def _draw(self, req: Request) -> list[str]:
        """Картинка YandexART через тот же WebSocket.

        Промпт оборачивается в «Нарисуй …». В ответ приходит не URL, а
        ``generation_id`` внутри ``typed_callback_serialized`` (base64).
        Ссылка строится как ``https://yaart-web-alice-images.s3.yandex.net/{id}:1``
        и появляется на S3 через несколько секунд — ждём.
        """
        import asyncio
        import base64 as _b64

        gen_id = ""

        async def capture() -> None:
            nonlocal gen_id
            import websockets

            if self.credential.value and not self.credential.header_safe:
                raise AuthError(
                    "кука не пролезает в заголовок", self.name)

            headers = {"Origin": self.ORIGIN}
            if self.credential.value:
                headers["Cookie"] = f"Session_id={self.credential.value}"

            try:
                conn = websockets.connect(
                    self.ENDPOINT, additional_headers=headers,
                    ssl=ssl.create_default_context(),
                    ping_interval=20, ping_timeout=60)
            except TypeError:
                conn = websockets.connect(
                    self.ENDPOINT, extra_headers=headers,
                    ssl=ssl.create_default_context(),
                    ping_interval=20, ping_timeout=60)

            async with conn as socket:
                draw_frame, _ = self._frame(f"Нарисуй {req.prompt}")
                await socket.send(draw_frame)
                for _ in range(10):
                    try:
                        raw = await asyncio.wait_for(socket.recv(), 15)
                    except asyncio.TimeoutError:
                        break
                    if not isinstance(raw, str):
                        continue
                    if "typed_callback" not in raw:
                        continue
                    for encoded in re.findall(
                            r'"typed_callback_serialized":"([A-Za-z0-9+/=]+)"',
                            raw):
                        decoded = _b64.b64decode(encoded)
                        ids = re.findall(rb"[0-9a-f]{32}", decoded)
                        if ids:
                            gen_id = ids[0].decode()
                            return

        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                pool.submit(asyncio.run, capture()).result(timeout=60)
        except RuntimeError:
            asyncio.run(capture())

        if not gen_id:
            raise ProviderError("не получен generation_id", self.name)

        url = f"https://yaart-web-alice-images.s3.yandex.net/{gen_id}:1"

        import time
        from foxroute.providers._http import session as make_session

        for _ in range(12):
            time.sleep(5)
            try:
                with make_session() as session:
                    response = session.get(url, timeout=10)
                if response.status_code == 200 and len(response.content) > 1000:
                    return [url]
            except Exception:  # noqa: BLE001 — картинка ещё не готова
                continue
        # За 60 секунд картинка так и не проявилась. Отдавать ссылку на
        # неё нельзя: в чате будет битое изображение, а выглядит как
        # успех. Честный отказ, как у bing/chatgpt.
        raise ProviderError(
            "картинка не собралась за 60 секунд", self.name)
