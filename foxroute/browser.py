"""Вход в провайдера через отдельное окно браузера — для «Доступов».

Флоу, который видит человек:
1. Жмёт «Войти» у провайдера — открывается ЧИСТОЕ окно Chrome (свой
   временный профиль, без личных данных и расширений), сразу на нужном
   сайте.
2. Логинится там как обычно.
3. Жмёт «Забрать куки» — отсюда по протоколу отладчика снимается нужная
   кука или поле localStorage, приводится к формату адаптера и уходит в
   хранилище. Окно закрывается, временный профиль удаляется.

Почему отдельное окно, а не его основной браузер: чистый профиль на
свежем порту отладки не трогает личные вкладки и куки, и его не жалко
закрыть. Куки снимаются с адреса ЭТОЙ машины — там же, где потом
работает шлюз, поэтому IP совпадает (см. registry.IP_RISK).

Требуется установленный Chrome/Chromium и пакет ``websocket-client``.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

#: Как снять доступ у каждого провайдера. Режимы совпадают с bench-скриптом:
#:   cookie      — одна кука по имени (``name``)
#:   cookies_bar — несколько кук (``names``) через «|»
#:   cookie_str  — вся строка кук «a=b; c=d»
#:   cookie_json — куки объектом JSON (Venice)
#:   storage     — поле localStorage как есть (``key``)
#:   storage_val — поле localStorage со значением-JSON {"value": …}
GRAB: dict[str, dict] = {
    "chatgpt":     {"url": "https://chatgpt.com", "mode": "cookie",
                    "name": "__Secure-next-auth.session-token"},
    "claude_web":  {"url": "https://claude.ai/login", "mode": "cookie_str"},
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

#: Провайдер → открытая сессия входа {proc, port, profile}. По одному окну
#: на провайдера: повторный «Войти» закрывает прежнее.
_sessions: dict[str, dict] = {}
#: Реентрантный — `launch` держит его и внутри зовёт `close`. Защищает
#: составные операции над `_sessions` от гонки (двойной клик «Войти»,
#: параллельные вкладки): сервер потоковый.
_sessions_lock = threading.RLock()


def _cleanup_all() -> None:
    """Закрыть все окна входа и убрать их профили — на выходе процесса.

    Без этого Ctrl-C/рестарт сервера оставлял бы висеть окна Chrome и их
    временные профили в %TEMP%: в памяти `_sessions` после рестарта пуст,
    и подобрать сирот было бы некому.
    """
    with _sessions_lock:
        for provider in list(_sessions):
            close(provider)


import atexit  # noqa: E402 — рядом с местом использования

atexit.register(_cleanup_all)


def supported(provider: str) -> bool:
    return provider in GRAB


def find_chrome() -> str:
    """Путь к Chrome/Chromium или пустая строка."""
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome", "chrome.exe"):
        found = shutil.which(name)
        if found:
            return found
    candidates: list[str] = []
    if sys.platform == "win32":
        for base in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            root = os.environ.get(base)
            if root:
                candidates.append(
                    os.path.join(root, "Google", "Chrome", "Application",
                                 "chrome.exe"))
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        candidates += ["/opt/google/chrome/chrome",
                       "/usr/bin/chromium-browser", "/usr/bin/chromium"]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(provider: str) -> dict:
    """Открыть чистое окно Chrome на сайте провайдера. Вернуть {port}."""
    plan = GRAB.get(provider)
    if not plan:
        raise RuntimeError(
            f"для {provider} вход через браузер не заведён "
            "(это ключ API или файловый доступ)")
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(
            "Chrome не найден — установи Google Chrome или Chromium, "
            "или заведи доступ вручную по ссылке")

    from foxroute import settings

    # Вход через прокси именно ЭТОГО провайдера, если задан, иначе общий:
    # тогда кука снимается под тем же IP, под которым провайдер и работает.
    # Auth-часть (user:pass) Chrome в --proxy-server не берёт, вносим её через
    # отладчик (см. _auth_thread).
    proxy = settings.provider_proxy(provider) or settings.global_proxy()
    proxy_auth = _proxy_server_arg(proxy)

    # Под локом: закрыть прежнее окно этого провайдера, поднять Chrome и
    # зарегистрировать сессию — атомарно, иначе двойной клик «Войти» оставил
    # бы первое окно сиротой. Долгое ожидание отладчика — уже ВНЕ лока.
    with _sessions_lock:
        close(provider)
        port = _free_port()
        profile = tempfile.mkdtemp(prefix="foxroute-login-")
        args = [chrome, f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}", "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check", "--new-window"]
        if proxy_auth[0]:
            args.append(f"--proxy-server={proxy_auth[0]}")
        args.append("about:blank")
        # ПУСТАЯ вкладка сначала: через отладчик гасим признак автоматизации
        # (и вносим прокси-логин), и только потом идём на сайт провайдера.
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            # Chrome найден, но не запустился (битый exe, блок антивируса). Без
            # этой уборки пустой temp-профиль остался бы сиротой на диске.
            shutil.rmtree(profile, ignore_errors=True)
            raise RuntimeError(f"не удалось запустить Chrome: {exc}") from exc
        _sessions[provider] = {"proc": proc, "port": port, "profile": profile,
                               "proxy_auth": proxy_auth[1]}

    # Ждём, пока отладчик поднимется (до ~8 с).
    for _ in range(40):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            break
        except Exception:  # noqa: BLE001 — ещё стартует
            time.sleep(0.2)

    # Прокси с логином/паролем — поднимаем фоновый ответчик ДО навигации,
    # чтобы первый же запрос через прокси прошёл авторизацию.
    if proxy_auth[1]:
        _auth_thread(port, proxy_auth[1][0], proxy_auth[1][1])

    try:
        _prime(port, plan["url"])
    except Exception:  # noqa: BLE001 — маскировка не критична, откроем как есть
        try:
            _open_url(port, plan["url"])
        except Exception:  # noqa: BLE001 — окно уже открыто, дальше человек сам
            pass
    return {"port": port}


def _proxy_server_arg(proxy: str) -> tuple[str, tuple[str, str] | None]:
    """Разобрать прокси на аргумент ``--proxy-server`` и логин/пароль.

    Chrome в ``--proxy-server`` не принимает ``user:pass`` — их вносим через
    отладчик (см. _auth_thread). Возвращает ``(server, (user, pass)|None)``.
    """
    if not proxy:
        return "", None
    from urllib.parse import urlparse

    u = urlparse(proxy if "://" in proxy else "http://" + proxy)
    if not u.hostname:
        return "", None
    scheme = u.scheme or "http"
    server = f"{scheme}://{u.hostname}"
    if u.port:
        server += f":{u.port}"
    auth = (u.username, u.password or "") if u.username else None
    return server, auth


def _auth_thread(port: int, user: str, password: str) -> None:
    """Фоново отвечать на запрос логина/пароля прокси, пока окно живо.

    Прокси с ``user:pass`` требует ответить на ``Fetch.authRequired`` — иначе
    Chrome показал бы диалог, и человеку пришлось бы вводить пароль руками.
    Слушаем на отдельном ws весь срок сессии.
    """
    import websocket  # noqa: PLC0415

    def loop():
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=5).read())
            page = next((t for t in tabs if t.get("type") == "page"
                         and t.get("webSocketDebuggerUrl")), None)
            if not page:
                return
            ws = websocket.create_connection(
                page["webSocketDebuggerUrl"], timeout=10)
            # Таймаут был нужен только на установку соединения. Дальше поток
            # ЖДЁТ запросов авторизации прокси, между которыми человек читает
            # страницу входа десятки секунд — при таймауте на recv поток умер
            # бы через 10 с молчания, и диалог пароля прокси вылез бы снова.
            ws.settimeout(None)
            ws.send(json.dumps({
                "id": 1, "method": "Fetch.enable",
                "params": {"handleAuthRequests": True,
                           "patterns": [{"urlPattern": "*"}]}}))
            while True:
                frame = json.loads(ws.recv())
                method = frame.get("method")
                params = frame.get("params", {})
                if method == "Fetch.authRequired":
                    ws.send(json.dumps({
                        "id": 9, "method": "Fetch.continueWithAuth",
                        "params": {"requestId": params["requestId"],
                                   "authChallengeResponse": {
                                       "response": "ProvideCredentials",
                                       "username": user,
                                       "password": password}}}))
                elif method == "Fetch.requestPaused":
                    ws.send(json.dumps({
                        "id": 9, "method": "Fetch.continueRequest",
                        "params": {"requestId": params["requestId"]}}))
        except Exception:  # noqa: BLE001 — окно закрыли/сеть, поток просто уходит
            return

    threading.Thread(target=loop, daemon=True).start()


def _prime(port: int, url: str) -> None:
    """Убрать navigator.webdriver ДО загрузки сайта и перейти на него.

    ``addScriptToEvaluateOnNewDocument`` выполняется в каждом новом
    документе ПЕРЕД его скриптами — значит сайт увидит уже «чистый»
    navigator, без признака отладчика. Флагов командной строки не трогаем,
    поэтому и жёлтой плашки Chrome нет.
    """
    import websocket  # noqa: PLC0415

    tabs = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json", timeout=5).read())
    page = next((t for t in tabs if t.get("type") == "page"
                 and t.get("webSocketDebuggerUrl")), None)
    if not page:
        return
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({
            "id": 1, "method": "Page.addScriptToEvaluateOnNewDocument",
            "params": {"source":
                       "Object.defineProperty(navigator,'webdriver',"
                       "{get:()=>undefined});"}}))
        ws.recv()
        ws.send(json.dumps({"id": 2, "method": "Page.navigate",
                            "params": {"url": url}}))
        ws.recv()
    finally:
        ws.close()


def _open_url(port: int, url: str) -> None:
    """Запасной путь: просто открыть URL, если маскировка не удалась."""
    import websocket  # noqa: PLC0415

    tabs = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json", timeout=5).read())
    page = next((t for t in tabs if t.get("type") == "page"
                 and t.get("webSocketDebuggerUrl")), None)
    if not page:
        return
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.navigate",
                            "params": {"url": url}}))
        ws.recv()
    finally:
        ws.close()


def close(provider: str) -> None:
    """Закрыть окно входа и убрать временный профиль."""
    with _sessions_lock:
        sess = _sessions.pop(provider, None)
    if not sess:
        return
    try:
        sess["proc"].terminate()
        try:
            sess["proc"].wait(timeout=5)
        except Exception:  # noqa: BLE001
            sess["proc"].kill()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(sess["profile"], ignore_errors=True)


# ── снятие доступа по CDP ─────────────────────────────────────────────

def _ws(port: int):
    import websocket  # noqa: PLC0415 — не тянем в импорт, если фича не нужна

    try:
        tabs = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=10).read())
    except Exception as exc:  # noqa: BLE001 — отладчик недоступен
        raise RuntimeError(
            "окно входа закрыто или недоступно — нажми «Войти в браузере» "
            "заново, залогинься и НЕ закрывай окно до «Забрать куки»"
        ) from exc
    page = next((t for t in tabs if t.get("type") == "page"
                 and t.get("webSocketDebuggerUrl")), None)
    if not page:
        raise RuntimeError(
            "вкладка входа не найдена — окно закрыли? Открой «Войти в "
            "браузере» заново")
    return websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15)


def _cookies(ws) -> list[dict]:
    ws.send(json.dumps({"id": 1, "method": "Network.getCookies"}))
    return json.loads(ws.recv()).get("result", {}).get("cookies", [])


def _evaluate(ws, expr: str) -> str:
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True}}))
    r = json.loads(ws.recv())
    return r.get("result", {}).get("result", {}).get("value", "") or ""


def _extract(port: int, plan: dict) -> str:
    ws = _ws(port)
    try:
        mode = plan["mode"]
        if mode == "storage":
            return _evaluate(ws, f"localStorage.getItem({plan['key']!r})||''")
        if mode == "storage_val":
            raw = _evaluate(ws, f"localStorage.getItem({plan['key']!r})||''")
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                return raw
            # Ждём объект {"value": …}; но JSON мог оказаться строкой/числом —
            # тогда .get() бросил бы AttributeError мимо except ValueError.
            return parsed.get("value", "") if isinstance(parsed, dict) else raw
        cookies = _cookies(ws)
        jar = {c["name"]: c["value"] for c in cookies}
        if mode == "cookie":
            name = plan["name"]
            if name in jar:
                return jar[name]
            # NextAuth дробит крупную куку (session-token у ChatGPT/Perplexity
            # это делает регулярно) на name.0/name.1/… Сервер собирает их
            # простой конкатенацией по порядку — собираем так же, БЕЗ
            # разделителя: chatgpt потом всё равно стирает '|', а perplexity
            # вставляет значение как есть, и лишний символ сломал бы куку.
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
        raise RuntimeError(f"неизвестный режим {mode}")
    finally:
        ws.close()


def grab(provider: str) -> str:
    """Снять доступ из открытого окна и закрыть его. Вернуть строку доступа."""
    sess = _sessions.get(provider)
    if not sess:
        raise RuntimeError("окно входа не открыто — сначала «Войти»")
    plan = GRAB[provider]
    # close() ВСЕГДА, даже если снятие упало (окно закрыли до «Забрать куки»,
    # Chrome умер): иначе временный профиль оставался бы в %TEMP% навсегда,
    # а протухшая запись — висеть в _sessions.
    try:
        return _extract(sess["port"], plan).strip()
    finally:
        close(provider)
