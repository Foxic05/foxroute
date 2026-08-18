"""Bing Image Creator — единственный провайдер, который только рисует.

Текста не пишет вовсе, поэтому в текстовую цепочку его пускать нельзя: он на
каждый запрос кинет отказ и просто съест попытку, отодвинув живых.

Доступ — куки ``.bing.com`` строкой «a=b; c=d», нужна ``_U``.

**Публичные реализации, включая g4f, стучатся по устаревшим адресам** и
получают пустоту. Настоящий порядок снят с живой страницы:

1. ``POST /images/create/ai-image-generator`` с параметрами в строке запроса
   и заголовком ``Content-Type: application/x-www-form-urlencoded``. **Тело
   пустое**, идентификатор присваивает сервер: попытка задать свой ``id``
   генерацию НЕ запускает — запрос просто теряется.
2. В ответной странице лежит ``requestId`` вида ``1-<32 hex>``.
3. Результат опрашивается на ``/images/create/async/mycreations``; там HTML
   с JSON внутри атрибута ``data-result-json``.
4. Берём запись со СВОИМ ``requestId`` и статусом 2. Без сверки
   идентификатора вернутся чужие картинки — те, что аккаунт делал раньше.
5. Ссылки лежат в ``mediaItems[].src`` на ``th.bing.com`` (а не на ``tse…``,
   как в устаревших реализациях), по четыре штуки за раз.

Даёт 1024×1024 без водяного знака.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, ProviderUnavailable, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

CREATE_URL = "https://www.bing.com/images/create/ai-image-generator"
POLL_URL = "https://www.bing.com/images/create/async/mycreations"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
              "Edg/140.0.0.0")

#: Соотношение сторон в их нумерации.
ASPECTS = {"1:1": "1", "3:2": "4", "2:3": "5"}

#: Статусы записи в галерее.
STATUS_READY = 2
STATUS_REJECTED = 3

#: Сколько ждём готовности. Рисование занимает десятки секунд.
POLL_ATTEMPTS = 30
POLL_INTERVAL = 4

RESULT_PATTERN = re.compile(r'data-result-json="(.*?)"\s', re.S)
ID_IN_URL = re.compile(r"[?&]id=([^&\"']+)")
ID_IN_HTML_ESCAPED = re.compile(r"requestId&quot;:&quot;([^&]+?)&quot;")
ID_IN_HTML = re.compile(r'"requestId"\s*:\s*"([^"]+)"')


class BingImagesProvider(Provider):
    name = "bing_images"
    #: Только рисует. Текста нет — и это заявлено честно, чтобы
    #: маршрутизатор не слал сюда то, чего провайдер не умеет.
    capabilities = Capabilities(text=False, streaming=False, images_out=True)

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if "_U=" not in credential.value:
            raise AuthError(
                "нужны куки .bing.com строкой «a=b; c=d», обязательна _U",
                self.name)

    def _session(self):
        session = _http.session()
        for part in self.credential.value.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            session.cookies.set(key, value, domain=".bing.com")
        return session

    def _start(self, session, prompt: str, aspect: str) -> str:
        """Запустить рисование, вернуть идентификатор запроса."""
        url = (f"{CREATE_URL}?FORM=GENCRE"
               f"&q={urllib.parse.quote(prompt)}"
               f"&ctype=image&mdl=10&ar={aspect}&rt=4")
        response = _http.request(
            session, "POST", url, provider=self.name,
            headers={
                "User-Agent": USER_AGENT,
                # Тело пустое, но заголовок обязателен: без него страница
                # отвечает формой, а генерация не запускается.
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": CREATE_URL,
                "Origin": "https://www.bing.com",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=_http.REQUEST_TIMEOUT)
        _http.check(self.name, response)

        page = response.text or ""
        lowered = page.lower()
        if "you have reached your limit" in lowered or "out of boosts" in lowered:
            raise RateLimited("дневная норма картинок выбрана", self.name)

        # Идентификатор приезжает либо в адресе после переадресации, либо
        # внутри страницы — в двух видах экранирования.
        found = ID_IN_URL.search(str(response.url))
        if found:
            return urllib.parse.unquote(found.group(1))
        for pattern in (ID_IN_HTML_ESCAPED, ID_IN_HTML):
            found = pattern.search(page)
            if found:
                return found.group(1)
        raise ProviderError(
            "сервер не вернул идентификатор запроса — вероятно, куки "
            "не приняты", self.name)

    def _collect(self, session, request_id: str, aspect: str) -> list[str]:
        """Дождаться готовности и собрать ссылки."""
        url = (f"{POLL_URL}?requestId={request_id}&offset=0&count=40"
               f"&mdl=10&ar={aspect}")

        for _ in range(POLL_ATTEMPTS):
            time.sleep(POLL_INTERVAL)
            response = _http.request(
                session, "GET", url, provider=self.name,
                headers={"User-Agent": USER_AGENT, "Referer": CREATE_URL},
                timeout=60)
            if getattr(response, "status_code", 0) != 200:
                continue

            found = RESULT_PATTERN.search(response.text or "")
            if not found:
                continue
            try:
                data = json.loads(html.unescape(found.group(1)))
            except (ValueError, TypeError):
                continue

            # Сверка идентификатора обязательна: в галерее лежат и прошлые
            # работы аккаунта, и без неё вернулись бы они.
            record = next((item for item in data.get("records", [])
                           if item.get("requestId") == request_id), None)
            if not record:
                continue

            status = record.get("requestStatus")
            if status == STATUS_REJECTED:
                raise ProviderError("запрос отклонён модерацией", self.name)
            if status != STATUS_READY:
                continue

            links = []
            for item in record.get("mediaItems", []):
                source = (item.get("src") or "").split("?")[0]
                if source:
                    links.append(
                        f"{source}?w=1024&h=1024&qlt=100&pid=ImgGn")
            if links:
                return links

        waited = POLL_ATTEMPTS * POLL_INTERVAL
        raise ProviderUnavailable(
            f"картинки не готовы за {waited} с", self.name)

    def _draw(self, req: Request) -> list[str]:
        aspect = ASPECTS.get(req.aspect, ASPECTS["1:1"])
        with self._session() as session:
            request_id = self._start(session, req.prompt, aspect)
            return self._collect(session, request_id, aspect)
