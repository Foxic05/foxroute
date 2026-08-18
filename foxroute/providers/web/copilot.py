"""MS Copilot — мобильный API приложения Copilot.

Доступ устроен иначе, чем у прочих: не кука в настройках, а **device code
flow** — тот же вход, что в мобильном приложении. Токены лежат в файле,
``refresh_token`` живёт месяцами, ``access_token`` около часа и обновляется
сам перед запросом.

Это полноценный аккаунт, а не анонимный доступ: в реестре провайдер помечен
``no_key``, но значит это лишь «ключа в настройках нет», а не «работает без
входа». Без токенов сервис тоже отвечает, но лимиты там заметно ниже.

**Ловушка со сроком.** В файл пишется АБСОЛЮТНОЕ время истечения
(``expires_at``), а не ``expires_in``. Если считать срок как «сейчас плюс
час» и не писать ``expires_in`` в файл, любой токен с диска будет считаться
свежим ещё час после запуска — и после перезапуска Copilot почти час
отвечает 401, что выглядит как «опять отвалился».

**И вторая: сроку нельзя верить.** Сервер может считать токен мёртвым, когда
по нашей отметке он ещё жив. На 401 обновляемся принудительно и повторяем
один раз — без этого один рассинхрон выключал бы провайдера надолго.

Норма здесь — ТРОТТЛ, а не бюджет: после отказа сервис оживает сам примерно
через 25 минут, так что пауза действительно лечит.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.paths import app_dir
from foxroute.providers._async import to_sync
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)

#: Приложение, от имени которого выдаются токены.
CLIENT_ID = "14638111-3389-403d-b206-a6a71d9f8f16"
SCOPE = ("140e65af-45d1-4427-bf08-3e7295db6836/ChatAI.ReadWrite "
         "offline_access openid profile")
USER_AGENT = ("CopilotNative/30.0.440505001-prod "
              "(Android 14; Google; Pixel 8 Pro)")

TOKENS_NAME = "copilot_tokens.json"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

#: Запас перед истечением: обновляемся заранее, чтобы токен не протух
#: посреди длинного ответа.
REFRESH_MARGIN = 300


def _aio_proxy(name: str = "ms_copilot"):
    """Прокси для aiohttp → пара ``(connector, proxy)``.

    aiohttp сам socks не умеет: http(s) идёт параметром ``proxy=`` у запроса,
    а socks5 — через ``ProxyConnector`` из пакета ``aiohttp-socks``, который
    ставится на всю сессию. Так Copilot ходит через любой прокси, как и
    curl_cffi-провайдеры. Без прокси — ``(None, None)``.
    """
    from foxroute import settings

    proxy = settings.current_proxy()
    if not proxy:
        return None, None
    if proxy.startswith(("http://", "https://")):
        return None, proxy
    try:
        from aiohttp_socks import ProxyConnector
    except ImportError as exc:
        raise ProviderError(
            "для socks5-прокси нужен пакет aiohttp-socks "
            "(pip install aiohttp-socks) — либо укажи http-прокси", name) from exc
    return ProxyConnector.from_url(proxy), None


class TokenStore:
    """Токены на диске с автообновлением.

    Отдельным классом, потому что это не протокол, а хранилище: его удобно
    подменить и проверить отдельно от сетевой части.
    """

    def __init__(self, path=None):
        self._path = path or (app_dir() / TOKENS_NAME)
        self._lock = threading.Lock()
        self._access = ""
        self._refresh = ""
        self._until = 0.0
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._access = data.get("access_token", "")
        self._refresh = data.get("refresh_token", "")
        # Именно absolute expires_at. Нет отметки — считаем протухшим и
        # обновляем: лучше лишний запрос, чем час ответов 401.
        try:
            self._until = float(data.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            self._until = 0.0

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps({
                "access_token": self._access,
                "refresh_token": self._refresh,
                "expires_at": round(self._until, 3),
                "client_id": CLIENT_ID,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # Не смогли записать — токен всё равно годен в этом процессе.
            pass

    def _refresh_now(self) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": self._refresh,
            "scope": SCOPE,
        }).encode()
        request = urllib.request.Request(
            TOKEN_ENDPOINT, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())

        self._access = data["access_token"]
        # Сервис выдаёт новый refresh взамен старого — теряя его, потеряем
        # вход целиком, а восстанавливается он только повторным device code
        # flow, то есть руками.
        self._refresh = data.get("refresh_token", self._refresh)
        self._until = time.time() + data.get("expires_in", 3600) - REFRESH_MARGIN
        self._save()

    @property
    def has_account(self) -> bool:
        with self._lock:
            self._load()
            return bool(self._refresh)

    def token(self, force: bool = False) -> str:
        """Свежий access-токен. Пусто — аккаунта нет, работаем гостем.

        ``force`` обновляет, не глядя на срок: нужно, когда сервер уже
        ответил 401, то есть наше представление о сроке разошлось с его.
        """
        with self._lock:
            self._load()
            if not self._refresh:
                return ""
            if not force and self._access and time.time() < self._until:
                return self._access
            try:
                self._refresh_now()
            except Exception as exc:  # noqa: BLE001 — сеть, формат, что угодно
                if force:
                    raise AuthError(
                        f"не удалось обновить токен: {exc}", "ms_copilot"
                    ) from exc
                # Не принудительное обновление: пробуем старым, вдруг жив.
                return self._access
            return self._access


class MSCopilotProvider(Provider):
    name = "ms_copilot"
    #: Веб-поиск ЕСТЬ. В интерфейсе Copilot это отдельный режим «Поиск»
    #: (наряду со Smart и Think Deeper). Строка режима подобрана перебором:
    #: рабочая — ``researcher`` (находит факт со ссылками), а ``search``
    #: сервис не понимает. Кнопка переключает в этот режим.
    #: (Сам Smart тоже web-grounded, но researcher даёт расширенные ссылки.)
    #: Глубокое исследование — режим ``research``. Норма МЕСЯЧНАЯ: пять
    #: запусков, дальше отказ ``over-research-quota`` со сроком возврата.
    #: Размышление — «Think Deeper» в их интерфейсе, режим ``reasoning``.
    #: Отдельного потока мыслей он не отдаёт (проверено: ноль кусков
    #: thinking), но режим настоящий — сервис и сам в него переключается,
    #: когда вопрос того требует.
    capabilities = Capabilities(text=True, images_out=True, conversations=True,
                                files_in=True, vision=True, web_search=True,
                                deep_research=True, thinking=True)

    BASE = "https://copilot.microsoft.com"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self._tokens = TokenStore()
        # Аккаунт есть, если на диске лежит refresh-токен. Без него сервис
        # тоже ответит, но лимиты будут заметно ниже.
        self.authorized = self._tokens.has_account

    # ── протокол ──────────────────────────────────────────────────────

    def _headers(self, token: str) -> dict:
        headers = {
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Host": "copilot.microsoft.com",
            "User-Agent": USER_AGENT,
            "X-Search-UILang": "en-US",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Приёмник один — ``/c/api/attachments``, но зовут его двумя разными
    # способами, и подменить один другим нельзя.
    #
    # Картинка уходит СЫРЫМИ БАЙТАМИ, тип — в заголовке; в ответе адрес,
    # который потом кладётся в сообщение как ``url``. Документ уходит
    # обычным multipart; в ответе идентификатор, и в сообщении он зовётся
    # уже ``attachmentId``. Разные поля для разных вещей — сервис по ним и
    # различает, показать модели картинку или дать ей прочитать файл.

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    async def _attach(self, session, headers: dict, item) -> dict:
        """Положить вложение и собрать запись для поля ``content``."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        mime = item.mime or "application/octet-stream"
        picture = item.kind == "image" or mime.startswith("image/")
        url = f"{self.BASE}/c/api/attachments"
        # Заголовок беседы несёт свой Content-Type — для загрузки он чужой.
        base = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        # Только СТРОКА http-прокси: socks у сессии уже висит коннектором из
        # _talk, а звать _aio_proxy тут нельзя — он на каждый вызов создавал
        # бы новый ProxyConnector и бросал его незакрытым (утечка на вложение).
        from foxroute import settings

        _p = settings.current_proxy()
        aio_proxy = _p if _p.startswith(("http://", "https://")) else None
        if picture:
            sent = session.post(url, data=raw, proxy=aio_proxy, headers={
                **base, "Content-Type": mime, "Content-Length": str(len(raw))})
        else:
            import aiohttp

            form = aiohttp.FormData()
            form.add_field("file", raw, filename=item.filename or "file.bin",
                           content_type=mime)
            sent = session.post(url, data=form, proxy=aio_proxy, headers=base)

        async with sent as response:
            body = await response.text()
            if response.status != 200:
                raise ProviderError(
                    f"вложение не принято: HTTP {response.status} "
                    f"{body[:200]}", self.name)
            try:
                saved = json.loads(body) or {}
            except ValueError as exc:
                raise ProviderError(
                    f"не JSON в ответе на загрузку: {body[:200]}",
                    self.name) from exc

        if picture:
            if not saved.get("url"):
                raise ProviderError(
                    f"сервис не выдал адрес картинки: {str(saved)[:200]}",
                    self.name)
            return {"type": "image", "url": saved["url"]}
        if not saved.get("id"):
            raise ProviderError(
                f"сервис не выдал идентификатор файла: {str(saved)[:200]}",
                self.name)
        return {"type": "document", "attachmentId": saved["id"]}

    async def _talk(self, token: str, req: Request,
                    mode: str) -> AsyncIterator[str]:
        """Начать или продолжить беседу и вычитать ответ из сокета."""
        try:
            import aiohttp
        except ImportError as exc:
            raise ProviderError("нужен пакет aiohttp", self.name) from exc

        prompt = req.prompt
        prior = req.conversation.chat_id if req.conversation else ""

        headers = self._headers(token)
        connector, aio_proxy = _aio_proxy(self.name)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"{self.BASE}/c/api/start", headers=headers, proxy=aio_proxy,
                json={
                    "timeZone": "Europe/Moscow",
                    # Продолжаем прежнюю беседу — не начинаем новую.
                    "startNewConversation": not prior,
                    "teenSupportEnabled": True,
                    "correctPersonalizationSetting": True,
                    "deferredDataUseCapable": True,
                },
            ) as response:
                if response.status == 401:
                    raise AuthError("токен отвергнут", self.name)
                if response.status == 429:
                    raise RateLimited("норма выбрана", self.name)
                if response.status != 200:
                    raise ProviderError(
                        f"начало беседы: HTTP {response.status} "
                        f"{(await response.text())[:200]}", self.name)
                conversation = (await response.json()).get(
                    "currentConversationId", "") or prior
            # Запоминаем беседу, чтобы следующий ход пришёл в тот же чат.
            if conversation:
                if req.conversation is None:
                    req.conversation = Conversation(provider=self.name,
                                                    chat_id=conversation)
                req.conversation.chat_id = conversation

            # Вложения грузятся ПОСЛЕ начала беседы: до неё сервис отвечает
            # отказом, привязки к чату ещё нет.
            attached = [await self._attach(session, headers, item)
                        for item in req.attachments]

            url = (f"wss://copilot.microsoft.com/c/api/chat"
                   f"?api-version=2&clientSessionId={uuid.uuid4()}")
            if token:
                url += f"&accessToken={urllib.parse.quote(token)}"

            async with session.ws_connect(url, headers=headers,
                                          proxy=aio_proxy) as socket:
                # Сначала дожидаемся подтверждения соединения: отправка до
                # него теряется молча.
                async for message in socket:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    if json.loads(message.data).get("event") == "connected":
                        break

                letter = {
                    "event": "send",
                    "content": [*attached,
                                {"type": "text", "text": prompt}],
                    "conversationId": conversation,
                    "mode": mode,
                }
                await socket.send_json(letter)

                # Исследование живёт по своим правилам, см. _research_report.
                research = mode == "research"
                # Пересылать в выбранном сервисом режиме — не больше раза.
                resent = False
                written = 0
                # messageId сообщения, которое СЕЙЧАС слушаем. При смене
                # режима (modeSelected → пересылка письма) сервер запускает
                # НОВОЕ сообщение с новым id, а старый генератор ещё какое-то
                # время шлёт свои appendText. Без фильтра по активному id два
                # потока текста мешались в кашу вида «КоротКоротко»,
                # «структурированнаяные» (наблюдалось на новостных дайджестах,
                # где Copilot переключается в Think Deeper).
                active_msg = ""

                async for message in socket:
                    if message.type == aiohttp.WSMsgType.ERROR:
                        raise ProviderError(
                            f"сокет: {socket.exception()}", self.name)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    frame = json.loads(message.data)
                    event = frame.get("event")

                    # Начало сообщения задаёт активный id. Новое (после
                    # пересылки) смещает старое: его куски дальше игнорируем.
                    if event in ("startMessage", "startResponse"):
                        mid = frame.get("messageId")
                        if mid and mid != active_msg:
                            active_msg = mid
                            written = 0
                        continue

                    # Кадр от старого, уже смещённого сообщения.
                    mid = frame.get("messageId")
                    stale = bool(active_msg and mid and mid != active_msg)

                    if event == "modeSelected":
                        # Сервис сам решает, что вопрос требует другого
                        # режима, и говорит об этом кадром ``modeSelected``.
                        # ОТВЕТА ПОСЛЕ ЭТОГО НЕ БУДЕТ: после него идут минуты
                        # полной тишины, сокет жив, текста нет. Он ждёт, что
                        # письмо придёт заново уже в выбранном режиме, и тогда
                        # отвечает за те же пять секунд. Без этого любой
                        # вопрос, который Copilot счёл достойным «Think
                        # Deeper», отдавал бы пустоту.
                        chosen = str(frame.get("mode") or "")
                        if chosen and chosen != letter["mode"] and not resent:
                            resent = True
                            letter["mode"] = chosen
                            await socket.send_json(letter)
                        continue
                    if event == "appendText":
                        # В исследовании этим потоком идёт лишь расписка
                        # «я начал, зайдите позже» — она не ответ. Куски
                        # старого сообщения (stale) — тоже мимо.
                        if research or stale:
                            continue
                        piece = frame.get("text", "")
                        if piece:
                            written += len(piece)
                            yield piece
                    elif event == "done":
                        # ``done`` закрывает СООБЩЕНИЕ, а не исследование:
                        # в режиме research оно приходит через три секунды
                        # после расписки, а работа идёт ещё минуты.
                        if research:
                            continue
                        # ``done`` СТАРОГО сообщения не завершает новый ответ.
                        if stale:
                            continue
                        # После пересылки первое, брошенное письмо тоже
                        # закрывается своим ``done`` — и пустым. Выйти на
                        # нём значило бы не дождаться настоящего ответа.
                        if resent and not written:
                            continue
                        return
                    elif event == "taskUpdate" and research:
                        state = self._task_state(frame)
                        if state in ("failed", "cancelled"):
                            raise ProviderError(
                                f"исследование прервано: {state}", self.name)
                        if state == "completed":
                            yield await self._research_report(
                                session, headers, conversation)
                            return
                    elif event == "error":
                        self._raise_frame_error(frame)

                if research:
                    raise ProviderError(
                        "сокет закрылся до конца исследования", self.name)

    @staticmethod
    def _until(detail: dict) -> float | None:
        """Сколько секунд ждать по отметке ``nextAvailableAt``."""
        stamp = str(detail.get("nextAvailableAt") or "")
        if not stamp:
            return None
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        left = (when - datetime.now(timezone.utc)).total_seconds()
        return left if left > 0 else None

    @staticmethod
    def _task_state(frame: dict) -> str:
        """Достать новое состояние задачи из кадра ``taskUpdate``.

        Обновления приходят набором правок в духе JSON Patch: у каждой свои
        ``op``, ``path`` и ``value``. Нас занимает единственный путь —
        ``/status``: он проходит ``pending`` → ``running`` → ``completed``.
        """
        for change in frame.get("update") or ():
            if isinstance(change, dict) and change.get("path") == "/status":
                return str(change.get("value") or "")
        return ""

    async def _research_report(self, session, headers: dict,
                               conversation: str) -> str:
        """Забрать готовый отчёт исследования из истории беседы.

        Сокет отдаёт только ход работы — запросы к поисковику, размышления и
        ссылки; сам отчёт по нему НЕ ПРИХОДИТ (проверено: после ``completed``
        три минуты полной тишины). Расписка не врёт — «готовый отчёт
        сохранится в этом чате»: текст лежит в истории, в поле
        ``finalResponse`` задачи. Ссылки хранятся отдельно, списком
        ``inlineCitations``, и в самом тексте их нет — поэтому подшиваем их
        в конец, иначе от исследования остаётся пересказ без источников.
        """
        url = f"{self.BASE}/c/api/conversations/{conversation}/history"
        # Отчёт кладут в хранилище чуть позже отметки о готовности.
        for attempt in range(6):
            if attempt:
                await asyncio.sleep(3.0)
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    continue
                history = await response.json()
            for message in history.get("results") or ():
                for part in message.get("content") or ():
                    task = part.get("task") if isinstance(part, dict) else None
                    if not isinstance(task, dict):
                        continue
                    report = str(task.get("finalResponse") or "").strip()
                    if report:
                        return report + self._sources(task)
        raise ProviderError("исследование готово, но отчёта нет в истории",
                            self.name)

    @staticmethod
    def _sources(task: dict) -> str:
        """Сложить список источников исследования."""
        seen: set[str] = set()
        lines: list[str] = []
        for cite in task.get("inlineCitations") or ():
            if not isinstance(cite, dict):
                continue
            link = str(cite.get("url") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            title = str(cite.get("title") or cite.get("publisher") or link)
            lines.append(f"{len(lines) + 1}. [{title}]({link})")
        if not lines:
            return ""
        return "\n\n## Источники\n\n" + "\n".join(lines)

    def _raise_frame_error(self, frame: dict) -> None:
        """Перевести кадр отказа в типизированную ошибку."""
        code = str(frame.get("errorCode") or "")
        text = str(frame.get("text") or frame)
        detail = frame.get("errorDetail") or {}
        if "quota" in code:
            # Норма на ИССЛЕДОВАНИЯ считается отдельно от нормы на вопросы и
            # она МЕСЯЧНАЯ: пять запусков, дальше отказ со сроком
            # ``nextAvailableAt``. Срок называют явно, значит это бюджет,
            # а не троттл: пауза его не создаст, надо уходить к другому
            # провайдеру.
            raise RateLimited(
                f"норма выбрана: {code}"
                + (f", до {detail['nextAvailableAt']}"
                   if detail.get("nextAvailableAt") else ""),
                self.name, retry_after=self._until(detail),
                kind=RateLimited.BUDGET)
        if "too-many-messages" in code or "throttl" in code.lower():
            # Это ТРОТТЛ, а не исчерпанный бюджет — провайдер оживает сам
            # примерно через 25 минут. Срок не указываем, и тогда
            # классификация по умолчанию отнесёт отказ к коротким.
            raise RateLimited(f"слишком часто: {code}", self.name)
        if "unauthor" in code.lower() or "401" in text:
            raise AuthError(f"доступ не принят: {code or text[:120]}", self.name)
        raise ProviderError(f"сервис вернул ошибку: {text[:200]}", self.name)

    #: Режимы, которые сервис ПРИНИМАЕТ. Старые стили Bing (balanced/
    #: creative/precise) Microsoft убрал — на них приходит invalid-event за
    #: 0 секунд. reasoning/researcher/research обычно включаются флагами, но
    #: принять их и по имени модели не вредно; всё незнакомое → smart.
    MODEL_MODES = ("smart", "reasoning", "researcher", "research")

    def _model_mode(self, req: Request) -> str:
        """Режим по имени модели, с защитой от мёртвых стилей Bing."""
        wanted = (self.resolve_model(req) or "smart").lower()
        return wanted if wanted in self.MODEL_MODES else "smart"

    def _stream(self, req: Request) -> Iterator[str]:
        # Режим = функция. Поиск («Поиск» в их UI) — строка ``researcher``,
        # подобрана перебором и проверена свежим фактом. Think Deeper —
        # ``reasoning`` (дольше думает, но отдельного потока мыслей нет,
        # поэтому в контракте thinking мы его не заявляем). Иначе Smart.
        if req.deep_research:
            mode = "research"
        elif req.web_search:
            mode = "researcher"
        elif req.thinking:
            mode = "reasoning"
        else:
            mode = self._model_mode(req)
        token = self._tokens.token()

        produced = False
        try:
            for piece in to_sync(lambda: self._talk(token, req, mode),
                                 timeout=req.timeout):
                produced = True
                yield piece
        except AuthError:
            # Сервер считает токен мёртвым, хотя по нашей отметке он жив.
            # Отметке верить нельзя: обновляемся принудительно и пробуем
            # ещё раз. Повтор безопасен — до сюда мы ничего не отдали.
            if produced or not token:
                raise
            fresh = self._tokens.token(force=True)
            if not fresh or fresh == token:
                raise
            for piece in to_sync(lambda: self._talk(fresh, req, mode),
                                 timeout=req.timeout):
                produced = True
                yield piece

        if not produced:
            raise ProviderError("пустой ответ", self.name)

    def _draw(self, req: Request) -> list[str]:
        """Картинки через Copilot (DALL-E / Aurora).

        Тот же поток, что и для текста, но собираем события ``imageGenerated``
        вместо ``appendText``. При исчерпании нормы Copilot не отдаёт ошибку:
        он вежливо пишет об этом текстом — без разбора выглядит как поломка.
        """
        mode = self._model_mode(req)
        token = self._tokens.token()

        async def collect(tok: str) -> tuple[list[str], str]:
            try:
                import aiohttp
            except ImportError as exc:
                raise ProviderError("нужен пакет aiohttp", self.name) from exc

            headers = self._headers(tok)
            connector, aio_proxy = _aio_proxy(self.name)
            images: list[str] = []
            said = ""

            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    f"{self.BASE}/c/api/start", headers=headers,
                    proxy=aio_proxy,
                    json={
                        "timeZone": "Europe/Moscow",
                        "startNewConversation": True,
                        "teenSupportEnabled": True,
                        "correctPersonalizationSetting": True,
                        "deferredDataUseCapable": True,
                    },
                ) as response:
                    if response.status == 401:
                        raise AuthError("токен отвергнут", self.name)
                    if response.status != 200:
                        raise ProviderError(
                            f"начало беседы: HTTP {response.status}",
                            self.name)
                    conversation = (await response.json()).get(
                        "currentConversationId", "")

                url = (f"wss://copilot.microsoft.com/c/api/chat"
                       f"?api-version=2&clientSessionId={uuid.uuid4()}")
                if tok:
                    url += f"&accessToken={urllib.parse.quote(tok)}"

                async with session.ws_connect(url, headers=headers,
                                          proxy=aio_proxy) as socket:
                    async for message in socket:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        if json.loads(message.data).get("event") == "connected":
                            break

                    await socket.send_json({
                        "event": "send",
                        "content": [{"type": "text", "text": req.prompt}],
                        "conversationId": conversation,
                        "mode": mode,
                    })

                    async for message in socket:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        frame = json.loads(message.data)
                        event = frame.get("event")
                        if event == "imageGenerated":
                            img_url = frame.get("url", "")
                            if img_url:
                                images.append(img_url)
                        elif event == "appendText":
                            piece = frame.get("text", "")
                            if piece:
                                said += piece
                        elif event == "done":
                            break
                        elif event == "error":
                            self._raise_frame_error(frame)
            return images, said

        import asyncio

        def run(tok):
            try:
                asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    return pool.submit(asyncio.run, collect(tok)).result(
                        timeout=req.timeout)
            except RuntimeError:
                return asyncio.run(collect(tok))

        try:
            images, said = run(token)
        except AuthError:
            if not token:
                raise
            fresh = self._tokens.token(force=True)
            if not fresh or fresh == token:
                raise
            images, said = run(fresh)

        if images:
            return images
        # Лимит приходит текстом, не ошибкой.
        lowered = (said or "").lower()
        if any(word in lowered for word in
               ("лимит", "limit", "quota", "exceeded")):
            raise RateLimited(
                f"норма картинок выбрана: {said.strip()[:100]}", self.name)
        raise ProviderError("картинка не сгенерирована", self.name)
