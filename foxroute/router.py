"""Маршрутизатор: кому отдать запрос и что делать, когда тот упал.

Без него слой работает ровно до первой протухшей куки: ``open_provider``
берёт учётку и, если провайдер отказал, запрос умирает. Здесь появляется
цепочка.

Четыре решения, каждое из наблюдения, а не из общих соображений.

**Диспетчер, а не перебор списка.** Спрашиваем СВОБОДНОГО из подходящих, а
не первого по порядку. При параллельной работе иначе все запросы встают в
очередь к лучшему провайдеру, пока остальные простаивают: у веб-сессии
``slots = 1``, это один разговор в браузере, и второй запрос в него не
ускоряется, а ждёт.

**Порядок — по измерениям, а не по репутации.** Эмпирический рейтинг
расходится с ожидаемым — провайдер, слывущий слабым, на деле обходит
именитого, — поэтому порядок берётся из ``measurements.json``, который
обновляется замерами.

**Фолбэк только ДО первого куска.** В потоковом режиме, отдав клиенту
начало ответа, переключиться на другого провайдера уже нельзя — получится
склейка из двух разных ответов. Поэтому: упал до первого куска — идём
дальше по цепочке, упал после — это ошибка.

**Отказ классифицируется типом.** ``AuthError`` выключает учётку (незачем
ходить в неё снова), ``RateLimited`` ставит паузу нужного вида, остальное —
просто следующий кандидат.
"""
from __future__ import annotations

import threading
import logging
import time
from dataclasses import dataclass, field
from typing import Iterator

from foxroute.accounts import Accounts, default as default_store, open_provider
from foxroute.errors import (
    AuthError,
    ContextTooLarge,
    ProviderError,
    RateLimited,
    Unsupported,
)
from foxroute.measurements import AGENTIC_GOOD, AGENTIC_SHORT, get as measured
from foxroute.providers import capabilities_of, implemented
from foxroute.providers.base import Provider, Request
from foxroute.quota import QuotaTracker, default as default_quota
from foxroute.registry import (
    AUTH_FILE, AUTH_NONE, AUTH_OPTIONAL, auth_kind, config, is_api)


log = logging.getLogger(__name__)

@dataclass
class Need:
    """Чего требует запрос. По умолчанию — просто текст.

    Требования проверяются ДО обращения к сети: провайдер, который заведомо
    не умеет нужного, не должен съедать попытку и время.
    """

    text: bool = True
    images: bool = False
    web_search: bool = False
    #: Остальные умения. Проверяются ДО обращения к сети: иначе просьбу
    #: «исследуй» маршрутизатор отдаёт тому, кто исследовать не умеет, —
    #: отказ приходит уже ПОСЛЕ обращения к сервису, то есть после траты
    #: сообщения из нормы.
    thinking: bool = False
    deep_research: bool = False
    vision: bool = False
    files_in: bool = False
    #: Нужен ли провайдер, годный для агентного цикла. Годны не все:
    #: одни зацикливаются, другие не подставляют аргументы.
    agentic: bool = False
    #: Сколько символов должен вместить вход. 0 — не важно.
    context_chars: int = 0
    #: Пускать ли платных. По умолчанию нет: у них конечный кошелёк.
    allow_paid: bool = False
    #: Кого не брать — для «ответить заново другим»: исключаем того, кто
    #: только что ответил, чтобы маршрут дал другого.
    exclude: frozenset[str] = frozenset()


@dataclass
class Attempt:
    """След одной попытки — для отчёта, когда всё упало."""

    provider: str
    account: str
    error: str
    seconds: float


class Busy:
    """Кто сейчас занят.

    У веб-сессии один разговор за раз (``slots = 1``), у API их несколько.
    Без учёта занятости диспетчер вырождается в перебор списка: все запросы
    уходят к первому, он выстраивает их в очередь внутри сервиса, а
    остальные провайдеры простаивают.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str], int] = {}

    def take(self, provider: str, account: str, slots: int) -> bool:
        key = (provider, account)
        with self._lock:
            if self._active.get(key, 0) >= slots:
                return False
            self._active[key] = self._active.get(key, 0) + 1
            return True

    def release(self, provider: str, account: str) -> None:
        key = (provider, account)
        with self._lock:
            left = self._active.get(key, 0) - 1
            if left > 0:
                self._active[key] = left
            else:
                self._active.pop(key, None)

    def count(self, provider: str, account: str) -> int:
        with self._lock:
            return self._active.get((provider, account), 0)


def _chat_models(models: dict) -> list[dict]:
    """Модели для выбора человеком — только те, что ведут беседу.

    Отсеиваются распознавание речи, озвучка и прочее не-чатовое. Отбор по
    подстроке «whisper» в имени ненадёжен — озвучка Orpheus так
    проскакивает; признак берём из реестра, а не из имени.

    Норма едет вместе с именем: у Groq она у каждой модели своя и
    различается в четырнадцать раз — выбирать вслепую бессмысленно.
    """
    out = []
    for name, facts in sorted(models.items()):
        facts = facts if isinstance(facts, dict) else {}
        if not facts.get("chat", True) or facts.get("audio") or facts.get("tts"):
            continue
        out.append({
            "id": name,
            "req_day": facts.get("req_day") or 0,
            "ctx": facts.get("ctx") or 0,
            "vision": bool(facts.get("vision")),
        })
    return out


class Router:
    """Выбор провайдера и выполнение с фолбэком."""

    def __init__(self, store: Accounts | None = None,
                 quota: QuotaTracker | None = None):
        self.store = store or default_store
        self.quota = quota or default_quota
        self.busy = Busy()

    # ── отбор кандидатов ──────────────────────────────────────────────

    def _suits(self, name: str, need: Need) -> bool:
        """Подходит ли провайдер под требования — по реестру и измерениям."""
        settings = config(name)
        if not settings:
            return False
        if settings.get("paid") and not need.allow_paid:
            return False

        # Умения спрашиваем у самого адаптера: список в стороннем модуле
        # разошёлся бы с ними при первой правке.
        can = capabilities_of(name)
        if need.text and not can.text:
            return False
        if need.images and not can.images_out:
            return False
        if need.web_search and not can.web_search:
            return False
        if need.thinking and not can.thinking:
            return False
        if need.deep_research and not can.deep_research:
            return False
        # Картинку принимают и «зрячие», и те, кто берёт её как файл.
        if need.vision and not (can.vision or can.files_in):
            return False
        if need.files_in and not (can.files_in or can.vision):
            return False

        stats = measured(name)
        if need.agentic and stats.agentic not in (AGENTIC_GOOD, AGENTIC_SHORT):
            return False
        if need.context_chars and stats.context_chars < need.context_chars:
            return False
        return True

    def _rank(self, name: str, need: Need) -> tuple:
        """Чем меньше, тем раньше в очереди.

        Сначала пригодность к циклу (если просили), затем скорость хода,
        затем предпочтение веб-сессиям: API-ключ конечен, а веб-сессия
        живёт на бесплатном аккаунте и масштабируется их количеством.
        """
        stats = measured(name)
        agentic_rank = {AGENTIC_GOOD: 0, AGENTIC_SHORT: 1}.get(
            stats.agentic, 2)
        return (
            agentic_rank if need.agentic else 0,
            0 if not is_api(name) else 1,
            stats.median_turn_sec,
            name,
        )

    def candidates(self, need: Need | None = None) -> list[str]:
        """Подходящие провайдеры в порядке предпочтения."""
        need = need or Need()
        names = [n for n in implemented()
                 if n not in need.exclude and self._suits(n, need)]
        return sorted(names, key=lambda n: self._rank(n, need))

    def _usable_without_record(self, name: str) -> bool:
        """Работает ли провайдер без записи в хранилище учёток.

        Да — у анонимных (``none``), у необязательного входа (``optional``,
        отвечает и без ключа, пусть урезанно) и у файловых (Copilot, Meta
        AI), КОГДА файлы токенов на месте. У ключевых/куковых без записи —
        нет: без доступа они не «живые», и показывать их таковыми — обман.
        """
        kind = auth_kind(name)
        if kind in (AUTH_NONE, AUTH_OPTIONAL):
            return True
        if kind == AUTH_FILE:
            return not self.store.missing_parts(name)
        return False

    def _free_account(self, name: str, model: str = "") -> str | None:
        """Свободная учётка провайдера с наибольшим остатком.

        Возвращает None, если все заняты, на паузе или выбрали норму.

        ``model`` обязателен там, где нормы считаются ПО МОДЕЛЯМ: у Groq
        одна модель даёт 14 400 запросов в сутки, другая — тысячу, и
        нормы не пересекаются. Без модели весь учёт схлопывался в один
        ключ с пустым именем, и раздельные квоты не работали вовсе.
        """
        pool = [a.account for a in self.store.usable(name)]
        if not pool:
            # Условная учётка «default» — для тех, кто авторизуется иначе
            # (Z.ai — вообще без входа, Copilot/Meta AI — файлом токена).
            # Подставлять её ВСЕМ с пустым хранилищем нельзя: провайдер,
            # требующий вход, показывался бы «живым» без единой куки. Только
            # тем, кто реально работает без записи.
            #
            # Если записи ЕСТЬ, но все выключены (протухла кука), «default»
            # тоже нельзя: иначе провайдер навсегда остаётся в цепочке и
            # сжигает попытку фолбэка, снова получая тот же отказ.
            if self.store.all(name) or not self._usable_without_record(name):
                return None
            pool = ["default"]

        slots = 1 if not is_api(name) else 4
        ready = [a for a in pool
                 if self.quota.is_available(name, a, model)
                 and self.busy.count(name, a) < slots]
        if not ready:
            return None
        return self.quota.best_account(name, ready, model) or ready[0]

    # ── выполнение ────────────────────────────────────────────────────

    def _open(self, name: str, account: str, model: str) -> Provider:
        return open_provider(name, account if account != "default" else "",
                             model=model, store=self.store,
                             allow_anonymous=True)

    def _on_failure(self, name: str, account: str,
                    error: ProviderError, model: str = "") -> None:
        """Разобраться с отказом: пауза, выключение или ничего."""
        if isinstance(error, RateLimited):
            self.quota.handle_rate_limited(
                name, account, error.retry_after,
                error.is_budget, str(error)[:120], model)
        elif isinstance(error, AuthError):
            # Сначала пробуем ЗАПАСНОЙ доступ, если сервис его выдавал.
            # Kimi, например, присылает новый refresh_token в каждом
            # ответе; он хранится в поле rotated. Пустить его в дело нужно
            # именно здесь: при сборке провайдера с allow_anonymous=True
            # AuthError не поднимается, и отказ приходит позже, во время
            # потока.
            if account != "default" and self._try_rotation(name, account):
                return
            # Доступ отвергнут — ходить сюда снова незачем, пока человек
            # не обновит куку. Выключаем учётку, а не ставим паузу.
            if account != "default":
                try:
                    self.store.set_enabled(
                        name, account, False,
                        f"доступ отвергнут: {str(error)[:80]}")
                except Exception as exc:  # noqa: BLE001
                    # Запись в хранилище — побочное дело: если файл занят
                    # или блокировка не далась, это не повод обрывать
                    # перебор провайдеров. Иначе TimeoutError отсюда
                    # улетел бы наружу и похоронил весь фолбэк.
                    log.warning("%s: не удалось выключить учётку %s: %s",
                                name, account, exc)

    def _try_rotation(self, name: str, account: str) -> bool:
        """Пустить в дело запасной доступ. True — получилось."""
        try:
            entry = self.store.get(name, account)
            if entry is None or not entry.rotated:
                return False
            self.store.promote_rotation(name, account)
        except Exception as exc:  # noqa: BLE001 — хранилище не должно ронять
            log.warning("%s: не удалось повысить запасной доступ: %s",
                        name, exc)
            return False
        log.info("%s/%s: исходный доступ отвергнут, перешли на запасной",
                 name, account)
        return True

    def _exhausted(self, attempts: list) -> None:
        """Цепочка не дала ответа — поднять внятную ошибку.

        Различаем два случая: кого-то ПРОБОВАЛИ и получили отказ (тогда
        перечисляем, кого и почему) — и никого не пробовали вовсе, потому
        что подходящие есть, но не залогинены или на паузе. Без разделения
        оба сваливались бы в «все кандидаты отказали: » с пустым хвостом.
        """
        if attempts:
            raise ProviderError(
                "все кандидаты отказали: "
                + "; ".join(f"{a.provider}/{a.account} — {a.error}"
                            for a in attempts), "router")
        raise Unsupported(
            "нет доступного провайдера для такого запроса: подходящие есть, "
            "но не залогинены или на паузе. Заведи доступ во вкладке "
            "«Настройки» или измени режим (убери исследование/поиск/картинку).",
            "router")

    def stream(self, request: Request, need: Need | None = None,
               model: str = "", limit: int = 4,
               on_pick=None) -> Iterator[str]:
        """Выполнить с фолбэком. Поток приращений.

        ``limit`` — сколько провайдеров пробуем, прежде чем сдаться. Не
        бесконечно: каждая попытка к веб-сессии стоит сообщения из квоты,
        и перебирать двадцать штук ради одного ответа расточительно.

        ``on_pick(провайдер, учётка)`` вызывается, когда выбор сделан и
        первый кусок пошёл. Нужен вызывающему, чтобы показать человеку, кто
        именно ответил: при фолбэке это уже не тот, кого просили.
        """
        need = need or Need()
        chain = self.candidates(need)
        if not chain:
            raise Unsupported(
                "нет провайдера под такие требования: "
                f"агентный={need.agentic}, поиск={need.web_search}, "
                f"контекст={need.context_chars}", "router")

        attempts: list[Attempt] = []
        tried = 0

        for name in chain:
            if tried >= limit:
                break
            account = self._free_account(name, model)
            if account is None:
                continue
            tried += 1

            slots = 1 if not is_api(name) else 4
            if not self.busy.take(name, account, slots):
                continue

            started = time.time()
            produced = False
            provider = None
            try:
                provider = self._open(name, account, model)
                for piece in provider.stream(request):
                    if not produced and on_pick:
                        # Сообщаем на первом же куске: раньше нельзя —
                        # провайдер ещё может отказать, и мы уйдём к другому.
                        on_pick(name, account)
                    produced = True
                    yield piece
            except ProviderError as exc:
                self._on_failure(name, account, exc, model)
                attempts.append(Attempt(name, account, f"{type(exc).__name__}: "
                                        f"{str(exc)[:100]}",
                                        round(time.time() - started, 1)))
                if produced:
                    # Часть ответа уже у клиента. Переключиться нельзя:
                    # получится склейка из двух разных ответов.
                    raise
                continue
            except Exception as exc:  # noqa: BLE001 — чужая библиотека
                attempts.append(Attempt(name, account,
                                        f"{type(exc).__name__}: {str(exc)[:100]}",
                                        round(time.time() - started, 1)))
                if produced:
                    raise
                continue
            finally:
                # Закрываем провайдер на КАЖДОМ выходе, как это делает
                # прямой путь в server.py. Без этого веб-сессия (Gemini,
                # curl_cffi) оставляла поток и открытое соединение на
                # каждом auto-запросе — течёт незаметно и накапливается.
                if provider is not None:
                    try:
                        provider.close()
                    except Exception:  # noqa: BLE001 — уборка не роняет ответ
                        pass
                self.busy.release(name, account)

            self.quota.record(name, account, model)
            return

        self._exhausted(attempts)

    def stream_rich(self, request: Request, need: Need | None = None,
                    model: str = "", limit: int = 4,
                    on_pick=None) -> Iterator[tuple[str, str]]:
        """Поток пар (тип, текст) с фолбэком. Для thinking."""
        need = need or Need()
        chain = self.candidates(need)
        if not chain:
            raise Unsupported("нет провайдера", "router")

        attempts: list[Attempt] = []
        tried = 0

        for name in chain:
            if tried >= limit:
                break
            account = self._free_account(name, model)
            if account is None:
                continue
            tried += 1

            slots = 1 if not is_api(name) else 4
            if not self.busy.take(name, account, slots):
                continue

            started = time.time()
            produced = False
            provider = None
            try:
                provider = self._open(name, account, model)
                for pair in provider.stream_rich(request):
                    if not produced and on_pick:
                        on_pick(name, account)
                    produced = True
                    yield pair
            except ProviderError as exc:
                self._on_failure(name, account, exc, model)
                attempts.append(Attempt(name, account,
                                        f"{type(exc).__name__}: "
                                        f"{str(exc)[:100]}",
                                        round(time.time() - started, 1)))
                if produced:
                    raise
                continue
            except Exception as exc:  # noqa: BLE001
                attempts.append(Attempt(name, account,
                                        f"{type(exc).__name__}: {str(exc)[:100]}",
                                        round(time.time() - started, 1)))
                if produced:
                    raise
                continue
            finally:
                # Та же уборка, что и в stream(): без close веб-сессия
                # течёт потоком и соединением на каждом auto-запросе.
                if provider is not None:
                    try:
                        provider.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.busy.release(name, account)

            self.quota.record(name, account, model)
            return

        self._exhausted(attempts)

    def complete(self, request: Request, need: Need | None = None,
                 model: str = "", limit: int = 4, on_pick=None) -> str:
        """Целый ответ с фолбэком.

        ``on_pick`` пробрасывается в ``stream``: вызывающему нужно знать, кто
        в итоге ответил, чтобы вернуть его имя в поле ``model`` (при не-стрим
        ответе на ``auto`` иначе там осталось бы «auto»).
        """
        return "".join(
            self.stream(request, need, model, limit, on_pick)).strip()

    # ── отчёт ─────────────────────────────────────────────────────────

    def status(self, need: Need | None = None) -> list[dict]:
        """Кто сейчас на что годен — для CLI и панели состояния.

        Платных показываем наравне с прочими. Отбор кандидатов их
        пропускает намеренно (кошелёк конечный, «Авто» не вправе его
        тратить), но панель на то и панель, чтобы ничего не прятать:
        иначе провайдер с живым ключом просто исчезает из виду, и
        непонятно, куда он делся.
        """
        need = need or Need(allow_paid=True)
        out = []
        for name in self.candidates(need):
            stats = measured(name)
            # Учётки для показа: реальные записи, а «default» — лишь тем, кто
            # честно работает без записи (иначе «1 учётка» рисовалась бы у
            # незалогиненного провайдера). Та же логика, что в _free_account.
            pool = [a.account for a in self.store.usable(name)]
            if not pool and not self.store.all(name) \
                    and self._usable_without_record(name):
                pool = ["default"]
            free = self._free_account(name)
            paused = []
            for account in pool:
                # Здесь модели нет и быть не может: сводка общая по
                # провайдеру, а не по конкретной модели.
                cd = self.quota.cooldown(name, account)
                if cd:
                    kind = "бюджет" if cd.is_budget else "троттл"
                    paused.append(f"{account}: {kind} "
                                  f"{int(cd.remaining_seconds)}с")
            settings = config(name) or {}
            out.append({
                "provider": name,
                "вид": "api" if is_api(name) else "веб",
                "учёток": len(pool),
                "свободна": free,
                "цикл": stats.agentic,
                "контекст": stats.context_chars,
                "ход": stats.median_turn_sec,
                "паузы": paused,
                # Платного «Авто» не берёт: выбрать его можно только руками.
                "платный": bool(settings.get("paid")),
                "модель": settings.get("model") or "",
                "модели": _chat_models(settings.get("models") or {}),
            })
        return out


#: Общий маршрутизатор процесса.
default = Router()
