"""Ошибки провайдеров — типами, а не текстом.

Классификация отказа разбором строки исключения — поиском подстрок
``401``, ``rate_limit``, ``quota`` — ломается молча. Показательный пример:
Grok формирует сообщение «rate limit» с пробелом, а поиск подстроки
``rate_limit`` с подчёркиванием его не находит — cooldown не ставится, и
следующий запрос снова уходит в исчерпанного провайдера.

Здесь тип несёт смысл сам, а текст остаётся только для человека.
"""
from __future__ import annotations


class ProviderError(Exception):
    """Провайдер не смог выполнить запрос.

    ``provider`` — ключ реестра, чтобы вызывающий знал, кто именно упал,
    даже если исключение всплыло через несколько слоёв.
    """

    def __init__(self, message: str, provider: str = ""):
        super().__init__(message)
        self.provider = provider

    def __str__(self) -> str:
        base = super().__str__()
        return f"[{self.provider}] {base}" if self.provider else base


class AuthError(ProviderError):
    """Доступ не принят: кука протухла, токен отозван, ключ неверный.

    Осторожно: это НЕ первый диагноз при внезапном отказе. Три провайдера
    отдают одноразовые токены (ChatGPT, Z.ai, MS Copilot), и повторное
    использование закешированного токена выглядит ровно так же. Прежде чем
    идти перелогиниваться — см. health.diagnose().
    """


class RateLimited(ProviderError):
    """Норма выбрана. Важно, на сколько именно.

    Различение определяет поведение слоя:

    * ``THROTTLE`` — короткая задержка, провайдер вернётся сам. MS Copilot
      отдаёт ``too-many-messages`` на 5-м ходу и оживает через 25 минут.
      Здесь пауза действительно лечит.
    * ``BUDGET`` — окно исчерпано. Mistral после 11 сообщений уходит на
      168 минут, Poe — на 17 часов. Пауза не создаёт бюджет, а только
      размазывает тот же по времени, поэтому надо уходить к другому.
    """

    THROTTLE = "throttle"
    BUDGET = "budget"

    #: Граница между «подождать» и «уходить». Ниже неё провайдер обычно
    #: возвращается сам в пределах одной задачи, выше — нет.
    BUDGET_THRESHOLD_SEC = 30 * 60

    def __init__(self, message: str, provider: str = "",
                 retry_after: float | None = None, kind: str | None = None):
        super().__init__(message, provider)
        self.retry_after = retry_after
        self.kind = kind or self._classify(retry_after)

    @staticmethod
    def _classify(retry_after: float | None) -> str:
        """Без явного срока считаем троттлом.

        Провайдеры, которые вообще не называют срок, на практике оказывались
        короткими троттлами (Copilot). Те, у кого окно длинное, срок как раз
        сообщают — им есть что сказать.
        """
        if retry_after is None:
            return RateLimited.THROTTLE
        return (RateLimited.BUDGET
                if retry_after >= RateLimited.BUDGET_THRESHOLD_SEC
                else RateLimited.THROTTLE)

    @property
    def is_budget(self) -> bool:
        return self.kind == RateLimited.BUDGET


class ProviderRefused(ProviderError):
    """Сервис ответил, но выполнять отказался.

    Отдельный тип, потому что это не поломка и лечится не сменой куки, а
    сменой формулировки. Устойчивый случай: на промпт с JSON- или XML-схемами
    инструментов ChatGPT, MS Copilot, Meta AI и Manus отвечают отказом вида
    «инструменты в этой среде недоступны», а Meta AI прямо пишет, что это
    «попытка подменить среду выполнения». Тот же запрос в построчном формате
    они выполняют.

    См. translate.py — там инструменты рендерятся построчно именно поэтому.
    """


class ProviderUnavailable(ProviderError):
    """Транспорт не дал ответа: таймаут, обрыв, 5xx на стороне сервиса."""


class ContextTooLarge(ProviderError):
    """Вход не влез. Отдельный тип, потому что лечится обрезкой контекста.

    Определяется надёжно не у всех: Groq честно отдаёт HTTP 413, а ChatGPT
    на 100k символов отвечает HTTP 500, неотличимым от настоящей аварии.
    Поэтому основная защита — не ловить это исключение, а не превышать
    измеренный потолок из registry.MEASURED.
    """

    def __init__(self, message: str, provider: str = "",
                 limit_chars: int | None = None):
        super().__init__(message, provider)
        self.limit_chars = limit_chars


class Unsupported(ProviderError):
    """Запрошено то, чего этот провайдер физически не умеет.

    Отдаётся честно, вместо того чтобы тихо подменить смысл запроса. Так,
    веб-сессии не умеют ``n>1``, ``logprobs`` и ``seed``: там нет ручек,
    которые это включают.
    """
