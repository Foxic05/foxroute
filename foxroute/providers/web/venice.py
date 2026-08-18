"""Venice — веб-сессия через api.venice.ai/api/inference/chat.

Авторизация — Clerk JWT, который живёт 60 секунд. Провайдер обновляет
его перед каждым запросом через ``/v1/client/sessions/{sid}/tokens``.
Кредентой служит **весь набор кук** из залогиненного браузера, но на деле
достаточно ``__session`` — из неё извлекается ``sid``, а сам JWT
обновляется через Clerk.

Формат ответа — не SSE, а NDJSON (одна строка = один JSON-объект):
``{"kind":"content","content":"..."}`` для текста,
``{"kind":"reasoning","content":"..."}`` для хода мысли,
``{"kind":"meta",...}`` для метаданных.

Бесплатный тариф: 10 текстовых запросов в день, модель
``venice-uncensored-1-2``. Платные модели (GPT, Claude, Gemini,
DeepSeek) отвечают 402 insufficientBalance.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request


class VeniceProvider(Provider):
    name = "venice"
    #: Поиск в сети ЕСТЬ — поле ``webEnabled``, проверено свежим фактом.
    #: Размышлений НЕТ: поле ``reasoning`` схема принимает, но единственная
    #: бесплатная модель ``venice-uncensored-1-2`` их не умеет
    #: (``supportsReasoning: false`` в каталоге, на живом — ноль кусков
    #: ``reasoning``). Остальные 84 рассуждающие модели платные, поэтому
    #: кнопку «думать» заявлять нельзя — она бы не работала.
    #: Картинок на вход тоже нет: в схеме тела ``content`` — только строка,
    #: полей под вложения нет вовсе.
    capabilities = Capabilities(text=True, streaming=True, web_search=True)

    BASE = "https://api.venice.ai"
    CLERK = "https://clerk.venice.ai"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        raw = credential.value
        if not raw:
            raise ProviderError("нужны куки из venice.ai", self.name)
        # Кредента — либо чистый JWT (__session), либо JSON с куками.
        # JWT начинается с eyJ, куки — с {.
        if raw.strip().startswith("{"):
            self._cookies: dict[str, str] = json.loads(raw)
        else:
            self._cookies = {"__session": raw}
        self._sid = self._extract_sid()

    def _extract_sid(self) -> str:
        """Clerk session ID из JWT."""
        token = self._cookies.get("__session", "")
        idx = token.find("eyJ")
        if idx >= 0:
            token = token[idx:]
        parts = token.split(".")
        if len(parts) < 2:
            raise AuthError("невалидный JWT в __session", self.name)
        try:
            padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.b64decode(padded))
            return payload.get("sid", "")
        except Exception as exc:
            raise AuthError(f"не разобрал JWT: {exc}", self.name) from exc

    def _refresh_jwt(self) -> str:
        """Свежий Clerk JWT — живёт ~60 секунд."""
        safe_cookies = {}
        for k, v in self._cookies.items():
            try:
                v.encode("latin-1")
                safe_cookies[k] = v
            except (UnicodeEncodeError, AttributeError):
                idx = v.find("eyJ")
                if idx >= 0:
                    safe_cookies[k] = v[idx:]

        url = (
            f"{self.CLERK}/v1/client/sessions/{self._sid}"
            f"/tokens?_clerk_js_version=5.56.0"
        )
        with _http.session() as session:
            response = _http.request(
                session, "POST", url,
                provider=self.name,
                headers={
                    "Origin": "https://venice.ai",
                    "Referer": "https://venice.ai/",
                },
                cookies=safe_cookies,
                timeout=15,
            )
        if response.status_code == 401:
            raise AuthError("Clerk-сессия протухла, нужен перелогин", self.name)
        _http.check(self.name, response)
        data = response.json()
        jwt = data.get("jwt", "")
        if not jwt:
            raise AuthError("Clerk не вернул JWT", self.name)
        return jwt

    def _headers(self, jwt: str) -> dict:
        return {
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Origin": "https://venice.ai",
            "Referer": "https://venice.ai/chat/v2",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    def _body(self, req: Request) -> dict:
        model = self.resolve_model(req)
        return {
            "modelId": model,
            "prompt": [{"role": "user", "content": req.prompt}],
            "requestId": str(uuid.uuid4()),
            # Поиск в сети. Имя поля снято со схемы: сервис проверяет тело
            # через zod и на неверном ТИПЕ называет поле, а выдуманные поля
            # молча пропускает — так и вышел список настоящих.
            "webEnabled": bool(req.web_search),
        }

    def _ndjson_stream(self, response) -> Iterator[tuple[str, str]]:
        """Разбираем NDJSON: каждая строка — JSON-объект с ``kind``."""
        for raw in response.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = obj.get("kind", "")
            content = obj.get("content", "")
            if kind == "content" and content:
                yield ("text", content)
            elif kind == "reasoning" and content:
                yield ("thinking", content)
            elif kind == "meta" and obj.get("references"):
                # Найденное в сети приходит ОТДЕЛЬНО, последним кадром. В
                # тексте от него остаются только сноски вида ^1^ — без
                # этого списка они висят в пустоте.
                yield ("text", self._sources(obj["references"]))

    @staticmethod
    def _sources(references: list) -> str:
        """Список источников под ответом."""
        seen: set[str] = set()
        lines: list[str] = []
        for ref in references:
            if not isinstance(ref, dict):
                continue
            link = str(ref.get("url") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            title = str(ref.get("title") or link).strip()
            lines.append(f"{len(lines) + 1}. [{title}]({link})")
        if not lines:
            return ""
        return "\n\n## Источники\n\n" + "\n".join(lines)

    def _stream(self, req: Request) -> Iterator[str]:
        jwt = self._refresh_jwt()
        safe_cookies = {}
        for k, v in self._cookies.items():
            try:
                v.encode("latin-1")
                safe_cookies[k] = v
            except (UnicodeEncodeError, AttributeError):
                idx = v.find("eyJ")
                if idx >= 0:
                    safe_cookies[k] = v[idx:]
        safe_cookies["__session"] = jwt

        # ВЕСЬ разбор ответа — внутри with: поток и response.text привязаны
        # к сессии curl_cffi, и читать их после её закрытия нельзя (у
        # остальных адаптеров with охватывает весь цикл чтения).
        with _http.session() as session:
            response = _http.request(
                session, "POST",
                f"{self.BASE}/api/inference/chat",
                provider=self.name,
                headers=self._headers(jwt),
                cookies=safe_cookies,
                json=self._body(req),
                timeout=req.timeout,
                stream=True,
            )
            if response.status_code == 402:
                body = response.text[:200]
                if "insufficientBalance" in body:
                    raise RateLimited(
                        "бесплатный лимит исчерпан (10/день)", self.name)
                raise RateLimited(body[:120], self.name)
            if response.status_code == 400:
                body = response.text[:200]
                if "proOnlyModel" in body:
                    raise ProviderError(
                        "модель доступна только на Pro", self.name)
                if "modelNotFound" in body:
                    raise ProviderError(
                        f"модель не найдена: {self.resolve_model(req)}",
                        self.name)
            _http.check(self.name, response)

            produced = False
            for kind, piece in self._ndjson_stream(response):
                if kind == "text":
                    produced = True
                    yield piece
            if not produced:
                raise ProviderError("пустой ответ", self.name)

    def stream_rich(self, req: Request) -> Iterator[tuple[str, str]]:
        self.validate(req)
        jwt = self._refresh_jwt()
        safe_cookies = {}
        for k, v in self._cookies.items():
            try:
                v.encode("latin-1")
                safe_cookies[k] = v
            except (UnicodeEncodeError, AttributeError):
                idx = v.find("eyJ")
                if idx >= 0:
                    safe_cookies[k] = v[idx:]
        safe_cookies["__session"] = jwt

        with _http.session() as session:
            response = _http.request(
                session, "POST",
                f"{self.BASE}/api/inference/chat",
                provider=self.name,
                headers=self._headers(jwt),
                cookies=safe_cookies,
                json=self._body(req),
                timeout=req.timeout,
                stream=True,
            )
            if response.status_code == 402:
                raise RateLimited(
                    "бесплатный лимит исчерпан (10/день)", self.name)
            _http.check(self.name, response)

            produced = False
            for pair in self._ndjson_stream(response):
                produced = True
                yield pair
            if not produced:
                raise ProviderError("пустой ответ", self.name)
