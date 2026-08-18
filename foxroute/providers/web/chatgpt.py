"""ChatGPT — веб-сессия chatgpt.com.

Доступ — кука ``__Secure-next-auth.session-token``. Браузер иногда режет её
на ``.0`` и ``.1``; такие половинки склеиваются через ``|``, и разделитель
убирается при разборе.

Протокол в четыре шага: access-токен, требования sentinel, решение
Proof-of-Work, сама беседа потоком. Turnstile пока не спрашивают.

**Главная ловушка — одноразовый access-токен.** Кешировать его нельзя: OpenAI
принимает токен ровно один раз, повторный уходит в 403. Даже короткий кеш
на 120 секунд оставит рабочим только первый запрос в окне, а выглядит это
как «кука протухла»: со свежим токеном проходят 4 запроса из 4,
с закешированным — ни одного.

Держит около 48 000 символов входа (на 100 000 — HTTP 500), медиана хода
3.8 секунды.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers._http import Accumulated
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)


class ChatGPTProvider(Provider):
    name = "chatgpt"
    capabilities = Capabilities(text=True, images_out=True,
                                conversations=True, files_in=True,
                                files_out=True, vision=True,
                                web_search=True, thinking=True,
                                deep_research=True)

    BASE = "https://chatgpt.com"
    #: Тот же User-Agent обязан уйти и в заголовок, и в конфигурацию PoW —
    #: сервер сверяет их между собой.
    USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

    #: Потолок перебора при решении PoW. Обычная сложность решается за
    #: единицы тысяч итераций; потолок нужен, чтобы не молотить вечно, если
    #: сервис однажды поднимет требования.
    POW_ATTEMPTS = 200_000

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if not credential.value:
            raise ProviderError(
                "нужна кука session-token с chatgpt.com", self.name)
        # Кука могла приехать двумя половинками, склеенными через '|'.
        self._cookie = credential.value.replace("|", "")

    # ── доступ ────────────────────────────────────────────────────────

    def _session(self):
        session = _http.session()
        session.headers["User-Agent"] = self.USER_AGENT
        session.cookies.set("__Secure-next-auth.session-token",
                            self._cookie, domain="chatgpt.com")
        return session

    def _access_token(self, session) -> str:
        """Свежий access-токен. Именно свежий — см. заметку в шапке модуля.

        Кеша здесь нет намеренно, и заводить его нельзя: запрос дешёвый
        (обычный GET), а закешированный токен ломает всё, кроме первого
        обращения, причём в виде, неотличимом от протухшей куки.
        """
        response = _http.request(
            session, "GET", f"{self.BASE}/api/auth/session",
            provider=self.name, timeout=60)
        _http.check(self.name, response)
        try:
            token = (response.json() or {}).get("accessToken", "")
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на запрос сессии", self.name) from exc
        if not token:
            raise AuthError(
                "сессия без токена — кука session-token протухла", self.name)
        return token

    # ── Proof-of-Work ─────────────────────────────────────────────────

    @classmethod
    def solve_pow(cls, seed: str, difficulty: str) -> str | None:
        """Подобрать ответ на задачу sentinel.

        Ищем такой счётчик, при котором ``sha3_512(seed + base64(конфиг))``
        начинается достаточно малым префиксом. Конфигурация имитирует
        браузерное окружение; её состав подсмотрен у настоящего клиента и
        менять поля нельзя — сервер сверяет часть из них (User-Agent, язык)
        с заголовками запроса.
        """
        width = len(difficulty)
        if not width:
            return None

        config = [
            random.choice([3008, 4010, 6000]) * random.choice([1, 2, 4]),
            datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            None,
            0,  # счётчик, подбирается ниже
            cls.USER_AGENT,
            "https://tcr9i.chat.openai.com/v2/"
            "35536E1E-65B4-4D96-9D97-6ADB7EFF8147/api.js",
            "dpl=1440a687921de39ff5ee56b92807faaadce73f13",
            "en", "en-US", None,
            "plugins-[object PluginArray]",
            "_reactListeningcfilawjnerp", "alert",
        ]

        for attempt in range(cls.POW_ATTEMPTS):
            config[3] = attempt
            encoded = base64.b64encode(
                json.dumps(config).encode()).decode()
            digest = hashlib.sha3_512((seed + encoded).encode()).hexdigest()
            if digest[:width] <= difficulty:
                return "gAAAAAB" + encoded
        return None

    # ── протокол ──────────────────────────────────────────────────────

    def _requirements(self, session, headers: dict) -> tuple[str, str | None]:
        """Токен требований и, если просят, решённый PoW."""
        response = _http.request(
            session, "POST",
            f"{self.BASE}/backend-api/sentinel/chat-requirements",
            provider=self.name, headers=headers, json={"p": None}, timeout=60)
        _http.check(self.name, response)
        try:
            payload = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в требованиях sentinel", self.name) from exc

        chat_token = payload.get("token")
        if not chat_token:
            raise ProviderError("sentinel не выдал токен требований", self.name)

        task = payload.get("proofofwork") or {}
        if not task.get("required"):
            return chat_token, None

        solved = self.solve_pow(task.get("seed", ""), task.get("difficulty", ""))
        if solved is None:
            raise ProviderError(
                f"не удалось решить Proof-of-Work за {self.POW_ATTEMPTS} "
                "попыток — сервис поднял сложность", self.name)
        return chat_token, solved

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Загрузка в три шага, и пропустить нельзя ни одного: завести файл,
    # положить байты в их хранилище, подтвердить готовность. Байты уезжают
    # НЕ на chatgpt.com, а в Azure Blob по одноразовой ссылке, поэтому там
    # свои заголовки (``x-ms-blob-type``) и никакой нашей авторизации.
    #
    # ``use_case`` решает, чем файл станет для модели: ``multimodal`` —
    # картинка, которую видно, ``my_files`` — документ, который читают.
    # Перепутать значит отдать картинку как документ и получить разбор
    # байтов вместо описания.

    #: Потолок на файл. Сервис отвергает с 513 МБ (``file_too_large``),
    #: причём одинаково для документов и картинок. По ТИПУ он не фильтрует
    #: вообще — принимает и .exe, и .zip, и .mp4; прочитает ли их модель,
    #: вопрос отдельный.
    #:
    #: Свой потолок держим много ниже: файл едет к нам в base64 (это +33%
    #: к объёму) и целиком живёт в памяти браузера и сервера. Полгигабайта
    #: так гонять нельзя, а 64 МБ покрывают любой документ и фотографию.
    SERVICE_LIMIT = 512 * 1024 * 1024
    MAX_UPLOAD = 64 * 1024 * 1024

    @staticmethod
    def _measure(raw: bytes) -> tuple[int, int]:
        """Ширина и высота картинки прямо из байтов. (0, 0) — не разобрали.

        Размеры обязательны: без них сервис принимает файл, но модель его
        не видит и отвечает пустотой. Читаем заголовок сами — тащить ради
        двух чисел Pillow в зависимости незачем.
        """
        import struct

        # PNG: ширина и высота лежат в IHDR сразу после подписи.
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 24:
            width, height = struct.unpack(">II", raw[16:24])
            return width, height

        # GIF: little-endian сразу после версии.
        if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) > 10:
            width, height = struct.unpack("<HH", raw[6:10])
            return width, height

        # JPEG: идём по сегментам до кадра SOF, там и размеры.
        if raw[:2] == b"\xff\xd8":
            pos = 2
            while pos + 9 < len(raw):
                if raw[pos] != 0xFF:
                    pos += 1
                    continue
                marker = raw[pos + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", raw[pos + 5:pos + 9])
                    return width, height
                size = struct.unpack(">H", raw[pos + 2:pos + 4])[0]
                pos += 2 + size

        # WebP: размеры в блоке VP8X, простой вариант.
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) > 30:
            if raw[12:16] == b"VP8X":
                width = int.from_bytes(raw[24:27], "little") + 1
                height = int.from_bytes(raw[27:30], "little") + 1
                return width, height

        return 0, 0

    def _upload(self, session, headers: dict, item) -> dict:
        """Залить один файл. Возвращает описание для тела сообщения."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("вложение пустое", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл {item.filename or ''} больше "
                f"{self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        mime = item.mime or "application/octet-stream"
        picture = mime.startswith("image/")
        name = item.filename or ("image.png" if picture else "file.bin")

        width = height = 0
        opening = {"file_name": name, "file_size": len(raw),
                   "use_case": "multimodal" if picture else "my_files"}
        if picture:
            width, height = self._measure(raw)
            if not width or not height:
                # Размеры не разобрали — как картинку слать нельзя, модель
                # получит пустоту. Отдаём документом: прочитать сможет.
                picture = False
                opening["use_case"] = "my_files"
            else:
                opening.update({"width": width, "height": height})

        opened = _http.request(
            session, "POST", f"{self.BASE}/backend-api/files",
            provider=self.name, headers=headers, json=opening, timeout=60)
        _http.check(self.name, opened)
        info = opened.json() or {}
        file_id = info.get("file_id")
        target = info.get("upload_url")
        if not file_id or not target:
            raise ProviderError("сервис не выдал ссылку для загрузки",
                                self.name)

        put = _http.request(
            session, "PUT", target, provider=self.name, data=raw,
            headers={"Content-Type": mime,
                     "x-ms-blob-type": "BlockBlob",
                     "x-ms-version": "2020-04-08",
                     "Origin": self.BASE},
            timeout=180)
        _http.check(self.name, put)

        done = _http.request(
            session, "POST",
            f"{self.BASE}/backend-api/files/{file_id}/uploaded",
            provider=self.name, headers=headers, json={}, timeout=60)
        _http.check(self.name, done)

        return {"id": file_id, "name": name, "size": len(raw),
                "mime": mime, "picture": picture,
                "width": width, "height": height}

    @staticmethod
    def _with_files(message: dict, uploaded: list[dict]) -> None:
        """Вписать загруженные файлы в сообщение.

        Картинки идут ещё и в ``parts`` указателями — иначе модель их не
        увидит, сколько их ни перечисляй в метаданных. Документы живут
        только в метаданных, их сервис читает сам.
        """
        pictures = [f for f in uploaded if f["picture"]]
        if pictures:
            text = message["content"]["parts"][0]
            message["content"] = {
                "content_type": "multimodal_text",
                "parts": [
                    *({"asset_pointer": f"file-service://{f['id']}",
                       "size_bytes": f["size"],
                       "width": f["width"], "height": f["height"]}
                      for f in pictures),
                    text,
                ],
            }
        message["metadata"] = {
            "attachments": [
                {"id": f["id"], "name": f["name"],
                 "size": f["size"], "mimeType": f["mime"],
                 **({"width": f["width"], "height": f["height"]}
                    if f["picture"] else {})}
                for f in uploaded
            ]
        }

    #: Метки цитат на файл. Сервис оборачивает их символами приватной
    #: зоны Юникода:  открывает,  закрывает,  разделяет
    #: части. В чате это выглядит как «filecite turn0file0L1-L2» —
    #: мусор, которого человек не звал. Вырезаем вместе с содержимым.
    #: Хвост без закрывающего символа тоже режем: ответ приходит
    #: НАКОПЛЕННЫМ, и на середине цитаты кадр обрывается прямо посреди
    #: метки. Оставь его — и подсчёт приращений собьётся, текст задвоится.
    _CITATION = re.compile(".*?(?:|$)", re.S)

    @classmethod
    def _clean(cls, text: str) -> str:
        """Убрать служебные метки цитат из ответа."""
        if "" not in text:
            return text
        return cls._CITATION.sub("", text).rstrip()

    #: Размышление и исследование у ChatGPT — это ОТДЕЛЬНЫЕ МОДЕЛИ, а не
    #: флаги. Список для нашей учётки берётся из ``/backend-api/models``:
    #: ``gpt-5-4-t-mini`` там подписана «GPT-5.4 Thinking Mini»,
    #: ``research`` — «Deep Research». Подсказку ``system_hints: ["reason"]``
    #: сервис молча глотает, ответ обычный.
    THINKING_MODEL = "gpt-5-4-t-mini"
    RESEARCH_MODEL = "research"

    def _pick_model(self, req: Request) -> str:
        """Модель под задачу: исследование, размышление или обычная."""
        if req.deep_research:
            return self.RESEARCH_MODEL
        if req.thinking:
            return self.THINKING_MODEL
        return self.resolve_model(req)

    def _body(self, model: str, req: Request) -> dict:
        conv = req.conversation
        # Продолжение беседы: реальный parent = id прошлого ответа, тот же
        # conversation_id. Первый ход — случайный parent, чата ещё нет.
        parent = (conv.last_message_id if conv and conv.last_message_id
                  else str(uuid.uuid4()))
        body = {
            "action": "next",
            "messages": [{
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [req.prompt]},
            }],
            "parent_message_id": parent,
            "model": model,
            "timezone_offset_min": -180,
            "conversation_mode": {"kind": "primary_assistant"},
            # Для продолжения история должна храниться на сервере — иначе
            # ChatGPT не вспомнит прошлый ход даже по conversation_id.
            "history_and_training_disabled": False,
        }
        # Поиск в сети включается подсказкой, тем же способом, что и
        # рисование (``picture_v2`` в ``_draw``). Без неё модель отвечает
        # по памяти и о свежем говорит уверенно и неверно.
        if req.web_search:
            body["system_hints"] = ["search"]
        if conv and conv.chat_id:
            body["conversation_id"] = conv.chat_id
        return body

    def _remember(self, req: Request, conv_id: str, msg_id: str) -> None:
        if not conv_id:
            return
        if req.conversation is None:
            req.conversation = Conversation(provider=self.name,
                                            chat_id=conv_id)
        req.conversation.chat_id = conv_id
        if msg_id:
            req.conversation.last_message_id = msg_id

    @classmethod
    def _assistant_text(cls, event: dict) -> str:
        """Накопленный текст ответа из кадра. Пусто — кадр не наш."""
        message = event.get("message") or {}
        if (message.get("author") or {}).get("role") != "assistant":
            return ""
        parts = (message.get("content") or {}).get("parts") or []
        if not parts:
            return ""
        return cls._clean("".join(str(part) for part in parts))

    #: Ссылки на файлы, созданные моделью в песочнице кода. Настоящим
    #: адресом это не является: файл лежит у них внутри и забирается
    #: отдельным запросом.
    _SANDBOX = re.compile(r"sandbox:(/[^\s\)\]]+)")

    #: Сколько файлов забираем за один ход и насколько крупный тянем.
    #: Файл уезжает человеку прямо в тексте ответа (base64, это +33%), так
    #: что потолок здесь — не про сервис, а про то, сколько разумно
    #: положить в одно сообщение.
    MAX_MADE_FILES = 5
    MAX_MADE_BYTES = 12 * 1024 * 1024

    def _fetch_made_files(self, session, headers: dict, conv_id: str,
                          msg_id: str, text: str) -> str:
        """Забрать файлы, которые модель создала сама.

        Возвращает готовый кусок разметки со ссылками или пустую строку.

        Просить файл надо ровно с двумя параметрами — ``message_id`` и
        ``sandbox_path``; без первого приходит 422, без второго тоже.
        Выданная ссылка ведёт на их ``backend-api`` и живёт только с нашей
        авторизацией, поэтому тянем байты здесь и отдаём их сами — иначе у
        человека в браузере будет 403, как и с картинками.
        """
        paths = list(dict.fromkeys(self._SANDBOX.findall(text or "")))
        if not paths or not conv_id or not msg_id:
            return ""

        links: list[str] = []
        trouble: list[str] = []
        for path in paths[:self.MAX_MADE_FILES]:
            name = path.rsplit("/", 1)[-1]
            try:
                blob = self._download_made(session, headers, conv_id,
                                           msg_id, path)
            except ProviderError as exc:
                trouble.append(f"{name}: {exc}")
                continue
            if blob is None:
                continue

            kind, body = blob
            packed = base64.b64encode(body).decode()
            size = len(body) // 1024 or 1
            links.append(f"[{name}](data:{kind};base64,{packed}) — {size} КБ")

        nl = chr(10)
        tail = ""
        if links:
            tail += nl + nl + "Файлы: " + " · ".join(links)
        if trouble:
            # Молчать нельзя: человек видит в тексте ссылку на файл, а файла
            # нет, и без причины это выглядит как поломка интерфейса.
            tail += nl + nl + "Не удалось забрать: " + "; ".join(trouble)
        return tail

    def _download_made(self, session, headers: dict, conv_id: str,
                       msg_id: str, path: str) -> tuple[str, bytes] | None:
        """Один файл из песочницы: тип содержимого и байты.

        ``None`` — файла по этому пути нет (модель сослалась на то, чего не
        создала). Настоящая неурядица уходит исключением.
        """
        query = urllib.parse.urlencode(
            {"message_id": msg_id, "sandbox_path": path})
        handle = _http.request(
            session, "GET",
            f"{self.BASE}/backend-api/conversation/{conv_id}"
            f"/interpreter/download?{query}",
            provider=self.name, headers=headers, timeout=60)
        if handle.status_code == 404:
            return None
        _http.check(self.name, handle)
        try:
            url = (handle.json() or {}).get("download_url", "")
        except ValueError as exc:
            raise ProviderError("не JSON в ответе на запрос файла",
                                self.name) from exc
        if not url:
            return None

        blob = _http.request(session, "GET", url, provider=self.name,
                             headers=headers, timeout=120)
        _http.check(self.name, blob)
        body = blob.content or b""
        if not body:
            return None
        if len(body) > self.MAX_MADE_BYTES:
            raise ProviderError(
                f"{len(body) // 1024 // 1024} МБ — больше потолка "
                f"{self.MAX_MADE_BYTES // 1024 // 1024} МБ на файл в ответе",
                self.name)

        kind = (blob.headers.get("content-type")
                or "application/octet-stream").split(";")[0]
        return kind, body

    def _hide_conversation(self, session, headers: dict, conv_id: str) -> None:
        """Убрать беседу из аккаунта. Ошибка тут не важна."""
        if not conv_id:
            return
        try:
            session.patch(f"{self.BASE}/backend-api/conversation/{conv_id}",
                          headers=headers, json={"is_visible": False},
                          timeout=10)
        except Exception:  # noqa: BLE001 — уборка не должна ломать ответ
            pass

    def _stream(self, req: Request) -> Iterator[str]:
        model = self._pick_model(req)

        with self._session() as session:
            headers = {
                "Authorization": f"Bearer {self._access_token(session)}",
                "Content-Type": "application/json",
            }
            chat_token, pow_token = self._requirements(session, headers)

            stream_headers = {
                **headers,
                "accept": "text/event-stream",
                "openai-sentinel-chat-requirements-token": chat_token,
            }
            if pow_token:
                stream_headers["openai-sentinel-proof-token"] = pow_token

            body = self._body(model, req)
            if req.attachments:
                uploaded = [self._upload(session, headers, item)
                            for item in req.attachments]
                self._with_files(body["messages"][-1], uploaded)

            response = _http.request(
                session, "POST", f"{self.BASE}/backend-api/conversation",
                provider=self.name, headers=stream_headers,
                json=body, timeout=req.timeout, stream=True)
            _http.check(self.name, response)

            # Ответ приходит НАКОПЛЕННЫМ: в каждом кадре весь текст целиком,
            # а не добавка. Сводим к приращениям, как требует контракт.
            grown = Accumulated()
            conv_id = ""
            msg_id = ""
            produced = False
            for event in _http.sse_events(response):
                if not conv_id:
                    conv_id = event.get("conversation_id", "")
                message = event.get("message") or {}
                if (message.get("author") or {}).get("role") == "assistant":
                    msg_id = message.get("id") or msg_id
                whole = self._assistant_text(event)
                if not whole:
                    continue
                delta = grown.feed(whole)
                if delta:
                    produced = True
                    yield delta

            if not produced:
                raise ProviderError("пустой ответ", self.name)

            # Модель могла собрать файл в песочнице кода и дать на него
            # ссылку вида sandbox:/mnt/data/… — сама по себе она никуда не
            # ведёт. Забираем такие файлы и дописываем рабочими ссылками
            # отдельным куском: заменить их в уже отданном тексте нельзя,
            # он ушёл человеку по мере поступления.
            made = self._fetch_made_files(session, headers, conv_id, msg_id,
                                          grown.text)
            if made:
                yield made

            # Беседу НЕ прячем: спрятанную нельзя продолжить. Запоминаем
            # ручку, чтобы следующий ход пришёл в тот же чат.
            self._remember(req, conv_id, msg_id)

    # ── картинки (GPT Image) ─────────────────────────────────────────

    #: Слова, по которым различаем «лимит» от «поломки»: ChatGPT при
    #: исчерпании не отдаёт HTTP-ошибку, а отвечает обычным текстом.
    _LIMIT_WORDS = ("лимит", "исчерпан", "превыс", "limit", "quota",
                    "rate limit", "try again later", "reached your",
                    "come back later")

    def _draw(self, req: Request) -> list[str]:
        """Картинка через GPT Image 2.

        ``system_hints: ["picture_v2"]`` включает генерацию. Ответ содержит
        не URL, а ``asset_pointer`` (``sediment://`` идентификатор), по
        которому файл запрашивается отдельно. Готов он не сразу: OpenAI
        дорисовывает уже ПОСЛЕ закрытия потока, поэтому указатели
        обходятся по кругу.

        **Отдаём байты, а не ссылку.** Ссылка ведёт на ``backend-api`` и
        живёт только с нашей авторизацией — браузеру человека она вернёт
        403, и в чате будет пустое место.

        **Ошибки скачивания не глушим.** Если обернуть цикл в
        ``except Exception: pass``, при любой поломке наружу уйдёт
        бесполезное «картинка не получена» — понять, что именно случилось,
        будет нельзя. Поэтому причина копится и попадает в текст ошибки.

        Подпись модели («вот милая лисичка…») сохраняется в
        ``last_caption``: сервис часто присылает картинку вместе с
        комментарием, и терять его незачем.
        """
        import time as _time

        self.last_caption = ""

        with self._session() as session:
            headers = {
                "Authorization": f"Bearer {self._access_token(session)}",
                "Content-Type": "application/json",
            }
            chat_token, pow_token = self._requirements(session, headers)

            send_headers = {
                **headers,
                "accept": "text/event-stream",
                "openai-sentinel-chat-requirements-token": chat_token,
            }
            if pow_token:
                send_headers["openai-sentinel-proof-token"] = pow_token

            body = {
                "action": "next",
                "messages": [{
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "content": {"content_type": "text",
                                "parts": [req.prompt]},
                }],
                "model": "auto",
                "conversation_mode": {"kind": "primary_assistant"},
                "system_hints": ["picture_v2"],
            }
            response = _http.request(
                session, "POST", f"{self.BASE}/backend-api/conversation",
                provider=self.name, headers=send_headers, json=body,
                timeout=240, stream=True)
            _http.check(self.name, response)

            assets: list[str] = []
            said = ""
            conv_id = ""

            for event in _http.sse_events(response):
                if not conv_id:
                    conv_id = event.get("conversation_id", "")
                message = event.get("message") or {}
                role = (message.get("author") or {}).get("role", "")
                content = message.get("content") or {}
                for part in content.get("parts", []):
                    if isinstance(part, dict):
                        pointer = part.get("asset_pointer", "")
                        aid = pointer.split("://", 1)[-1] if pointer else ""
                        if aid and aid not in assets:
                            assets.append(aid)
                    elif (isinstance(part, str) and role == "assistant"
                          and content.get("content_type") == "text"
                          and len(part) > len(said)):
                        said = part

            self.last_caption = said.strip()

            if not assets:
                self._hide_conversation(session, headers, conv_id)
                self._check_refusal(said)

            # ПОРЯДОК ВАЖЕН: сначала забрать файлы, и только потом прятать
            # беседу. Наоборот не работает — у скрытой беседы вложения
            # перестают отдаваться, и запрос ссылки возвращает HTTP 404.
            # Иначе картинка теряется: она рисуется, указатель приходит,
            # а забрать её уже нельзя.
            trouble: list[str] = []
            picture = ""
            try:
                for _attempt in range(30):
                    for aid in assets:
                        picture = self._fetch_asset(
                            session, headers, aid, trouble)
                        if picture:
                            break
                    if picture:
                        break
                    _time.sleep(4)
            finally:
                # Прячем в любом случае, в том числе при ошибке: иначе в
                # аккаунте копятся беседы от каждого рисования.
                self._hide_conversation(session, headers, conv_id)

            if picture:
                return [picture]

            why = "; ".join(dict.fromkeys(trouble))[:200] or "файл так и не отдан"
            raise ProviderError(
                f"картинка не собралась за 2 минуты ({len(assets)} указ.): "
                f"{why}", self.name)

    def _fetch_asset(self, session, headers: dict, aid: str,
                     trouble: list[str]) -> str:
        """Забрать один файл как ``data:``-строку. Пусто — ещё не готов.

        Каждая неудача записывается в ``trouble``: молчаливый пропуск
        превратил бы любую поломку в неотличимое «картинка не получена».
        """
        try:
            handle = _http.request(
                session, "GET",
                f"{self.BASE}/backend-api/files/{aid}/download?no_redirect=1",
                provider=self.name, headers=headers, timeout=20)
        except Exception as exc:  # noqa: BLE001 — сеть, таймаут, что угодно
            trouble.append(f"запрос ссылки: {type(exc).__name__}")
            return ""

        code = getattr(handle, "status_code", 0)
        if code != 200:
            trouble.append(f"ссылка: HTTP {code}")
            return ""

        try:
            url = (handle.json() or {}).get("download_url", "")
        except ValueError:
            trouble.append("ссылка: ответ не JSON")
            return ""
        if not url:
            trouble.append("ссылки нет в ответе")
            return ""

        # Чужой домен (S3 и подобные) браузер откроет сам.
        if not url.startswith(self.BASE):
            return url

        try:
            blob = _http.request(session, "GET", url, provider=self.name,
                                 headers=headers, timeout=90)
        except Exception as exc:  # noqa: BLE001
            trouble.append(f"скачивание: {type(exc).__name__}")
            return ""

        code = getattr(blob, "status_code", 0)
        if code != 200:
            trouble.append(f"скачивание: HTTP {code}")
            return ""
        if len(blob.content) < 2000:
            trouble.append(f"файл мал ({len(blob.content)} б) — ещё рисуется")
            return ""

        kind = (blob.headers.get("content-type") or "image/png").split(";")[0]
        return f"data:{kind};base64,{base64.b64encode(blob.content).decode()}"

    def _check_refusal(self, said: str, fallback: str = "") -> None:
        """Если модель ответила текстом вместо картинки — разобраться почему.

        ChatGPT при исчерпании нормы картинок НЕ отдаёт HTTP-ошибку:
        он отвечает обычным текстом. Без разбора этого текста лимит
        выглядит как поломка адаптера.
        """
        lowered = (said or "").lower()
        if any(word in lowered for word in self._LIMIT_WORDS):
            raise RateLimited(
                f"норма картинок выбрана: {said.strip()[:100]}", self.name)
        if said.strip():
            raise ProviderError(
                f"картинка не получена — модель ответила: "
                f"{said.strip()[:120]}", self.name)
        raise ProviderError(
            fallback or "картинка не сгенерирована", self.name)
