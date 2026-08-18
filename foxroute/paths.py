"""Где библиотека держит свои файлы.

Адаптеры провайдеров обращаются сюда за каталогом данных: там лежат токены
Copilot, шаблоны Meta AI, статистика запросов.

Каталог задаётся переменной окружения ``FOXROUTE_HOME``. Это нужно не для
красоты: указав его на каталог с уже снятыми куками, слой подхватывает
готовые доступы, ничего никуда не копируя. По умолчанию — ``~/.foxroute``.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_lock = threading.Lock()
_dir: Path | None = None


def app_dir() -> Path:
    """Каталог данных. Создаётся при первом обращении."""
    global _dir
    with _lock:
        if _dir is None:
            raw = os.environ.get("FOXROUTE_HOME", "").strip()
            _dir = Path(raw).expanduser() if raw else Path.home() / ".foxroute"
            _dir.mkdir(parents=True, exist_ok=True)
        return _dir


def set_app_dir(path: str | Path) -> Path:
    """Переопределить каталог данных из кода (тесты, встраивание)."""
    global _dir
    with _lock:
        _dir = Path(path).expanduser()
        _dir.mkdir(parents=True, exist_ok=True)
        return _dir


def load() -> dict:
    """Настройки слоя одним словарём.

    Адаптеры лезут сюда только за ``proxy_base`` (Meta AI удаляет за собой
    чаты через прокси, потому что rd_challenge привязан к IP). Настоящее
    хранилище доступов — credentials.py.
    """
    import json

    path = app_dir() / "settings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — битый файл не должен ронять запрос
        return {}
