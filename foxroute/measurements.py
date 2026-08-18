"""Характеристики провайдеров, снятые на живых сервисах.

Живут в JSON, а не в исходнике, по одной причине: они протухают. Сервис
меняет модель, режет лимит, чинит фронтенд — и вчерашняя цифра врёт. Данные
должна уметь обновлять канарейка, а не человек через правку кода.

Порядок поиска:

1. ``$FOXROUTE_HOME/measurements.json`` — свежие, снятые у пользователя;
2. встроенный ``foxroute/data/measurements.json`` — заводские.

Своё перекрывает заводское по каждому провайдеру отдельно: если ты померил
только Qwen, остальные останутся заводскими, а не пропадут.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from foxroute.paths import app_dir

#: Провайдер доводит агентную задачу до верного ответа.
AGENTIC_GOOD = "good"
#: Идёт верно, но умирает от квоты за 5-7 ходов. Только короткие задачи.
AGENTIC_SHORT = "short"
#: В цикл не пускать: зацикливается или не подставляет аргументы.
AGENTIC_NO = "no"
#: Не проверялся.
AGENTIC_UNKNOWN = "unknown"

_BUILTIN = Path(__file__).parent / "data" / "measurements.json"
_USER_NAME = "measurements.json"


@dataclass(frozen=True)
class Measured:
    """Что намерено у провайдера.

    ``context_chars`` — последний размер входа, на котором провайдер вернул
    ОБА маркера, из начала и из конца. То есть реально прочитанный вход, а не
    заявленное окно модели: чат-фронтенд может обрезать молча.
    """

    context_chars: int = 16_000
    median_turn_sec: float = 10.0
    agentic: str = AGENTIC_UNKNOWN
    note: str = "не измерялся"
    #: Приблизительные значения по умолчанию, до реального замера.
    provisional: bool = True

    @property
    def agentic_ok(self) -> bool:
        return self.agentic == AGENTIC_GOOD


DEFAULT = Measured()

_lock = threading.Lock()
_cache: dict[str, Measured] | None = None
_measured_at: str = ""


def _read(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Битый файл замеров не повод ронять запрос: без цифр слой работает,
        # просто осторожнее.
        return {}, ""
    providers = raw.get("providers")
    return (providers if isinstance(providers, dict) else {},
            str(raw.get("measured_at") or ""))


def _build() -> tuple[dict[str, Measured], str]:
    merged, stamp = _read(_BUILTIN)
    user, user_stamp = _read(app_dir() / _USER_NAME)
    merged = {**merged, **user}
    fields = {f for f in Measured.__dataclass_fields__}
    out: dict[str, Measured] = {}
    for name, entry in merged.items():
        if not isinstance(entry, dict):
            continue
        known = {k: v for k, v in entry.items() if k in fields}
        try:
            out[name] = replace(DEFAULT, **known)
        except TypeError:
            out[name] = DEFAULT
    return out, (user_stamp or stamp)


def _all() -> dict[str, Measured]:
    global _cache, _measured_at
    with _lock:
        if _cache is None:
            _cache, _measured_at = _build()
        return _cache


def reload() -> None:
    """Сбросить кеш — после того, как канарейка перезаписала файл."""
    global _cache
    with _lock:
        _cache = None


def get(name: str) -> Measured:
    """Характеристики провайдера. Неизвестный получает осторожные умолчания."""
    return _all().get(name, DEFAULT)


def measured_at() -> str:
    _all()
    return _measured_at


def save(name: str, value: Measured) -> None:
    """Записать свежий замер в пользовательский файл.

    Заводские данные не трогаются: пользовательский файл их перекрывает,
    поэтому обновление одного провайдера не стирает остальные.
    """
    path = app_dir() / _USER_NAME
    with _lock:
        existing, _ = _read(path)
        existing[name] = {
            "context_chars": value.context_chars,
            "median_turn_sec": value.median_turn_sec,
            "agentic": value.agentic,
            "note": value.note,
            "provisional": value.provisional,
        }
        import time

        path.write_text(json.dumps(
            {"measured_at": time.strftime("%Y-%m-%d"), "providers": existing},
            ensure_ascii=False, indent=2), encoding="utf-8")
        global _cache
        _cache = None


def names(agentic: str | None = None) -> list[str]:
    """Провайдеры, о которых есть данные. Можно отфильтровать по пригодности."""
    data = _all()
    if agentic is None:
        return sorted(data)
    return sorted(n for n, m in data.items() if m.agentic == agentic)
