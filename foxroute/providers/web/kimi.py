"""Kimi — веб-сессия kimi.com (Moonshot).

Протокол не совсем обычный: **Connect-RPC**. Тот же JSON, но завёрнутый в
рамку «байт флага, длина четырьмя байтами big-endian, тело» — и в обе стороны.

Доступ — ``refresh_token`` из куки ``kimi.com``, живёт месяцами. По нему
берётся часовой access-токен. Важная особенность: сервис **выдаёт новый
refresh-токен в ответе**, и его надо сохранять, иначе однажды останешься со
старым (см. ``Provider.rotated``).

Держит 200 000 символов входа, медиана хода 2.0 секунды.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Iterator

from foxroute.errors import AuthError, ProviderError
from foxroute.providers import _http
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)
from foxroute.tokens import jwt_expiry


def encode_frame(payload: dict) -> bytes:
    """Рамка Connect-RPC: байт флага, длина big-endian четырьмя байтами, тело."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return b"\x00" + len(body).to_bytes(4, "big") + body


def decode_frames(response) -> Iterator[dict]:
    """Разобрать поток рамок в объекты.

    Кусок из сети может оборвать рамку посередине, поэтому копим буфер и
    достаём только целые. Без этого последний ответ терялся бы через раз.
    """
    buffer = b""
    for chunk in response.iter_content(chunk_size=4096):
        if not chunk:
            continue
        buffer += chunk
        while len(buffer) >= 5:
            size = int.from_bytes(buffer[1:5], "big")
            if len(buffer) < 5 + size:
                break
            body, buffer = buffer[5:5 + size], buffer[5 + size:]
            try:
                yield json.loads(body)
            except ValueError:
                continue


class KimiProvider(Provider):
    name = "kimi"
    #: Картинок нет намеренно. Разбор у них текстовый: PNG проходит все
    #: четыре шага загрузки и отваливается на разборе с ``no_content`` —
    #: искали текст, не нашли. Отдельного пути для зрения найти не удалось:
    #: подписанный адрес с ``action: image`` ведёт в другое хранилище, а
    #: блоки ``image``/``picture``/``media`` сервис молча выбрасывает —
    #: лишние поля protobuf не считает ошибкой, и модель отвечает «не вижу
    #: картинки». Поставить флаг значило бы обещать это в интерфейсе.
    #: Веб-поиск НЕ заявлен: инструмент TOOL_TYPE_SEARCH сервис принимает,
    #: но поиск не запускается — проверено свежим фактом, ответ из памяти.
    #: Разобраться требует перехвата живого запроса (нужен вход в браузере).
    #: Размышления ЕСТЬ, но только в линии 2.5 — см. ``_scenario``.
    #: Глубокое исследование у них в интерфейсе ЕСТЬ (кнопка Deep
    #: Research рядом с полем ввода), но нам недоступно: на отправку
    #: сервис показывает окно «слишком много желающих, подпишись ради
    #: приоритетной очереди» и запрос не выпускает. Не заявляем.
    #: Поиска в сети тоже не заявляем: в их меню он только Auto/Off,
    #: то есть управляемого «включить» нет — сервис решает сам.
    capabilities = Capabilities(text=True, conversations=True, files_in=True,
                                thinking=True)

    BASE = "https://www.kimi.com"
    CHAT_PATH = "/apiv2/kimi.gateway.chat.v1.ChatService/Chat"

    #: Насколько access-токен считается свежим. Сервис даёт час, берём
    #: с большим запасом: обновление стоит одного дешёвого GET, а протухший
    #: токен посреди потока стоит всего ответа.
    ACCESS_TTL = 600

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if not credential.value:
            raise ProviderError(
                "нужен refresh_token из куки kimi.com", self.name)
        self._access = ""
        self._access_until = 0.0
        self._auth_lock = threading.Lock()

    @property
    def expires_at(self):
        """Когда протухнет refresh-токен (он же JWT)."""
        return jwt_expiry(self.credential.value)

    # ── доступ ────────────────────────────────────────────────────────

    def _access_token(self) -> str:
        """Свежий access-токен, при необходимости обновлённый."""
        with self._auth_lock:
            if self._access and time.time() < self._access_until:
                return self._access

            with _http.session() as session:
                response = _http.request(
                    session, "GET", f"{self.BASE}/api/auth/token/refresh",
                    provider=self.name,
                    headers={
                        "Authorization": f"Bearer {self.credential.value}",
                        "Origin": self.BASE,
                        "Referer": f"{self.BASE}/",
                    },
                    timeout=30,
                )
                _http.check(self.name, response)
                try:
                    payload = response.json() or {}
                except ValueError as exc:
                    raise ProviderError(
                        "не JSON при обновлении токена", self.name) from exc

            access = payload.get("access_token") or ""
            if not access:
                raise AuthError(
                    "refresh_token не принят — обновить куку kimi.com", self.name)

            # Сервис прислал новый refresh. Сообщаем наверх — но заметим,
            # что решение сохранять его принимает хранилище, а не мы.
            #
            # Исходный токен из куки продолжает приниматься после ротации,
            # и каждая выдаёт НОВЫЙ, каждый раз другой. То есть в куке лежит
            # долгоживущий корневой токен, а выдаются временные. Перезаписать
            # хранилище временным — значит променять месяцы жизни на часы.
            # Поэтому политика простая: держаться исходного, пока его
            # принимают.
            self.rotated(payload.get("refresh_token") or "")

            self._access = access
            self._access_until = time.time() + self.ACCESS_TTL
            return access

    # ── протокол ──────────────────────────────────────────────────────

    @staticmethod
    def _scenario(model: str, thinking: bool = False) -> str:
        """Сценарий разговора. Размышления живут ТОЛЬКО в линии 2.5.

        На ``SCENARIO_K2D6`` блока ``think`` не бывает ни при каком имени
        флага (``thinking``, ``thinking_enabled``, ``enable_thinking``,
        ``reasoning``) — у ответов 2.6 просто нет такой части. На
        ``SCENARIO_K2D5`` с ``options.thinking`` он приходит. Поэтому
        нажатая кнопка «думать» переводит разговор в 2.5: иначе кнопка была
        бы обманом — нажимается, а размышлений нет.
        """
        if thinking:
            return "SCENARIO_K2D5"
        return "SCENARIO_K2D6" if "k2.6" in model.lower() else "SCENARIO_K2D5"

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Загрузка в четыре шага, и последний важнее остальных: файл, который
    # не разобран, для модели не существует.
    #
    #   1. ``/api/pre-sign-url`` — подписанный адрес в их хранилище (TOS,
    #      это объектное хранилище Volcano Engine, не их домен);
    #   2. ``PUT`` байтов туда, без нашей авторизации;
    #   3. ``/api/file`` — завести запись, получить ``id`` и статус
    #      ``initialized``;
    #   4. ``/api/file/parse_process`` — РАЗБОР. Отвечает потоком; пока он
    #      не досмотрен до статуса ``parsed``, файл остаётся сырым.
    #
    # Ссылка на файл кладётся в сообщение ОТДЕЛЬНЫМ БЛОКОМ рядом с текстом
    # — ``{"file": запись}``. Перебраны и другие места: ``refs`` списком
    # идентификаторов принимается без ошибки, но модель отвечает «не вижу
    # прикреплённого файла»; ``message.refs`` отвергается как
    # ``invalid_argument``. То есть тихо не сработавший вариант тут
    # выглядит рабочим — отсюда проверка живым вопросом по содержимому.

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    def _json_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": self.BASE,
            "Referer": f"{self.BASE}/",
        }

    def _upload(self, session, token: str, item) -> dict:
        """Положить вложение и вернуть запись файла для блока сообщения."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        name = item.filename or "file.bin"
        mime = item.mime or "application/octet-stream"
        headers = self._json_headers(token)

        # ``action`` тут не про то, чем файл является: картинки идут тем же
        # «file». Отдельный ``image`` существует, отвечает подписанным
        # адресом в ДРУГОМ хранилище (kfs), и следующий шаг такой объект уже
        # не находит — «файловый объект не существует». Проверено обоими
        # путями на одном и том же PNG.
        signed = _http.request(
            session, "POST", f"{self.BASE}/api/pre-sign-url",
            provider=self.name, headers=headers,
            json={"action": "file", "name": name},
            timeout=60)
        _http.check(self.name, signed)
        try:
            place = signed.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в подписанном адресе", self.name) from exc
        if not place.get("url"):
            raise ProviderError(
                f"сервис не выдал адрес для загрузки: {str(place)[:200]}",
                self.name)

        put = _http.request(session, "PUT", place["url"], provider=self.name,
                            headers={"Content-Type": mime}, data=raw,
                            timeout=300)
        _http.check(self.name, put)

        made = _http.request(
            session, "POST", f"{self.BASE}/api/file", provider=self.name,
            headers=headers,
            json={"type": "file", "name": name,
                  "object_name": place.get("object_name", ""), "file_id": ""},
            timeout=60)
        _http.check(self.name, made)
        try:
            record = made.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на создание файла", self.name) from exc
        if not record.get("id"):
            raise ProviderError(
                f"сервис не выдал идентификатор файла: {str(record)[:200]}",
                self.name)

        self._parse(session, headers, record["id"])
        return record

    def _parse(self, session, headers: dict, file_id: str) -> None:
        """Дождаться разбора файла. Поток нужно досмотреть до конца."""
        stream = _http.request(
            session, "POST", f"{self.BASE}/api/file/parse_process",
            provider=self.name, headers=headers, json={"ids": [file_id]},
            timeout=300, stream=True)
        _http.check(self.name, stream)
        status = ""
        for line in stream.iter_lines():
            if not line:
                continue
            text = line.decode() if isinstance(line, bytes) else line
            if not text.startswith("data:"):
                continue
            try:
                event = json.loads(text[5:].strip())
            except ValueError:
                continue
            state = (event.get("file_info") or {}).get("status") or ""
            if state:
                status = state
            if status in ("parsed", "failed"):
                break
        if status == "failed":
            raise ProviderError("сервис не смог разобрать файл", self.name)

    def _body(self, model: str, req: Request,
              chat_id: str = "", parent_id: str = "",
              files: list[dict] | None = None) -> dict:
        scenario = self._scenario(model, req.thinking)
        blocks = [{"message_id": "", "file": record} for record in files or []]
        blocks.append({"message_id": "", "text": {"content": req.prompt}})
        return {
            "scenario": scenario,
            "chat_id": chat_id,
            "tools": ([{"type": "TOOL_TYPE_SEARCH", "search": {}}]
                      if req.web_search else []),
            "message": {
                "parent_id": parent_id,
                "role": "user",
                "blocks": blocks,
                "scenario": scenario,
            },
            "options": {"thinking": req.thinking},
        }

    def _begin(self, req: Request) -> tuple[str, str]:
        """chat_id, parent_id — из беседы или пустые."""
        conv = req.conversation
        if conv and conv.chat_id:
            return conv.chat_id, conv.last_message_id or ""
        return "", ""

    def _remember(self, req: Request, chat_id: str, msg_id: str) -> None:
        if not chat_id:
            return
        if req.conversation is None:
            req.conversation = Conversation(provider=self.name,
                                            chat_id=chat_id)
        req.conversation.chat_id = chat_id
        if msg_id:
            req.conversation.last_message_id = msg_id

    def _stream(self, req: Request) -> Iterator[str]:
        for kind, piece in self._pairs(req):
            if kind == "text":
                yield piece

    def stream_rich(self, req: Request) -> Iterator[tuple[str, str]]:
        self.validate(req)
        yield from self._pairs(req)

    def _pairs(self, req: Request) -> Iterator[tuple[str, str]]:
        """Общее тело потока: пары ``("text"|"thinking", кусок)``.

        Прежде ``_stream`` (только текст) и ``stream_rich`` (текст+мысли)
        копировали весь Connect-RPC обмен дважды. Теперь он здесь один раз.
        """
        token = self._access_token()
        model = self.resolve_model(req)
        chat_id, parent_id = self._begin(req)

        with _http.session() as session:
            files = [self._upload(session, token, item)
                     for item in req.attachments]
            response = _http.request(
                session, "POST", f"{self.BASE}{self.CHAT_PATH}",
                provider=self.name,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/connect+json",
                    "Accept": "*/*",
                    "Origin": self.BASE,
                    "Referer": f"{self.BASE}/",
                },
                data=encode_frame(self._body(model, req, chat_id, parent_id,
                                            files)),
                timeout=req.timeout, stream=True)
            _http.check(self.name, response)

            whole_text = ""
            whole_think = ""
            produced = False
            resp_chat_id = chat_id
            resp_msg_id = ""

            for frame in decode_frames(response):
                if frame.get("error"):
                    raise ProviderError(
                        f"сервис вернул ошибку: {str(frame['error'])[:200]}",
                        self.name)

                chat = frame.get("chat") or {}
                if chat.get("id") and not resp_chat_id:
                    resp_chat_id = chat["id"]
                msg = frame.get("message") or {}
                if msg.get("role") == "assistant" and msg.get("id"):
                    resp_msg_id = msg["id"]

                block = frame.get("block") or {}
                is_set = frame.get("op") == "set"

                text_content = (block.get("text") or {}).get("content")
                if isinstance(text_content, str) and text_content:
                    if is_set:
                        delta = (text_content[len(whole_text):]
                                 if text_content.startswith(whole_text)
                                 else text_content)
                        whole_text = text_content
                    else:
                        delta = text_content
                        whole_text += text_content
                    if delta:
                        produced = True
                        yield ("text", delta)

                think_content = (block.get("think") or {}).get("content")
                if isinstance(think_content, str) and think_content:
                    if is_set:
                        delta = (think_content[len(whole_think):]
                                 if think_content.startswith(whole_think)
                                 else think_content)
                        whole_think = think_content
                    else:
                        delta = think_content
                        whole_think += think_content
                    if delta:
                        produced = True
                        yield ("thinking", delta)

            if produced:
                self._remember(req, resp_chat_id, resp_msg_id)
            else:
                raise ProviderError("пустой ответ", self.name)
