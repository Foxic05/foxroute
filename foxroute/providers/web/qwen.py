"""Qwen — веб-сессия через chat.qwenlm.ai.

Почему не основной домен: ``chat.qwen.ai`` закрыт антиботом TMD (Alibaba),
который без headless-браузера не обходится. ``chat.qwenlm.ai`` — тот же
бэкенд, те же модели и тот же токен, но проверки там нет. Это и делает
провайдера пригодным для чистого Python.

Доступ — JWT целиком, из ``localStorage`` на ``chat.qwen.ai``, поле ``token``.
Сам он не обновляется, срок лежит внутри токена.

Держит 200 000 символов входа, медиана хода 2.3 секунды, агентную задачу
отладки решает за 8 ходов.
"""
from __future__ import annotations

import time
import uuid
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)
from foxroute.tokens import jwt_expiry


class QwenProvider(Provider):
    name = "qwen"
    #: Веб-поиск НЕ заявлен: в UI qwen тумблер есть, но мы ходим через
    #: зеркало chat.qwenlm.ai (без антибота TMD), и оно поиск молча не
    #: включает — отвечает из памяти. Проводка поиска оставлена дремать;
    #: настоящий chat.qwen.ai закрыт антиботом.
    #: Глубокое исследование РАБОТАЕТ даже на зеркале (в отличие от
    #: обычного поиска): отдельный вид беседы ``deep_research``. Занимает
    #: ~310 секунд и ~15 700 символов сводки против 633 обычных.
    capabilities = Capabilities(text=True, images_out=True, conversations=True,
                                thinking=True, files_in=True, vision=True,
                                deep_research=True)

    BASE = "https://chat.qwenlm.ai"
    #: Версия клиента в заголовке. Сервис её проверяет — с пустой или явно
    #: чужой отвечает отказом.
    CLIENT_VERSION = "0.2.63"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self._token = credential.value
        if not self._token:
            raise ProviderError(
                "нужен token из localStorage chat.qwen.ai", self.name)
        #: Информационно: когда JWT протухнет. Не блокируем запрос по этому
        #: полю — расхождение часов не повод отказывать живому токену.
        self.expires_at = jwt_expiry(self._token)

    # ── протокол ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Origin": self.BASE,
            "Referer": f"{self.BASE}/",
            "X-Request-Id": str(uuid.uuid4()),
            "source": "web",
            "version": self.CLIENT_VERSION,
        }

    def _check_envelope(self, payload: dict) -> None:
        """Проверить конверт ответа.

        Qwen отвечает **HTTP 200 даже на отвергнутый токен**, а отказ кладёт
        в тело::

            {"success": false,
             "data": {"code": "unauthorized",
                      "details": "Your session has expired..."}}

        Проверки по коду ответа тут недостаточно, и это не мелочь: без разбора
        конверта протухшая кука выглядит как «провайдер вернул пустоту», то
        есть как поломка на нашей стороне.
        """
        if payload.get("success") is not False:
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        code = str(data.get("code") or "")
        details = str(data.get("details") or payload.get("message") or "")
        if code == "unauthorized" or "expired" in details.lower():
            raise AuthError(f"токен отвергнут сервисом: {details}", self.name)
        raise ProviderError(f"отказ {code or 'без кода'}: {details}", self.name)

    def _open_chat(self, session, model: str, chat_type: str = "t2t") -> str:
        # chat_type ЗАВОДИМОГО чата обязан совпадать с тем, что потом
        # объявит сообщение. При рассогласовании (чат t2t, а сообщение
        # шлёт "search") сервис видит чат-t2t и тихо отвечает по памяти,
        # поиск не включается. Тип задаётся при создании.
        response = _http.request(
            session, "POST", f"{self.BASE}/api/v2/chats/new",
            provider=self.name,
            headers=self._headers(),
            json={
                "title": "New Chat",
                "models": [model],
                "chat_mode": "normal",
                "chat_type": chat_type,
                "timestamp": int(time.time() * 1000),
                "project_id": "",
            },
            timeout=60,
        )
        _http.check(self.name, response)
        try:
            payload = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на создание чата", self.name) from exc
        self._check_envelope(payload)
        return (payload.get("data") or {}).get("id") or ""

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Их собственный клиент грузит файл в объектное хранилище Alibaba: берёт
    # временный STS-ключ, подписывает запрос HMAC и кладёт байты прямо в
    # OSS. Мы туда не ходим — у сервиса есть простой приём одним multipart,
    # он отдаёт тот же идентификатор и работает без подписей.
    #
    # А вот запись в ``files`` разбирается сервисом придирчиво: по трём
    # полям (``type``, ``showType``, ``file_class``) он решает, ЧЕМ файл
    # является. Ошибись — и картинка уедет как документ, то есть модель
    # получит разбор байтов вместо изображения.

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    @staticmethod
    def _kind(mime: str) -> tuple[str, str]:
        """``(type, file_class)`` по типу содержимого."""
        if mime.startswith("image/"):
            return "image", "vision"
        if mime.startswith("video/"):
            return "video", "video"
        if mime.startswith("audio/"):
            return "audio", "audio"
        return "file", "document"

    def _upload(self, session, item) -> dict:
        """Положить вложение и собрать запись для поля ``files``."""
        raw = item.data or b""
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)
        name = item.filename or "file.bin"
        mime = item.mime or "application/octet-stream"

        body, ctype = _http.multipart(
            filename=name, data=raw, content_type=mime)
        # Свой Content-Type у сессии уже есть — заменяем, а не добавляем
        # вторым ключом, иначе уедут оба и разбор границы сломается.
        headers = {k: v for k, v in self._headers().items()
                   if k.lower() != "content-type"}
        headers["Content-Type"] = ctype

        response = _http.request(
            session, "POST", f"{self.BASE}/api/v1/files/", provider=self.name,
            headers=headers, data=body, timeout=180)
        _http.check(self.name, response)
        try:
            saved = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на загрузку файла", self.name) from exc
        self._check_envelope(saved)

        file_id = saved.get("id") or ""
        if not file_id:
            raise ProviderError(
                f"сервис не выдал идентификатор файла: {str(saved)[:200]}",
                self.name)

        shape, file_class = self._kind(mime)
        return {
            "type": shape,
            "file": saved,
            "id": file_id,
            "url": f"/api/v1/files/{file_id}",
            "name": name,
            "collection_name": "",
            "progress": 0,
            "status": "uploaded",
            "greenNet": "success",
            "size": len(raw),
            "error": "",
            "itemId": str(uuid.uuid4()),
            "file_type": mime,
            "showType": shape,
            "file_class": file_class,
            "uploadTaskId": str(uuid.uuid4()),
        }

    @staticmethod
    def _chat_kind(req: Request) -> str:
        """Вид беседы: обычная, поиск или исследование.

        У Qwen это не флаги, а РАЗНЫЕ виды чата, и вид заводимого чата
        обязан совпадать с тем, что объявит сообщение.
        """
        if req.deep_research:
            return "deep_research"
        return "search" if req.web_search else "t2t"

    def _message_body(self, chat_id: str, model: str, prompt: str,
                      thinking: bool = False,
                      parent_id: str | None = None,
                      files: list[dict] | None = None,
                      kind: str = "t2t") -> dict:
        # parent_id связывает ход с предыдущим ответом: с ним сервис держит
        # контекст сам, и второй запрос несёт только новое сообщение.
        now = int(time.time() * 1000)
        return {
            "stream": True,
            "version": "2.1",
            "incremental_output": True,
            "chat_id": chat_id,
            "chat_mode": "normal",
            "model": model,
            "parent_id": parent_id,
            "messages": [{
                "fid": str(uuid.uuid4()),
                "parentId": parent_id,
                "childrenIds": [],
                "role": "user",
                "content": prompt,
                "user_action": "chat",
                "files": files or [],
                "timestamp": now,
                "models": [model],
                # Поиск в сети — это отдельный ВИД беседы, а не флаг:
                # сервис смотрит на chat_type и обязан видеть то же самое
                # в sub_chat_type и в extra.meta, иначе тихо отвечает по
                # памяти.
                "chat_type": kind,
                "feature_config": {
                    "thinking_enabled": thinking,
                    "output_schema": "phase",
                    "thinking_budget": 81920,
                    # У живого клиента поиск включается именно так.
                    "search_enabled": kind != "t2t",
                    "auto_search": kind != "t2t",
                },
                "extra": {"meta": {"subChatType": kind}},
                "sub_chat_type": kind,
            }],
            "timestamp": now,
        }

    @staticmethod
    def _response_id(event: dict) -> str:
        """id ответа из кадра — им продолжают беседу (parent следующего)."""
        created = event.get("response.created")
        if isinstance(created, dict) and created.get("response_id"):
            return created["response_id"]
        return event.get("response_id") or ""

    def _begin(self, session, model: str, req: Request) -> tuple[str, str | None]:
        """Готовим чат: продолжаем беседу или открываем новый."""
        conv = req.conversation
        if conv and conv.chat_id:
            return conv.chat_id, (conv.last_message_id or None)
        return self._open_chat(session, model, self._chat_kind(req)), None

    def _remember(self, req: Request, chat_id: str, response_id: str) -> None:
        """Запомнить беседу в req, чтобы сервер отдал её клиенту."""
        if not chat_id:
            return
        if req.conversation is None:
            req.conversation = Conversation(provider=self.name,
                                            chat_id=chat_id)
        req.conversation.chat_id = chat_id
        if response_id:
            req.conversation.last_message_id = response_id

    @staticmethod
    def _answer_delta(event: dict) -> str:
        """Кусок ответа. Только ``phase == "answer"``."""
        try:
            delta = event["choices"][0]["delta"]
        except (KeyError, IndexError, TypeError):
            return ""
        if delta.get("phase") not in (None, "answer"):
            return ""
        content = delta.get("content")
        return content if isinstance(content, str) else ""

    @staticmethod
    def _rich_delta(event: dict) -> tuple[str, str] | None:
        """Кусок с типом: ``("text", кусок)`` или ``("thinking", кусок)``."""
        try:
            delta = event["choices"][0]["delta"]
        except (KeyError, IndexError, TypeError):
            return None
        content = delta.get("content")
        if not isinstance(content, str) or not content:
            return None
        phase = delta.get("phase")
        if phase == "think":
            return ("thinking", content)
        if phase in (None, "answer"):
            return ("text", content)
        return None

    # ── контракт Provider ─────────────────────────────────────────────

    def _pairs(self, req: Request) -> Iterator[tuple[str, str]]:
        """Общее тело потока: пары ``("text"|"thinking", кусок)``.

        ``_stream`` и ``stream_rich`` различаются лишь фильтром дельт
        (``_answer_delta`` против ``_rich_delta``); сетевой обмен общий и
        живёт здесь, чтобы не дублироваться.
        """
        model = self.resolve_model(req)
        with _http.session() as session:
            chat_id, parent = self._begin(session, model, req)
            files = [self._upload(session, item) for item in req.attachments]
            response = _http.request(
                session, "POST", f"{self.BASE}/api/v2/chat/completions",
                provider=self.name,
                params={"chat_id": chat_id} if chat_id else None,
                headers=self._headers(),
                json=self._message_body(chat_id, model, req.prompt,
                                       thinking=req.thinking,
                                       parent_id=parent, files=files,
                                       kind=self._chat_kind(req)),
                timeout=req.timeout,
                stream=True,
            )
            _http.check(self.name, response)

            produced = False
            response_id = ""
            for event in _http.sse_events(response):
                response_id = self._response_id(event) or response_id
                pair = self._rich_delta(event)
                if pair:
                    produced = True
                    yield pair

            if not produced:
                raise ProviderError(
                    f"пустой ответ (модель {model!r} могла быть отвергнута)",
                    self.name)
            self._remember(req, chat_id, response_id)

    def _stream(self, req: Request) -> Iterator[str]:
        for kind, piece in self._pairs(req):
            if kind == "text":
                yield piece

    def stream_rich(self, req: Request) -> Iterator[tuple[str, str]]:
        self.validate(req)
        yield from self._pairs(req)

    def _draw(self, req: Request) -> list[str]:
        """Картинки через Qwen-Image (g4f обходит TMD на chat.qwen.ai).

        Возвращает 1-4 URL на 2688x1536 PNG без вотермарок — лучшее
        качество из всех рисовальщиков в пуле.
        """
        import asyncio
        import re as _re

        try:
            from g4f.Provider.Qwen import Qwen
            from g4f.providers.response import ImageResponse
        except ImportError as exc:
            raise ProviderError(
                "нужен пакет g4f для рисования через Qwen", self.name
            ) from exc

        async def run() -> list[str]:
            urls: list[str] = []
            async for chunk in Qwen.create_async_generator(
                model=self.resolve_model(req),
                messages=[{"role": "user", "content": req.prompt}],
                chat_type="t2i",
                aspect_ratio=req.aspect,
                token=self._token,
            ):
                if isinstance(chunk, ImageResponse):
                    for match in _re.finditer(r"https://[^\s)]+", str(chunk)):
                        url = match.group(0)
                        if url not in urls:
                            urls.append(url)
            return urls

        try:
            try:
                asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    urls = pool.submit(asyncio.run, run()).result(
                        timeout=req.timeout)
            except RuntimeError:
                urls = asyncio.run(run())
        except Exception as exc:  # noqa: BLE001 — g4f бросает свои классы
            message = str(exc)
            if "rate" in message.lower() or "limit" in message.lower():
                raise RateLimited(message[:120], self.name) from exc
            raise ProviderError(message[:200], self.name) from exc

        if not urls:
            raise ProviderError("картинки не получены", self.name)
        return urls
