"""Poe — доступ ко многим ботам на дневных points (poe.com).

Считался непробиваемым: каждый запрос подписывается, а подпись считает
обфусцированный JS. Вскрыт полностью, и работает **на чистом Python**, без
браузера.

Как устроена подпись:

* ``poe-formkey`` — в HTML главной лежит сид из 64 hex-символов внутри
  ``window.<случайное_имя>("<сид>")``. Ключ получается перестановкой символов
  сида по постоянному массиву и обрезкой до 32;
* ``poe-tag-id`` — ``md5(тело + formkey + соль)``, после чего hex-символы на
  позициях 20 и 24 меняются местами.

Как эти постоянные добыты — важнее, чем они сами, потому что при следующей
сборке их JS они изменятся. Обфускацию не разбирали: их же функции вызвали с
подставными аргументами. Декой вместо md5 поймал прообраз, откуда сразу
видны соль и порядок склейки; строка из 64 РАЗНЫХ символов на входе
восстановила перестановку по выходу.

.. warning::
   ``PERMUTATION``, ``SALT``, позиции свопа, ``REVISION`` и ``SEND_HASH`` —
   постоянные конкретной сборки. При редеплое их фронтенда они слетят, и
   провайдер отвалится с ``PersistedQueryNotFound`` или отказом подписи.
   Переснимать тем же приёмом; браузер нужен только на переизвлечение, не
   на каждый запрос.

**Отправка и ответ разнесены.** Мутация возвращает только НАШ идентификатор
сообщения, текста там нет. Ответ бота приезжает потоком по отдельному
сокету tchannel, параметры которого лежат в том же HTML.

**Исчерпание points выглядит обманчиво.** Отправка проходит успешно, HTTP 200
и ``status: "success"``, а отказ приезжает СОСТОЯНИЕМ ответного сообщения:
``state == "error_insufficient_fund"`` с пустым текстом. Без разбора
состояния это выглядит как «провайдер вернул пустоту».
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import time
import uuid
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

#: Порядок, в котором символы сида превращаются в formkey.
PERMUTATION = [27, 53, 47, 48, 52, 14, 63, 7, 38, 28, 60, 30, 51, 42, 15, 40,
               19, 16, 58, 11, 17, 36, 59, 50, 1, 26, 46, 62, 55, 20, 56, 49,
               9, 43]
SALT = "4LxgHM6KpFqokX0Ox"
REVISION = "118ed515209a69eb123c780f0365b39d6fae9c1b"
SEND_HASH = "5fabca615d673d8ed3f075ad35080e9658878e5d7779d2b8e7193f740e1d0e88"

#: Позиции hex-символов, которые меняются местами в подписи тела.
SWAP = (20, 24)

#: Имя функции с сидом у них случайное на каждую сборку, поэтому опираемся на
#: структуру. Длина сужена до 56-72, иначе ловится посторонний короткий hex.
SEED_PATTERN = re.compile(r'window\.[A-Za-z0-9_$]+\("([0-9a-f]{56,72})"\)')
TCHANNEL_PATTERN = re.compile(r'"tchannelData":\{([^}]+)\}')
RESET_PATTERN = re.compile(r'"messagePointResetTime":(\d+)')
AVAILABLE_PATTERN = re.compile(r'"dailyMessagePointsAvailable":(true|false)')

NONCE_ALPHABET = ("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def formkey(seed: str) -> str:
    """Сид из HTML в ключ подписи."""
    return "".join(seed[position] for position in PERMUTATION)[:32]


def tag_id(body: str, key: str) -> str:
    """Подпись тела запроса."""
    digest = list(hashlib.md5((body + key + SALT).encode()).hexdigest())
    first, second = SWAP
    digest[first], digest[second] = digest[second], digest[first]
    return "".join(digest)


def nonce(length: int = 16) -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


class PoeProvider(Provider):
    name = "poe"
    #: Файлы уходят по спецификации GraphQL multipart — см. ``_send``.
    #: Пока ОДИН файл за ход: сборщик тела кладёт одно вложение.
    capabilities = Capabilities(text=True, files_in=True, vision=True)

    BASE = "https://poe.com"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self.cookies = self._parse_cookies(credential.value)
        if "p-b" not in self.cookies:
            raise AuthError(
                "в куках нет p-b — это и есть сессия poe.com", self.name)
        #: Остаток дневных points: -1 есть, 0 кончились, None не спрашивали.
        self.remaining: int | None = None
        self.reset_after = ""

    @staticmethod
    def _parse_cookies(raw: str) -> dict:
        cookies = {}
        for part in (raw or "").split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            cookies[key] = value
        return cookies

    # ── страница ──────────────────────────────────────────────────────

    def _load_page(self, session) -> tuple[str, dict]:
        """Забрать с главной ключ подписи и параметры сокета.

        Заодно читаем остаток points — он лежит в той же странице, и это
        редкость: у большинства сервисов квота не видна вовсе. Но отказывать
        по ней нельзя, см. ``_read_quota``.
        """
        for name, value in self.cookies.items():
            session.cookies.set(name, value, domain=".poe.com")

        response = _http.request(session, "GET", f"{self.BASE}/",
                                 provider=self.name, timeout=60)
        _http.check(self.name, response)
        html = response.text or ""

        self._read_quota(html)

        seed = SEED_PATTERN.search(html)
        channel = TCHANNEL_PATTERN.search(html)
        if not seed or not channel:
            raise ProviderError(
                "в HTML нет сида или параметров сокета — скорее всего сменилась "
                "сборка фронтенда, постоянные подписи надо переснять",
                self.name)
        return formkey(seed.group(1)), json.loads("{" + channel.group(1) + "}")

    def _read_quota(self, html: str) -> None:
        """Разобрать остаток points и время сброса.

        Сведения СПРАВОЧНЫЕ: отказывать по ним нельзя. При
        ``dailyMessagePointsAvailable: false`` бесплатный бот Assistant всё
        равно отвечает; флаг относится, судя по всему, к платным ботам, а не
        ко всему аккаунту.

        Отказ по этому флагу выключил бы провайдер целиком, хотя он
        работает. Единственный достоверный сигнал об исчерпании — состояние
        ответного сообщения ``error_insufficient_fund``, и оно приходит уже
        из сокета.
        """
        reset = RESET_PATTERN.search(html)
        if reset:
            # Время в МИКРОсекундах, не в миллисекундах.
            seconds_left = int(reset.group(1)) / 1e6 - time.time()
            if seconds_left > 0:
                hours, minutes = divmod(int(seconds_left) // 60, 60)
                self.reset_after = f"{hours}ч{minutes:02d}м"

        available = AVAILABLE_PATTERN.search(html)
        if available:
            self.remaining = -1 if available.group(1) == "true" else 0

    # ── отправка ──────────────────────────────────────────────────────

    def _headers(self, query_name: str, body: str, key: str,
                 channel: dict, bot: str) -> dict:
        return {
            "Content-Type": "application/json",
            "poe-formkey": key,
            "poe-tag-id": tag_id(body, key),
            "poe-queryname": query_name,
            "poegraphql": "0",
            "poe-revision": REVISION,
            "poe-tchannel": channel.get("channel", ""),
            "Origin": self.BASE,
            "Referer": f"{self.BASE}/{bot}",
        }

    def _open_socket(self, channel: dict):
        """Поднять сокет ДО отправки, иначе теряется начало ответа."""
        try:
            from websocket import create_connection
        except ImportError as exc:
            raise ProviderError(
                "нужен пакет websocket-client", self.name) from exc

        url = (f"wss://tch{random.randint(100000, 999999)}.tch.poe.com/up/"
               f"{channel['boxName']}/updates?min_seq={channel['minSeq']}"
               f"&channel={channel['channel']}&hash={channel['channelHash']}"
               "&generation=1")
        cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return create_connection(
            url, header=[f"Cookie: {cookie_header}", f"Origin: {self.BASE}"],
            timeout=30)

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    def _mutation(self, prompt: str, bot: str, attachments: list) -> str:
        """Тело мутации отправки. ``attachments`` — метки файлов."""
        return json.dumps({
            "queryName": "sendMessageMutation",
            "variables": {
                "chatId": None, "bot": bot, "query": prompt,
                "source": {"sourceType": "chat_input",
                           "chatInputMetadata": {"useVoiceRecord": False}},
                "clientNonce": nonce(), "sdid": str(uuid.uuid4()),
                "attachments": attachments, "chatNonce": nonce(),
                "existingMessageAttachmentsIds": [], "shouldFetchChat": True,
                "referencedMessageId": None, "parameters": None,
                "fileHashJwts": [], "isTemporary": False,
            },
            "extensions": {"hash": SEND_HASH},
        }, separators=(",", ":"))

    def _send(self, session, prompt: str, key: str, channel: dict,
              bot: str, files: list | None = None) -> str:
        """Отправить сообщение. Возвращает НАШ идентификатор сообщения.

        С файлами уходит НЕ обычным JSON, а по спецификации GraphQL
        multipart: поле ``operations`` с телом мутации, где на месте
        вложений стоят ``null``, поле ``map`` с указанием, какой файл в
        какое место подставить, и сами файлы под именами «0», «1», …
        Иная форма (``queryInfo`` + ``file``) отвергается пустым 400 без
        объяснения.
        """
        files = files or []
        if files:
            holes = [None] * len(files)
            body = self._mutation(prompt, bot, holes)
            fields = {
                "operations": body,
                "map": json.dumps(
                    {str(i): [f"variables.attachments.{i}"]
                     for i in range(len(files))}),
            }
            # Наш сборщик кладёт один файл; для нескольких собираем сами.
            packed, ctype = _http.multipart(
                fields, name="0", filename=files[0][0], data=files[0][1],
                content_type=files[0][2])
            headers = {k: v for k, v in
                       self._headers("sendMessageMutation", body, key,
                                     channel, bot).items()
                       if k.lower() != "content-type"}
            headers["Content-Type"] = ctype
            response = _http.request(
                session, "POST", f"{self.BASE}/api/gql_upload_POST",
                provider=self.name, headers=headers, data=packed, timeout=180)
        else:
            body = self._mutation(prompt, bot, [])
            response = _http.request(
                session, "POST", f"{self.BASE}/api/gql_POST",
                provider=self.name,
                headers=self._headers("sendMessageMutation", body, key,
                                      channel, bot),
                data=body, timeout=60)
        _http.check(self.name, response)
        result = response.json() or {}

        if result.get("errors"):
            message = json.dumps(result["errors"], ensure_ascii=False)[:300]
            if "PersistedQueryNotFound" in message:
                raise ProviderError(
                    "persisted-хеш устарел — сборка фронтенда сменилась, "
                    "постоянные надо переснять", self.name)
            raise ProviderError(message, self.name)

        edge = (result.get("data") or {}).get("messageEdgeCreate") or {}
        status = edge.get("status")
        if status and status != "success":
            raise RateLimited(
                str(edge.get("statusMessage") or status), self.name)

        message_id = ((edge.get("message") or {}).get("node") or {}).get(
            "messageId")
        if message_id is None:
            raise ProviderError(
                "сервер не принял сообщение и не сказал почему", self.name)
        return message_id

    # ── чтение ответа ─────────────────────────────────────────────────

    @staticmethod
    def _messages(raw: str) -> Iterator[dict]:
        """Развернуть конверт сокета в события ``messageAdded``."""
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            return
        for packed in envelope.get("messages") or []:
            try:
                message = json.loads(packed)
            except (ValueError, TypeError):
                continue
            payload = message.get("payload") or {}
            if payload.get("subscription_name") != "messageAdded":
                continue
            added = (payload.get("data") or {}).get("messageAdded")
            if isinstance(added, dict):
                yield added

    def _stream(self, req: Request) -> Iterator[str]:
        bot = self.resolve_model(req) or "Assistant"

        with _http.session() as session:
            key, channel = self._load_page(session)
            socket = self._open_socket(channel)
            try:
                files = []
                for item in req.attachments:
                    raw = item.data or b""
                    if not raw:
                        continue
                    if len(raw) > self.MAX_UPLOAD:
                        raise ProviderError(
                            f"файл больше "
                            f"{self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)
                    files.append((item.filename or "file.bin", raw,
                                  item.mime or "application/octet-stream"))
                my_id = self._send(session, req.prompt, key, channel, bot,
                                   files)

                emitted = ""
                deadline = time.time() + req.timeout

                while time.time() < deadline:
                    try:
                        socket.settimeout(3)
                        raw = socket.recv()
                    except TimeoutError:
                        # Таймаут — штатная пауза, ждём дальше.
                        continue
                    except Exception as exc:  # noqa: BLE001
                        # А вот обрыв связи глотать НЕЛЬЗЯ: иначе порванный
                        # сокет крутит пустой цикл до конца общего срока —
                        # минута ожидания вместо внятного отказа.
                        raise ProviderError(
                            f"связь с сервисом оборвалась: {exc}",
                            self.name) from exc

                    for added in self._messages(raw):
                        # Своё сообщение пропускаем. Различаем ИМЕННО по
                        # идентификатору: поле author у Poe всегда пустое,
                        # и полагаться на него нельзя.
                        if added.get("messageId") == my_id:
                            continue
                        if added.get("author") == "human":
                            continue

                        state = added.get("state") or ""
                        if state.startswith("error"):
                            if "insufficient" in state or "fund" in state:
                                raise RateLimited(
                                    "дневные points кончились", self.name)
                            if emitted.strip():
                                return
                            raise ProviderError(
                                f"ответ не сформирован ({state})", self.name)

                        # Текст приходит НАКОПЛЕННЫМ: в каждом кадре всё
                        # написанное целиком, а не добавка.
                        whole = added.get("text") or ""
                        if whole and len(whole) > len(emitted):
                            yield whole[len(emitted):]
                            emitted = whole

                        if state == "complete":
                            return

                if not emitted:
                    raise ProviderError(
                        f"ответа нет за {req.timeout:.0f} с", self.name)
            finally:
                try:
                    socket.close()
                except Exception:  # noqa: BLE001 — уборка не должна ронять ответ
                    pass
