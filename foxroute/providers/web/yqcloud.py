"""Yqcloud — gpt-4 без всякого входа.

Самый простой адаптер пула: ни кук, ни ключа, ни подписи. Витрина живёт на
``chat9.yqcloud.top``, а отвечает чужой эндпоинт ``api.binjie.fun`` — витрина
и бэкенд у них на разных доменах, поэтому ``Origin`` и ``Referer`` обязаны
указывать на витрину, иначе запрос отбивается.

**Ответ приходит голым текстом, а не SSE.** Ни ``data:``, ни JSON — сервер
просто льёт куски строки в тело ответа. Это редкость: почти все остальные
в пуле отдают SSE, и общий разбор кадров тут не применим, читаем поток как
есть.

**``userId`` — не аккаунт, а метка разговора.** Формат ``#/chat/<мс>`` взят с
их фронтенда: это кусок адресной строки браузера, который они переиспользуют
как идентификатор. Нового ставим на каждый запрос — при ``withoutContext``
сервер историю всё равно не держит.

Ценность провайдера — в нулевом пороге: он работает сразу после установки,
без единого доступа, и годится как запасной, когда весь пул на паузе.
"""
from __future__ import annotations

import time
from typing import Iterator

from foxroute.errors import ProviderError
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request


class YqcloudProvider(Provider):
    name = "yqcloud"
    capabilities = Capabilities(text=True)

    SITE = "https://chat9.yqcloud.top"
    ENDPOINT = "https://api.binjie.fun/api/generateStream"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # Доступа не требует вовсе: считаем авторизованным всегда, иначе
        # маршрутизатор счёл бы его недоступным и не стал бы предлагать.
        self.authorized = True

    def _headers(self) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            # Витрина и бэкенд на разных доменах — без этой пары отказ.
            "Origin": self.SITE,
            "Referer": f"{self.SITE}/",
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        }

    def _stream(self, req: Request) -> Iterator[str]:
        body = {
            "prompt": req.prompt,
            "userId": f"#/chat/{int(time.time() * 1000)}",
            "network": bool(req.web_search),
            "system": "",
            # Историю держим мы, а не они: контекст уже склеен в промпт.
            "withoutContext": False,
            "stream": True,
        }

        produced = False
        with _http.session() as session:
            response = _http.request(
                session, "POST", self.ENDPOINT, provider=self.name,
                headers=self._headers(), json=body,
                timeout=req.timeout, stream=True)
            _http.check(self.name, response)

            for chunk in response.iter_content(chunk_size=None):
                if not chunk:
                    continue
                piece = chunk.decode("utf-8", "replace")
                if piece:
                    produced = True
                    yield piece

        if not produced:
            raise ProviderError("пустой ответ", self.name)
