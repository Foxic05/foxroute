"""Grok — мобильный gRPC-API grok.com.

Доступ — куки ``sso`` и ``sso-rw`` с ``.grok.com``. В настройках они лежат
либо через ``|``, либо объектом JSON. Разделитель здесь СКЛЕИВАЕТ две части
одного доступа, а не разделяет пул (см. ``registry.MULTI_KEY``).

Разбор кадров и сборка запроса берутся из пакета ``grok3api``. Это осознанно:
там полторы тысячи строк с настоящим знанием протокола — у сервера два
разных формата ответа в зависимости от того, новая беседа или нет, плюс
семантика полутора десятков тегов. Переписывать это значит потерять знание,
а не приобрести чистоту. Своё здесь — обвязка: типизация отказов, поток и
таймауты.

**Медленный.** 18–26 секунд на короткий вопрос. Таймаут поднят до общего:
при шестидесяти секундах он падал под параллельной нагрузкой, хотя был жив.

Отдельно стоит отметить, ЧТО именно здесь чинится типизацией. Отказ по норме
приходит с текстом «rate limit» — через пробел, — и классификатор по
подстроке ``rate_limit`` с подчёркиванием его бы не опознал: пауза не
ставится, и каждый следующий запрос снова уходит в исчерпанного
провайдера. Здесь смысл несёт тип, и разойтись им негде.
"""
from __future__ import annotations

import base64
import json
import re
import struct
from typing import Iterator

from foxroute.errors import (
    AuthError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

ENDPOINT = "https://grok.com/grok_api.Chat/CreateConversationAndRespond"
USER_AGENT = ("grpc-java-okhttp/1.65.1 ai.x.grok/1.1.65-release.00 "
              "(Android; okhttp/4.12.0)")

#: Коды состояния gRPC, которые нам важны.
GRPC_OK = "0"
GRPC_PERMISSION_DENIED = "7"
GRPC_RESOURCE_EXHAUSTED = "8"
GRPC_UNAUTHENTICATED = "16"

#: Тег поля, в котором сервис присылает баннер с отказом.
BANNER_TAG = 49


class GrokProvider(Provider):
    name = "grok"
    #: Размышление НЕ заявлено, и это окончательно: в их интерфейсе это
    #: режим Expert («Thinks hard»), а он платный — на бесплатной учётке
    #: доступен только Fast. Отсюда и ответ протокола: `expert` даёт
    #: «Model is not found», хотя имена `reasoning`/`think`/`thinking`
    #: сервис знает. Разборщик размечает куски `is_thinking`, но взяться
    #: им неоткуда — проводка не нужна.
    capabilities = Capabilities(text=True, web_search=True, images_out=True,
                                files_in=True, vision=True,
                                deep_research=True)

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self.cookies = self._parse_cookies(credential.value)
        # ОБЕ куки обязательны и непусты: «половинный» захват ("sso|" с пустой
        # sso-rw) иначе прошёл бы как валидный доступ и упал бы уже на
        # авторизации — понятнее отвергнуть сразу, на заведении.
        if not self.cookies.get("sso") or not self.cookies.get("sso-rw"):
            raise ProviderError(
                "нужны ОБЕ куки sso и sso-rw с .grok.com — либо через '|', "
                'либо объектом {"sso": "…", "sso-rw": "…"}', self.name)

    @staticmethod
    def _parse_cookies(raw: str) -> dict:
        raw = (raw or "").strip()
        if not raw:
            return {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        if "|" in raw:
            first, second = raw.split("|", 1)
            return {"sso": first.strip(), "sso-rw": second.strip()}
        return {}

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/grpc+proto",
            "Accept": "application/grpc+proto",
            "TE": "trailers",
            "User-Agent": USER_AGENT,
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }

    # ── отказы ────────────────────────────────────────────────────────

    def _check_status(self, response) -> None:
        """Проверить состояние gRPC. Оно приходит заголовком, не кодом HTTP."""
        headers = getattr(response, "headers", None) or {}
        status = str(headers.get("grpc-status", "") or "")
        if not status or status == GRPC_OK:
            return
        message = str(headers.get("grpc-message", "")).replace("%20", " ")

        if status == GRPC_RESOURCE_EXHAUSTED:
            raise RateLimited(f"норма выбрана: {message[:200]}", self.name)
        if status in (GRPC_UNAUTHENTICATED, GRPC_PERMISSION_DENIED):
            raise AuthError(f"куки не приняты: {message[:200]}", self.name)
        raise ProviderError(f"gRPC {status}: {message[:200]}", self.name)

    def _check_banner(self, frame: bytes) -> None:
        """Поискать в кадре баннер с отказом.

        Сервис умеет отказать, не меняя состояния gRPC: сообщение приезжает
        внутри обычного кадра. Текст бывает и строкой, и вложенным
        сообщением — разбираем оба.
        """
        from grok3api.utils.protobuf import pb_parse

        for outer in pb_parse(frame).get(1, []):
            if not isinstance(outer, (bytes, bytearray)):
                continue
            for banner in pb_parse(outer).get(BANNER_TAG, []):
                if not isinstance(banner, (bytes, bytearray)):
                    continue
                text = self._banner_text(banner)
                if not text:
                    continue
                if "rate limit" in text.lower() or "limit" in text.lower():
                    raise RateLimited(text[:200], self.name)
                raise ProviderError(text[:200], self.name)

    @staticmethod
    def _banner_text(banner: bytes) -> str:
        from grok3api.utils.protobuf import pb_parse

        try:
            return banner.decode("utf-8")
        except UnicodeDecodeError:
            pass
        # Не строка — значит вложенное сообщение, текст лежит в поле 1.
        try:
            for value in pb_parse(banner).get(1, []):
                if isinstance(value, (bytes, bytearray)):
                    try:
                        return value.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
        except Exception:  # noqa: BLE001 — разбор баннера не должен ронять ответ
            pass
        return ""

    # ── протокол ──────────────────────────────────────────────────────

    @staticmethod
    def _frames(response) -> Iterator[bytes]:
        """Разрезать поток на кадры gRPC.

        Кадр: байт флага, длина четырьмя байтами big-endian, тело. Кусок из
        сети может оборвать кадр посередине, поэтому копим буфер и отдаём
        только целые.
        """
        buffer = b""
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            buffer += chunk
            while len(buffer) >= 5:
                length = struct.unpack(">I", buffer[1:5])[0]
                if len(buffer) < 5 + length:
                    break
                yield buffer[5:5 + length]
                buffer = buffer[5 + length:]

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Беседа идёт по gRPC, а вот файлы кладутся ОБЫЧНЫМ JSON по адресу
    # ``/rest/app-chat/upload-file``: имя, тип и содержимое в base64.
    # Заголовки для него другие — те, что нужны gRPC (``application/
    # grpc+proto``, ``TE: trailers``), сюда не годятся, поэтому собираем
    # свои, оставив только куки.
    #
    # Идентификатор из ответа кладётся в ``file_attachments`` — и документ,
    # и картинка. Соседнее ``image_attachments`` выглядит подходящим для
    # картинок и молча не работает: тот же PNG через него даёт «нет
    # картинки», а через ``file_attachments`` модель называет цвет.
    # Проверено на одном и том же файле.

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    UPLOAD = "https://grok.com/rest/app-chat/upload-file"

    def _upload(self, session, item) -> str:
        """Положить вложение и вернуть его идентификатор."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        mime = item.mime or "application/octet-stream"
        headers = {k: v for k, v in self._headers().items()
                   if k.lower() not in ("content-type", "accept", "te")}
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        response = _http.request(
            session, "POST", self.UPLOAD, provider=self.name, headers=headers,
            json={"fileName": item.filename or "file.bin",
                  "fileMimeType": mime,
                  "content": base64.b64encode(raw).decode()},
            timeout=300)
        _http.check(self.name, response)
        try:
            saved = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на загрузку файла", self.name) from exc

        file_id = saved.get("fileMetadataId") or ""
        if not file_id:
            raise ProviderError(
                f"сервис не выдал идентификатор файла: {str(saved)[:200]}",
                self.name)
        return file_id

    #: Служебная разметка, которую Grok примешивает прямо в текст ответа:
    #: карточки вызванных инструментов и «плашки» цитат. Человеку это не
    #: ответ, а внутренняя кухня — вырезаем. Незакрытый хвост тоже режем:
    #: в исследовании текст идёт накопленным, и кадр рвётся посреди тега.
    _JUNK = re.compile(
        r"<xai:tool_usage_card>.*?(?:</xai:tool_usage_card>|$)"
        r"|<grok:render\b.*?(?:</grok:render>|$)"
        r"|<xai:tool_usage_card\b[^>]*$",
        re.S)

    #: Строка состояния, с которой начинается исследование. Это не ответ,
    #: а надпись «думаю» — в чат ей не место.
    _STATUS = "Thinking about your request"

    @classmethod
    def _clean(cls, text: str) -> str:
        """Убрать служебную разметку и надписи состояния из ответа."""
        if text.startswith(cls._STATUS):
            text = text[len(cls._STATUS):]
        if "<xai:" not in text and "<grok:" not in text:
            return text
        return cls._JUNK.sub("", text)

    #: Конец карточки инструмента. После ПОСЛЕДНЕЙ такой карточки идёт
    #: собственно ответ, а до неё — надписи о ходе работы и служебное.
    _CARD_END = "</xai:tool_usage_card>"

    @classmethod
    def _deep_answer(cls, whole: str) -> str:
        """Ответ из потока исследования: всё, что после последней карточки."""
        at = whole.rfind(cls._CARD_END)
        tail = whole[at + len(cls._CARD_END):] if at >= 0 else whole
        return cls._clean(tail).strip()

    @staticmethod
    def _plain_tokens(frame: bytes, parse_chunk) -> Iterator[str]:
        """Куски обычного ответа — разбором библиотеки."""
        for piece in parse_chunk(frame):
            if getattr(piece, "is_thinking", False):
                continue
            token = getattr(piece, "token", "")
            if token:
                yield token

    @staticmethod
    def _deep_tokens(frame: bytes) -> Iterator[str]:
        """Куски ответа в режиме исследования.

        Текст лежит в поле 2 вложенного сообщения. Рядом идут поля с
        поисковыми запросами (50) и источниками (6, 8) — они нужны сервису
        для своей ленты, в ответ человеку не годятся.
        """
        from grok3api.utils.protobuf import pb_parse

        try:
            top = pb_parse(frame)
        except Exception:  # noqa: BLE001 — кадр может быть служебным
            return
        for outer in top.get(1, []):
            if not isinstance(outer, (bytes, bytearray)):
                continue
            try:
                inner = pb_parse(outer)
            except Exception:  # noqa: BLE001
                continue
            for value in inner.get(2, []):
                if isinstance(value, (bytes, bytearray)):
                    try:
                        yield value.decode("utf-8")
                    except UnicodeDecodeError:
                        continue

    def _stream(self, req: Request) -> Iterator[str]:
        try:
            from grok3api.types.request import ChatRequest
            from grok3api.utils.protobuf import grpc_frame
            from grok3api.utils.parse_response import parse_chunk
        except ImportError as exc:
            raise ProviderError("нужен пакет grok3api", self.name) from exc

        # Глубокое исследование — это отдельный РЕЖИМ (``mode_id``), а не
        # флаг: Grok делает несколько поисковых запросов подряд и сводит
        # источники. Проверено: 4 запроса, 4 источника, ответ со ссылками.
        deep = req.deep_research
        request = ChatRequest(
            temporary=True,
            message=req.prompt,
            disable_search=False if deep else not req.web_search,
            enable_image_generation=False,
            send_final_metadata=True,
            mode_id="deepsearch" if deep else (self.resolve_model(req)
                                               or "fast"),
        )

        # Отпечаток мобильного Chrome: сервис говорит с приложением Android,
        # и обычный десктопный ему не подходит.
        with _http.session(impersonate="chrome131_android") as session:
            for item in req.attachments:
                request.file_attachments.append(self._upload(session, item))

            response = _http.request(
                session, "POST", ENDPOINT, provider=self.name,
                data=grpc_frame(request.encode()), headers=self._headers(),
                timeout=req.timeout, stream=True)
            _http.check(self.name, response)

            produced = False
            # В исследовании то же поле несёт ПРИРАЩЕНИЯ, и вперемешку с
            # ответом идут надписи о ходе работы («Исследуя последние…») и
            # карточки вызванных инструментов. Порядок всегда один: сперва
            # надписи и карточки, ПОТОМ ответ. Поэтому копим всё, а в конце
            # отрезаем по последней карточке — то, что за ней, и есть
            # ответ. Потоковость тут ничего не теряет: исследование идёт
            # минутами и приходит одним куском.
            collected: list[str] = []
            for frame in self._frames(response):
                self._check_banner(frame)
                # В режиме исследования кадры устроены иначе, и разборщик
                # библиотеки на них падает (`invalid literal for int()`).
                # Поэтому там читаем поле сами, а обычный режим оставляем
                # библиотеке — она знает про два формата ответа.
                pieces = (self._deep_tokens(frame) if deep
                          else self._plain_tokens(frame, parse_chunk))
                for token in pieces:
                    if deep:
                        collected.append(token)
                        continue
                    clean = self._clean(token)
                    if clean:
                        produced = True
                        yield clean

            if deep:
                answer = self._deep_answer("".join(collected))
                if answer:
                    produced = True
                    yield answer

            # Состояние gRPC приезжает трейлером, то есть ПОСЛЕ тела.
            # Поэтому проверяем его здесь, а не до чтения.
            self._check_status(response)

            if not produced:
                raise ProviderUnavailable(
                    "пустой ответ при успешном состоянии gRPC", self.name)

    def _draw(self, req: Request) -> list[str]:
        """Картинки через Grok Imagine (Aurora).

        Тот же gRPC-эндпоинт, но с ``enable_image_generation=True``.
        URL картинки лежит в card JSON внутри tag 13 кадра ответа.
        """
        try:
            from grok3api.types.request import ChatRequest
            from grok3api.utils.protobuf import grpc_frame, pb_parse
        except ImportError as exc:
            raise ProviderError("нужен пакет grok3api", self.name) from exc

        request = ChatRequest(
            temporary=True,
            message=req.prompt,
            disable_search=True,
            enable_image_generation=True,
            enable_image_streaming=True,
            image_generation_count=1,
            send_final_metadata=True,
            mode_id="fast",
        )

        image_urls: list[str] = []
        with _http.session(impersonate="chrome131_android") as session:
            response = _http.request(
                session, "POST", ENDPOINT, provider=self.name,
                data=grpc_frame(request.encode()), headers=self._headers(),
                timeout=req.timeout, stream=True)
            _http.check(self.name, response)

            for frame in self._frames(response):
                self._check_banner(frame)
                self._extract_images(frame, image_urls, pb_parse)

            self._check_status(response)

        if not image_urls:
            raise ProviderError("картинки не получены", self.name)
        return image_urls

    @staticmethod
    def _extract_images(frame: bytes, out: list, pb_parse) -> None:
        """Достать URL картинок из card JSON в tag 13.

        Card может лежать и прямым JSON, и вложенным protobuf с JSON внутри
        tag 1 — разбираем оба. Берём только готовые: progress >= 100 и без
        ошибки.
        """
        ASSETS = "https://assets.grok.com"
        top = pb_parse(frame)
        for outer in top.get(1, []):
            if not isinstance(outer, (bytes, bytearray)):
                continue
            inner = pb_parse(outer)
            for raw in inner.get(13, []):
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                card = None
                try:
                    card = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                if card is None:
                    try:
                        for nested in pb_parse(raw).get(1, []):
                            if isinstance(nested, (bytes, bytearray)):
                                try:
                                    card = json.loads(nested.decode("utf-8"))
                                    break
                                except (UnicodeDecodeError,
                                        json.JSONDecodeError):
                                    pass
                    except Exception:  # noqa: BLE001
                        pass
                if not card:
                    continue
                chunk = card.get("image_chunk") or {}
                url = chunk.get("imageUrl", "")
                if url and chunk.get("progress", 0) >= 100 and not chunk.get(
                        "systemErrCode"):
                    full = url if url.startswith("http") else f"{ASSETS}/{url}"
                    if full not in out:
                        out.append(full)
