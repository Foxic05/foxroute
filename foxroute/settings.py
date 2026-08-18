"""Глобальные настройки шлюза — пока это прокси, дальше будет больше.

Отдельно от учёток: доступы это «кто мы у провайдера», а настройки —
«как шлюз работает в целом». Лежат в ``settings.json`` рядом с данными.

Прокси. Один общий на весь шлюз (и на запросы, и на вход через браузер),
но у отдельной учётки может быть свой — тогда берётся он. Формат — любой,
что понимает libcurl и Chrome::

    http://host:port
    http://user:pass@host:port
    socks5://host:port
    socks5://user:pass@host:port
"""
from __future__ import annotations

import json
import os
import secrets
import threading

from foxroute.paths import app_dir

_lock = threading.Lock()
_cache: dict | None = None

#: Текущий прокси для ЭТОГО запроса — переопределяет глобальный. Ставится
#: сервером/маршрутизатором, когда у учётки свой прокси. Потоко-локальный:
#: сервер обслуживает каждый запрос в своём потоке.
_local = threading.local()


def _path():
    return app_dir() / "settings.json"


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def get(key: str, default=""):
    return _load().get(key, default)


def put(key: str, value) -> None:
    with _lock:
        data = dict(_load())
        if value in (None, ""):
            data.pop(key, None)
        else:
            data[key] = value
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        globals()["_cache"] = data


def all_settings() -> dict:
    return dict(_load())


# ── API-ключ ──────────────────────────────────────────────────────────
#
# Ключ закрывает API для программных клиентов (чужой проект, curl, SDK).
# Свой интерфейс за туннелем ходит без ключа (см. server._auth_ok). Источник
# по приоритету: переменная окружения FOXROUTE_API_KEY (в т.ч. пустая — тогда
# авторизация выключена НАМЕРЕННО) → сохранённый в settings.json → сгенерить
# при первом запуске. Так каждый, кто развернёт проект у себя, получает свой
# уникальный ключ без ручных команд.

def api_key() -> str:
    env = os.environ.get("FOXROUTE_API_KEY")
    if env is not None:
        return env
    return str(get("api_key", "") or "")


def ensure_api_key() -> str:
    """Вернуть ключ, а если его нет и env не задан — сгенерировать и сохранить."""
    if os.environ.get("FOXROUTE_API_KEY") is not None:
        return os.environ["FOXROUTE_API_KEY"]  # env решает, в т.ч. пустой = выкл
    stored = str(get("api_key", "") or "")
    if stored:
        return stored
    key = "fox_" + secrets.token_hex(24)
    put("api_key", key)
    return key


def rotate_api_key() -> str:
    """Сгенерировать НОВЫЙ ключ и сохранить (старый перестаёт работать)."""
    key = "fox_" + secrets.token_hex(24)
    put("api_key", key)
    return key


# ── прокси ────────────────────────────────────────────────────────────

def global_proxy() -> str:
    """Общий прокси шлюза (пусто — прямое соединение)."""
    return str(get("proxy", "") or "")


def provider_proxy(name: str) -> str:
    """Прокси, заданный на КОНКРЕТНОГО провайдера (пусто — не задан).

    Слой между общим и учёткой: «весь ChatGPT через прокси A, весь DeepSeek
    через B». Идёт и на вход через браузер, и на запросы этого провайдера,
    если у самой учётки свой прокси не выставлен.
    """
    table = get("proxy_by_provider", {}) or {}
    return str(table.get(name, "") or "")


def set_provider_proxy(name: str, proxy: str) -> None:
    """Задать (или снять пустой строкой) прокси провайдера."""
    with _lock:
        data = dict(_load())
        table = dict(data.get("proxy_by_provider") or {})
        if proxy:
            table[name] = proxy
        else:
            table.pop(name, None)
        if table:
            data["proxy_by_provider"] = table
        else:
            data.pop("proxy_by_provider", None)
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        globals()["_cache"] = data


def set_request_proxy(proxy: str | None) -> None:
    """Прокси для текущего запроса. ``None`` — вернуться к глобальному."""
    _local.proxy = proxy


def current_proxy() -> str:
    """Какой прокси применять сейчас: свой у запроса, иначе глобальный."""
    own = getattr(_local, "proxy", None)
    if own is not None:
        return own
    return global_proxy()
