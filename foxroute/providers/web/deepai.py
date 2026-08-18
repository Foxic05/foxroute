"""DeepAI — текст на бесплатных кредитах аккаунта.

Доступ — куки ``deepai.org`` строкой «a=b; c=d», нужна ``sessionid``.

Эндпоинт называется ``/hacking_is_a_serious_crime`` — это не шутка автора
адаптера, так он назван у них. Тело — multipart с полями, подсмотренными в
их же скрипте, включая обязательное ``hacker_is_stinky=very_stinky``.

**Ключ считается нестандартно.** Их ``myhashfunction`` — это md5, у которого
**hex-строка развёрнута задом наперёд**. Обычный md5 чат-эндпоинт
проглатывает, а рисовалка отбивает с 401 — отсюда и брались жалобы на
«неверный ключ» при внешне правильном.

Держит меньше 4 000 символов входа — теряет маркеры уже на четырёх тысячах,
худший результат из всех. Годится на короткие однократные запросы.
"""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

#: Тот же User-Agent обязан уйти и в заголовок, и в расчёт ключа.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

#: Соль лежит в их JS открытым текстом.
SALT = "hackers_become_a_little_stinkier_every_time_they_hack"


def _reversed_md5(value: str) -> str:
    """md5 с развёрнутой hex-строкой — то, что у них зовётся myhashfunction."""
    return hashlib.md5(value.encode()).hexdigest()[::-1]


def make_key() -> str:
    """Одноразовый ключ ``tryit-…``, как его собирает generateIslandKey."""
    nonce = str(random.randint(0, 10 ** 11))
    inner = _reversed_md5(USER_AGENT + nonce + SALT)
    middle = _reversed_md5(USER_AGENT + inner)
    return f"tryit-{nonce}-" + _reversed_md5(USER_AGENT + middle)


class DeepAIProvider(Provider):
    name = "deepai"
    # Эндпоинт возвращает готовый текст: потока тут нет по природе.
    capabilities = Capabilities(text=True, streaming=False, images_out=True)

    ENDPOINT = "https://api.deepai.org/hacking_is_a_serious_crime"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # Ключ ``tryit-…`` считается на нашей стороне, а кука привязывает
        # бесплатные кредиты к аккаунту. Без неё сервис отвечает как
        # анонимному гостю — рабочий режим, но кредитов у гостя меньше.
        self.authorized = bool(credential.value)

    def _session(self):
        session = _http.session()
        for part in self.credential.value.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            # Домен именно с точкой: запросы уходят на api.deepai.org, и на
            # голом deepai.org сессия туда не долетает.
            session.cookies.set(key, value, domain=".deepai.org")
        return session

    def _form(self, req: Request):
        from curl_cffi import CurlMime

        fields = {
            "chat_style": "chat",
            "chatHistory": json.dumps(
                [{"role": "user", "content": req.prompt}]),
            "model": self.resolve_model(req),
            "session_uuid": str(uuid.uuid4()),
            "sensitivity_request_id": str(uuid.uuid4()),
            "hacker_is_stinky": "very_stinky",
        }
        form = CurlMime()
        for name, value in fields.items():
            form.addpart(name=name, data=value.encode())
        return form

    def _stream(self, req: Request) -> Iterator[str]:
        """Ответ приходит целиком: эндпоинт отдаёт готовый текст."""
        with self._session() as session:
            response = _http.request(
                session, "POST", self.ENDPOINT, provider=self.name,
                headers={
                    "api-key": make_key(),
                    "User-Agent": USER_AGENT,
                    "Origin": "https://deepai.org",
                    "Referer": "https://deepai.org/",
                },
                multipart=self._form(req), timeout=req.timeout)
            _http.check(self.name, response)
            text = (response.text or "").strip()

        # Отказ приезжает обычным текстом с кодом 200: без разбора он выглядел
        # бы как нормальный ответ модели и уехал бы пользователю.
        lowered = text.lower()
        if "only paid accounts" in lowered or "please login" in lowered:
            raise AuthError(f"доступ не принят: {text[:200]}", self.name)
        if not text:
            raise ProviderError("пустой ответ", self.name)
        yield text

    def _draw(self, req: Request) -> list[str]:
        """Картинка через /api/text2img.

        Поле ``generation_source=chat`` обязательно: без него эндпоинт
        требует настоящий платный ключ и отбивает 401, хотя ключ
        вычисленный ``tryit-…`` для чата работает.
        """
        from curl_cffi import CurlMime

        form = CurlMime()
        form.addpart(name="text", data=req.prompt.encode())
        form.addpart(name="generation_source", data=b"chat")

        with self._session() as session:
            response = _http.request(
                session, "POST", "https://api.deepai.org/api/text2img",
                provider=self.name,
                headers={
                    "api-key": make_key(),
                    "User-Agent": USER_AGENT,
                    "Origin": "https://deepai.org",
                    "Referer": "https://deepai.org/",
                },
                multipart=form, timeout=req.timeout)
            _http.check(self.name, response)

        try:
            data = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на генерацию картинки", self.name) from exc

        url = data.get("output_url", "")
        if not url:
            status = str(data.get("status", ""))[:120]
            lowered = status.lower()
            if "limit" in lowered or "credit" in lowered:
                raise RateLimited(f"норма выбрана: {status}", self.name)
            raise ProviderError(
                f"картинка не создана: {status}", self.name)
        return [url]
