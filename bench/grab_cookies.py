"""Автоматическое снятие доступа из браузера НА СЕРВЕРЕ через CDP.

Зачем: раньше доступ каждого провайдера доставали руками — F12, Console,
скопировать нужную куку или поле localStorage, вставить в «Доступы».
Здесь это делает скрипт: открывает сайт провайдера в серверном Chrome,
ждёт, пока человек залогинится через noVNC, снимает нужное по протоколу
отладчика и кладёт в хранилище в том формате, которого ждёт адаптер.

    python3 bench/grab_cookies.py claude_web
    python3 bench/grab_cookies.py qwen deepseek kimi     # несколько подряд

Работает с ЛЮБЫМ Chrome, у которого открыт порт отладки, — локальным или
серверным. Адрес отладчика берётся из FOXROUTE_CDP (умолчание
127.0.0.1:9977). Chrome запускается так:

    chrome --remote-debugging-port=9977 --remote-allow-origins=*

ВАЖНО про IP. Куки снимаются с ТОГО адреса, откуда ходит этот Chrome.
Если гонять инструмент там же, где логинишься (свой ноут / тот же
сервер), — IP совпадает, ничего не «переезжает». Если снять на одном
адресе, а крутить на другом (снял локально → залил на сервер), часть
доступов это переживёт, а часть нет — см. docs/credentials.md, раздел про
IP. Проще всего снимать доступ ТАМ ЖЕ, где он будет работать: тогда
вопрос снимается для всех сразу.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

import websocket  # пакет websocket-client

from foxroute.accounts import Accounts

CDP = "http://" + os.environ.get("FOXROUTE_CDP", "127.0.0.1:9977")

#: Снять без ожидания Enter — вкладки уже открыты и человек уже вошёл. Для
#: запуска на сервере, где терминала (stdin) нет. REPLACE вдобавок убирает
#: прежние учётки провайдера, чтобы свежая встала на место протухшей, а не
#: легла второй записью рядом.
NOWAIT = bool(os.environ.get("FOXROUTE_GRAB_NOWAIT"))
REPLACE = bool(os.environ.get("FOXROUTE_GRAB_REPLACE"))

#: Как снять доступ у каждого провайдера. ``mode``:
#:   cookie      — одна кука по имени (``name``)
#:   cookies_bar — несколько кук по именам (``names``) через «|»
#:   cookie_str  — вся строка кук «a=b; c=d»
#:   cookie_json — куки объектом JSON (нужно Venice)
#:   storage      — поле localStorage как есть (``key``)
#:   storage_val  — поле localStorage, где значение это JSON {"value": …}
#:   storage_prefix — поле localStorage по префиксу ключа (Auth0, ``prefix``+``contains``)
GRAB: dict[str, dict] = {
    "chatgpt":     {"url": "https://chatgpt.com", "mode": "cookie",
                    "name": "__Secure-next-auth.session-token"},
    "claude_web":  {"url": "https://claude.ai", "mode": "cookie_str"},
    "gemini_web":  {"url": "https://gemini.google.com", "mode": "cookies_bar",
                    "names": ["__Secure-1PSID", "__Secure-1PSIDTS"]},
    "qwen":        {"url": "https://chat.qwen.ai", "mode": "storage",
                    "key": "token"},
    "deepseek":    {"url": "https://chat.deepseek.com", "mode": "storage_val",
                    "key": "userToken"},
    "kimi":        {"url": "https://www.kimi.com", "mode": "cookie",
                    "name": "refresh_token"},
    "alice":       {"url": "https://alice.yandex.ru", "mode": "cookie",
                    "name": "Session_id"},
    "grok":        {"url": "https://grok.com", "mode": "cookies_bar",
                    "names": ["sso", "sso-rw"]},
    "pi":          {"url": "https://pi.ai/talk", "mode": "cookie_str"},
    "poe":         {"url": "https://poe.com", "mode": "cookie_str"},
    "perplexity":  {"url": "https://www.perplexity.ai", "mode": "cookie",
                    "name": "__Secure-next-auth.session-token"},
    "manus":       {"url": "https://manus.im", "mode": "cookie",
                    "name": "session_id"},
    "venice":      {"url": "https://venice.ai", "mode": "cookie_json"},
    "mistral":     {"url": "https://chat.mistral.ai", "mode": "cookie_str"},
    "bing_images": {"url": "https://www.bing.com/images/create",
                    "mode": "cookie_str"},
    "deepai":      {"url": "https://deepai.org", "mode": "cookie_str"},
}


def _cdp_get(path: str):
    with urllib.request.urlopen(CDP + path, timeout=15) as resp:
        return json.loads(resp.read())


def _open_tab(url: str) -> dict:
    """Открыть вкладку в серверном Chrome (Chrome 151+ требует PUT)."""
    req = urllib.request.Request(
        f"{CDP}/json/new?{urllib.parse.quote(url, safe=':/?=&')}",
        method="PUT")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _find_tab(host: str) -> dict | None:
    for tab in _cdp_get("/json"):
        if host in tab.get("url", ""):
            return tab
    return None


def _cookies(ws) -> list[dict]:
    ws.send(json.dumps({"id": 1, "method": "Network.getCookies"}))
    return json.loads(ws.recv()).get("result", {}).get("cookies", [])


def _evaluate(ws, expr: str) -> str:
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True}}))
    r = json.loads(ws.recv())
    return r.get("result", {}).get("result", {}).get("value", "") or ""


def extract(ws, plan: dict) -> str:
    """Снять доступ по плану провайдера и вернуть строку для хранилища."""
    mode = plan["mode"]

    if mode == "storage":
        return _evaluate(ws, f"localStorage.getItem({plan['key']!r}) || ''")
    if mode == "storage_val":
        raw = _evaluate(ws, f"localStorage.getItem({plan['key']!r}) || ''")
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            return raw
        return parsed.get("value", "") if isinstance(parsed, dict) else raw
    if mode == "storage_prefix":
        # Ключ Auth0 SPA заранее не известен (в нём client_id и scope) —
        # ищем по префиксу и подстроке аудитории, значение отдаём целиком.
        expr = ("(function(){for(var i=0;i<localStorage.length;i++){"
                "var k=localStorage.key(i);"
                "if(k&&k.indexOf(%r)===0&&k.indexOf(%r)!==-1)"
                "return localStorage.getItem(k);}return '';})()"
                % (plan["prefix"], plan["contains"]))
        return _evaluate(ws, expr)

    cookies = _cookies(ws)
    jar = {c["name"]: c["value"] for c in cookies}

    if mode == "cookie":
        name = plan["name"]
        if name in jar:
            return jar[name]
        # NextAuth дробит крупную куку на name.0/name.1/… — сервер собирает
        # их конкатенацией по порядку, без разделителя (см. browser.py).
        parts, i = [], 0
        while f"{name}.{i}" in jar:
            parts.append(jar[f"{name}.{i}"])
            i += 1
        return "".join(parts)
    if mode == "cookies_bar":
        return "|".join(jar.get(n, "") for n in plan["names"])
    if mode == "cookie_str":
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    if mode == "cookie_json":
        return json.dumps(jar)
    raise ValueError(f"неизвестный режим {mode}")


def grab(name: str, store: Accounts) -> bool:
    plan = GRAB.get(name)
    if not plan:
        print(f"  {name}: нет в таблице (это ключ API или файловый доступ — "
              f"заводится иначе)")
        return False

    host = urllib.parse.urlparse(plan["url"]).netloc
    print(f"\n=== {name} ===")
    if NOWAIT:
        tab = _find_tab(host)
        if not tab:
            print(f"  вкладки {host} нет — открой сайт и залогинься, потом повтори")
            return False
    else:
        tab = _find_tab(host) or _open_tab(plan["url"])
        print(f"  вкладка {plan['url']} открыта в Chrome ({CDP}).")
        print(f"  Залогинься в {host} (локально — прямо в окне Chrome; на "
              f"сервере — через noVNC) и нажми Enter здесь…")
        input()
        tab = _find_tab(host)
        if not tab:
            print(f"  {name}: вкладка потерялась — открой сайт заново")
            return False

    # Без заголовка Origin: иначе Chrome, запущенный без
    # --remote-allow-origins, отбивает сокет 403-м. Пустой Origin он пускает.
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=15,
                                     suppress_origin=True)
    try:
        value = extract(ws, plan).strip()
    finally:
        ws.close()

    if not value or value == "|" or all(c in "|;= " for c in value):
        print(f"  {name}: доступ пуст — точно залогинился? (снято {value!r})")
        return False

    if REPLACE:
        # Повторный вход вместо протухшего: убираем прежние учётки, свежая
        # встанет на их место (та же «main», а не второй записью рядом).
        for acc in [a.account for a in store.all(name)]:
            store.remove(name, acc)

    added = store.add(name, value)
    tail = value[-6:] if len(value) > 6 else value
    print(f"  {name}: снято {len(value)} символов (…{tail}), "
          f"в хранилище {len(added)} учётк(а)")
    return True


def main() -> int:
    names = sys.argv[1:] or list(GRAB)
    store = Accounts()
    ok = 0
    for name in names:
        try:
            ok += grab(name, store)
        except Exception as exc:  # noqa: BLE001 — по одному, не роняем пачку
            print(f"  {name}: сбой — {type(exc).__name__}: {exc}")
    print(f"\nготово: {ok} из {len(names)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
