"""Meta AI — двоичный WebSocket на gateway.meta.ai.

Самый сложный адаптер пула, и единственный, работающий **без браузера**:
публичные реализации без него не обходятся.

Транспорт — их собственный протокол DGW (см. ``_dgw``), внутри кадра JSON,
внутри JSON — protobuf в base64. Схемы protobuf у нас нет, поэтому запрос
собирается по полям вручную, а неизменная часть берётся из шаблона,
снятого один раз с живого клиента.

**Поле f1 трогать нельзя.** Оно целиком из шаблона: там служебные данные
клиента, которые мы не разбираем и подделать не смогли бы. Меняется только
f2 — текст и идентификаторы беседы.

**Ответ приходит в двух форматах, понимать надо оба.** Старый —
``root.f1.f4`` с накопленным текстом и JSON-путём, где ``/sections/0/`` это
размышления, а ``/sections/1/`` ответ. Новый, у рассуждающей модели, —
``root.f1.f5``, где секция опознаётся по идентификатору: начинается на
``reasoning`` значит поток размышлений. Второй формат появился, когда Meta
включила reasoning: разбор одного лишь первого поля даёт пустоту на длинных
запросах, хотя ответ идёт.

**Бюджет кадров щедрый намеренно.** Рассуждающая модель на просьбу вроде
«текст в 800-950 символов» тратит 250+ кадров на один только подсчёт
символов. Низкий потолок кадров оборвал бы поток до ответа, и это выглядело
бы как протухшие куки.

Отвечает за ~19 секунд на односложный ответ, ~35 на протокольный промпт. В
агентный цикл непригоден — отказывается от промптов с описанием инструментов,
отвечая, что это «попытка подменить среду выполнения».
"""
from __future__ import annotations

import base64
import json
import random
import ssl
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterator

from foxroute.errors import AuthError, ProviderError
from foxroute.paths import app_dir
from foxroute.providers._async import to_sync
from foxroute.providers._http import Accumulated
from foxroute.providers.web import _dgw
from foxroute.providers.base import Capabilities, Credential, Provider, Request

#: Потолок кадров — защита от бесконечного потока.
MAX_FRAMES = 600
#: Потолок времени — защита от зацикливания. Работают вместе: первый ловит
#: болтливую модель, второй — застрявшую.
MAX_SECONDS = 180
#: Сколько ждём очередной кадр, прежде чем считать, что ответ кончился.
FRAME_TIMEOUT = 15
#: После того как ответ пошёл, хвост ждём коротко: Meta не всегда шлёт
#: финальный кадр is_end, и без этого поток висел бы полные FRAME_TIMEOUT
#: секунд уже после последнего слова.
TAIL_TIMEOUT = 3

#: Потолок ВХОДЯЩЕГО кадра. Библиотека ``websockets`` по умолчанию рвёт связь
#: на кадре больше 1 МиБ, закрывая её кодом 1009 «message too big» — и это
#: выглядит как отказ сервиса, хотя обрываем мы сами. У Meta так и вышло: она
#: шлёт в каждом кадре ВЕСЬ накопленный ответ, поэтому на длинной генерации
#: кадры неизбежно перерастают умолчание.
MAX_FRAME_BYTES = 32 * 1024 * 1024


class Template:
    """Неизменная часть запроса, снятая с живого клиента.

    Читается один раз. Файлы кладёт процедура захвата: она поднимает Chrome
    через прокси и сохраняет настоящий кадр, из которого берётся поле f1.
    """

    TEMPLATE_FILE = "meta_proto_template.json"
    AUTH_FILE = "meta_ws_auth.json"
    COOKIES_FILE = "meta_cookies.json"
    #: Запасные источники — из прежней раскладки файлов.
    LEGACY_URL_FILE = "meta_ws_url.txt"
    LEGACY_COOKIE_FILE = "/tmp/meta_cookie_str.txt"

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory or app_dir())
        self.f1 = b""
        self.req_id = ""
        self.ws_url = ""
        self.cookie = ""
        self._loaded = False

    def _read_json(self, name: str) -> dict:
        path = self.dir / name
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _split_f1(proto: bytes) -> bytes:
        """Отрезать от шаблона первое поле целиком, вместе с шапкой.

        Формат: байт тега, затем длина переменным числом байт, затем тело.
        Берём всё это как есть — содержимое нас не касается.
        """
        position = 1  # тег поля
        length = 0
        shift = 0
        while position < len(proto):
            byte = proto[position]
            position += 1
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return proto[:position + length]

    def _cookie_string(self) -> str:
        """Куки строкой «a=b; c=d».

        Основной источник — ``meta_cookies.json``. Запасной лежит в ``/tmp``
        и теряется при перезагрузке сервера; читаем его только если
        основного нет.
        """
        stored = self._read_json(self.COOKIES_FILE).get("cookies")
        if isinstance(stored, dict) and stored:
            return "; ".join(f"{k}={v}" for k, v in stored.items())
        if isinstance(stored, list) and stored:
            pairs = [(item.get("name"), item.get("value"))
                     for item in stored if isinstance(item, dict)]
            return "; ".join(f"{k}={v}" for k, v in pairs if k)

        legacy = Path(self.LEGACY_COOKIE_FILE)
        if legacy.exists():
            try:
                return legacy.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        return ""

    def load(self) -> "Template":
        if self._loaded:
            return self

        template = self._read_json(self.TEMPLATE_FILE)
        if not template.get("proto_b64"):
            raise ProviderError(
                f"нет шаблона запроса ({self.dir / self.TEMPLATE_FILE}). "
                "Он снимается один раз с живого клиента: без поля f1 запрос "
                "собрать невозможно", "meta_ai")

        self.f1 = self._split_f1(base64.b64decode(template["proto_b64"]))
        self.req_id = template.get("req_id", "")

        self.ws_url = self._read_json(self.AUTH_FILE).get("ws_url", "")
        if not self.ws_url:
            legacy = self.dir / self.LEGACY_URL_FILE
            if legacy.exists():
                self.ws_url = legacy.read_text(encoding="utf-8").strip()
        if not self.ws_url:
            raise ProviderError(
                f"нет адреса сокета ({self.dir / self.AUTH_FILE})", "meta_ai")

        self.cookie = self._cookie_string()
        self._loaded = True
        return self


class MetaAIProvider(Provider):
    name = "meta_ai"
    #: Вложения на вход — см. ``_upload``. Берёт и картинки, и
    #: документы: приёмник у них так и зовётся «document».
    capabilities = Capabilities(text=True, images_out=True,
                                files_in=True, vision=True)

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self._template = Template()
        # Доступ лежит в файлах, а не в строке ключа. Наличие куки и решает,
        # работаем мы от аккаунта или гостем.
        try:
            self.authorized = bool(self._template.load().cookie)
        except ProviderError:
            # Шаблона нет — провайдер вообще не поднимется, но пусть об этом
            # скажет первый же запрос, с внятным текстом.
            self.authorized = False

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Картинка кладётся в их приёмник ``rupload`` — тот же, что у Facebook,
    # и в два шага на один и тот же адрес со случайным uuid:
    #
    #   1. ``GET`` — заявляем размер и хеш, сервис отвечает смещением;
    #   2. ``POST`` — сами байты, в ответ ``{"media_id": …}``.
    #
    # Авторизация — НЕ куки. Куки он не принимает вовсе («User not
    # authorized»), нужен заголовок ``Authorization: OAuth <токен>``, где
    # токен — тот же ``ecto1:…``, которым мы открываем сокет. Приставка
    # ``OAuth`` обязательна: с голым токеном тот же отказ.

    UPLOAD_URL = "https://rupload.meta.ai/gen_ai_document_gen_ai_tenant"

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64.
    MAX_UPLOAD = 32 * 1024 * 1024

    def _auth_token(self) -> str:
        """Токен доступа из того же файла, что и адрес сокета."""
        return str(self._template._read_json(
            Template.AUTH_FILE).get("auth") or "")

    def _upload(self, item) -> dict:
        """Положить картинку и вернуть её описание для сообщения."""
        import hashlib

        from foxroute.providers import _http

        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        template = self._template.load()
        token = self._auth_token()
        if not token:
            raise AuthError(
                "нет токена доступа — снять заново (meta_ws_auth.json)",
                self.name)

        name = item.filename or "picture.png"
        mime = item.mime or "image/png"
        target = f"{self.UPLOAD_URL}/{uuid.uuid4()}"
        head = {
            "Origin": "https://www.meta.ai",
            "Referer": "https://www.meta.ai/",
            "Cookie": template.cookie or "",
            "Authorization": f"OAuth {token}",
            "x-entity-length": str(len(raw)),
            "desired_upload_handler": "genai_document",
            "is_abra_user": "true",
            "ecto_auth_token": "true",
        }
        digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()

        with _http.session() as session:
            probe = _http.request(
                session, "GET", target, provider=self.name,
                headers={**head, "x-entity-digest": f"sha256 {digest}"},
                timeout=60)
            _http.check(self.name, probe)
            sent = _http.request(
                session, "POST", target, provider=self.name,
                headers={**head, "Offset": "0", "x-entity-type": mime,
                         "x-entity-name": name},
                data=raw, timeout=300)
            _http.check(self.name, sent)
            try:
                answer = sent.json() or {}
            except ValueError as exc:
                raise ProviderError(
                    f"не JSON в ответе приёмника: {(sent.text or '')[:200]}",
                    self.name) from exc

        media_id = answer.get("media_id")
        if not media_id:
            raise ProviderError(
                f"приёмник не выдал метку файла: {str(answer)[:200]}",
                self.name)
        return {"media_id": media_id, "mime": mime, "filename": name}

    # ── сборка запроса ────────────────────────────────────────────────

    @staticmethod
    def _build_f2(text: str, conversation_id: str, message_id: str,
                  timestamp_ms: int, unique_id: int,
                  media: list[dict] | None = None) -> bytes:
        """Изменяемая часть запроса: текст, привязка к беседе и вложения.

        Номера полей и вложенность сняты с настоящего кадра. Значение
        ``\\x0a\\x01\\x30`` в поле 4 — постоянная из шаблона, смысл её нам
        неизвестен, но без неё запрос не принимается.

        Вложение — поле 3, и одного его мало: в САМ ТЕКСТ спереди
        приписывается метка ``<|attachment:N|>``. Без неё модель картинку
        не смотрит.
        """
        conversation = (_dgw.pb_bytes(1, conversation_id)
                        + _dgw.pb_varint(2, timestamp_ms)
                        + _dgw.pb_varint(3, unique_id))
        header = (_dgw.pb_bytes(1, message_id)
                  + _dgw.pb_bytes(2, conversation)
                  + _dgw.pb_varint(5, 1))

        marks, blocks = "", b""
        for number, item in enumerate(media or []):
            marks += f"<|attachment:{number}|> "
            blocks += _dgw.pb_bytes(3, (
                _dgw.pb_bytes(1, _dgw.pb_varint(1, int(item["media_id"])))
                + _dgw.pb_varint(2, 1)
                + _dgw.pb_bytes(3, b"")
                + _dgw.pb_varint(5, 0)
                + _dgw.pb_bytes(6, item["mime"])
                + _dgw.pb_bytes(7, item["filename"])))

        body = (_dgw.pb_bytes(1, header)
                + _dgw.pb_bytes(2, marks + text)
                + blocks
                + _dgw.pb_bytes(4, b"\x0a\x01\x30"))
        return _dgw.pb_bytes(2, body)

    # ── разбор ответа ─────────────────────────────────────────────────

    @staticmethod
    def _extract(payload: bytes) -> tuple[str, str]:
        """Достать из кадра (ответ, размышления). Оба накопленные."""
        root = _dgw.pb_parse(payload)
        answer = ""
        thinking = ""

        for kind, outer in root.get(1, []):
            if kind != "bytes":
                continue
            inner = _dgw.pb_parse(outer)

            # Старый формат: текст в поле 4, а к какой секции он относится —
            # написано в JSON-пути поля 10.
            for kind4, block in inner.get(4, []):
                if kind4 != "bytes":
                    continue
                fields = _dgw.pb_parse(block)

                path = ""
                for kind10, raw in fields.get(10, []):
                    if kind10 != "bytes":
                        continue
                    try:
                        document = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    for operation in document.get("operations", []):
                        path = operation.get("path", "") or path

                text = _dgw.pb_text(fields, 2)
                if not text:
                    continue
                if "/sections/1/" in path:
                    answer = max(answer, text, key=len)
                elif "/sections/0/" in path:
                    thinking = max(thinking, text, key=len)
                elif not path:
                    answer = max(answer, text, key=len)

            # Новый формат: секция опознаётся по идентификатору.
            for kind5, block in inner.get(5, []):
                if kind5 != "bytes":
                    continue
                fields = _dgw.pb_parse(block)
                section = _dgw.pb_text(fields, 1)
                text = _dgw.pb_text(fields, 2)
                if not text:
                    continue
                if section.startswith("reasoning"):
                    thinking = max(thinking, text, key=len)
                else:
                    answer = max(answer, text, key=len)

        return answer, thinking

    # ── протокол ──────────────────────────────────────────────────────

    async def _talk(self, prompt: str,
                    media: list[dict] | None = None) -> AsyncIterator[str]:
        import websockets

        template = self._template.load()

        conversation_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        # Идентификатор сообщения у них собирается из времени и случайных
        # битов — повторяем ту же схему.
        unique = (now_ms << 22) | random.getrandbits(22)

        proto = template.f1 + self._build_f2(
            prompt, conversation_id, message_id, now_ms, unique, media)
        payload = json.dumps(
            {"req-id": template.req_id,
             "payload": base64.b64encode(proto).decode()},
            separators=(",", ":")).encode()

        headers = {"Origin": "https://www.meta.ai"}
        if template.cookie:
            headers["Cookie"] = template.cookie

        import asyncio

        try:
            connection = websockets.connect(
                template.ws_url, additional_headers=headers,
                ssl=ssl.create_default_context(), max_size=MAX_FRAME_BYTES)
        except TypeError:
            connection = websockets.connect(
                template.ws_url, extra_headers=headers,
                ssl=ssl.create_default_context(), max_size=MAX_FRAME_BYTES)

        async with connection as socket:
            # Порядок обязателен: приветствие, установка потока, ещё одно
            # подтверждение, и только потом данные.
            await asyncio.wait_for(socket.recv(), 5)
            await socket.send(_dgw.build_estab_frame(conversation_id))
            await asyncio.wait_for(socket.recv(), 5)
            await socket.send(_dgw.build_data_frame(payload))

            grown = Accumulated()
            deadline = time.time() + MAX_SECONDS
            recv_timeout = FRAME_TIMEOUT

            for _ in range(MAX_FRAMES):
                if time.time() > deadline:
                    return
                try:
                    raw = await asyncio.wait_for(socket.recv(), recv_timeout)
                except asyncio.TimeoutError:
                    return

                frame = _dgw.parse_frame(raw)
                if frame.is_end:
                    return
                if not frame.is_data or not frame.payload:
                    continue

                answer, _thinking = self._extract(frame.payload)
                if not answer:
                    continue
                delta = grown.feed(answer)
                if delta:
                    yield delta
                    # Ответ пошёл — дальше хвост ждём коротко.
                    recv_timeout = TAIL_TIMEOUT

    async def _talk_rich(self, prompt: str) -> AsyncIterator[tuple[str, str]]:
        """То же, но с размышлениями."""
        import asyncio
        import websockets

        template = self._template.load()
        conversation_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        unique = (now_ms << 22) | random.getrandbits(22)

        proto = template.f1 + self._build_f2(
            prompt, conversation_id, message_id, now_ms, unique)
        payload = json.dumps(
            {"req-id": template.req_id,
             "payload": base64.b64encode(proto).decode()},
            separators=(",", ":")).encode()

        headers = {"Origin": "https://www.meta.ai"}
        if template.cookie:
            headers["Cookie"] = template.cookie

        try:
            connection = websockets.connect(
                template.ws_url, additional_headers=headers,
                ssl=ssl.create_default_context(), max_size=MAX_FRAME_BYTES)
        except TypeError:
            connection = websockets.connect(
                template.ws_url, extra_headers=headers,
                ssl=ssl.create_default_context(), max_size=MAX_FRAME_BYTES)

        async with connection as socket:
            await asyncio.wait_for(socket.recv(), 5)
            await socket.send(_dgw.build_estab_frame(conversation_id))
            await asyncio.wait_for(socket.recv(), 5)
            await socket.send(_dgw.build_data_frame(payload))

            grown_answer = Accumulated()
            grown_think = Accumulated()
            deadline = time.time() + MAX_SECONDS
            recv_timeout = FRAME_TIMEOUT

            for _ in range(MAX_FRAMES):
                if time.time() > deadline:
                    return
                try:
                    raw = await asyncio.wait_for(socket.recv(), recv_timeout)
                except asyncio.TimeoutError:
                    return

                frame = _dgw.parse_frame(raw)
                if frame.is_end:
                    return
                if not frame.is_data or not frame.payload:
                    continue

                answer, thinking = self._extract(frame.payload)
                if thinking:
                    delta = grown_think.feed(thinking)
                    if delta:
                        yield ("thinking", delta)
                        recv_timeout = TAIL_TIMEOUT
                if answer:
                    delta = grown_answer.feed(answer)
                    if delta:
                        yield ("text", delta)
                        recv_timeout = TAIL_TIMEOUT

    def _stream(self, req: Request) -> Iterator[str]:
        produced = False
        media = [self._upload(item) for item in req.attachments]
        for piece in to_sync(lambda: self._talk(req.prompt, media),
                             timeout=req.timeout):
            produced = True
            yield piece

        if not produced:
            if not self._template.cookie:
                raise AuthError(
                    "ответа не пришло, и куки не заданы — снять их заново "
                    "(meta_cookies.json)", self.name)
            raise ProviderError(
                "пустой ответ: либо устарел шаблон запроса, либо кончился "
                f"бюджет в {MAX_FRAMES} кадров", self.name)

    # stream_rich НЕ переопределяем. У Meta AI секции reasoning-* содержат
    # тот же текст, что и ответ, а не отдельные рассуждения. На простых
    # запросах thinking = дубль ответа, показывать его дважды только путает.
    # Настоящие размышления появляются лишь на сложных вопросах, и там они
    # смешаны с ответом в одном потоке, отделить невозможно.

    def _draw(self, req: Request) -> list[str]:
        """Картинки через Muse Image по тому же DGW WebSocket.

        Отличие от текста: ссылки на картинки лежат прямо в бинарных кадрах
        как URL вида ``https://scontent…``. Protobuf разбирать не нужно —
        regex по сырым байтам надёжнее.
        """
        import re as _re

        urls: list[str] = []

        async def capture():
            import asyncio
            import websockets

            template = self._template.load()
            conversation_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
            now_ms = int(time.time() * 1000)
            unique = (now_ms << 22) | random.getrandbits(22)

            proto = template.f1 + self._build_f2(
                req.prompt, conversation_id, message_id, now_ms, unique)
            payload = json.dumps(
                {"req-id": template.req_id,
                 "payload": base64.b64encode(proto).decode()},
                separators=(",", ":")).encode()

            headers = {"Origin": "https://www.meta.ai"}
            if template.cookie:
                headers["Cookie"] = template.cookie

            try:
                connection = websockets.connect(
                    template.ws_url, additional_headers=headers,
                    ssl=ssl.create_default_context(), max_size=MAX_FRAME_BYTES)
            except TypeError:
                connection = websockets.connect(
                    template.ws_url, extra_headers=headers,
                    ssl=ssl.create_default_context(), max_size=MAX_FRAME_BYTES)

            async with connection as socket:
                await asyncio.wait_for(socket.recv(), 5)
                await socket.send(_dgw.build_estab_frame(conversation_id))
                await asyncio.wait_for(socket.recv(), 5)
                await socket.send(_dgw.build_data_frame(payload))

                deadline = time.time() + MAX_SECONDS
                for _ in range(MAX_FRAMES):
                    if time.time() > deadline:
                        break
                    try:
                        raw = await asyncio.wait_for(socket.recv(), 20)
                    except asyncio.TimeoutError:
                        break

                    frame = _dgw.parse_frame(raw)
                    if frame.is_end:
                        break
                    if frame.is_data and frame.payload:
                        for match in _re.finditer(
                                rb"https://scontent[^\s\x00-\x1f\"]{20,}",
                                frame.payload):
                            url = match.group(0).decode("utf-8", "replace")
                            if url not in urls:
                                urls.append(url)

        import asyncio
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                pool.submit(asyncio.run, capture()).result(timeout=req.timeout)
        except RuntimeError:
            asyncio.run(capture())

        if not urls:
            raise ProviderError("картинки не получены", self.name)
        return urls
