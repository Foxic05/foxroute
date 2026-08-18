"""Писарь: открыть вкладки провайдеров и записывать весь их обмен.

Нужен там, где угадать протокол нельзя, а автоматика не справляется:
всплывающие меню Алисы и Manus не открываются ни программным кликом, ни
настоящей мышью. Поэтому кнопки нажимает человек, а скрипт молча пишет.

    python3 bench/watch_tabs.py                 # все вкладки
    python3 bench/watch_tabs.py alice grok      # только эти

Дальше зайти в браузер по туннелю и понажимать. Записи ложатся в
``/tmp/watch/<имя>.jsonl`` — по строке на событие.

Пишем ТОЛЬКО обмен со службой: статику, счётчики и телеметрию отбрасываем,
иначе нужное тонет. Тела запросов длиннее 4 КБ обрезаем: файл целиком нам
не нужен, нужна форма запроса.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEBUG = "http://127.0.0.1:9977"
OUT = Path("/tmp/watch")

#: Куда идти и что там нажать. Подпись печатается человеку.
PLACES = {
    "alice": ("https://alice.yandex.ru/",
              "меню «88» → «Рассуждать», отправить что угодно; "
              "затем то же с «Исследовать сложную тему»"),
    "grok": ("https://grok.com/",
             "включить «Think» и отправить вопрос (ответ не важен, "
             "нужен сам запрос)"),
    "manus": ("https://manus.im/app",
              "«+» в поле ввода → приложить файл"),
    "qwen": ("https://chat.qwen.ai/",
             "попросить сделать файл, затем НАЖАТЬ ссылку на скачивание"),
    "kimi": ("https://www.kimi.com/",
             "включить поиск в сети и отправить вопрос"),
    "meta_ai": ("https://www.meta.ai/",
                "приложить картинку (если страница вообще откроется)"),
}

#: Мусор, который только мешает читать запись.
SKIP = re.compile(
    r"(\.js|\.css|\.woff2?|\.png|\.jpg|\.svg|\.ico|\.webp|_next/static"
    r"|user_behavior|amplitude|sentry|analytics|googletagmanager|doubleclick"
    r"|metrika|yandex\.ru/clck|favicon)", re.I)

#: Кадры Алисы и Manus крупные: у первой в теле едут списки экспериментов,
#: и на 4 КБ признак режима не помещался — обрезалось самое нужное.
BODY_LIMIT = 32768

#: Что в заголовках прятать. Само имя заголовка оставляем — по нему видно,
#: чем сервис авторизует запрос; значение в записи не нужно.
SECRET = re.compile(r"(auth|cookie|token|secret|sig|key)", re.I)


def clean_headers(headers: dict) -> dict:
    """Заголовки запроса без секретов, но с их именами."""
    out = {}
    for key, value in headers.items():
        text = str(value)
        out[key] = f"<скрыто, {len(text)} знаков>" if SECRET.search(key)             else text[:300]
    return out


def http_json(path: str):
    with urllib.request.urlopen(DEBUG + path, timeout=15) as answer:
        return json.loads(answer.read() or b"null")


def open_tab(url: str, claimed: set[str]) -> dict:
    """Своя вкладка под каждого. Занятые не отдаём дважды.

    Без учёта занятых три писаря садились на ОДНУ пустую вкладку: пока
    первый её не увёл, она для остальных всё ещё выглядела свободной, и в
    трёх файлах оказывался обмен одной и той же страницы.
    """
    host = url.split("//")[1].split("/")[0]
    tabs = http_json("/json/list") or []
    for tab in tabs:
        if tab.get("id") in claimed or tab.get("type") != "page":
            continue
        if host in (tab.get("url") or ""):
            return tab
    for tab in tabs:
        if tab.get("id") in claimed or tab.get("type") != "page":
            continue
        if (tab.get("url") or "") == "about:blank":
            return tab
    request = urllib.request.Request(
        DEBUG + "/json/new?url=" + urllib.parse.quote(url, safe=""),
        method="PUT")
    with urllib.request.urlopen(request, timeout=20) as answer:
        return json.loads(answer.read())


class Writer:
    """Пишет события одной вкладки в свой файл."""

    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url
        self.path = OUT / f"{name}.jsonl"
        self.count = 0

    def put(self, kind: str, data: dict) -> None:
        self.count += 1
        row = {"когда": round(time.time(), 3), "что": kind, **data}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run(self, tab: dict) -> None:
        import websocket  # noqa: PLC0415

        sock = websocket.create_connection(tab["webSocketDebuggerUrl"],
                                           timeout=30, suppress_origin=True)
        ident = [0]

        def call(method, params=None):
            ident[0] += 1
            sock.send(json.dumps({"id": ident[0], "method": method,
                                  "params": params or {}}))

        call("Network.enable", {"maxPostDataSize": BODY_LIMIT})
        call("Page.enable")
        # Уводим вкладку, только если она не там: перезапуск писаря не
        # должен сбивать страницу, на которой человек уже работает.
        here = (tab.get("url") or "")
        if self.url.split("//")[1].split("/")[0] not in here:
            call("Page.navigate", {"url": self.url})
        self.put("начали", {"адрес": self.url})

        pending: dict[str, str] = {}
        while True:
            try:
                frame = json.loads(sock.recv())
            except Exception:
                time.sleep(1)
                continue
            method = frame.get("method") or ""
            params = frame.get("params") or {}

            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = req.get("url", "")
                if SKIP.search(url) or req.get("method") == "OPTIONS":
                    continue
                pending[params.get("requestId", "")] = url
                self.put("запрос", {
                    "способ": req.get("method"),
                    "адрес": url[:400],
                    "заголовки": clean_headers(req.get("headers") or {}),
                    "тело": (req.get("postData") or "")[:BODY_LIMIT],
                })
            elif method == "Network.responseReceived":
                url = pending.get(params.get("requestId", ""), "")
                if not url:
                    continue
                status = ((params.get("response") or {}).get("status"))
                self.put("ответ", {"адрес": url[:300], "код": status})
                # Тело ответа берём только у тех, где оно нам нужно:
                # приёмники файлов возвращают метку, по которой файл потом
                # и подставляется в сообщение.
                if any(w in url for w in ("upload", "rupload", "file")):
                    call("Network.getResponseBody",
                         {"requestId": params.get("requestId")})
            elif not method and frame.get("result"):
                got = (frame.get("result") or {}).get("body")
                if got:
                    self.put("тело ответа", {"текст": got[:BODY_LIMIT]})
            elif method in ("Network.webSocketFrameSent",
                            "Network.webSocketFrameReceived"):
                payload = ((params.get("response") or {}).get("payloadData")
                           or "")
                if len(payload) < 24:
                    continue
                self.put("сокет→" if method.endswith("Sent") else "сокет←",
                         {"тело": payload[:BODY_LIMIT]})


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    names = sys.argv[1:] or list(PLACES)
    unknown = [n for n in names if n not in PLACES]
    if unknown:
        raise SystemExit(f"не знаю таких: {', '.join(unknown)}")

    # Вкладки готовим ПО ОЧЕРЕДИ и в главном потоке: отладочный порт
    # Chrome не выдерживает шести одновременных обращений — потоки на нём
    # молча зависали, и запись не начиналась вовсе.
    ready: list = []
    claimed: set[str] = set()
    for name in names:
        url, hint = PLACES[name]
        try:
            tab = open_tab(url, claimed)
        except Exception as exc:
            print(f"  {name:8} НЕ ОТКРЫЛСЯ: {exc}")
            continue
        claimed.add(tab.get("id"))
        ready.append((name, url, hint, tab))
        time.sleep(1.5)

    for name, url, hint, tab in ready:
        writer = Writer(name, url)
        thread = threading.Thread(target=writer.run, args=(tab,),
                                  daemon=True, name=name)
        thread.start()
        time.sleep(1.5)
        print(f"  {name:8} {url:34} — {hint}")

    print(f"\nПишу в {OUT}. Останавливать: pkill -f watch_tabs")
    while True:
        time.sleep(30)


if __name__ == "__main__":
    main()
