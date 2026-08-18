"""Канарейка: кто жив и почему упал.

Отвечает на вопрос, который дороже всего стоит ошибиться: **провайдер молчит
или доступ протух?** Половина «протухших» на деле оказывается своими же
багами — перелогиниваться в таких случаях незачем.

**Трёхтестовая диагностика.** Три провайдера отдают ОДНОРАЗОВЫЕ токены —
ChatGPT (``access_token``), Z.ai (капча), MS Copilot. Симптом их
неправильного кеширования всегда одинаков и обманчив: «работало, потом резко
отвалилось, наверное куки протухли». Различаем так:

1. короткий запрос свежим объектом,
2. второй запрос ТЕМ ЖЕ объектом,
3. запрос новым объектом.

Проходят 1 и 3, но не 2 — это кеш одноразового токена, а не доступ. Куки
менять не нужно, чинить надо кеш.

**Молчаливая деградация.** У Gemini Web и Mistral «ответил» НЕ доказывает,
что аккаунт жив: они на отвергнутый доступ не отказывают, а тихо обслуживают
гостем. Там канарейка проверяет не ответ, а косвенные признаки — наличие
кеша кук у Gemini, поведение на втором сообщении у Mistral.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from foxroute.accounts import Accounts, default as default_store, open_provider
from foxroute.errors import (
    AuthError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from foxroute.providers.base import Request
from foxroute.quota import QuotaTracker, default as default_quota
from foxroute.registry import SILENT_DEGRADE, auth_kind, AUTH_NONE

#: Что спрашиваем. Коротко: ответ не важен, важен факт ответа, а каждый
#: запрос к веб-сессии стоит сообщения из квоты.
PROBE = "Ответь одним словом: да"
PROBE_TIMEOUT = 90

#: Состояния.
ALIVE = "жив"
DEAD_ACCESS = "доступ отвергнут"
RATE_LIMITED = "норма выбрана"
BROKEN = "поломка адаптера"
CACHE_BUG = "кеш одноразового токена"
DEGRADED = "отвечает, но гостем"
UNKNOWN = "не проверялся"


@dataclass
class Verdict:
    """Что показала проверка одного провайдера."""

    provider: str
    account: str = "default"
    state: str = UNKNOWN
    detail: str = ""
    seconds: float = 0.0
    #: Нужно ли человеку что-то делать руками.
    needs_hands: bool = False

    def __str__(self) -> str:
        mark = {ALIVE: "OK", RATE_LIMITED: "--", DEGRADED: "??"}.get(
            self.state, "!!")
        return (f"{mark} {self.provider}/{self.account}: {self.state}"
                + (f" — {self.detail}" if self.detail else ""))


class Canary:
    """Проверка живости провайдеров."""

    def __init__(self, store: Accounts | None = None,
                 quota: QuotaTracker | None = None):
        self.store = store or default_store
        self.quota = quota or default_quota
        self.last: dict[tuple[str, str], Verdict] = {}

    # ── одна проверка ─────────────────────────────────────────────────

    def check(self, provider: str, account: str = "",
              deep: bool = False) -> Verdict:
        """Проверить провайдера.

        ``deep`` включает трёхтестовую диагностику — она стоит трёх
        сообщений из квоты вместо одного, поэтому по умолчанию выключена и
        применяется точечно, когда обычная проверка дала непонятный отказ.
        """
        account = account or self._first_account(provider)
        verdict = Verdict(provider=provider, account=account)
        started = time.time()

        # Чего не хватает помимо ключа — видно без обращения к сети.
        missing = self.store.missing_parts(provider)
        if missing:
            verdict.state = DEAD_ACCESS
            verdict.detail = "не хватает: " + ", ".join(missing)
            verdict.needs_hands = True
            verdict.seconds = round(time.time() - started, 1)
            self.last[(provider, account)] = verdict
            return verdict

        try:
            instance = open_provider(provider, account, store=self.store,
                                     allow_anonymous=True)
            text = instance.complete(
                Request(prompt=PROBE, timeout=PROBE_TIMEOUT))
        except RateLimited as exc:
            verdict.state = RATE_LIMITED
            verdict.detail = str(exc)[:120]
            self.quota.handle_rate_limited(
                provider, account, exc.retry_after, exc.is_budget)
        except AuthError as exc:
            verdict.state = DEAD_ACCESS
            verdict.detail = str(exc)[:120]
            verdict.needs_hands = True
        except ProviderError as exc:
            # Непонятный отказ. Именно здесь стоит копнуть глубже: половина
            # таких оказывается багом адаптера, а не протухшим доступом.
            verdict.state = BROKEN
            verdict.detail = f"{type(exc).__name__}: {str(exc)[:110]}"
            if deep:
                deeper = self._diagnose(provider, account)
                if deeper:
                    verdict.state = CACHE_BUG
                    verdict.detail = deeper
                    verdict.needs_hands = False
        else:
            spent = time.time() - started
            if not text.strip():
                verdict.state = BROKEN
                verdict.detail = "пустой ответ при успешном запросе"
            elif provider in SILENT_DEGRADE:
                degraded = self._guest_signs(provider, instance, spent, deep)
                if degraded:
                    verdict.state = DEGRADED
                    verdict.detail = degraded
                    verdict.needs_hands = True
                else:
                    verdict.state = ALIVE
                    verdict.detail = text.strip()[:40]
                    self.quota.clear_cooldown(provider, account)
            else:
                verdict.state = ALIVE
                verdict.detail = text.strip()[:40]
                self.quota.clear_cooldown(provider, account)

        verdict.seconds = round(time.time() - started, 1)
        self.last[(provider, account)] = verdict
        return verdict

    # ── признаки гостевого режима ─────────────────────────────────────

    def _guest_signs(self, provider: str, instance, spent: float,
                     deep: bool) -> str:
        """Не обслуживают ли нас гостем, несмотря на наличие ключа.

        У этих провайдеров ``authorized`` всегда истинно — ключ ведь есть.
        Но принят он или отвергнут, по самому ответу не понять: отказа они
        не присылают, а тихо переключаются в гостевой режим. Признаки
        приходится искать косвенные, и они у каждого свои.
        """
        if provider == "gemini_web":
            # Время ответа. С рабочим кешем кук — 12-18 секунд, без него
            # библиотека тратит около 180 на бесплодные попытки входа и
            # только потом отвечает анонимно. Разница слишком велика,
            # чтобы быть случайной.
            if spent > 60:
                return (f"ответ шёл {spent:.0f}с вместо обычных 12-18 — "
                        "похоже, кеш кук пуст и запрос ушёл анонимно")
            return ""

        if provider == "mistral":
            # Гостю отвечают ровно на ОДНО сообщение, дальше молчат.
            # Проверяется только вторым запросом, поэтому лишь при deep:
            # иначе каждая проверка стоила бы двух сообщений вместо одного.
            if not deep:
                return ""
            try:
                again = instance.complete(
                    Request(prompt=PROBE, timeout=PROBE_TIMEOUT))
            except ProviderError:
                again = ""
            if not again.strip():
                return ("на второе сообщение не ответил — гостю дают ровно "
                        "одно, значит кука не принята")
            return ""

        return ""

    # ── трёхтестовая диагностика ──────────────────────────────────────

    def _diagnose(self, provider: str, account: str) -> str:
        """Отличить кеш одноразового токена от протухшего доступа.

        Возвращает пояснение, если дело в кеше, иначе пустую строку.
        Стоит трёх сообщений из квоты — вызывать точечно.
        """
        def probe(instance) -> bool:
            try:
                return bool(instance.complete(
                    Request(prompt=PROBE, timeout=PROBE_TIMEOUT)).strip())
            except ProviderError:
                return False

        try:
            first = open_provider(provider, account, store=self.store,
                                  allow_anonymous=True)
        except ProviderError:
            return ""

        # 1. Свежий объект.
        if not probe(first):
            return ""  # не отвечает и с первого раза — дело не в кеше

        # 2. Тот же объект повторно.
        second_ok = probe(first)

        # 3. Новый объект.
        try:
            fresh = open_provider(provider, account, store=self.store,
                                  allow_anonymous=True)
        except ProviderError:
            return ""
        third_ok = probe(fresh)

        if third_ok and not second_ok:
            return ("первый и третий запросы прошли, второй тем же объектом — "
                    "нет. Это КЕШ одноразового токена, а не доступ: "
                    "куки менять не нужно")
        return ""

    # ── обход ─────────────────────────────────────────────────────────

    def sweep(self, providers: list[str] | None = None,
              on_verdict: Callable[[Verdict], None] | None = None,
              skip_paused: bool = True) -> list[Verdict]:
        """Проверить всех. Возвращает вердикты.

        ``skip_paused`` пропускает тех, кто и так на паузе: тратить на них
        сообщение бессмысленно, состояние известно.
        """
        from foxroute.providers import capabilities_of, implemented

        names = providers or implemented()
        out = []
        for name in names:
            if not capabilities_of(name).text:
                continue  # рисовальщика этой пробой не проверить
            account = self._first_account(name)
            if skip_paused and self.quota.cooldown(name, account):
                cd = self.quota.cooldown(name, account)
                verdict = Verdict(
                    provider=name, account=account, state=RATE_LIMITED,
                    detail=f"на паузе ещё {int(cd.remaining_seconds)}с")
                out.append(verdict)
                if on_verdict:
                    on_verdict(verdict)
                continue
            verdict = self.check(name, account)
            out.append(verdict)
            if on_verdict:
                on_verdict(verdict)
        return out

    def _first_account(self, provider: str) -> str:
        if auth_kind(provider) == AUTH_NONE:
            return "default"
        pool = self.store.usable(provider)
        return pool[0].account if pool else "default"

    # ── отчёт ─────────────────────────────────────────────────────────

    def report(self) -> dict:
        """Сводка последней проверки."""
        by_state: dict[str, list[str]] = {}
        for verdict in self.last.values():
            by_state.setdefault(verdict.state, []).append(
                f"{verdict.provider}/{verdict.account}")
        return {
            "всего": len(self.last),
            "живых": len(by_state.get(ALIVE, [])),
            "по состояниям": by_state,
            "требуют рук": [f"{v.provider}/{v.account}: {v.detail}"
                            for v in self.last.values() if v.needs_hands],
        }


#: Общая канарейка процесса.
default = Canary()
