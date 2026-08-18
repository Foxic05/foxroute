"""Забрать доступы из браузера на сервере после ручного входа.

Как пользоваться::

    bash bench/browser_session.sh start     # поднять браузер
    # войти в аккаунты через VNC-туннель
    python3 bench/harvest_cookies.py        # показать, что нашлось
    python3 bench/harvest_cookies.py --save # положить в хранилище

Читаем через отладочный порт Chrome, а не из файлов профиля. Причина не в
удобстве: половина сервисов держит доступ НЕ в куке, а в ``localStorage``
(Qwen, Z.ai, DeepSeek), и оттуда его пришлось бы доставать из LevelDB —
формата, который Chrome меняет между версиями и держит под блокировкой,
пока браузер запущен. Через отладку и то, и другое читается ровно так, как
их видит сама страница.

Значения НЕ печатаются: в журнал попадает только длина и хвост. Живая кука
в выводе — это живая кука в истории терминала.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEBUG = "http://127.0.0.1:9977"

#: Что и откуда берём. ``cookies`` — имена кук; несколько имён означают
#: склейку через «|» в том же порядке (так устроены доступы Grok и Gemini).
#: ``storage`` — ключ в localStorage нужного происхождения.
WANTED = {
    "chatgpt": {
        "origin": "https://chatgpt.com",
        "cookies": ["__Secure-next-auth.session-token"],
        "note": "кука может быть разрезана на .0 и .1 — склеиваем",
    },
    "gemini_web": {
        "origin": "https://gemini.google.com",
        "cookies": ["__Secure-1PSID", "__Secure-1PSIDTS"],
    },
    "grok": {
        "origin": "https://grok.com",
        "cookies": ["sso", "sso-rw"],
    },
    "kimi": {
        "origin": "https://www.kimi.com",
        "storage": "refresh_token",
    },
    "qwen": {
        "origin": "https://chat.qwen.ai",
        "storage": "token",
    },
    "zai": {
        "origin": "https://chat.z.ai",
        "storage": "token",
    },
    "deepseek": {
        "origin": "https://chat.deepseek.com",
        "storage": "userToken",
        "note": "внутри JSON — берём поле value",
    },
    "perplexity": {
        "origin": "https://www.perplexity.ai",
        "cookies": ["__Secure-next-auth.session-token"],
    },
    "mistral": {
        "origin": "https://chat.mistral.ai",
        "cookies": ["ory_session_mistral"],
    },
    "manus": {
        "origin": "https://manus.im",
        "cookies": ["session_id"],
    },
    "poe": {
        "origin": "https://poe.com",
        "cookies": ["p-b", "p-lat"],
    },
    "alice": {
        "origin": "https://alice.yandex.ru",
        "cookies": ["Session_id"],
    },
    "venice": {
        "origin": "https://venice.ai",
        "cookies": ["__client"],
    },
}


def fetch(path: str, body: dict | None = None):
    url = DEBUG + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=15) as answer:
        return json.loads(answer.read() or b"null")


def connect(url: str):
    """Соединение с отладкой Chrome.

    ``suppress_origin`` обязателен: библиотека шлёт заголовок Origin, а
    Chrome отвергает такие подключения к отладке (403 Forbidden) — защита
    от того, чтобы открытая в браузере страница управляла им же. Нам этот
    заголовок не нужен, и снять его правильнее, чем ослаблять Chrome
    флагом --remote-allow-origins.
    """
    import websocket  # noqa: PLC0415

    return websocket.create_connection(url, timeout=20, suppress_origin=True)


def all_cookies() -> list[dict]:
    """Все куки браузера разом.

    Берутся через отладочный протокол целиком, а не по одной вкладке:
    HttpOnly-куки странице не видны, а нам нужны именно они.
    """
    tabs = [t for t in fetch("/json/list") if t.get("type") == "page"]
    if not tabs:
        raise SystemExit("нет ни одной вкладки — браузер закрыт?")
    socket = connect(tabs[0]["webSocketDebuggerUrl"])
    try:
        socket.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        while True:
            frame = json.loads(socket.recv())
            if frame.get("id") == 1:
                return (frame.get("result") or {}).get("cookies") or []
    finally:
        socket.close()


def storage_of(origin: str, key: str) -> str:
    """Значение из localStorage нужного происхождения.

    Читаем в ЕГО вкладке: localStorage привязан к происхождению, и из чужой
    вкладки он не виден вовсе.
    """
    tabs = [t for t in fetch("/json/list") if t.get("type") == "page"]
    mine = [t for t in tabs if (t.get("url") or "").startswith(origin)]
    if not mine:
        return ""
    socket = connect(mine[0]["webSocketDebuggerUrl"])
    try:
        socket.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": f"localStorage.getItem({key!r}) || ''",
                       "returnByValue": True}}))
        while True:
            frame = json.loads(socket.recv())
            if frame.get("id") == 1:
                got = ((frame.get("result") or {}).get("result") or {})
                return str(got.get("value") or "")
    finally:
        socket.close()


def unwrap(raw: str) -> str:
    """DeepSeek кладёт токен в JSON — достаём поле value."""
    text = (raw or "").strip()
    if not text.startswith("{"):
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    return str(parsed.get("value") or parsed.get("token") or text)


def glue(jar: dict[str, str], names: list[str]) -> str:
    """Склеить куки в порядке, которого ждёт адаптер."""
    parts = [jar.get(name, "") for name in names]
    if not parts[0]:
        return ""
    # Разрезанная кука ChatGPT: половинки лежат под .0 и .1.
    if len(names) == 1 and jar.get(names[0] + ".0"):
        return jar[names[0] + ".0"] + jar.get(names[0] + ".1", "")
    return "|".join(p for p in parts if p) if len(names) > 1 else parts[0]


def tail(value: str) -> str:
    return f"{len(value)} симв, хвост …{value[-6:]}" if value else "—"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true",
                        help="положить найденное в хранилище учёток")
    parser.add_argument("--only", default="",
                        help="через запятую: только эти провайдеры")
    args = parser.parse_args()

    only = {n.strip() for n in args.only.split(",") if n.strip()}
    jar: dict[str, str] = {}
    for cookie in all_cookies():
        jar.setdefault(cookie["name"], cookie["value"])

    found: dict[str, str] = {}
    for name, how in sorted(WANTED.items()):
        if only and name not in only:
            continue
        if how.get("cookies"):
            value = glue(jar, how["cookies"])
        else:
            value = unwrap(storage_of(how["origin"], how["storage"]))
        if value:
            found[name] = value
        where = ("куки " + ", ".join(how["cookies"]) if how.get("cookies")
                 else "localStorage " + how["storage"])
        print(f"{name:<12} {tail(value):<28} {where}")
        if not value and how.get("storage"):
            print(f"{'':<12} вкладка {how['origin']} должна быть ОТКРЫТА — "
                  "localStorage читается только в ней")

    if not args.save:
        print(f"\nнайдено: {len(found)}. Положить в хранилище: --save")
        return

    from foxroute.accounts import Accounts

    store = Accounts()
    for name, value in found.items():
        store.add(name, value)
        print(f"сохранено: {name}")


if __name__ == "__main__":
    main()
