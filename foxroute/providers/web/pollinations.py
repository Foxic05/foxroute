"""Pollinations — картинки по адресу, без ключа и без очереди.

Рисование устроено предельно просто: описание кладётся прямо в путь запроса,
а в ответ приходит готовый JPEG. Ни тела запроса, ни опроса готовности, ни
идентификатора задания — то, что у Bing или Alice занимает десятки секунд с
ожиданием, тут укладывается в одну секунду.

**Текстовую часть намеренно не берём.** Их ``text.pollinations.ai`` отдаёт
119 моделей, включая claude-opus-5 и grok-4.5, и это выглядит очень заманчиво
— но он отдаёт **402 Payment Required** уже на втором запросе, а в теле
ответа сказано, что старый текстовый API сворачивают. Обещание без доступа
хуже отсутствия: маршрутизатор потратил бы на него попытку.

**Ссылку возвращаем, а не байты.** Адрес детерминирован — то же описание с
тем же ``seed`` даёт ту же картинку, — поэтому он годится как постоянная
ссылка, и лишняя перекачка через нас никому не нужна.

Проверять этот сервис обычным ``curl`` бесполезно: он отдаёт страницу
блокировки. Нужен отпечаток настоящего браузера, который даёт ``_http``.
"""
from __future__ import annotations

import urllib.parse
from typing import Iterator

from foxroute.errors import ProviderError, Unsupported
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

#: Соотношение сторон → размер. Сервис принимает любые числа, но кратные 64
#: он обрабатывает заметно быстрее.
_SIZES = {
    "1:1": (1024, 1024),
    "3:2": (1216, 832),
    "2:3": (832, 1216),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
}


class PollinationsProvider(Provider):
    name = "pollinations"
    #: Только рисование: текст у них платный, см. шапку модуля.
    capabilities = Capabilities(text=False, images_out=True, streaming=False)

    ENDPOINT = "https://image.pollinations.ai/prompt/{}"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self.authorized = True

    def _stream(self, req: Request) -> Iterator[str]:
        raise Unsupported("Pollinations отдаёт только картинки", self.name)
        yield  # pragma: no cover — делает функцию генератором

    def _draw(self, req: Request) -> list[str]:
        width, height = _SIZES.get(req.aspect, _SIZES["1:1"])
        query = urllib.parse.urlencode({
            "width": width,
            "height": height,
            # Иначе поверх картинки печатается их водяной знак.
            "nologo": "true",
            "model": self.resolve_model(req) or "flux",
        })
        url = self.ENDPOINT.format(
            urllib.parse.quote(req.prompt, safe="")) + "?" + query

        with _http.session() as session:
            response = _http.request(
                session, "GET", url, provider=self.name, timeout=req.timeout)
            _http.check(self.name, response)

            kind = (response.headers.get("content-type") or "").lower()
            if not kind.startswith("image/"):
                raise ProviderError(
                    f"вместо картинки пришло {kind or 'непонятно что'}",
                    self.name)
            if len(response.content) < 1000:
                raise ProviderError("картинка подозрительно мала", self.name)

        return [url]
