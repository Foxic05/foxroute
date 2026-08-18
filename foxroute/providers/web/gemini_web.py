"""Gemini через веб-сессию gemini.google.com.

Единственный из наших адаптеров, который не разбирает протокол сам, а
опирается на пакет ``gemini-webapi``. Причина простая: протокол там уже
разобран и поддерживается, а дублировать чужую работу незачем.

Доступ — две куки через ``|``: ``__Secure-1PSID|__Secure-1PSIDTS``. Вторая
для части аккаунтов необязательна, но с ней стабильнее. Это СКЛЕЙКА, а не
пул (см. ``registry.MULTI_KEY``).

Две тонкости, которые дались опытом.

**Клиент должен жить постоянно.** Библиотека сама обновляет
``__Secure-1PSIDTS`` в фоне, поэтому поднимать её на каждый запрос нельзя —
обновление не успевает случиться. Держим свой вечный цикл событий в фоновом
потоке.

**Кеш кук — это ЧАСТЬ ДОСТУПА, а не ускорение.** Одной ``__Secure-1PSID``
из настроек мало: рабочая ``__Secure-1PSIDTS`` живёт только в кеше, куда её
кладёт библиотека, обновляя в фоне. Пустой каталог кеша означает откат на
устаревшую куку из настроек, то есть анонимный режим.

Разница заметная и обманчивая: с кешем ответ приходит за 12–18 секунд, без
него библиотека тратит около 180 секунд на попытки авторизоваться, пишет в
журнал ``Account status: UNAUTHENTICATED`` — и всё-таки отвечает, анонимно.
Легко принять за протухшую сессию, хотя сессия жива.

Хуже того, неудачный прогон **перезаписывает кеш** своей ущербной версией,
и следующий запуск наследует поломку. Поэтому при переносе доступа каталог
кеша надо переносить вместе с ключом, а не рассчитывать, что он наполнится
сам.

По умолчанию пакет держит кеш во временной папке системы, которая на сервере
вычищается при перезагрузке. Уводим его в каталог данных.

Модель берём Flash намеренно: Pro у бесплатного аккаунта режется примерно до
пяти запросов в день и на объём не годится.

Держит 100 000 символов входа, медиана хода 9.1 секунды — самый медленный
из годных.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import logging
import mimetypes
import os
import re
import threading
from pathlib import Path
from typing import Iterator

from foxroute.errors import (
    AuthError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from foxroute.paths import app_dir
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)

log = logging.getLogger(__name__)

#: Заплата ставится один раз на процесс.
_cache_protected = False


def _protect_cookie_cache() -> None:
    """Запретить библиотеке ЗАТИРАТЬ кеш кук — только дополнять.

    Рабочая ``__Secure-1PSIDTS`` живёт ТОЛЬКО в кеше библиотеки, в настройках
    её нет. А библиотека сохраняет кеш ЗАМЕНОЙ: пишет ровно те куки, что
    сейчас в сессии. Если заход не удался (сеть, отказ, чужая ротация),
    в сессии остаётся огрызок — и он затирает рабочий набор. Дальше каждый
    следующий заход стартует с худшего кеша, чем предыдущий, пока не
    скатится в анонимный режим: ``UNAUTHENTICATED`` и ~180 с на попытки.
    На практике было 6 кук (1269 байт), стало 4 (896) — пропадают
    ``__Secure-1PSIDCC`` и ``__Secure-3PSIDCC``.

    Лечим слиянием: читаем, что уже лежит на диске, накладываем сверху
    новое и пишем объединённое. Потерять куку так нельзя, а протухшие
    записи безвредны — при чтении библиотека их и так пропускает.

    Правим не файлы пакета, а его имена в памяти: переустановка пакета
    заплату не смоет. Имя связано в трёх местах, и подменить надо во всех —
    ``from … import save_cookies`` делает свою копию ссылки.
    """
    global _cache_protected
    if _cache_protected:
        return

    import json
    import sys

    from gemini_webapi import client as gem_client
    from gemini_webapi import utils as gem_utils

    # Внутри пакета имя ``rotate_1psidts`` занято ОДНОИМЁННОЙ ФУНКЦИЕЙ,
    # которую туда втянул __init__, поэтому обычный import отдаёт функцию,
    # а не модуль. Берём модуль из списка загруженных.
    gem_rotate = sys.modules.get("gemini_webapi.utils.rotate_1psidts")
    if gem_rotate is None:  # pragma: no cover — модуль тянет сам пакет
        return

    original = gem_rotate.save_cookies

    def merging_save(cookies, verbose: bool = False) -> None:
        path = gem_rotate._get_cookies_cache_path(cookies, verbose)
        was: list[dict] = []
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text() or "[]")
                if isinstance(loaded, list):
                    was = [c for c in loaded if isinstance(c, dict)
                           and c.get("name")]
            except (OSError, ValueError):
                was = []

        original(cookies, verbose)

        if not was or not path or not path.is_file():
            return
        try:
            now = json.loads(path.read_text() or "[]")
            if not isinstance(now, list):
                return
        except (OSError, ValueError):
            return

        merged = {c["name"]: c for c in was}
        merged.update({c["name"]: c for c in now
                       if isinstance(c, dict) and c.get("name")})
        if len(merged) > len(now):
            lost = sorted(set(merged) - {c.get("name") for c in now})
            log.info("gemini_web: сохранение затирало куки %s — дописал",
                     ", ".join(lost))
            path.write_text(json.dumps(list(merged.values())))
            path.chmod(0o600)

    for module in (gem_rotate, gem_utils, gem_client):
        if getattr(module, "save_cookies", None) is not None:
            module.save_cookies = merging_save
    _cache_protected = True


class GeminiWebProvider(Provider):
    name = "gemini_web"
    # Библиотека отдаёт готовый ответ, потоком не умеет.
    capabilities = Capabilities(text=True, streaming=False,
                                conversations=True, images_out=True,
                                files_in=True, files_out=True, vision=True,
                                thinking=True)

    #: Веб-версия отвечает медленнее API, ждём терпеливее.
    INIT_TIMEOUT = 200

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)

        psid, _, psidts = credential.value.partition("|")
        psid, psidts = psid.strip(), psidts.strip()
        if not psid:
            raise ProviderError(
                "нужна кука __Secure-1PSID (формат: "
                "__Secure-1PSID|__Secure-1PSIDTS)", self.name)

        self.cookie_cache = app_dir() / "cache" / "gemini_web"
        self.cookie_cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("GEMINI_COOKIE_PATH", str(self.cookie_cache))

        # Пустой кеш — не мелочь: без него запрос идёт анонимно и втрое
        # дольше. Предупреждаем сразу, а не оставляем гадать, почему первый
        # ответ шёл три минуты.
        self.cache_ready = any(self.cookie_cache.glob(".cached_cookies_*"))
        if not self.cache_ready:
            log.warning(
                "%s: каталог кеша кук пуст (%s). Рабочая __Secure-1PSIDTS "
                "живёт там, и без неё запрос уйдёт анонимно, потратив ~180 с "
                "на попытки входа. Перенеси кеш вместе с ключом.",
                self.name, self.cookie_cache)

        try:
            from gemini_webapi import GeminiClient
        except ImportError as exc:
            raise ProviderError(
                "нужен пакет gemini-webapi", self.name) from exc

        # curl_cffi под капотом gemini-webapi понимает и http, и socks5 —
        # отдаём строку прокси как есть (пустую превращаем в None).
        from foxroute import settings

        _proxy = settings.current_proxy() or None
        _protect_cookie_cache()
        self._client = GeminiClient(psid, psidts or None, proxy=_proxy)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True,
            name="foxroute-gemini-web")
        self._thread.start()
        self._ready = False
        self._ready_lock = threading.Lock()

    def _await(self, coroutine, timeout: float):
        """Выполнить корутину в своём цикле и дождаться результата."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            # Без явной отмены корутина осталась бы висеть в цикле, держа
            # соединение, а наверх улетел бы сырой таймаут исполнителя.
            future.cancel()
            raise ProviderUnavailable(
                f"ответа нет дольше {timeout:.0f} с", self.name) from exc

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        with self._ready_lock:
            if self._ready:
                return
            # auto_refresh — сама обновляет вторую куку в фоне;
            # auto_close=False — сессию между запросами не роняем.
            self._await(
                self._client.init(timeout=180, auto_close=False,
                                  auto_refresh=True),
                timeout=self.INIT_TIMEOUT)
            self._ready = True

    #: Размышление у Gemini — это ОТДЕЛЬНАЯ МОДЕЛЬ, флага нет: в перечне
    #: библиотеки рядом с ``gemini-3-flash`` стоит
    #: ``gemini-3-flash-thinking``. Кнопка переключает на неё.
    #: Молча соврать переключение не может: незнакомое имя модели
    #: библиотека отдаёт как есть и сервис отвечает отказом.
    THINKING_MODEL = "gemini-3-flash-thinking"

    def _pick_model(self, req: Request) -> str:
        """Модель под задачу: с размышлением или обычная."""
        if req.thinking:
            return self.THINKING_MODEL
        return self.resolve_model(req)

    def _resolve(self, name: str):
        """Имя модели -> enum библиотеки.

        ``generate_content`` принимает и строку, но по значению enum не
        строится (значение — кортеж), поэтому подбираем по ``model_name``.
        Не нашли — отдаём как есть, пусть библиотека разбирается.
        """
        if not name:
            return None
        try:
            from gemini_webapi.constants import Model

            for candidate in Model:
                if getattr(candidate, "model_name", None) == name:
                    return candidate
        except Exception:  # noqa: BLE001 — состав enum меняется между версиями
            pass
        return name

    def _translate(self, exc: Exception) -> Exception:
        kind = type(exc).__name__
        text = str(exc)
        if "AuthError" in kind or "UNAUTHENTICATED" in text.upper():
            return AuthError(f"куки не приняты: {text[:200]}", self.name)
        if "UsageLimitExceeded" in kind or "429" in text:
            return RateLimited(f"норма выбрана: {text[:200]}", self.name)
        # 1096 — их внутренний код, который библиотека честно не понимает.
        # По наблюдению это первый признак исчерпанной нормы: следом те же
        # запросы начинают отвечать прямым 429. Отдельный тип нужен, чтобы
        # маршрутизатор ушёл к другому провайдеру, а не считал нас
        # сломанными.
        if "1096" in text:
            return RateLimited(
                "норма Google на сегодня, похоже, выбрана (их код 1096, "
                f"следом идёт 429): {text[:150]}", self.name)
        if "TemporarilyBlocked" in kind:
            return ProviderUnavailable(
                f"сервис временно закрыл доступ: {text[:200]}", self.name)
        return ProviderError(f"{kind}: {text[:200]}", self.name)

    def _get_chat(self, req: Request):
        """Получить или создать ChatSession для продолжения беседы."""
        conv = req.conversation
        if conv and conv.chat_id:
            chat = self._client.start_chat()
            chat.cid = conv.chat_id
            if conv.last_message_id:
                parts = conv.last_message_id.split("|", 1)
                chat.rid = parts[0]
                if len(parts) > 1:
                    chat.rcid = parts[1]
            return chat
        return self._client.start_chat()

    def _remember(self, req: Request, chat) -> None:
        cid = getattr(chat, "cid", "")
        if not cid:
            return
        rid = getattr(chat, "rid", "") or ""
        rcid = getattr(chat, "rcid", "") or ""
        last_id = f"{rid}|{rcid}" if rcid else rid
        if req.conversation is None:
            req.conversation = Conversation(provider=self.name, chat_id=cid)
        req.conversation.chat_id = cid
        if last_id:
            req.conversation.last_message_id = last_id

    #: Потолок на файл. Такой же, как у прочих: вложение едет к нам в
    #: base64 (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    #: Ссылки на вложенный файл: сервис дописывает «[cite: 1]» после
    #: фразы, взятой из документа. В чате это мусор — человек и так знает,
    #: что файл приложил он сам.
    _CITE = re.compile(r"\s*\[cite[^\]]*\]")

    @classmethod
    def _clean(cls, text: str) -> str:
        return cls._CITE.sub("", text).strip()

    def _files(self, req: Request) -> list:
        """Разложить вложения во временные файлы для библиотеки.

        Библиотека принимает и сырые байты, но тогда теряется ИМЯ файла, а
        вместе с ним расширение — по нему сервис и понимает, чем файл
        является. Безымянные байты он разбирает заметно хуже, поэтому
        кладём на диск с настоящим именем.

        Файлы временные и удаляются сразу после ответа: вложения человека
        не должны оседать на сервере.
        """
        import tempfile

        paths = []
        for item in req.attachments:
            raw = item.data or b""
            if not raw:
                continue
            if len(raw) > self.MAX_UPLOAD:
                raise ProviderError(
                    f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ",
                    self.name)
            name = item.filename or "file.bin"
            folder = tempfile.mkdtemp(prefix="foxroute-")
            path = Path(folder) / name
            path.write_bytes(raw)
            paths.append(path)
        self._temp = paths
        return paths

    def _drop_temp(self) -> None:
        """Убрать временные файлы вместе с их каталогами."""
        import shutil

        for path in getattr(self, "_temp", []):
            shutil.rmtree(path.parent, ignore_errors=True)
        self._temp = []

    # ── что модель собрала сама ───────────────────────────────────────
    #
    # Gemini отдаёт результат не только текстом: нарисованные картинки,
    # снятое видео, озвучку. В ответе это не байты и не ссылки, годные для
    # браузера, а объекты библиотеки — забрать содержимое можно только её
    # же методом ``save``, потому что за адресом стоит их авторизация и
    # (у видео) ожидание готовности с опросом.
    #
    # Отсюда путь через диск: сохраняем во временный каталог, читаем байты,
    # каталог сносим. Прямого способа получить байты библиотека не даёт, а
    # переписывать её загрузку у себя значит повторить и опрос готовности.

    #: Потолок на один собранный файл. Он уезжает человеку прямо в тексте
    #: ответа (base64, +33% к объёму), поэтому меньше, чем у входа.
    MAX_MADE_BYTES = 12 * 1024 * 1024
    #: Сколько таких файлов забираем за один ход.
    MAX_MADE_FILES = 6

    #: Что в имени файла оставляем. Имя уезжает в атрибут ``download``
    #: разметки, поэтому кавычки, скобки и слеши выкидываем — не из
    #: осторожности вообще, а чтобы не разорвать атрибут.
    _NAME_JUNK = re.compile(r"[^\w \-]+", re.U)

    @classmethod
    def _made_name(cls, item, number: int) -> str:
        """Человеческое имя файла из названия, что дал сервис.

        Своё имя библиотека собирает из отметки времени и хеша адреса —
        для диска сойдёт, а человеку в чате показывать нечего.
        """
        title = (getattr(item, "title", "") or "").strip()
        # «[Image]», «[Video]», «[Media]» — заглушки библиотеки, не названия.
        if title.startswith("[") or not title:
            title = ""
        clean = cls._NAME_JUNK.sub(" ", title).strip()
        clean = " ".join(clean.split())[:48]
        return clean or f"gemini-{number}"

    def _made_files(self, result) -> str:
        """Разметка со всем, что модель создала. Пусто — создавать нечего."""
        import shutil
        import tempfile

        pieces = list(getattr(result, "images", None) or [])
        pieces += list(getattr(result, "videos", None) or [])
        pieces += list(getattr(result, "media", None) or [])
        # Картинки из веба сюда не берём: это иллюстрации к ответу, найденные
        # поиском, а не работа модели. Их адреса публичные и живут сами.
        pieces = [item for item in pieces
                  if type(item).__name__ != "WebImage"][:self.MAX_MADE_FILES]
        if not pieces:
            return ""

        folder = tempfile.mkdtemp(prefix="foxroute-out-")
        images, links, trouble = [], [], []
        try:
            for number, item in enumerate(pieces, 1):
                name = self._made_name(item, number)
                try:
                    saved = self._await(item.save(path=folder), timeout=180)
                except Exception as exc:  # noqa: BLE001 — чужая библиотека
                    trouble.append(f"{name}: {type(exc).__name__}")
                    continue
                # Картинка отдаёт путь строкой, видео и звук — словарь путей
                # (само видео, обложка, отдельная звуковая дорожка).
                found = ([Path(v) for v in saved.values() if v]
                         if isinstance(saved, dict)
                         else ([Path(saved)] if saved else []))
                if not found:
                    trouble.append(f"{name}: сервис не отдал содержимое")
                    continue

                for path in found:
                    raw = path.read_bytes() if path.is_file() else b""
                    if not raw:
                        continue
                    label = name + path.suffix
                    if len(raw) > self.MAX_MADE_BYTES:
                        trouble.append(
                            f"{label}: {len(raw) // 1024 // 1024} МБ — больше "
                            f"потолка {self.MAX_MADE_BYTES // 1024 // 1024} МБ")
                        continue
                    kind = (mimetypes.guess_type(path.name)[0]
                            or "application/octet-stream")
                    packed = base64.b64encode(raw).decode()
                    if kind.startswith("image/"):
                        images.append(f"![{label}](data:{kind};base64,{packed})")
                    else:
                        links.append(f"[{label}](data:{kind};base64,{packed})"
                                     f" — {len(raw) // 1024 or 1} КБ")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

        nl = chr(10)
        tail = ""
        if images:
            tail += nl + nl + nl.join(images)
        if links:
            tail += nl + nl + "Файлы: " + " · ".join(links)
        if trouble:
            # Молчать нельзя: в тексте модель обещала картинку, а её нет —
            # без причины это неотличимо от поломки интерфейса.
            tail += nl + nl + "Не удалось забрать: " + "; ".join(trouble)
        return tail

    def _stream(self, req: Request) -> Iterator[str]:
        """Ответ приходит целиком: библиотека потоком его не отдаёт.

        Выдаём одним куском, а не притворяемся, будто стримим. Контракт это
        допускает — он требует, чтобы склейка кусков давала полный ответ,
        а не чтобы кусков было много.
        """
        try:
            self._ensure_ready()
            model = self._resolve(self._pick_model(req))
            chat = self._get_chat(req)
            arguments = {"model": model, "chat": chat} if model else {"chat": chat}
            files = self._files(req)
            if files:
                arguments["files"] = files
            result = self._await(
                self._client.generate_content(req.prompt, **arguments),
                timeout=req.timeout)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — переводим в свой тип
            raise self._translate(exc) from exc
        finally:
            self._drop_temp()

        text = self._clean(getattr(result, "text", "") or "")
        made = self._made_files(result)
        if not text and not made:
            raise ProviderError("пустой ответ", self.name)
        self._remember(req, chat)
        yield text + made

    def _draw(self, req: Request) -> list[str]:
        """Нарисовать через тот же чат: отдельного эндпоинта у веб-версии нет.

        Отдаём байты в ``data:``, а не адрес. Адрес картинки Gemini живёт
        только с нашими куками — в браузере человека он вернёт отказ, и в
        чате будет пустое место.
        """
        try:
            self._ensure_ready()
            model = self._resolve(self._pick_model(req))
            chat = self._client.start_chat()
            arguments = {"model": model, "chat": chat} if model else {"chat": chat}
            result = self._await(
                self._client.generate_content(req.prompt, **arguments),
                timeout=req.timeout)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — переводим в свой тип
            raise self._translate(exc) from exc

        made = self._made_files(result)
        links = re.findall(r"!\[[^\]]*\]\((data:image/[^)]+)\)", made)
        if not links:
            # Отказ приходит обычным текстом, а не ошибкой: «лимит будет
            # сброшен». Без этого наверх ушло бы невнятное «пусто».
            reason = self._clean(getattr(result, "text", "") or "")
            raise ProviderError(
                f"картинки нет: {reason[:200] or 'сервис ничего не отдал'}",
                self.name)
        self.last_caption = self._clean(getattr(result, "text", "") or "")
        return links

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
