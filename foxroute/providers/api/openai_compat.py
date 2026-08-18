"""Сервисы с OpenAI-совместимым API.

Один адаптер на всех, кто говорит на ``/v1/chat/completions``: сейчас это
Groq и AgentRouter. Отличаются они адресом, моделями и заголовками, а всё
это задаётся наследником и реестром.

**Почему не SDK openai.** Он подставляет собственные заголовки, по которым
себя опознаёт, и AgentRouter на них отвечает ``401 unauthorized client
detected``. Тот же ключ через сырой запрос принимается. Заодно
уходит зависимость от пакета, а протокол здесь простой и опубликованный —
писать по нему дешевле, чем обходить чужие умолчания.

Два решения этого адаптера.

**Стриминг.** Ответ не ждём целиком: API умеет отдавать потоком, а у слоя
наверху ``stream=true`` — ждать значило бы терять его на ровном месте.

**Лимиты не в поля, а в ошибку.** Складывать ``x-ratelimit-*`` в атрибуты
объекта — заставлять вызывающего самого догадаться их прочитать. Здесь
исчерпание поднимается ``RateLimited`` с честным сроком — тем же типом,
что и у веб-сессий, так что вызывающему не нужно знать, с кем он говорит.
"""
from __future__ import annotations

import mimetypes

import base64
from typing import Iterator

from foxroute.errors import ProviderError
from foxroute.providers import _http
from foxroute.providers._http import ThinkTags
from foxroute.providers.base import Capabilities, Credential, Provider, Request
from foxroute.registry import config


class OpenAICompatProvider(Provider):
    """Базовый адаптер. Наследник задаёт ``name`` и, если надо, заголовки."""

    #: Картинки — да, документы — нет. PNG частью ``image_url`` понимают
    #: и OpenRouter, и AgentRouter; тот же файл частью ``file`` даёт 402
    #: у OpenRouter (разбор документов у них платный) и 500 у AgentRouter.
    #: Наследник, чья модель не видит, снимает флаг у себя.
    capabilities = Capabilities(text=True, vision=True)
    #: Официальный API держит параллельные запросы, в отличие от веб-сессии.
    slots = 4

    #: Дополнительные заголовки сервиса. Пусто — ничего сверх обычных.
    extra_headers: dict[str, str] = {}

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if not credential.value:
            raise ProviderError("нужен API-ключ", self.name)

        from foxroute.registry import splits_pool

        if splits_pool(self.name) and "|" in credential.value:
            raise ProviderError(
                "получен пул из нескольких ключей через '|', а адаптеру "
                "полагается ровно один — разверни через Credential.expand(). "
                "Иначе вся строка уедет в заголовок и вернётся 401, "
                "неотличимый от протухшего ключа", self.name)

        base_url = config(self.name).get("base_url")
        if not base_url:
            raise ProviderError(
                f"в реестре нет base_url для {self.name}", self.name)
        self.base_url = base_url.rstrip("/")

        #: Остаток запросов по данным сервиса. -1 — сервис не сказал.
        #: Преимущество официального API: остаток известен ДО того, как
        #: упрёшься. У большинства веб-сессий его нет вовсе.
        self.remaining = -1
        self.limit = -1
        self.reset_after = ""

    # ── запрос ────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.credential.value}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self.extra_headers,
        }

    #: Потолок на вложение. У API отдельной загрузки нет: файл едет ВНУТРИ
    #: запроса в base64 (+33% к объёму), поэтому потолок много ниже, чем у
    #: веб-сессий.
    MAX_UPLOAD = 15 * 1024 * 1024

    def _content(self, req: Request):
        """Содержимое сообщения: строка или список частей с вложениями.

        Пока вложений нет, отдаём ПРОСТУЮ строку. Список частей понимают не
        все совместимые сервисы, а строку — все; переходить на список ради
        единообразия значило бы сломать тех, кто и так работает.

        Форматов два, и они не взаимозаменяемы: картинка идёт как
        ``image_url`` с ``data:``-адресом, документ — как ``file`` с
        ``file_data``. Умеет ли конкретная модель то или другое — вопрос
        модели, а не протокола.
        """
        if not req.attachments:
            return req.prompt

        parts: list = [{"type": "text", "text": req.prompt}]
        for item in req.attachments:
            raw = item.data or b""
            if not raw:
                continue
            if len(raw) > self.MAX_UPLOAD:
                raise ProviderError(
                    f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ — "
                    "у API вложение едет внутри запроса", self.name)
            mime = item.mime or "application/octet-stream"
            packed = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
            if item.kind == "image" or mime.startswith("image/"):
                parts.append({"type": "image_url",
                              "image_url": {"url": packed}})
            else:
                parts.append({"type": "file",
                              "file": {"filename": item.filename or "file.bin",
                                       "file_data": packed}})
        return parts

    #: Модель, которой отдаём запрос с картинкой, если обычная не видит.
    #: Пусто — переключать не на что.
    VISION_MODEL = ""

    def _pick_model(self, req: Request) -> str:
        """Модель под запрос. С картинкой — зрячая, если она объявлена.

        Подмена оправдана тем, что у этих сервисов квота считается ПО
        МОДЕЛЯМ и не пересекается: зрячая модель тратит свою норму, а не
        отъедает от рабочей. Без подмены пришлось бы либо отказывать
        картинкам, либо держать зрячую модель основной — а она обычно
        медленнее и с меньшей нормой.
        """
        model = self.resolve_model(req)
        if not self.VISION_MODEL:
            return model
        wants_eyes = any(item.kind == "image"
                         or (item.mime or "").startswith("image/")
                         for item in req.attachments)
        return self.VISION_MODEL if wants_eyes else model

    def _body(self, req: Request) -> dict:
        body = {
            "model": self._pick_model(req),
            "messages": [{"role": "user", "content": self._content(req)}],
            "stream": True,
        }
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            body["temperature"] = req.temperature
        return body

    def _absorb_limits(self, response) -> None:
        """Запомнить остаток из заголовков ответа."""
        headers = getattr(response, "headers", None)
        if not headers:
            return
        for key, attr in (("x-ratelimit-remaining-requests", "remaining"),
                          ("x-ratelimit-limit-requests", "limit")):
            raw = headers.get(key)
            if raw is None:
                continue
            try:
                setattr(self, attr, int(raw))
            except (TypeError, ValueError):
                pass
        reset = headers.get("x-ratelimit-reset-requests")
        if reset:
            self.reset_after = str(reset)

    #: Поля, куда разные сервисы кладут ход рассуждения. OpenRouter шлёт
    #: ``reasoning``, DeepSeek и Qwen в OpenAI-совместимом виде —
    #: ``reasoning_content``.
    REASONING_FIELDS = ("reasoning", "reasoning_content")

    @classmethod
    def _split_delta(cls, event: dict) -> tuple[str, str]:
        """Разложить кадр на ``(ответ, рассуждение)``.

        Слить оба поля в одну строку значит вывалить у рассуждающих моделей
        OpenRouter весь ход мысли в чат перед ответом. Держим их порознь:
        смешивать нельзя, это разные вещи для читателя.
        """
        choices = event.get("choices") or []
        if not choices:
            return "", ""
        delta = choices[0].get("delta") or {}

        answer = delta.get("content")
        answer = answer if isinstance(answer, str) else ""

        think = ""
        for field in cls.REASONING_FIELDS:
            value = delta.get(field)
            if isinstance(value, str) and value:
                think = value
                break
        return answer, think

    def _events(self, req: Request):
        """Кадры потока с разбором ошибки, присланной внутри него."""
        with _http.session() as session:
            response = _http.request(
                session, "POST", f"{self.base_url}/chat/completions",
                provider=self.name, headers=self._headers(),
                json=self._body(req), timeout=req.timeout, stream=True)
            self._absorb_limits(response)
            _http.check(self.name, response)

            for event in _http.sse_events(response):
                # Ошибку сервис может прислать и внутри успешного потока.
                if event.get("error"):
                    message = event["error"]
                    if isinstance(message, dict):
                        message = message.get("message") or str(message)
                    raise ProviderError(str(message)[:300], self.name)
                yield event

    def _pairs(self, req: Request) -> Iterator[tuple[str, str]]:
        """Поток пар ``(тип, кусок)`` с уже разобранными рассуждениями.

        Разбирать приходится в двух местах сразу. Отдельное поле
        (``reasoning``) даёт не всякая модель: зрячая ``qwen3.6-27b`` у Groq
        кладёт ход мысли прямо в ответ тегами ``<think>``, и без разбора он
        уезжает читателю как часть текста.
        """
        tags = ThinkTags()
        for event in self._events(req):
            answer, think = self._split_delta(event)
            if think:
                yield ("thinking", think)
            if answer:
                yield from tags.feed(answer)
        yield from tags.drain()

    def _stream(self, req: Request) -> Iterator[str]:
        # Рассуждение придерживаем: у gpt-oss на Groq в ``reasoning`` лежит
        # сам ответ, а ``content`` пуст — там его отдать надо. Но если
        # content всё же пришёл, придержанное было ходом мысли, и в текст
        # ему нельзя.
        held: list[str] = []
        produced = False

        for kind, piece in self._pairs(req):
            if kind == "thinking":
                if not produced:
                    held.append(piece)
                continue
            produced = True
            held.clear()
            yield piece

        if not produced:
            if held:
                yield "".join(held)
                return
            raise ProviderError("пустой ответ", self.name)

    def stream_rich(self, req: Request) -> Iterator[tuple[str, str]]:
        self.validate(req)
        held: list[str] = []
        produced = False

        for kind, piece in self._pairs(req):
            if kind == "thinking" and not produced:
                held.append(piece)
            if kind == "text":
                produced = True
            yield (kind, piece)

        if not produced:
            # Ответа не было вовсе — значит рассуждение им и являлось.
            if held:
                yield ("text", "".join(held))
                return
            raise ProviderError("пустой ответ", self.name)


class GroqProvider(OpenAICompatProvider):
    """Groq — быстрый, но слабый.

    Держит ~16 000 символов входа (на 48 000 — HTTP 413) — годится на
    короткую генерацию, не на работу с длинным контекстом.
    """

    name = "groq"

    #: Зрячая среди бесплатных ровно одна: ``qwen/qwen3.6-27b`` цвет
    #: называет, остальные из пятнадцати моделей списка отвечают «content
    #: must be a string», то есть списком частей их не кормить вовсе.
    #: Моделей Llama 4, которые обычно приводят как зрячие у Groq, в
    #: бесплатном списке нет — 404.
    #:
    #: Держать её основной незачем: квоты у Groq считаются ПО МОДЕЛЯМ и не
    #: пересекаются, так что подмена под картинку ничего не отнимает у
    #: рабочих 14 400 запросов в сутки.
    VISION_MODEL = "qwen/qwen3.6-27b"

    #: Голоса Orpheus TTS. Модель англоязычная; русский читает с акцентом,
    #: но разборчиво.
    TTS_VOICES = ("autumn", "diana", "hannah", "austin", "daniel", "troy")
    TTS_MODEL = "canopylabs/orpheus-v1-english"
    #: Потолок текста на озвучку: модель держит ~1200 токенов, на большем
    #: отдаёт 413. Русский в токенах тяжелее латиницы — режем с запасом.
    TTS_MAX_CHARS = 900

    def transcribe(self, audio_data: bytes, filename: str = "audio.wav",
                   model: str = "whisper-large-v3-turbo",
                   language: str | None = None) -> dict:
        """Распознать речь через Whisper на Groq."""
        # Тело собираем руками: ``curl_cffi`` не понимает привычный
        # ``files=`` и падает на нём NotImplementedError — иначе
        # распознавание речи не заработает вовсе, а отказ будет выглядеть
        # как внутренняя ошибка сервера.
        fields = {"model": model}
        if language:
            fields["language"] = language
        guessed = mimetypes.guess_type(filename)[0] or "audio/wav"
        body, content_type = _http.multipart(
            fields, name="file", filename=filename,
            data=audio_data, content_type=guessed)
        with _http.session() as session:
            response = _http.request(
                session, "POST",
                f"{self.base_url}/audio/transcriptions",
                provider=self.name,
                headers={"Authorization": f"Bearer {self.credential.value}",
                         "Content-Type": content_type},
                data=body,
                timeout=120,
            )
            self._absorb_limits(response)
            _http.check(self.name, response)
            return response.json()

    def synthesize(self, text: str, voice: str = "autumn") -> bytes:
        """Озвучить текст через Orpheus TTS. Возвращает WAV.

        Условия модели приняты не на всех аккаунтах, поэтому вызывающий
        (сервер) перебирает ключи и ловит ``ProviderRefused`` на том, где
        не приняты.
        """
        if voice not in self.TTS_VOICES:
            voice = "autumn"
        # Orpheus держит ~1200 токенов на запрос; на длинном тексте отдаёт
        # HTTP 413. Русский текст в токенах «тяжелее» латиницы, поэтому
        # режем консервативно — лучше озвучить начало, чем упасть.
        clip = text.strip()[:self.TTS_MAX_CHARS]
        with _http.session() as session:
            response = _http.request(
                session, "POST",
                f"{self.base_url}/audio/speech",
                provider=self.name,
                headers={"Authorization": f"Bearer {self.credential.value}",
                         "Content-Type": "application/json"},
                json={
                    "model": self.TTS_MODEL,
                    "input": clip,
                    "voice": voice,
                    "response_format": "wav",
                },
                timeout=60,
            )
            self._absorb_limits(response)
            body = response.text if response.status_code >= 400 else ""
            if response.status_code == 400 and "terms" in body.lower():
                raise ProviderError(
                    "условия TTS не приняты на этом ключе", self.name)
            if response.status_code == 413 or "too large" in body.lower():
                raise ProviderError(
                    "текст слишком длинный для озвучки — сократи", self.name)
            _http.check(self.name, response)
            return response.content


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter — API-хаб с 340+ моделями, 17 бесплатных.

    Полностью OpenAI-совместимый. Ключ ``sk-or-…`` берётся в настройках
    аккаунта. Бесплатные модели включают Nemotron 550B на миллион токенов
    контекста — самое большое окно в пуле.
    """

    name = "openrouter"


class AgentRouterProvider(OpenAICompatProvider):
    """AgentRouter — релей к фронтир-моделям по ПРОТОКОЛУ ANTHROPIC.

    Рабочая модель — ``gpt-5.6-sol``. ``claude-opus-4-8`` и ``claude-opus-5``
    токену тоже выданы, но у них общий *budget pool*, который часто
    исчерпан: ``402 Budget pool quota has been exhausted``. Иные модели
    (``claude-opus-4-6/4-7``, ``gpt-5.6-terra``, ``sonnet``) отвечают
    ``403 该令牌无权访问模型`` — доступа у токена нет.

    **Это НЕ OpenAI-совместимый вход, а Anthropic Messages.** Сервис —
    «бесплатный сервис для кодинга», и его API повторяет Anthropic: эндпоинт
    ``/v1/messages``, тело с ОБЯЗАТЕЛЬНЫМ ``max_tokens``, поток — Anthropic
    SSE (``content_block_delta`` → ``delta.text``). Прежний OpenAI-путь
    ``/v1/chat/completions`` у них закрыт и отвечает пустым ``402``. База в
    реестре БЕЗ ``/v1``: добавляем ``/v1/messages`` здесь.

    **Заголовки обязательны.** Сервис опознаёт клиента и отвечает
    ``401 unauthorized client detected`` всем, кого не узнал, — в том числе
    SDK openai. Тот же ключ с заголовками claude-cli и ``anthropic-version``
    принимается.

    Ключей может быть несколько через ``|`` — это ПУЛ, а не склейка (см.
    ``registry.MULTI_KEY``). Разбирать пул обязано хранилище учёток:
    адаптеру полагается ровно один ключ, иначе он не сможет честно сказать,
    какой именно кончился.
    """

    name = "agentrouter"
    #: Релей тормозит сильнее любого API, параллелим осторожнее.
    slots = 2
    #: Anthropic требует потолок ответа всегда; берём щедрый, если не задан.
    DEFAULT_MAX_TOKENS = 4096
    extra_headers = {
        "User-Agent": "claude-cli/2.0.0 (external, cli)",
        "x-app": "cli",
    }

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.credential.value}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": "2023-06-01",
            **self.extra_headers,
        }

    def _body(self, req: Request) -> dict:
        # Тело Anthropic: одиночное сообщение пользователя (историю выше по
        # стеку уже сплющили в prompt), обязательный потолок ответа.
        body = {
            "model": self._pick_model(req),
            "max_tokens": req.max_tokens or self.DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": req.prompt}],
            "stream": True,
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        return body

    def _events(self, req: Request):
        """Кадры Anthropic-потока с разбором ошибки, присланной внутри него."""
        with _http.session() as session:
            response = _http.request(
                session, "POST", f"{self.base_url}/v1/messages",
                provider=self.name, headers=self._headers(),
                json=self._body(req), timeout=req.timeout, stream=True)
            self._absorb_limits(response)
            _http.check(self.name, response)

            for event in _http.sse_events(response):
                # Anthropic присылает ошибку кадром {"type":"error","error":…}.
                if event.get("type") == "error" or event.get("error"):
                    message = event.get("error") or event
                    if isinstance(message, dict):
                        message = message.get("message") or str(message)
                    raise ProviderError(str(message)[:300], self.name)
                yield event

    @classmethod
    def _split_delta(cls, event: dict) -> tuple[str, str]:
        """Разложить Anthropic-кадр на ``(ответ, рассуждение)``.

        Текст идёт в ``content_block_delta`` с ``delta.type == "text_delta"``;
        ход мысли расширенного режима — ``thinking_delta`` с полем
        ``thinking``. Прочие кадры (``message_start``, ``*_stop`` …) пусты.
        """
        if event.get("type") != "content_block_delta":
            return "", ""
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            return (delta.get("text") or ""), ""
        if dtype == "thinking_delta":
            return "", (delta.get("thinking") or "")
        return "", ""

class Llm7Provider(OpenAICompatProvider):
    """LLM7.io — OpenAI-совместимый шлюз, работающий БЕЗ КЛЮЧА.

    Редкость: обычно «бесплатный API» означает бесплатный ключ, а тут не
    нужно вообще ничего — ни регистрации, ни почты. Для пула это ценно
    иначе, чем очередной ключ: такому провайдеру нечему протухнуть, и он
    остаётся последней опорой, когда все сессии на паузе.

    Бесплатных моделей три (остальные 31 — платные, уровень ``pro``, и на
    них приходит ``model_unavailable``). Все с большим окном:
    ``gpt-oss:20b`` 128k, ``gemma4:31b`` 262k, ``minimax-m2.7`` 180k.
    Отвечает на живой запрос без единого заголовка.

    Зрения нет ни у одной из бесплатных — флаг снят, иначе картинка
    уехала бы туда, где её не посмотрят.
    """

    name = "llm7"
    capabilities = Capabilities(text=True)
    #: Заявленный предел — 30 запросов в минуту. Параллелим осторожно.
    slots = 2

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        # Базовый класс требует ключ, а здесь его нет и быть не должно —
        # поэтому повторяем его настройку сами, без проверки ключа.
        Provider.__init__(self, credential, model, on_rotate)
        base_url = config(self.name).get("base_url")
        if not base_url:
            raise ProviderError(
                f"в реестре нет base_url для {self.name}", self.name)
        self.base_url = base_url.rstrip("/")
        self.remaining = -1
        self.limit = -1
        self.reset_after = ""
        self.authorized = True

    def _headers(self) -> dict:
        """Без ``Authorization``: сервис его не ждёт и не проверяет."""
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self.extra_headers,
        }

class CohereProvider(OpenAICompatProvider):
    """Cohere через их OpenAI-совместимый вход.

    Пробный ключ даётся без карты и без телефона. Норму сервис сообщает
    САМ, своими заголовками — редкая любезность:
    ``x-endpoint-monthly-call-limit: 1000`` и
    ``x-trial-endpoint-call-limit: 20`` (это в минуту). Разбираем их в
    ``_absorb_limits``, чтобы остаток был виден до того, как упрёмся.

    Зрячая модель отдельная — ``command-a-vision-07-2025``; подставляем
    её сама под картинку, как это сделано у Groq. Держать её основной
    незачем: на тексте она не лучше, а норма общая.
    """

    name = "cohere"
    VISION_MODEL = "command-a-vision-07-2025"
    #: Двадцать запросов в минуту — параллелить особо нечего.
    slots = 2

    def _absorb_limits(self, response) -> None:
        super()._absorb_limits(response)
        headers = getattr(response, "headers", None)
        if not headers:
            return
        # Cohere называет заголовки по-своему, общий разбор их не видит.
        monthly = headers.get("x-endpoint-monthly-call-limit")
        if monthly:
            try:
                self.limit = int(monthly)
            except (TypeError, ValueError):
                pass


class CloudflareProvider(OpenAICompatProvider):
    """Cloudflare Workers AI — 10 000 нейронов в сутки бесплатно.

    Вход OpenAI-совместимый, но base_url содержит account_id:
    ``https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1``.
    Account_id берётся из URL дашборда, токен создаётся шаблоном
    «Workers AI» в разделе API Tokens. Карта не нужна.

    Модели именуются ``@cf/meta/llama-3.3-70b-instruct-fp8-fast``,
    а не просто ``llama-3.3-70b``. Норма: 10 000 нейронов в сутки,
    сброс в 00:00 UTC.
    """

    name = "cloudflare"
    capabilities = Capabilities(text=True)
    slots = 2

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # account_id — не секрет, но идентифицирует конкретный аккаунт,
        # поэтому в реестре стоит плейсхолдер {account}, а само значение
        # берётся из окружения или settings.json (из URL дашборда Cloudflare).
        if "{account}" in self.base_url:
            import os

            from foxroute import settings

            account = (os.environ.get("FOXROUTE_CF_ACCOUNT")
                       or settings.get("cf_account", ""))
            if not account:
                raise ProviderError(
                    "нужен Cloudflare account id — задай FOXROUTE_CF_ACCOUNT "
                    "или cf_account в settings.json (берётся из URL дашборда)",
                    self.name)
            self.base_url = self.base_url.replace("{account}", str(account))
