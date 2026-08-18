"""Mistral — веб-версия Le Chat (chat.mistral.ai).

Протокол разобран с нуля: в открытых реализациях его нет.

Три вещи, на которых легко споткнуться:

* поле запроса называется именно ``content``. ``message``, ``text``,
  ``prompt`` и прочие очевидные варианты дают ``{"detail":"Empty message"}``;
* нужен заголовок ``x-csrf-token``, равный куке ``csrftoken``, — её сервер
  ставит на первой же загрузке страницы, поэтому перед отправкой ходим на
  ``/chat``;
* ответ приходит **патчами**, а не готовым текстом: ``replace /contentChunks``
  кладёт первый кусок, дальше ``append /contentChunks/<i>/text`` дописывает
  хвосты.

Идёт по задаче верно, но умирает от квоты на 7-м ходу и уходит в окно на
168 минут. Это бюджет, а не троттлинг: пауза его не обходит. Годится на
короткие задачи, не на агентный цикл.
"""
from __future__ import annotations

import json
import re
from typing import Iterator

from foxroute.errors import ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers._http import Accumulated
from foxroute.providers.base import Capabilities, Credential, Provider, Request

#: Кадр потока: номер, двоеточие, объект.
_FRAME = re.compile(r"^\d+:(\{.*)$")
#: Путь патча, дописывающего хвост к куску с известным номером.
_APPEND_PATH = re.compile(r"/contentChunks/(\d+)/text")


class MistralProvider(Provider):
    name = "mistral"
    #: Файлы — см. ``_upload``. Зрение есть: картинку модель разбирает тем
    #: же путём, что и документ.
    capabilities = Capabilities(text=True, files_in=True, vision=True)

    BASE = "https://chat.mistral.ai"

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if not credential.value:
            # Без куки сервер отвечает ровно на одно сообщение, дальше молчит.
            raise ProviderError(
                "нужны куки chat.mistral.ai строкой «a=b; c=d»", self.name)

    def _prepare(self, session) -> str:
        """Загрузить страницу ради csrftoken и вернуть его.

        Заодно Cloudflare выдаёт ``__cf_bm`` — без него следующий запрос
        отбивается.
        """
        for part in self.credential.value.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            session.cookies.set(key, value, domain="chat.mistral.ai")

        _http.request(session, "GET", f"{self.BASE}/chat",
                      provider=self.name, timeout=60)
        jar = {cookie.name: cookie.value for cookie in session.cookies.jar}
        return jar.get("csrftoken", "")

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Файл лежит не у них, а в хранилище Azure. Порядок снят с живой
    # страницы (сама она поле выбора файла прячет, пока не открыто меню
    # композера):
    #
    #   1. ``/api/trpc/file.uploadFile`` — просим место, получаем
    #      подписанный адрес на запись (``uploadURLs``) и на чтение;
    #   2. ``PUT`` байтов туда, без наших кук; Azure требует заголовок
    #      ``x-ms-blob-type``, без него отвечает 400;
    #   3. в сообщение кладём АДРЕС ЗАПИСИ, а не чтения — страница шлёт
    #      именно его, и подпись на чтение сервису не нужна.
    #
    # Путь ``/api/file/upload`` не существует, а Cloudflare отвечает за всё
    # несуществующее кодом 403 — это легко принять за запрет доступа.

    @staticmethod
    def _kind(item) -> str:
        """Как сервис называет вид вложения."""
        mime = (item.mime or "").lower()
        if item.kind == "image" or mime.startswith("image/"):
            return "image"
        return "text"

    def _upload(self, session, headers: dict, item) -> dict:
        """Положить вложение в хранилище и собрать запись для сообщения."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        kind = self._kind(item)
        asked = _http.request(
            session, "POST", f"{self.BASE}/api/trpc/file.uploadFile?batch=1",
            provider=self.name, headers=headers,
            json={"0": {"json": {"type": kind, "count": 1,
                                 "includeReadUrl": True}}},
            timeout=60)
        _http.check(self.name, asked)
        try:
            answer = asked.json()
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на запрос места под файл", self.name) from exc

        places = []
        for entry in answer if isinstance(answer, list) else [answer]:
            data = (((entry or {}).get("result") or {}).get("data") or {})
            places = (data.get("json") or {}).get("uploadURLs") or []
            if places:
                break
        if not places:
            raise ProviderError(
                f"сервис не выдал место под файл: {str(answer)[:200]}",
                self.name)
        target = places[0]

        put = _http.request(
            session, "PUT", target, provider=self.name,
            headers={"x-ms-blob-type": "BlockBlob",
                     "Content-Type": item.mime or "application/octet-stream"},
            data=raw, timeout=300)
        if put.status_code not in (200, 201, 202):
            raise ProviderError(
                f"хранилище отвергло файл: HTTP {put.status_code} "
                f"{(put.text or '')[:200]}", self.name)

        return {"type": kind, "url": target,
                "name": item.filename or "file.bin"}

    def _check_frame(self, frame: dict) -> None:
        """Поднять ошибку, если это кадр отказа.

        Отказ приезжает ОТДЕЛЬНЫМ кадром внутри потока, а HTTP при этом 200.
        Без разбора этого кадра исчерпанная норма выглядит как «пустой
        ответ», то есть как поломка адаптера.
        """
        if "message" not in frame or "httpCode" not in frame:
            return
        message = str(frame.get("message", ""))
        if frame.get("category") == "rate_limit" or frame.get("httpCode") == 429:
            wait = frame.get("retryAfterSeconds")
            try:
                wait = float(wait) if wait else None
            except (TypeError, ValueError):
                wait = None
            raise RateLimited(message, self.name, retry_after=wait)
        raise ProviderError(message[:300], self.name)

    @staticmethod
    def _apply(patches: list, chunks: dict[int, str]) -> None:
        """Наложить патчи кадра на накопленные куски."""
        for patch in patches:
            operation = patch.get("op")
            path = patch.get("path", "")
            value = patch.get("value")

            if operation == "replace" and path == "/contentChunks":
                if not isinstance(value, list):
                    continue
                for index, item in enumerate(value):
                    if isinstance(item, dict) and item.get("type") == "text":
                        chunks[index] = item.get("text", "")
            elif operation == "append":
                match = _APPEND_PATH.match(path)
                if match and isinstance(value, str):
                    index = int(match.group(1))
                    chunks[index] = chunks.get(index, "") + value

    def _stream(self, req: Request) -> Iterator[str]:
        with _http.session() as session:
            csrf = self._prepare(session)
            headers = {
                "Content-Type": "application/json",
                "Origin": self.BASE,
                "Referer": f"{self.BASE}/chat",
                "x-csrf-token": csrf,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            }
            files = [self._upload(session, headers, item)
                     for item in req.attachments]
            response = _http.request(
                session, "POST", f"{self.BASE}/api/chat",
                provider=self.name,
                headers=headers,
                json={"mode": "create", "files": files,
                      "content": req.prompt},
                timeout=req.timeout, stream=True)
            _http.check(self.name, response)

            # Куски нумерованные, и собранный текст — это склейка по
            # возрастанию номера. Патч может дописать в любой из них, поэтому
            # после каждого кадра пересобираем целое и отдаём разницу.
            chunks: dict[int, str] = {}
            grown = Accumulated()
            produced = False

            for raw in response.iter_lines():
                if not raw:
                    continue
                line = (raw.decode("utf-8", "replace")
                        if isinstance(raw, bytes) else raw)
                match = _FRAME.match(line.strip())
                if not match:
                    continue
                try:
                    frame = json.loads(match.group(1))
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue

                self._check_frame(frame)

                body = frame.get("json") or {}
                if body.get("type") != "message":
                    continue
                self._apply(body.get("patches") or [], chunks)

                whole = "".join(chunks[key] for key in sorted(chunks))
                delta = grown.feed(whole)
                if delta:
                    produced = True
                    yield delta

            if not produced:
                raise ProviderError("пустой ответ", self.name)
