"""Gemini через официальный API.

Самый быстрый из адаптеров: медиана хода 0.4 секунды, и 200 000 символов
входа читает не теряя ни начала, ни конца.

Но это API-ключ, а не веб-сессия: он конечен и однажды упрётся в бесплатный
тир. Держать его основой нельзя — он вспомогательный, зато отличный эталон
для сверки, потому что заведомо исправен.

Модели задаются ПЛАВАЮЩИМИ алиасами (``gemini-flash-latest``), а не
конкретным поколением. Причина не в красоте: прибитый ``gemini-2.5-flash``
однажды перестал выдаваться новым ключам, и провайдер отвалился с 404 на
каждом запросе.
"""
from __future__ import annotations

from typing import Iterator

from foxroute.errors import (
    AuthError,
    ContextTooLarge,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from foxroute.providers.base import Capabilities, Credential, Provider, Request


class GeminiAPIProvider(Provider):
    name = "gemini_api"
    capabilities = Capabilities(text=True, files_in=True, vision=True)
    slots = 4

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if not credential.value:
            raise ProviderError("нужен ключ AIza… с aistudio", self.name)
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise ProviderError(
                "нужен пакет google-generativeai", self.name) from exc
        # configure() глобальна на процесс — если однажды понадобятся два
        # ключа разом, придётся уходить на клиентский объект.
        genai.configure(api_key=credential.value)
        self._genai = genai

    def _translate(self, exc: Exception) -> Exception:
        """Перевести исключение SDK в наше.

        Классов у google-generativeai много и они меняются, поэтому смотрим
        на имя типа: это устойчивее, чем сверять иерархию, и не требует
        импортировать внутренности пакета.
        """
        kind = type(exc).__name__
        text = str(exc)[:300]
        low = text.lower()
        if "PermissionDenied" in kind or "Unauthenticated" in kind:
            return AuthError(f"ключ не принят: {text}", self.name)
        # Негодный ключ приезжает как InvalidArgument, а не как отказ в
        # доступе — то есть в одной куче с ошибками формата запроса.
        # Различаем по тексту, иначе протухший ключ выглядит как наш баг.
        if "api key not valid" in low or "api_key_invalid" in low:
            return AuthError(f"ключ не принят: {text}", self.name)
        if "ResourceExhausted" in kind:
            return RateLimited(f"норма выбрана: {text}", self.name)
        if "InvalidArgument" in kind and "token" in low:
            return ContextTooLarge(f"вход слишком велик: {text}", self.name)
        if "ServiceUnavailable" in kind or "InternalServerError" in kind:
            return ProviderUnavailable(f"сервис недоступен: {text}", self.name)
        return ProviderError(f"{kind}: {text}", self.name)

    #: Потолок на вложение. У API вложение едет ВНУТРИ запроса, и весь
    #: запрос ограничен примерно двадцатью мегабайтами; для большего у них
    #: есть отдельное файловое хранилище, которого мы не трогаем.
    MAX_UPLOAD = 15 * 1024 * 1024

    def _parts(self, req: Request) -> list:
        """Запрос частями: текст плюс вложения байтами.

        Отдельной загрузки тут нет — файл кладётся прямо в тело запроса,
        поэтому и потолок много ниже, чем у веб-сессий.
        """
        parts: list = [req.prompt]
        for item in req.attachments:
            raw = item.data or b""
            if not raw:
                continue
            if len(raw) > self.MAX_UPLOAD:
                raise ProviderError(
                    f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ — "
                    "у API вложение едет внутри запроса", self.name)
            parts.append({"mime_type": item.mime or "application/octet-stream",
                          "data": raw})
        return parts

    def _stream(self, req: Request) -> Iterator[str]:
        settings = {}
        if req.max_tokens is not None:
            settings["max_output_tokens"] = req.max_tokens
        if req.temperature is not None:
            settings["temperature"] = req.temperature

        produced = False
        try:
            model = self._genai.GenerativeModel(self.resolve_model(req))
            stream = model.generate_content(
                self._parts(req), stream=True,
                generation_config=settings or None)
            for event in stream:
                piece = getattr(event, "text", None)
                if piece:
                    produced = True
                    yield piece
        except Exception as exc:  # noqa: BLE001 — переводим в свой тип
            translated = self._translate(exc)
            if translated is exc:
                raise
            raise translated from exc

        if not produced:
            # Пустой ответ у Gemini чаще всего означает сработавший фильтр
            # безопасности, а не поломку: текста нет, а ошибки тоже нет.
            raise ProviderError(
                "пустой ответ — вероятно, сработал фильтр безопасности",
                self.name)
