"""Perplexity — отвечает с опорой на живую выдачу.

Ценность именно в этом: модель без доступа в сеть выдумывает правдоподобные
ссылки, и отличить их от настоящих по виду нельзя.

**Самый хрупкий адаптер из всех.** Тело запроса версионированное и полно
полей, назначение которых наружу не документировано; формат ответа за время
работы уже менялся (раньше ``answer`` лежал на верхнем уровне, теперь внутри
``blocks[].markdown_block.answer``). Поддерживаем оба вида намеренно, чтобы
не сломаться при очередном переключении.

Держит ~16 000 символов входа (на 48 000 теряет конец), в агентный цикл
непригоден — вместо подстановки пути выдаёт буквально шаблон синтаксиса
``READ <путь>``. Держим как источник ссылок, не как исполнителя.
"""
from __future__ import annotations

import json
import uuid
from typing import Iterator

from foxroute.errors import ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers._http import Accumulated
from foxroute.providers.base import Capabilities, Credential, Provider, Request


class PerplexityProvider(Provider):
    name = "perplexity"
    # Ответ приезжает одним кадром в конце: сервис копит его у себя.
    #: Файлы НЕ заявлены, хотя загрузка написана и работает. У бесплатной
    #: учётки в ``/rest/user/settings`` стоит ``upload_limit: 0`` — загрузок
    #: не положено ВООБЩЕ. Отсюда и странность: место под файл сервис выдаёт,
    #: S3 байты берёт, а модель файла не видит — вложение до неё просто не
    #: доходит. Код загрузки верен и оставлен на случай платной учётки, но
    #: умение заявлять нельзя: на бесплатной оно не работает никогда.
    capabilities = Capabilities(text=True, web_search=True,
                                streaming=False)

    BASE = "https://www.perplexity.ai"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # Сервис отвечает и анонимно, просто с более тесными лимитами:
        # даже с заведомо негодной кукой ответ приходит.
        self.authorized = bool(credential.value)

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Файл кладётся не к ним, а прямо в S3: сервис выдаёт подписанную форму
    # (``fields``), которую надо отправить ЦЕЛИКОМ и в том же составе —
    # подпись покрывает все поля, и лишнее либо недостающее ломает её.
    # Файл в этой форме идёт ПОСЛЕДНИМ, так требует S3.
    #
    # Затем в запрос кладётся не идентификатор, а конечный адрес объекта.

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    def _upload(self, session, headers: dict, item) -> str:
        """Положить вложение в их хранилище и вернуть адрес объекта."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        name = item.filename or "file.bin"
        mime = item.mime or "application/octet-stream"
        asked = _http.request(
            session, "POST", f"{self.BASE}/rest/uploads/create_upload_url",
            provider=self.name, headers=headers,
            json={"filename": name, "content_type": mime,
                  "file_size": len(raw), "source": "default"},
            timeout=60)
        _http.check(self.name, asked)
        try:
            place = asked.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на запрос места под файл", self.name) from exc

        if place.get("rate_limited"):
            # Норма на ЗАГРУЗКИ считается отдельно от нормы на вопросы и
            # выбирается заметно быстрее. Отказ приходит полем в теле, а не
            # кодом 429, — без разбора он выглядел бы поломкой.
            raise RateLimited("норма на загрузку файлов выбрана", self.name)

        bucket = place.get("s3_bucket_url") or ""
        target = place.get("s3_object_url") or ""
        fields = place.get("fields") or {}
        if not bucket or not target or not fields:
            raise ProviderError(
                f"сервис не выдал место под файл: {str(place)[:200]}",
                self.name)

        body, ctype = _http.multipart(fields, filename=name, data=raw,
                                      content_type=mime)
        put = _http.request(session, "POST", bucket, provider=self.name,
                            headers={"Content-Type": ctype}, data=body,
                            timeout=300)
        # S3 на успешную форму отвечает 204 без тела.
        if put.status_code not in (200, 201, 204):
            raise ProviderError(
                f"хранилище отвергло файл: HTTP {put.status_code} "
                f"{(put.text or '')[:200]}", self.name)
        return target

    def _params(self, model: str, req: Request) -> dict:
        """Параметры запроса.

        Состав снят с живого клиента. Убирать отсюда поля наугад нельзя:
        сервис версионирует тело и на неполном молча меняет поведение.
        """
        return {
            "attachments": [],
            "language": "ru-RU",
            "timezone": "Europe/Moscow",
            "search_focus": "internet",
            "sources": ["web"],
            "search_recency_filter": None,
            "frontend_uuid": str(uuid.uuid4()),
            "frontend_context_uuid": str(uuid.uuid4()),
            "mode": "copilot",
            "model_preference": model,
            "is_related_query": False,
            "is_sponsored": False,
            "prompt_source": "user",
            "query_source": "home",
            "is_incognito": False,
            "local_search_enabled": False,
            "use_schematized_api": True,
            # Без этого в потоке едут только блоки, а сам текст — нет.
            "send_back_text_in_streaming_api": True,
            "supported_block_use_cases": [],
            "client_coordinates": None,
            "mentions": [],
            "dsl_query": req.prompt,
            "skip_search_enabled": True,
            "is_nav_suggestions_disabled": False,
            "source": "default",
            "always_search_override": False,
            "override_no_search": not req.web_search,
            "supported_features": [],
            "version": "2.18",
        }

    @staticmethod
    def _answer(event: dict) -> str:
        """Накопленный текст ответа из кадра. Понимает оба формата."""
        # Нынешний: blocks -> markdown_block -> answer.
        for block in event.get("blocks") or []:
            markdown = block.get("markdown_block") or {}
            answer = markdown.get("answer")
            if isinstance(answer, str) and answer.strip():
                return answer

        # Прежний: answer или text на верхнем уровне. Иногда там лежит не
        # текст, а JSON строкой — с настоящим ответом внутри.
        for field in ("answer", "text"):
            value = event.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            if not value.lstrip().startswith("{"):
                return value
            try:
                inner = json.loads(value)
            except ValueError:
                return value
            nested = inner.get("answer") if isinstance(inner, dict) else None
            return nested if isinstance(nested, str) else value
        return ""

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            **({"Cookie": "__Secure-next-auth.session-token="
                          f"{self.credential.value}"}
               if self.credential.value else {}),
            "x-perplexity-request-reason":
                "perplexity-query-state-provider",
            "x-request-id": str(uuid.uuid4()),
            "Origin": self.BASE,
            "Referer": f"{self.BASE}/",
        }

    def _stream(self, req: Request) -> Iterator[str]:
        model = self.resolve_model(req)
        with _http.session() as session:
            headers = self._headers()
            params = self._params(model, req)
            params["attachments"] = [self._upload(session, headers, item)
                                     for item in req.attachments]
            response = _http.request(
                session, "POST", f"{self.BASE}/rest/sse/perplexity_ask",
                provider=self.name, headers=headers,
                json={"query_str": req.prompt, "params": params},
                timeout=req.timeout, stream=True)
            _http.check(self.name, response)

            # Текст приходит накопленным: в каждом кадре весь ответ целиком.
            grown = Accumulated()
            produced = False
            for event in _http.sse_events(response):
                whole = self._answer(event)
                if not whole:
                    continue
                delta = grown.feed(whole)
                if delta:
                    produced = True
                    yield delta

            if not produced:
                raise ProviderError("пустой ответ", self.name)
