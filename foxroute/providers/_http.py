"""Сетевой слой, общий для всех HTTP-адаптеров.

Здесь три вещи: единая точка создания сессии, разбор потоков событий и
превращение кода ответа в типизированную ошибку.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterator

from foxroute.errors import (
    AuthError,
    ContextTooLarge,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)

__all__ = ["REQUEST_TIMEOUT", "Accumulated", "ThinkTags", "check",
           "multipart", "request", "session", "sse_deltas", "sse_events"]

#: Веб-версии отвечают медленнее API: Grok тратит 18-26 секунд на короткий
#: вопрос, Manus поднимает виртуальный компьютер на 14-40. Общий потолок
#: щедрый намеренно — при 60 секундах Grok падал под нагрузкой, хотя был жив.
REQUEST_TIMEOUT = 300


def session(impersonate: str = "chrome", *, http1: bool = False):
    """Сессия с отпечатком настоящего браузера.

    Все запросы модуля идут через эту функцию, чтобы подмена в тестах
    делалась в одном месте. Патчить ``curl_cffi`` глобально нельзя — порченый
    на весь процесс модуль ведёт к трудноуловимым поломкам.

    ``impersonate`` меняется там, где сервис ждёт не браузер: Grok говорит с
    мобильным приложением и хочет отпечаток Chrome на Android.

    ``http1`` опускает соединение до HTTP/1.1. Нужен для загрузки файлов в
    DeepSeek: по HTTP/2 их приёмник рвёт поток на большом теле, и файл
    остаётся навсегда в состоянии ``PENDING`` — отказ приходит потом, уже
    от чата, в виде «invalid ref file id».
    """
    from curl_cffi import requests as creq

    # Прокси — общий у шлюза (или свой у учётки, если сервер его выставил).
    # Один и тот же URL на http и https; socks5://…, http://user:pass@… —
    # libcurl понимает всё. Пусто — прямое соединение.
    from foxroute import settings

    proxy = settings.current_proxy()
    extra: dict = {}
    if proxy:
        extra["proxies"] = {"http": proxy, "https": proxy}

    if http1:
        from curl_cffi import CurlHttpVersion

        return creq.Session(impersonate=impersonate,
                            http_version=CurlHttpVersion.V1_1, **extra)
    return creq.Session(impersonate=impersonate, **extra)


def multipart(fields: dict[str, str] | None = None, *,
              name: str = "file", filename: str = "",
              data: bytes = b"", content_type: str = "") -> tuple[bytes, str]:
    """Собрать тело ``multipart/form-data`` руками.

    Нужно потому, что ``curl_cffi`` не понимает привычный ``files=`` —
    у него для вложений свой ``CurlMime``, и подсунуть в него готовые
    заголовки провайдера не выходит. Собирать тело в каждом адаптере
    заново легко приводит к ошибке: у DeepSeek заголовки в нижнем регистре,
    и добавленный ``Content-Type`` с большой буквы уехал вторым ключом,
    после чего сервис ответил «Invalid boundary».

    Возвращает пару ``(тело, значение Content-Type)``. Заголовок надо
    ставить в словарь ЯВНО, предварительно убрав оттуда существующий
    вариант в любом регистре.
    """
    import os

    mark = "----foxroute" + os.urandom(12).hex()
    line = b"\r\n"
    parts: list[bytes] = []
    for key, value in (fields or {}).items():
        parts += [
            b"--" + mark.encode(), line,
            f'Content-Disposition: form-data; name="{key}"'.encode(),
            line, line, str(value).encode(), line,
        ]
    if filename or data:
        parts += [
            b"--" + mark.encode(), line,
            (f'Content-Disposition: form-data; name="{name}"; '
             f'filename="{filename}"').encode(), line,
            f"Content-Type: {content_type or 'application/octet-stream'}"
            .encode(), line, line, data, line,
        ]
    parts += [b"--" + mark.encode() + b"--", line]
    return b"".join(parts), f"multipart/form-data; boundary={mark}"


def request(session, method: str, url: str, provider: str = "", **kw) -> Any:
    """Единая точка сетевого запроса: сбои транспорта — в наши ошибки.

    Отдельный слой нужен ровно из-за одной вещи. Доступы уезжают в заголовки,
    а туда пролезает только latin-1; кривой ключ (скопированный вместе с
    подписью или с русским текстом) роняет запрос ``UnicodeEncodeError`` из
    недр curl, по которому невозможно догадаться, что дело в ключе.

    Проверять ключ заранее и отвергать нельзя: сервис может однажды начать
    выдавать что угодно, и наша догадка о «правильной форме» окажется
    единственным, что мешает работать. Поэтому пробуем как есть, а сбой
    объясняем.
    """
    try:
        return session.request(method, url, **kw)
    except UnicodeEncodeError as exc:
        raise AuthError(
            "ключ не пролезает в заголовок HTTP (туда можно только latin-1) — "
            "похоже, скопирован с лишним текстом", provider) from exc
    except Exception as exc:  # noqa: BLE001
        # curl_cffi поднимает свои исключения на таймаутах и обрывах. Ловим
        # по имени, чтобы не тащить импорт ради проверки типа и не глушить
        # заодно собственные ошибки программиста.
        if type(exc).__name__ in ("RequestsError", "CurlError", "Timeout",
                                  "ConnectionError"):
            # Если запрос идёт через прокси — он первый подозреваемый в
            # обрыве: подсказываем проверить его, а не гадать на сервис.
            from foxroute import settings

            hint = (" — возможно, прокси не отвечает, проверь его в настройках"
                    if settings.current_proxy() else "")
            raise ProviderUnavailable(
                f"транспорт не дал ответа: {type(exc).__name__}: "
                f"{str(exc)[:200]}{hint}", provider) from exc
        raise


def check(provider: str, response: Any) -> None:
    """Поднять типизированную ошибку, если ответ неуспешен.

    Смысл несёт тип исключения, а не текст. Вклеивать код ответа в строку
    незачем: классифицировать отказы разбором текста — плохой путь, для
    этого есть тип. Текст нужен только человеку.
    """
    code = getattr(response, "status_code", 0)
    if code < 400:
        return

    body = ""
    try:
        body = (response.text or "")[:300]
        if not body:
            # На потоковом ответе ``.text``/``.content`` пусты, пока поток не
            # прочитан. У ОШИБКИ тело короткое и мы его не итерируем —
            # дочитываем итерацией, иначе выходит «HTTP 402: » без объяснения.
            try:
                drained = b"".join(response.iter_content())
            except Exception:  # noqa: BLE001
                drained = b""
            if drained:
                body = drained.decode("utf-8", "replace")[:300]
    except Exception:  # noqa: BLE001 — тело может быть нечитаемым, это не важно
        pass

    if code in (401, 403):
        raise AuthError(f"HTTP {code}: доступ не принят. {body}", provider)
    if code == 413:
        raise ContextTooLarge(f"HTTP 413: вход слишком велик. {body}", provider)
    if code == 429:
        retry = _retry_after(response)
        # Больше часа до сброса — это суточная норма, а не троттл.
        daily = retry is not None and retry >= 3600
        msg = "дневной лимит исчерпан" if daily else "лимит запросов исчерпан"
        when = _human_reset(retry)
        if when:
            msg += f", сбросится {when}"
        raise RateLimited(msg, provider, retry_after=retry)
    if code >= 500:
        raise ProviderUnavailable(f"HTTP {code}: сервис недоступен. {body}",
                                  provider)
    raise ProviderError(f"HTTP {code}: {body}", provider)


def _parse_duration(raw: Any) -> float | None:
    """Разобрать срок из заголовка в секунды.

    Форматы разные: чистые секунды («30»), Groq-длительность («28m48s»,
    «800ms», «1h2m3s»). HTTP-дату не разбираем — её у этих сервисов нет.
    """
    import re
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    m = re.fullmatch(
        r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?", s)
    if m and any(m.groups()):
        h, mi, se, ms = m.groups()
        total = (int(h or 0) * 3600 + int(mi or 0) * 60
                 + float(se or 0) + int(ms or 0) / 1000)
        return total or None
    return None


def _retry_after(response: Any) -> float | None:
    """Срок до сброса, секунды. Собираем из всех известных заголовков."""
    try:
        headers = response.headers or {}
    except Exception:  # noqa: BLE001
        return None
    keys = ("Retry-After", "retry-after",
            "x-ratelimit-reset-requests", "x-ratelimit-reset",
            "x-ratelimit-reset-tokens")
    best = None
    for key in keys:
        value = _parse_duration(headers.get(key))
        # Берём наибольший срок: если и запросы, и токены на паузе, ждать
        # надо по дольшему.
        if value is not None and (best is None or value > best):
            best = value
    return best


def _human_reset(seconds: float | None) -> str:
    """Человеку: «через 25 мин», «через 2 ч», «завтра». None — молча."""
    if not seconds or seconds <= 0:
        return ""
    if seconds < 90:
        return f"через {int(seconds)} с"
    if seconds < 3600:
        return f"через {round(seconds / 60)} мин"
    if seconds < 20 * 3600:
        return f"через {round(seconds / 3600)} ч"
    return "завтра"


def sse_events(response: Any) -> Iterator[dict]:
    """Разобрать поток событий в словари.

    Понимает оба вида, которые встречаются у этих сервисов: строки
    ``data: {...}`` и голый JSON построчно. Мусор и неразобранные строки
    пропускаются молча — в потоках регулярно едут служебные кадры, и падать
    на них означало бы терять уже полученный ответ.
    """
    for raw in response.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()

        if line.startswith("data:"):
            chunk = line[5:].strip()
        elif line.startswith("{"):
            chunk = line
        else:
            continue

        if not chunk:
            continue
        if chunk == "[DONE]":
            return
        try:
            event = json.loads(chunk)
        except ValueError:
            continue
        # Разобраться могло во что угодно: сервисы шлют в потоке и голый
        # `null`, и числа, и строки. Наверх отдаём только словари, иначе
        # каждый потребитель будет проверять тип заново — и однажды забудет.
        if isinstance(event, dict):
            yield event


def sse_deltas(response: Any, pick: Callable[[dict], str]) -> Iterator[str]:
    """Приращения текста из потока. ``pick`` достаёт кусок из события."""
    for event in sse_events(response):
        piece = pick(event)
        if piece:
            yield piece


class Accumulated:
    """Превратить накопленный текст в приращения.

    Часть сервисов присылает в каждом кадре ВЕСЬ написанный текст, а не
    добавку: так делают Perplexity, Kimi (``op: set``) и Meta AI (растущее
    поле ``f2``). Склеивать такое конкатенацией нельзя — получится ответ с
    нарастающими повторами.

    Вычитание префикса вынесено сюда, а не оставлено потребителю: иначе
    каждый, кто читает поток, будет писать это заново и однажды ошибётся.
    """

    def __init__(self) -> None:
        self._seen = ""

    def feed(self, whole: str) -> str:
        """Отдать только то, чего ещё не было."""
        if not whole:
            return ""
        if whole.startswith(self._seen):
            delta = whole[len(self._seen):]
            self._seen = whole
            return delta
        # Текст разошёлся с накопленным: сервис переписал уже сказанное
        # (бывает, когда модель себя правит). Начинаем накопление заново и
        # отдаём всё — потерять кусок хуже, чем повторить.
        self._seen = whole
        return whole

    @property
    def text(self) -> str:
        """Весь текст, прошедший через накопитель.

        Нужен тем, кто после потока разбирает ответ целиком — например,
        ищет в нём ссылки на созданные моделью файлы.
        """
        return self._seen


class ThinkTags:
    """Отделить рассуждение, размеченное тегами прямо в тексте.

    Часть моделей не даёт для хода мысли отдельного поля, а вставляет его в
    ответ как ``<think>…</think>``. Без разбора это уезжает читателю как
    часть ответа.

    Тег приходит по кускам, и разрезать его может где угодно — поэтому
    хвост, похожий на начало тега, придерживается до следующего куска.
    Отдать его сразу значит однажды показать человеку голое ``<thi``.
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    @staticmethod
    def _held(buffer: str, tag: str) -> int:
        """Длина хвоста, который может оказаться началом тега."""
        for size in range(min(len(tag) - 1, len(buffer)), 0, -1):
            if tag.startswith(buffer[-size:]):
                return size
        return 0

    def feed(self, piece: str) -> Iterator[tuple[str, str]]:
        """Куски с типом: ``("text", …)`` или ``("thinking", …)``."""
        self._buffer += piece
        while True:
            tag = self.CLOSE if self._inside else self.OPEN
            kind = "thinking" if self._inside else "text"
            at = self._buffer.find(tag)
            if at < 0:
                break
            before, self._buffer = self._buffer[:at], self._buffer[at + len(tag):]
            if before:
                yield (kind, before)
            self._inside = not self._inside

        keep = self._held(self._buffer, self.CLOSE if self._inside else self.OPEN)
        ready = self._buffer[:len(self._buffer) - keep] if keep else self._buffer
        self._buffer = self._buffer[len(self._buffer) - keep:] if keep else ""
        if ready:
            yield ("thinking" if self._inside else "text", ready)

    def drain(self) -> Iterator[tuple[str, str]]:
        """Отдать придержанный хвост. Звать в конце потока."""
        if self._buffer:
            yield ("thinking" if self._inside else "text", self._buffer)
            self._buffer = ""
