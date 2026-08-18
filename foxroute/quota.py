"""Учёт квот — чтобы маршрутизатору было по чему выбирать.

Без этого модуля слой работает, но выбор внутри пула вырожден: берётся первая
пригодная учётка. А «пригодная» определяется лишь тем, что она включена.
Нет ни остатка, ни пауз, ни истории отказов.

Здесь решаются три вещи.

**Различение троттла и бюджета.** Copilot на 5-м ходу отдаёт
``too-many-messages`` и оживает через 25 минут — это троттлинг, ждать
разумно. Mistral после 11 сообщений уходит на 168 минут, Poe — на 17 часов
— это бюджет, ждать бессмысленно, нужно уходить. Правило: ``RateLimited``
с ``retry_after`` до 30 минут — ждём, дольше — снимаем с очереди до сброса.
Тип определяет ошибка ``errors.RateLimited``, а не разбор текста.

**Счёт сообщений на аккаунт.** У веб-сессий квота обычно не видна (из 20
провайдеров остаток отдают семеро), поэтому считаем сами. У API-провайдеров
остаток приходит заголовками, но счёт всё равно ведём: он нужен для выбора
из пула и для прогноза «хватит ли на задачу».

**Квоты Groq по моделям.** У Groq каждая модель со своей квотой, и квоты не
пересекаются. Счётчик ведётся по паре (аккаунт, модель), а не просто по
аккаунту.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Cooldown:
    """Пауза на конкретном аккаунте (или аккаунте + модели).

    ``is_budget`` определяет поведение маршрутизатора: троттлинг — ждать,
    бюджет — снять с очереди и не пробовать до сброса.
    """

    until: float = 0.0
    is_budget: bool = False
    reason: str = ""

    @property
    def active(self) -> bool:
        return time.time() < self.until

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.until - time.time())


@dataclass
class Usage:
    """Счётчик за текущие сутки."""

    date: str = ""
    requests: int = 0
    #: Информационно: последний известный потолок от сервиса. -1 — не знаем.
    limit: int = -1
    last_at: float = 0.0


class QuotaTracker:
    """Учёт квот по парам (провайдер/аккаунт, модель).

    Модель указывается, только когда квоты по моделям независимы (Groq).
    У остальных все модели идут в общий счёт, и ключ — просто аккаунт.

    Состояние живёт в памяти: перезапуск обнуляет счётчики. Это осознанно —
    записывать на диск при каждом запросе дорого, а суточные счётчики и без
    того обнуляются за ночь.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: (provider, account, model_or_empty) -> Usage
        self._usage: dict[tuple[str, str, str], Usage] = {}
        #: (provider, account, model_or_empty) -> Cooldown
        self._cooldowns: dict[tuple[str, str, str], Cooldown] = {}

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _key(self, provider: str, account: str,
             model: str = "") -> tuple[str, str, str]:
        return (provider, account, model)

    # ── запись ────────────────────────────────────────────────────────

    def record(self, provider: str, account: str, model: str = "") -> None:
        """Отметить успешный запрос."""
        key = self._key(provider, account, model)
        today = self._today()
        with self._lock:
            usage = self._usage.get(key)
            if usage is None or usage.date != today:
                usage = Usage(date=today)
                self._usage[key] = usage
            usage.requests += 1
            usage.last_at = time.time()

    def record_limit(self, provider: str, account: str,
                     limit: int, model: str = "") -> None:
        """Запомнить потолок, сообщённый сервисом (заголовки x-ratelimit-*)."""
        key = self._key(provider, account, model)
        today = self._today()
        with self._lock:
            usage = self._usage.get(key)
            if usage is None or usage.date != today:
                usage = Usage(date=today)
                self._usage[key] = usage
            usage.limit = limit

    def set_cooldown(self, provider: str, account: str,
                     seconds: float, is_budget: bool = False,
                     reason: str = "", model: str = "") -> None:
        """Поставить паузу после отказа."""
        key = self._key(provider, account, model)
        with self._lock:
            self._cooldowns[key] = Cooldown(
                until=time.time() + seconds,
                is_budget=is_budget,
                reason=reason)

    def clear_cooldown(self, provider: str, account: str,
                       model: str = "") -> None:
        """Снять паузу (провайдер ожил)."""
        key = self._key(provider, account, model)
        with self._lock:
            self._cooldowns.pop(key, None)

    # ── чтение ────────────────────────────────────────────────────────

    def cooldown(self, provider: str, account: str,
                 model: str = "") -> Cooldown | None:
        """Активная пауза, если есть."""
        key = self._key(provider, account, model)
        with self._lock:
            cd = self._cooldowns.get(key)
            if cd is None or not cd.active:
                return None
            return cd

    def used_today(self, provider: str, account: str,
                   model: str = "") -> int:
        key = self._key(provider, account, model)
        today = self._today()
        with self._lock:
            usage = self._usage.get(key)
            if usage is None or usage.date != today:
                return 0
            return usage.requests

    def remaining(self, provider: str, account: str,
                  model: str = "") -> int | None:
        """Сколько запросов осталось. None — не знаем (большинство веб-сессий)."""
        key = self._key(provider, account, model)
        today = self._today()
        with self._lock:
            usage = self._usage.get(key)
            if usage is None or usage.date != today or usage.limit < 0:
                return None
            return max(0, usage.limit - usage.requests)

    def is_available(self, provider: str, account: str,
                     model: str = "") -> bool:
        """Можно ли отправлять запрос прямо сейчас."""
        cd = self.cooldown(provider, account, model)
        if cd is not None:
            return False
        left = self.remaining(provider, account, model)
        if left is not None and left <= 0:
            return False
        return True

    def best_account(self, provider: str,
                     accounts: list[str],
                     model: str = "") -> str | None:
        """Выбрать учётку с наибольшим остатком, без паузы.

        Если остаток неизвестен (веб-сессии) — берём ту, которую использовали
        давнее всех: простейшая ротация, чтобы не долбить одну.
        """
        best = None
        best_score = (-1, 0.0)  # (remaining, -last_used)

        for account in accounts:
            if not self.is_available(provider, account, model):
                continue
            left = self.remaining(provider, account, model)
            key = self._key(provider, account, model)
            with self._lock:
                usage = self._usage.get(key)
            last = usage.last_at if usage else 0.0

            if left is not None:
                score = (left, -last)
            else:
                # Остаток неизвестен — предпочитаем давно не использованную.
                score = (999999, -last)

            if score > best_score:
                best_score = score
                best = account

        return best

    # ── обработка отказа ──────────────────────────────────────────────

    def handle_rate_limited(self, provider: str, account: str,
                            retry_after: float | None,
                            is_budget: bool = False,
                            reason: str = "",
                            model: str = "") -> None:
        """Обработать ``RateLimited``: поставить паузу нужного вида.

        Если ``retry_after`` не задан — ставим короткую паузу по умолчанию:
        провайдеры, у которых окно длинное (Mistral, Poe), срок сообщают,
        а те, у кого он короткий (Copilot), не сообщают.
        """
        from foxroute.errors import RateLimited

        if retry_after is None:
            seconds = 120.0  # две минуты — достаточно для троттлинга
        else:
            seconds = retry_after

        if is_budget or seconds >= RateLimited.BUDGET_THRESHOLD_SEC:
            is_budget = True

        self.set_cooldown(provider, account, seconds, is_budget, reason, model)

    # ── отчёт ─────────────────────────────────────────────────────────

    def summary(self) -> list[dict]:
        """Состояние всех известных пар — для CLI и отладки."""
        today = self._today()
        out = []
        with self._lock:
            all_keys = set(self._usage) | set(self._cooldowns)
        for key in sorted(all_keys):
            provider, account, model = key
            entry = {
                "provider": provider,
                "account": account,
                "model": model,
                "used": self.used_today(provider, account, model),
                "remaining": self.remaining(provider, account, model),
            }
            cd = self.cooldown(provider, account, model)
            if cd:
                entry["cooldown"] = round(cd.remaining_seconds)
                entry["cooldown_type"] = "budget" if cd.is_budget else "throttle"
                entry["cooldown_reason"] = cd.reason
            out.append(entry)
        return out


#: Общий трекер процесса.
default = QuotaTracker()
