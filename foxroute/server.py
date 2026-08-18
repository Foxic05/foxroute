"""OpenAI-совместимый HTTP-сервер.

Одна команда — и любой существующий клиент подключается к нашим провайдерам:

    python -m foxroute.server --port 8080

Потом:

    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8080/v1", api_key="любой")
    r = client.chat.completions.create(model="qwen", messages=[...])

Или ``curl``:

    curl http://localhost:8080/v1/chat/completions \\
      -H "Content-Type: application/json" \\
      -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}]}'

Поддерживается ``stream=true``: ответ идёт потоком SSE, как у настоящего API.
Что поддержать нельзя честно (``n``, ``logprobs``, ``seed``) — отдаёт
понятную ошибку, а не молча игнорирует.
"""
from __future__ import annotations

import binascii
import hmac
import json
import os
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Iterator


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

from foxroute.accounts import open_provider, default as store
from foxroute.errors import (
    AuthError,
    ContextTooLarge,
    ProviderError,
    RateLimited,
    Unsupported,
)
from foxroute.providers import capabilities_of, implemented
from foxroute.providers.base import (
    Attachment, Conversation, Credential, Request)
from foxroute.quota import default as quota
from foxroute.registry import MULTI_KEY, PROVIDER_CONFIGS, config, is_api
from foxroute.router import Need, default as router
from foxroute.translate import (
    attachments_from_messages, history_chars, messages_to_prompt,
    parse_tool_call, tools_to_instructions, trim_history)
from foxroute.measurements import get as measured


class Handler(BaseHTTPRequestHandler):

    def _auth_ok(self) -> bool:
        """True если пускаем; иначе шлёт 401.

        Модель: ключ закрывает API для ПРОГРАММНЫХ клиентов (чужой проект,
        curl, SDK). СВОЙ интерфейс за туннелем ходит БЕЗ ключа — его защищает
        приватный SSH-туннель и CSRF-проверка origin. Браузер помечает свои
        же запросы ``Sec-Fetch-Site: same-origin`` (подделать это кросс-сайт
        не может), а POST несёт локальный Origin — по ним и узнаём «свой» UI.
        Ключ пуст → авторизация выключена вовсе (локальная разработка).
        """
        from foxroute import settings

        key = settings.api_key()
        if not key:
            return True
        path = self.path.split("?", 1)[0]
        if not (path.startswith("/api/") or path.startswith("/v1/")):
            return True  # статика/шелл — без ключа
        # Свой интерфейс (same-origin за туннелем) — без ключа.
        if self.headers.get("Sec-Fetch-Site") == "same-origin":
            return True
        origin = self.headers.get("Origin") or ""
        if origin:
            from urllib.parse import urlparse
            if (urlparse(origin).hostname or "") in self.LOCAL_HOSTS:
                return True
        # Программный клиент — нужен ключ.
        auth = self.headers.get("Authorization", "")
        given = (auth[7:].strip() if auth[:7].lower() == "bearer "
                 else self.headers.get("x-api-key", "").strip())
        if given and hmac.compare_digest(given, key):
            return True
        self._error(401, "auth_error",
                    "нужен API-ключ: Authorization: Bearer <ключ>")
        return False

    def do_GET(self) -> None:
        if not self._auth_ok():
            return
        if self.path in ("/v1/models", "/v1/models/"):
            self._models()
        elif self.path == "/api/status":
            self._status()
        elif self.path == "/api/accounts":
            self._accounts()
        elif self.path == "/api/settings":
            from foxroute import settings
            self._json(200, {"proxy": settings.global_proxy()})
        elif self.path in ("/", "/index.html"):
            self._static("index.html", "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self._static("app.js", "application/javascript; charset=utf-8")
        elif self.path == "/app.css":
            self._static("app.css", "text/css; charset=utf-8")
        else:
            self._error(404, "not_found", f"unknown path: {self.path}")

    # ── состояние пула ────────────────────────────────────────────────

    def _status(self) -> None:
        """Кто на что годен прямо сейчас.

        Этого нет ни у Open WebUI, ни у LibreChat — им незачем, они ходят в
        API, которые просто работают. У нас двадцать хрупких сессий с
        квотами и паузами, и прятать это от человека значит оставлять его
        гадать, почему ответа нет.
        """
        rows = []
        for row in router.status():
            name = row["provider"]
            caps = capabilities_of(name)
            # Честное число учёток из router.status(): 0 у того, кто без входа
            # не работает и не залогинен. Не выдумываем «default» повторно.
            accounts_n = row["учёток"]
            # «Аноним»: работает БЕЗ входа (none/optional) и реальной записи
            # нет — тогда «1 учётка» была бы обманом, честнее «аноним». Вход
            # по файлу (Copilot, Meta AI) анонимом НЕ считаем: это логин.
            from foxroute.registry import (
                AUTH_NONE, AUTH_OPTIONAL, auth_kind)
            anon = (accounts_n > 0 and not store.usable(name)
                    and auth_kind(name) in (AUTH_NONE, AUTH_OPTIONAL))
            paused = row["паузы"]
            if not accounts_n:
                # Доступа нет вовсе — это не «занят», а «нужен вход».
                state = "locked"
            elif paused:
                state = "paused"
            elif row["свободна"]:
                state = "ready"
            else:
                state = "busy"
            rows.append({
                "id": name,
                "label": config(name).get("label", name),
                "kind": row["вид"],
                "state": state,
                "accounts": accounts_n,
                "anon": anon,
                "auth": auth_kind(name),
                "agentic": row["цикл"],
                "context": row["контекст"],
                "turn": row["ход"],
                "paused": paused,
                # Платного «Авто» обходит — в интерфейсе его надо пометить,
                # иначе непонятно, почему он не выбирается сам.
                "paid": row["платный"],
                "model": row["модель"],
                "models": row["модели"],
                # Умения — чтобы интерфейс не предлагал того, чего не будет.
                # Кнопка «Думать» у провайдера без размышлений или скрепка
                # там, где вложения не уходят, обещают несуществующее и
                # заканчиваются отказом уже после траты сообщения.
                "can": {
                    "thinking": caps.thinking,
                    "web_search": caps.web_search,
                    "deep_research": caps.deep_research,
                    "files": caps.files_in or caps.vision,
                    "files_out": caps.files_out,
                    "images": caps.images_out,
                },
            })

        # Рисующие — отдельным списком: картиночные-без-текста в общий статус
        # не попадают (Need.text), но бару «Рисует» нужны все и с состоянием,
        # иначе список рисовальщиков в интерфейсе врёт (нет Pollinations,
        # locked не помечены). Собираем по способности images_out.
        #
        # ВАЖНО: рисование почти везде требует ВХОДА, даже там, где ЧАТ у
        # провайдера работает анонимно. DeepAI на анонима отдаёт 401 на
        # картиночные модели, Алисе нужен кадр-шаблон и кука. Поэтому «готов
        # рисовать» строже, чем chat-доступность (см. draw_ready ниже): нужна
        # реальная учётка / беспарольный / файловый вход. Иначе бар предлагал
        # optional-рисовальщиков, которые тут же падали «нет учётки»/401.
        from foxroute.registry import AUTH_FILE, AUTH_NONE, auth_kind

        draw = []
        for name in sorted(implemented()):
            if not capabilities_of(name).images_out:
                continue
            kind = auth_kind(name)
            usable = bool(store.usable(name))
            # «Готов рисовать»: реальная учётка ИЛИ беспарольный (AUTH_NONE,
            # Pollinations) ИЛИ файловый вход с файлами на месте (Meta AI,
            # MS Copilot рисуют по токену-файлу — store.usable у них пуст, но
            # это вход, не аноним). Optional/ключевые без записи — «нужен вход»
            # (DeepAI на анонима отдаёт 401, Алисе нужен кадр-шаблон).
            draw_ready = (usable or kind == AUTH_NONE
                          or (kind == AUTH_FILE and not store.missing_parts(name)))
            draw.append({
                "id": name,
                "label": config(name).get("label", name),
                "state": "ready" if draw_ready else "locked",
                # «Аноним» только у беспарольного (Pollinations); учётка и
                # файловый вход — это логин, не аноним.
                "anon": draw_ready and not usable and kind == AUTH_NONE,
            })
        self._json(200, {"providers": rows, "draw": draw})

    # ── управление учётками ───────────────────────────────────────────
    #
    # Тот же набор действий над доступами, что у ``python -m foxroute``,
    # только по HTTP — плюс подсказка, где брать доступ.
    #
    # **Значение доступа наружу не отдаётся никогда.** В списке только хвост
    # в четыре символа — его хватает, чтобы отличить одну учётку от другой,
    # и не хватает, чтобы воспользоваться чужой.

    @staticmethod
    def _account_row(entry) -> dict:
        value = entry.value or ""
        return {
            "account": entry.account,
            "tail": value[-4:] if len(value) > 4 else "",
            "enabled": entry.enabled,
            "reason": entry.disabled_reason,
            "rotated": bool(entry.rotated),
            "added_at": entry.added_at,
            "note": entry.note,
            "proxy": entry.proxy,
        }

    def _accounts(self) -> None:
        """Учётки по провайдерам вместе с подсказкой, где взять доступ."""
        from foxroute import settings
        from foxroute.registry import access_hint, auth_kind, ip_risk

        def _browser_login(name: str) -> bool:
            from foxroute import browser
            return browser.supported(name)

        rows = []
        for name in sorted(implemented()):
            cfg = config(name)
            pool = store.all(name)
            rows.append({
                "id": name,
                "label": cfg.get("label", name),
                "kind": "api" if is_api(name) else "web",
                "auth": auth_kind(name),
                "ready": store.ready(name),
                "missing": store.missing_parts(name),
                # Несколько значений через | — это ПУЛ у API-ключей и
                # СКЛЕЙКА у веб-сессий. Интерфейсу надо знать разницу,
                # иначе он предложит не то.
                "multi_key": name in MULTI_KEY,
                # Привязка доступа к IP — для маркера в «Доступах».
                "ip": ip_risk(name),
                # Прокси, заданный на этого провайдера (для входа и запросов).
                "proxy": settings.provider_proxy(name),
                # Можно ли завести доступ кнопкой «Войти в браузере».
                "browser_login": _browser_login(name),
                "hint": access_hint(name),
                "accounts": [self._account_row(a) for a in pool],
            })
        self._json(200, {"providers": rows})

    def _accounts_action(self, action: str) -> None:
        """add | remove | toggle | check — по одной учётке за раз."""
        body = self._read_body()
        if body is None:
            return

        provider = str(body.get("provider") or "").strip()
        if not provider:
            return self._error(400, "invalid_request", "нужно поле provider")
        if provider not in implemented():
            return self._error(404, "not_found",
                               f"нет такого провайдера: {provider}")

        account = str(body.get("account") or "").strip()

        if action == "add":
            value = str(body.get("value") or "").strip()
            if not value:
                return self._error(400, "invalid_request",
                                   "нужно поле value — сам доступ")
            added = store.add(provider, value, account,
                              str(body.get("proxy") or ""),
                              str(body.get("note") or ""))
            return self._json(200, {
                "added": [self._account_row(a) for a in added],
                "missing": store.missing_parts(provider),
            })

        if action == "remove":
            if not account:
                return self._error(400, "invalid_request", "нужно поле account")
            found = store.remove(provider, account)
            if not found:
                return self._error(404, "not_found", "такой учётки нет")
            return self._json(200, {"removed": True})

        if action == "toggle":
            if not account:
                return self._error(400, "invalid_request", "нужно поле account")
            enabled = bool(body.get("enabled", True))
            reason = str(body.get("reason") or "выключено вручную")
            store.set_enabled(provider, account, enabled, reason)
            return self._json(200, {"enabled": enabled})

        if action == "set-proxy":
            if not account:
                return self._error(400, "invalid_request", "нужно поле account")
            proxy = str(body.get("proxy") or "").strip()
            if not store.set_proxy(provider, account, proxy):
                return self._error(404, "not_found", "такой учётки нет")
            return self._json(200, {"proxy": proxy})

        if action == "provider-proxy":
            # Прокси на всего провайдера: и на вход через браузер, и на его
            # запросы (учётка своим прокси может это переопределить).
            from foxroute import settings

            proxy = str(body.get("proxy") or "").strip()
            settings.set_provider_proxy(provider, proxy)
            return self._json(200, {"proxy": proxy})

        if action == "check":
            # Проверка тратит сообщение из квоты — это не бесплатное
            # действие, и интерфейс обязан предупредить об этом до нажатия.
            from foxroute.health import default as canary

            try:
                verdict = canary.check(provider, account)
            except Exception as exc:  # noqa: BLE001 — до человека доносим всё
                return self._error(502, "provider_error", str(exc))
            return self._json(200, {
                "state": verdict.state,
                "detail": verdict.detail,
                "needs_hands": verdict.needs_hands,
                "account": verdict.account,
            })

        if action == "login":
            # Открыть чистое окно браузера на сайте провайдера — человек
            # там залогинится, потом нажмёт «Забрать куки» (action grab).
            from foxroute import browser

            if not browser.supported(provider):
                return self._error(
                    400, "unsupported",
                    "вход через браузер тут не нужен — это ключ API или "
                    "файловый доступ, заводится вручную")
            try:
                info = browser.launch(provider)
            except Exception as exc:  # noqa: BLE001 — до человека доносим всё
                return self._error(502, "provider_error", str(exc))
            return self._json(200, info)

        if action == "grab":
            # Снять доступ из открытого окна, добавить в хранилище и сразу
            # проверить на живом — чтобы человек увидел «работает» или нет.
            from foxroute import browser
            from foxroute.health import default as canary

            try:
                value = browser.grab(provider)
            except Exception as exc:  # noqa: BLE001
                return self._error(502, "provider_error", str(exc))
            if not value or all(c in "|;= " for c in value):
                return self._error(
                    400, "invalid_request",
                    "доступ пуст — похоже, вход не завершён; залогинься в "
                    "открытом окне и попробуй снова")
            try:
                added = store.add(provider, value)
            except ValueError as exc:
                # store.add кидает ValueError на пустом/дублирующем доступе —
                # это внятный 400, а не 500 с трейсбеком.
                return self._error(400, "invalid_request", str(exc))
            account = added[0].account if added else account
            result = {"added": [self._account_row(a) for a in added],
                      "missing": store.missing_parts(provider)}
            # Автопроверка: тратит сообщение, но человек сам нажал «забрать»
            # — ему важно увидеть, ожил ли доступ.
            try:
                verdict = canary.check(provider, account)
                result["check"] = {"state": verdict.state,
                                   "detail": verdict.detail}
            except Exception as exc:  # noqa: BLE001 — доступ добавлен, проверка нет
                result["check"] = {"state": "unknown", "detail": str(exc)}
            return self._json(200, result)

        return self._error(404, "not_found", f"неизвестное действие: {action}")

    def _settings_action(self, action: str) -> None:
        """proxy — сохранить общий прокси; test-proxy — проверить связь."""
        from foxroute import settings

        body = self._read_body()
        if body is None:
            return
        proxy = str(body.get("proxy") or "").strip()

        if action == "proxy":
            settings.put("proxy", proxy)
            return self._json(200, {"proxy": settings.global_proxy()})

        if action == "test-proxy":
            # Пробный запрос ЧЕРЕЗ прокси на нейтральный адрес — вернуть
            # видимый IP, чтобы человек убедился, что трафик реально идёт
            # через прокси, а не напрямую.
            from foxroute.providers import _http

            if not proxy:
                return self._error(400, "invalid_request",
                                   "нужен адрес прокси для проверки")
            session = _http.session()
            try:
                session.proxies = {"http": proxy, "https": proxy}
                resp = _http.request(session, "GET",
                                     "https://api.ipify.org?format=json",
                                     provider="proxy-test", timeout=20)
                ip = ""
                try:
                    ip = (resp.json() or {}).get("ip", "")
                except ValueError:
                    ip = resp.text[:40]
                return self._json(200, {"ok": resp.status_code == 200,
                                        "ip": ip})
            except Exception as exc:  # noqa: BLE001 — до человека доносим всё
                return self._json(200, {"ok": False, "error": str(exc)[:150]})

        return self._error(404, "not_found",
                           f"неизвестное действие: {action}")

    def _static(self, name: str, content_type: str) -> None:
        from pathlib import Path

        path = Path(__file__).parent / "web" / name
        if not path.exists():
            return self._error(404, "not_found", f"нет файла {name}")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # no-store, а не no-cache: браузер не должен держать старый
        # app.js/css и показывать половину дизайна.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._auth_ok():
            return
        # Прокси запроса — потоко-локальный (open_provider его выставляет).
        # Сейчас безопасно (поток на запрос, keep-alive выкл), но сбрасываем
        # на входе защитно: если однажды включат HTTP/1.1/пул потоков, запрос,
        # не дошедший до open_provider, не унаследует чужой прокси с потока.
        from foxroute import settings

        settings.set_request_proxy(None)
        # Общая сетка под всеми обработчиками. Ловим ЛЮБОЕ исключение, а
        # не только наши типы: адаптер может уронить что угодно чужое
        # (сеть, разбор, библиотека провайдера), и раньше такое улетало
        # мимо обработчика — клиент получал оборванное соединение и
        # гадал, что случилось. Внятный 500 лучше пустоты.
        try:
            self._route_post()
        except Exception as exc:  # noqa: BLE001 — последний рубеж
            import traceback

            traceback.print_exc()
            try:
                self._error(500, "internal_error",
                            f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 — ответ уже начат, писать некуда
                pass

    def _route_post(self) -> None:
        # CSRF: чужая браузерная страница не должна дёргать наши POST — они
        # заводят/удаляют доступы и тратят квоту. «Простой» кросс-сайт POST
        # (text/plain + JSON-тело) preflight не вызывает, поэтому режем по
        # Origin здесь, а не только опускаем ACAO-заголовок в ответе.
        # curl/SDK без Origin проходят — это не браузер.
        if self._origin_hostile():
            return self._error(403, "forbidden",
                               "cross-origin POST отклонён")
        if self.path in ("/v1/chat/completions", "/v1/chat/completions/"):
            self._completions()
        elif self.path in ("/v1/images/generations", "/v1/images/generations/"):
            self._images()
        elif self.path in ("/v1/audio/transcriptions",
                           "/v1/audio/transcriptions/"):
            self._transcribe()
        elif self.path in ("/v1/audio/speech", "/v1/audio/speech/"):
            self._speak()
        elif self.path.startswith("/api/accounts/"):
            self._accounts_action(self.path.rsplit("/", 1)[-1])
        elif self.path.startswith("/api/settings/"):
            self._settings_action(self.path.rsplit("/", 1)[-1])
        else:
            self._error(404, "not_found", f"unknown path: {self.path}")

    # ── /v1/models ────────────────────────────────────────────────────

    def _models(self) -> None:
        # «auto» первым: это не провайдер, а маршрут с фолбэком по цепочке,
        # и для большинства применений он и нужен.
        models = [{
            "id": "auto",
            "object": "model",
            "created": 0,
            "owned_by": "foxroute (маршрутизатор с фолбэком)",
        }]
        for name in sorted(implemented()):
            # В /v1/models — только текстовые чат-модели. Картиночные и
            # TTS-движки (openai_fm, text=False) сюда не годятся: клиент
            # запросил бы их как chat-модель и получил бы отказ.
            if not capabilities_of(name).text:
                continue
            cfg = config(name)
            label = cfg.get("label", name)
            models.append({
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": label,
            })
            # Groq: отдельные модели тоже доступны напрямую
            for model_id in (cfg.get("models") or {}):
                if (cfg["models"][model_id]).get("audio"):
                    continue
                models.append({
                    "id": f"{name}/{model_id}",
                    "object": "model",
                    "created": 0,
                    "owned_by": label,
                })
        self._json(200, {"object": "list", "data": models})

    # ── /v1/chat/completions ──────────────────────────────────────────

    #: Потолок на одно вложение, скачанное по ссылке. Ссылку даёт клиент,
    #: и без потолка он мог бы (пусть и по неосторожности) занять всю
    #: память шлюза одним запросом.
    MAX_FETCH_BYTES = 32 * 1024 * 1024

    def _standard_attachments(self, messages: list) -> list:
        """Вложения из тела в форме OpenAI, готовые к отправке.

        Разбор формата — в ``translate``; здесь остаётся то, что ему не
        положено знать: скачивание по ссылке с потолком и таймаутом.
        """
        found = attachments_from_messages(messages)
        if not found:
            return []

        ready = []
        for item in found:
            data = item.get("data")
            mime = item.get("mime") or ""
            if data is None:
                data, mime = self._fetch_attachment(item["url"], mime)
            if not data:
                continue
            ready.append(Attachment(
                kind="image" if (item.get("kind") == "image"
                                 or mime.startswith("image/")) else "file",
                data=data,
                filename=item.get("filename") or "",
                mime=mime or "application/octet-stream",
            ))
        return ready

    def _fetch_attachment(self, url: str, mime: str) -> tuple[bytes, str]:
        """Скачать вложение по ссылке. Пусто — если не вышло.

        Ссылку даёт КЛИЕНТ, а ходит по ней СЕРВЕР своей сетевой позицией —
        поэтому SSRF-защита: только публичные http(s)-адреса, отказ на
        loopback/приватные/link-local/метаданные (169.254.169.254 — наш же
        127.0.0.1:порт/api/accounts), и НЕ следуем редиректам (3xx мог бы
        увести на внутренний адрес мимо первичной проверки).
        """
        import ipaddress
        import socket
        import urllib.error
        import urllib.request
        from urllib.parse import urlparse

        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            raise ValueError(f"схема {u.scheme!r} запрещена — только http(s)")
        if not u.hostname:
            raise ValueError("в ссылке нет хоста")
        try:
            infos = socket.getaddrinfo(u.hostname, u.port,
                                       proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise ValueError(f"хост не резолвится: {exc}") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                raise ValueError(
                    f"внутренний адрес {ip} — качать по нему нельзя")

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None  # редирект увёл бы мимо проверки — блокируем

        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(
            url, headers={"User-Agent": "foxroute"})
        try:
            with opener.open(request, timeout=60) as answer:
                declared = answer.headers.get("Content-Length")
                if declared and int(declared) > self.MAX_FETCH_BYTES:
                    raise ValueError(
                        f"вложение по ссылке больше "
                        f"{self.MAX_FETCH_BYTES // 1024 // 1024} МБ: {url[:80]}")
                # Читаем на байт больше потолка: так видно, что его перебрали,
                # даже когда длину не объявили заранее.
                blob = answer.read(self.MAX_FETCH_BYTES + 1)
                kind = mime or answer.headers.get_content_type() or ""
        except ValueError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ValueError(
                f"не удалось скачать вложение {url[:80]}: {exc}") from exc

        if len(blob) > self.MAX_FETCH_BYTES:
            raise ValueError(
                f"вложение по ссылке больше "
                f"{self.MAX_FETCH_BYTES // 1024 // 1024} МБ: {url[:80]}")
        return blob, kind

    def _completions(self) -> None:
        body = self._read_body()
        if body is None:
            return

        # Валидация
        model_raw = body.get("model", "")
        messages = body.get("messages")
        if not model_raw or not messages:
            return self._error(400, "invalid_request",
                               "model and messages are required")
        # model обязан быть строкой: число/список прошли бы проверку на
        # пустоту, а дальше `"/" in model_raw` дал бы TypeError → 500.
        if not isinstance(model_raw, str):
            return self._error(400, "invalid_request", "model must be a string")
        # messages обязан быть списком: строка/словарь/число прошли бы
        # проверку на пустоту, а дальше reversed()/.get() дали бы 500
        # вместо внятного 400.
        if not isinstance(messages, list):
            return self._error(400, "invalid_request",
                               "messages must be a list")
        # ...и каждый элемент — объектом: `["hi"]` прошёл бы проверку списка,
        # а затем m.get("content")/.get("role") упали бы AttributeError → 500.
        if not all(isinstance(m, dict) for m in messages):
            return self._error(400, "invalid_request",
                               "каждый элемент messages должен быть объектом")
        # Отбиваем по ЗНАЧЕНИЮ, а не по наличию ключа. Клиенты вроде
        # LangChain и LiteLLM кладут ``n: 1`` в каждый запрос по
        # умолчанию, а ``logprobs: false`` означает «не нужны» — иначе
        # оба получали бы 400 на ровном месте, хотя ничего невозможного не
        # просили. Отказываем только тому, чего мы правда не умеем.
        if body.get("n") not in (None, 1):
            return self._error(400, "unsupported_parameter",
                               "n > 1 is not supported by web sessions")
        if body.get("logprobs"):
            return self._error(400, "unsupported_parameter",
                               "logprobs is not supported by web sessions")
        if body.get("seed") is not None:
            return self._error(400, "unsupported_parameter",
                               "seed is not supported by web sessions")

        # model может быть "qwen", "groq/llama-3.3-70b-versatile" или "auto"
        if "/" in model_raw and model_raw.split("/", 1)[0] in PROVIDER_CONFIGS:
            provider_name, sub_model = model_raw.split("/", 1)
        else:
            provider_name, sub_model = model_raw, ""

        # Неизвестное имя модели — это 404 model_not_found, а НЕ 401. Иначе
        # такой запрос дошёл бы до open_provider, тот не нашёл бы учётки и
        # поднял бы AuthError → клиент получил бы 401 и решил, что дело в
        # ключе: LiteLLM и SDK на 401 не делают фолбэк на другую модель. По
        # стандарту OpenAI неизвестная модель — 404 с code model_not_found.
        if (provider_name not in ("auto", "best")
                and provider_name not in PROVIDER_CONFIGS):
            return self._error(
                404, "model_not_found",
                f"нет такой модели: {model_raw!r}. Доступны: auto, best, "
                + ", ".join(sorted(PROVIDER_CONFIGS))
                + " (или в форме provider/model)")

        # Не-текстовый провайдер (картиночный/TTS) для ЧАТА — внятный отказ,
        # а не падение в глубине адаптера, у которого нет текстового потока.
        # Картинки идут на /v1/images/generations, озвучка — на /v1/audio.
        if (provider_name not in ("auto", "best")
                and not capabilities_of(provider_name).text):
            return self._error(
                404, "model_not_found",
                f"модель {model_raw!r} не для чата — это картиночный или "
                "TTS-провайдер; выбери текстовую модель или auto (картинки — "
                "/v1/images/generations, озвучка — /v1/audio/speech)")

        stream = body.get("stream", False)

        # Серверная беседа: когда выбран КОНКРЕТНЫЙ провайдер, умеющий вести
        # чат на своей стороне, и клиент прислал ручку беседы — шлём только
        # новое сообщение, контекст держит провайдер. При «auto» нельзя:
        # следующий ход может ответить другой провайдер, а чат чужой.
        conv_data = body.get("conversation")
        conversation = None
        use_conv = (
            provider_name not in ("auto", "best")
            and isinstance(conv_data, dict)
            and capabilities_of(provider_name).conversations)

        if provider_name in ("auto", "best"):
            window = 200_000
        else:
            window = measured(provider_name).context_chars or 200_000

        if use_conv:
            last_user = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    last_user = messages_to_prompt([m])
                    break
            prompt = last_user or messages_to_prompt(messages)
            conversation = Conversation(
                provider=provider_name,
                chat_id=str(conv_data.get("chat_id") or ""),
                last_message_id=str(conv_data.get("last_message_id") or ""))
        else:
            messages = trim_history(messages, window)
            prompt = messages_to_prompt(messages)

        # Инструменты. Веб-сессии не умеют вызывать их «по-настоящему»:
        # у них нет протокола функций, есть только текст. Поэтому список
        # описывается ПОСТРОЧНО и приписывается к промпту, а вызов
        # вылавливается из ответа.
        #
        # Построчно, а не JSON-схемой, и это не вкусовщина: на схемах
        # ChatGPT, Copilot, Meta AI и Manus отказываются работать —
        # принимают их за попытку подменить среду выполнения.
        #
        # Без этого поле ``tools`` осталось бы лишь признаком «нужен
        # провайдер для агентного цикла», сами инструменты не доезжали бы
        # ни туда, ни обратно: клиент получал бы обычный текст и
        # ``tool_calls: null``, а его цикл ломался бы молча.
        tools = body.get("tools") or []
        described = tools_to_instructions(tools) if tools else ""
        if described:
            prompt = described + "\n\n" + prompt


        # Вложения приходят двумя дорогами, и понимать надо обе.
        #
        # Своя, короткая, — поле ``attachments`` из нашего интерфейса:
        # [{data: "base64…", filename, mime}].
        #
        # Чужая, стандартная, — частями сообщения, как это делает
        # openai-python: {"type": "image_url", …}. Без её разбора она
        # молча терялась бы, и картинка до провайдера не доезжала.
        attachments = []
        for att in body.get("attachments") or []:
            import base64 as b64
            raw = att.get("data", "")
            try:
                # ``validate=False`` терпит переносы строк, но не спасает
                # от мусора: кривая строка иначе уронила бы обработчик, и
                # клиент получил бы оборванное соединение вместо объяснения.
                data = b64.b64decode(raw) if raw else None
            except (ValueError, TypeError) as exc:
                return self._error(400, "invalid_request",
                                   f"вложение не разобрать как base64: {exc}")
            attachments.append(Attachment(
                kind="image" if (att.get("mime") or "").startswith("image/")
                     else "file",
                data=data,
                filename=att.get("filename", ""),
                mime=att.get("mime", ""),
            ))
        try:
            attachments += self._standard_attachments(body.get("messages") or [])
        except ValueError as exc:
            return self._error(400, "invalid_request", str(exc))

        request = Request(
            prompt=prompt,
            model=sub_model or None,
            max_tokens=body.get("max_tokens"),
            temperature=body.get("temperature"),
            thinking=bool(body.get("thinking")),
            # Эти два поля легко потерять: если интерфейс их шлёт, а сервер
            # не читает, кнопка поиска молча ничего не делает — провайдер
            # получает обычный запрос. Проверять умения надо на том пути,
            # которым ходит человек, а не только вызовом адаптера.
            web_search=bool(body.get("web_search")),
            deep_research=bool(body.get("deep_research")),
            # Исследование идёт МИНУТАМИ — дольше обычного потолка в 300
            # секунд: ответ обрывался бы у самой цели. Ждём дольше только
            # когда его действительно попросили.
            timeout=900.0 if body.get("deep_research") else 300.0,
            attachments=attachments,
            conversation=conversation,
            tools=tools,
        )

        # stream_options.include_usage: клиент (LiteLLM-прокси со счётом
        # стоимости) хочет финальный usage-чанк в потоке. Готовим накопитель
        # на self (хендлер — на запрос); _send_chunk копит, перед [DONE]
        # шлём usage-кадр. Без флага накопителя нет — ничего не меняется.
        if stream and (body.get("stream_options") or {}).get("include_usage"):
            self._usage = {"include": True, "prompt_chars": len(prompt or ""),
                           "chars": 0, "id": None, "model": None}

        # «auto» и «best» идут через маршрутизатор с фолбэком по цепочке;
        # названный провайдер — только он, без замен. Явный выбор человека
        # подменять нельзя: он мог просить конкретную модель осознанно.
        if provider_name in ("auto", "best"):
            # exclude — «ответить заново другим»: не брать того, кто уже
            # отвечал. Имена сверяем по id провайдера.
            exclude = body.get("exclude") or []
            # Размер входа отдаём маршрутизатору: он не пошлёт длинный
            # диалог тому, у кого окно меньше, — сэкономит попытку и 413.
            # Умения передаём маршрутизатору, а не только провайдеру:
            # иначе «авто» с просьбой исследовать выбирало того, кто
            # исследовать не умеет, и отказ приходил уже после обращения
            # к сервису — то есть после траты сообщения из нормы.
            need = Need(agentic=bool(body.get("tools")),
                        thinking=request.thinking,
                        web_search=request.web_search,
                        deep_research=request.deep_research,
                        vision=any(a.kind == "image" for a in attachments),
                        files_in=bool(attachments),
                        exclude=frozenset(exclude),
                        context_chars=history_chars(messages))
            if stream:
                self._stream_routed(request, need, model_raw)
            else:
                self._complete_routed(request, need, model_raw)
            return

        try:
            provider = open_provider(provider_name, store=store,
                                     model=sub_model, allow_anonymous=True)
        except AuthError as exc:
            return self._error(401, "auth_error", str(exc))
        except ProviderError as exc:
            return self._error(400, "provider_error", str(exc))

        # Слот занятости берём и на прямом пути. У веб-сессии один
        # разговор за раз, и маршрутизатор это соблюдает; без слота на
        # прямом пути две вкладки с одним и тем же mistral грузили бы одну
        # куку одновременно, и сервис отвечал бы вперемешку.
        account = getattr(provider.credential, "account", "") or "default"
        slots = 1 if not is_api(provider_name) else 4
        if not router.busy.take(provider_name, account, slots):
            provider.close()
            return self._error(
                429, "rate_limited",
                f"{provider_name}: этот провайдер сейчас занят другим "
                "запросом — веб-сессия ведёт один разговор за раз")

        fallback = (messages, window) if use_conv else None
        try:
            if stream:
                self._stream(provider, request, model_raw,
                             conv_fallback=fallback)
            else:
                self._complete(provider, request, model_raw,
                               conv_fallback=fallback)
        finally:
            router.busy.release(provider_name, account)
            # Закрываем ВСЕГДА. Адаптер Gemini держит на каждый экземпляр
            # вечный поток с циклом событий, и без закрытия каждый запрос
            # навсегда добавлял поток и открытое соединение.
            try:
                provider.close()
            except Exception:  # noqa: BLE001 — закрытие не должно ронять ответ
                pass

    # ── через маршрутизатор ───────────────────────────────────────────

    def _complete_routed(self, request: Request, need: Need,
                         model: str) -> None:
        # При auto/best человек вправе знать, КТО ответил: кладём в model имя
        # ответившего (при фолбэке — не того, к кому пошли первым), а не «auto».
        picked = {"name": model}

        def remember(provider: str, account: str) -> None:
            picked["name"] = provider

        try:
            text = router.complete(request, need, on_pick=remember)
        except RateLimited as exc:
            return self._error(429, "rate_limited", str(exc))
        except Unsupported as exc:
            return self._error(400, "unsupported", str(exc))
        except ProviderError as exc:
            return self._error(502, "provider_error", str(exc))
        self._json(200, self._completion_body(
            text, picked["name"], tools=request.tools,
            prompt_chars=len(request.prompt or "")))

    def _stream_routed(self, request: Request, need: Need,
                       model: str) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self._begin_stream()

        # Когда просили «auto», человек вправе знать, КТО в итоге ответил:
        # в этом весь смысл маршрутизатора. Кладём в поле model ЧИСТОЕ имя
        # ответившего (при фолбэке — не того, к кому пошли первым), чтобы его
        # можно было скопировать в запрос и повторить — как делает OpenAI.
        picked = {"name": model}

        def remember(provider: str, account: str) -> None:
            picked["name"] = provider

        # С инструментами текст ПРИДЕРЖИВАЕМ и на auto-пути тоже: иначе
        # агентный клиент, попросивший «auto» + tools в потоке, получал
        # сырой текст вместо tool_calls — в отличие от named-пути (_stream).
        buffer: list[str] = []
        hold = bool(request.tools)
        try:
            for kind, piece in router.stream_rich(request, need,
                                                    on_pick=remember):
                if kind == "thinking":
                    self._send_chunk(completion_id, picked["name"],
                                     {"thinking": piece})
                elif hold:
                    buffer.append(piece)
                else:
                    self._send_chunk(completion_id, picked["name"],
                                     {"content": piece})
        except RateLimited as exc:
            # Сюда попадаем, только когда лимит исчерпан у всех живых —
            # маршрутизатор уже перебрал цепочку.
            self._send_raw({"error": {
                "message": str(exc), "type": "rate_limited",
                "retry_after": exc.retry_after}})
            self._end_stream()
            return
        except ProviderError as exc:
            self._send_raw({"error": {"message": str(exc),
                                      "type": "provider_error"}})
            self._end_stream()
            return
        except Exception as exc:  # noqa: BLE001 — обрыв сети/чужая библиотека
            self._stream_broke(exc)
            return

        finish = "stop"
        if hold:
            whole = "".join(buffer)
            call = parse_tool_call(whole, request.tools)
            if call:
                self._send_chunk(completion_id, picked["name"],
                                 {"tool_calls": [{"index": 0, **call}]})
                finish = "tool_calls"
            elif whole:
                self._send_chunk(completion_id, picked["name"],
                                 {"content": whole})
        self._send_chunk(completion_id, picked["name"], {}, finish=finish)
        self._send_usage_chunk()
        self._end_stream()

    def _complete(self, provider, request: Request, model: str,
                  conv_fallback=None) -> None:
        try:
            text = provider.complete(request)
        except RateLimited as exc:
            quota.handle_rate_limited(
                provider.name, provider.credential.account,
                exc.retry_after, exc.is_budget, str(exc))
            return self._error(429, "rate_limited", str(exc))
        except AuthError as exc:
            return self._error(401, "auth_error", str(exc))
        except ContextTooLarge as exc:
            return self._error(413, "context_too_large", str(exc))
        except Unsupported as exc:
            return self._error(400, "unsupported", str(exc))
        except ProviderError as exc:
            if conv_fallback and request.conversation:
                return self._complete_conv_retry(
                    provider, request, model, conv_fallback)
            return self._error(502, "provider_error", str(exc))

        quota.record(provider.name, provider.credential.account)
        self._json(200, self._completion_body(
            text, model, request.conversation, request.tools,
            prompt_chars=len(request.prompt or "")))

    def _stream(self, provider, request: Request, model: str,
                conv_fallback=None) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self._begin_stream()
        produced = False
        # С инструментами текст ПРИДЕРЖИВАЕМ. Вызов виден только когда
        # ответ дописан до конца: отдать его по кускам, а в конце сказать
        # «на самом деле это был вызов функции» нельзя — клиент уже
        # показал бы человеку служебную строку как ответ.
        buffer: list[str] = []
        hold = bool(request.tools)
        try:
            for kind, piece in provider.stream_rich(request):
                produced = True
                if kind == "thinking":
                    self._send_chunk(completion_id, model,
                                     {"thinking": piece})
                elif hold:
                    buffer.append(piece)
                else:
                    self._send_chunk(completion_id, model,
                                     {"content": piece})
        except RateLimited as exc:
            quota.handle_rate_limited(
                provider.name, provider.credential.account,
                exc.retry_after, exc.is_budget, str(exc))
            self._send_raw({"error": {
                "message": str(exc), "type": "rate_limited",
                "retry_after": exc.retry_after}})
            return self._end_stream()
        except ProviderError as exc:
            if not produced and conv_fallback and request.conversation:
                self._stream_conv_retry(
                    provider, request, model, conv_fallback, completion_id)
                return
            self._send_raw({"error": {"message": str(exc),
                                      "type": "provider_error"}})
            return self._end_stream()
        except Exception as exc:  # noqa: BLE001 — обрыв сети/чужая библиотека
            return self._stream_broke(exc)

        quota.record(provider.name, provider.credential.account)
        conv = request.conversation
        extra = None
        if conv and conv.chat_id:
            extra = {"conversation": {
                "provider": conv.provider,
                "chat_id": conv.chat_id,
                "last_message_id": conv.last_message_id}}

        finish = "stop"
        if hold:
            whole = "".join(buffer)
            call = parse_tool_call(whole, request.tools)
            if call:
                self._send_chunk(completion_id, model,
                                 {"tool_calls": [{"index": 0, **call}]})
                finish = "tool_calls"
            elif whole:
                self._send_chunk(completion_id, model, {"content": whole})

        self._send_chunk(completion_id, model, {}, finish=finish, extra=extra)
        self._send_usage_chunk()
        self._end_stream()

    # ── fallback: протухшая серверная беседа → новый чат с историей ──

    def _rebuild_prompt(self, request, messages, window):
        """Пересобрать prompt из полной истории, сбросив беседу."""
        request.conversation = None
        trimmed = trim_history(messages, window)
        request.prompt = messages_to_prompt(trimmed)

    def _complete_conv_retry(self, provider, request, model,
                             conv_fallback) -> None:
        messages, window = conv_fallback
        self._rebuild_prompt(request, messages, window)
        try:
            text = provider.complete(request)
        # RateLimited — ПОДКЛАСС ProviderError, и порядок тут решает всё:
        # стоял бы общий обработчик первым, ветка нормы была бы мёртвой,
        # пауза не ставилась бы, а клиент получал бы 502 вместо 429.
        except RateLimited as exc:
            quota.handle_rate_limited(
                provider.name, provider.credential.account,
                exc.retry_after, exc.is_budget, str(exc))
            return self._error(429, "rate_limited", str(exc))
        except ProviderError as exc:
            return self._error(502, "provider_error", str(exc))
        quota.record(provider.name, provider.credential.account)
        self._json(200, self._completion_body(
            text, model, request.conversation, request.tools,
            prompt_chars=len(request.prompt or "")))

    def _stream_conv_retry(self, provider, request, model,
                           conv_fallback, completion_id) -> None:
        messages, window = conv_fallback
        self._rebuild_prompt(request, messages, window)
        # Придержка та же, что в _stream: с инструментами текст копим и
        # решаем в конце, вызов это или ответ. Без этих двух определений
        # хвост функции (if hold / buffer) падал NameError'ом — а ветка
        # живая: сюда попадаем, когда named-провайдер с беседой протух до
        # первого чанка.
        buffer: list[str] = []
        hold = bool(request.tools)
        try:
            for kind, piece in provider.stream_rich(request):
                if kind == "thinking":
                    self._send_chunk(completion_id, model,
                                     {"thinking": piece})
                elif hold:
                    buffer.append(piece)
                else:
                    self._send_chunk(completion_id, model,
                                     {"content": piece})
        # Порядок важен: RateLimited наследует ProviderError, и общий
        # обработчик впереди делал ветку нормы мёртвой — пауза не
        # ставилась, а клиент видел «сервис отказал» вместо «норма».
        except RateLimited as exc:
            quota.handle_rate_limited(
                provider.name, provider.credential.account,
                exc.retry_after, exc.is_budget, str(exc))
            self._send_raw({"error": {
                "message": str(exc), "type": "rate_limited",
                "retry_after": exc.retry_after}})
            return self._end_stream()
        except ProviderError as exc:
            self._send_raw({"error": {"message": str(exc),
                                      "type": "provider_error"}})
            return self._end_stream()
        except Exception as exc:  # noqa: BLE001 — обрыв сети/чужая библиотека
            return self._stream_broke(exc)
        quota.record(provider.name, provider.credential.account)
        conv = request.conversation
        extra = None
        if conv and conv.chat_id:
            extra = {"conversation": {
                "provider": conv.provider,
                "chat_id": conv.chat_id,
                "last_message_id": conv.last_message_id}}

        finish = "stop"
        if hold:
            whole = "".join(buffer)
            call = parse_tool_call(whole, request.tools)
            if call:
                self._send_chunk(completion_id, model,
                                 {"tool_calls": [{"index": 0, **call}]})
                finish = "tool_calls"
            elif whole:
                self._send_chunk(completion_id, model, {"content": whole})

        self._send_chunk(completion_id, model, {}, finish=finish, extra=extra)
        self._send_usage_chunk()
        self._end_stream()

    # ── /v1/images/generations ───────────────────────────────────────

    def _images(self) -> None:
        """Генерация картинок. Формат ответа как у OpenAI Images API.

        Кто рисует: Bing (специализированный, первый), DeepAI, Alice, Grok.
        При model="auto" маршрутизатор выберет рисовальщика сам.
        """
        body = self._read_body()
        if body is None:
            return

        prompt = body.get("prompt", "")
        if not prompt:
            return self._error(400, "invalid_request", "prompt is required")

        model_raw = body.get("model", "auto")
        aspect = body.get("size", "1024x1024")
        # OpenAI передаёт size="1024x1024", наши адаптеры ждут "1:1".
        aspect_map = {"1024x1024": "1:1", "1792x1024": "16:9",
                      "1024x1792": "9:16"}
        aspect_ratio = aspect_map.get(aspect, "1:1")

        from foxroute.providers.base import Request as ProviderRequest

        request = ProviderRequest(prompt=prompt, timeout=240,
                                  aspect=aspect_ratio)

        def _try_draw(name):
            """Открыть рисовальщика, ВЗЯТЬ слот занятости, нарисовать.

            Слот — как на чат-пути: веб-сессия ведёт один разговор за раз, и
            два параллельных рисунка одной кукой давали бы кашу. Успех →
            (provider, account, urls) с ЗАНЯТЫМ слотом и ОТКРЫТЫМ провайдером
            (освобождает вызывающий). Любой сбой → сам освобождает слот и
            закрывает провайдера, потом пробрасывает исключение.
            """
            prov = open_provider(name, store=store, allow_anonymous=True)
            account = getattr(prov.credential, "account", "") or "default"
            slots = 1 if not is_api(name) else 4
            if not router.busy.take(name, account, slots):
                self._quiet_close(prov)
                raise RateLimited(
                    f"{name}: занят другим запросом — веб-сессия рисует "
                    "по одному за раз", name)
            try:
                return prov, account, prov.draw(request)
            except BaseException:
                router.busy.release(name, account)
                self._quiet_close(prov)
                raise

        provider = None
        held = None  # (name, account) занятого слота — освободить в конце

        if model_raw in ("auto", "best"):
            need = Need(text=False, images=True)
            chain = router.candidates(need)
            if not chain:
                return self._error(400, "unsupported",
                                   "нет провайдера для картинок")
            urls = None
            errors = []
            for name in chain[:3]:
                try:
                    provider, account, urls = _try_draw(name)
                except ProviderError as exc:  # incl. RateLimited/Unsupported
                    errors.append(f"{name}: {exc}")
                    continue
                if urls:
                    held = (name, account)
                    break
                # Пусто без ошибки — освобождаем и пробуем следующего.
                router.busy.release(name, account)
                self._quiet_close(provider)
                provider = None
            if not urls:
                return self._error(502, "provider_error",
                                   "все рисовальщики отказали: "
                                   + "; ".join(errors))
        else:
            provider_name = (model_raw.split("/")[0]
                             if "/" in model_raw else model_raw)
            # Неизвестная модель — 404, как на чат-пути (а не 401 от
            # open_provider): клиент с фолбэком-на-404 сможет переключиться.
            if provider_name not in PROVIDER_CONFIGS:
                return self._error(404, "model_not_found",
                                   f"нет такой модели: {model_raw!r}")
            try:
                provider, account, urls = _try_draw(provider_name)
                held = (provider_name, account)
            except AuthError as exc:
                return self._error(401, "auth_error", str(exc))
            except RateLimited as exc:
                return self._error(429, "rate_limited", str(exc))
            except Unsupported as exc:
                return self._error(400, "unsupported", str(exc))
            except ProviderError as exc:
                return self._error(502, "provider_error", str(exc))

        # Дальше — сборка ответа. В finally ГАРАНТИРОВАННО освобождаем слот и
        # закрываем провайдера на любом исходе (успех, пустой ответ, ошибка
        # _image_entry): иначе Gemini-семейство держит поток+соединение.
        try:
            if not urls:
                return self._error(
                    502, "provider_error",
                    "рисовальщик не вернул ни одной картинки")
            # ``n`` — сколько просили. Больше нарисованного не выдумываем;
            # меньше — обрезаем, как OpenAI.
            try:
                wanted = int(body.get("n") or 0)
            except (TypeError, ValueError):
                wanted = 0
            if wanted > 0:
                urls = urls[:wanted]
            # Подпись сервиса → revised_prompt (поле Images API OpenAI); её
            # отдаёт пока только ChatGPT.
            caption = getattr(provider, "last_caption", "") if provider else ""
            want_b64 = str(body.get("response_format") or "url") == "b64_json"
            data = [self._image_entry(url, want_b64) for url in urls]
            if caption and data:
                data[0]["revised_prompt"] = caption
            self._json(200, {"created": int(time.time()), "data": data})
        finally:
            if held:
                router.busy.release(*held)
            self._quiet_close(provider)

    def _image_entry(self, url: str, want_b64: bool) -> dict:
        """Одна картинка в ответе Images API.

        ``response_format: "b64_json"`` — не прихоть: клиенты OpenAI просят
        его, когда хотят получить байты сразу, не ходя за ними отдельно.
        Без его поддержки такой клиент получал бы ссылку в поле, которого
        не ждал, — то есть пустоту.
        """
        if not want_b64:
            return {"url": url}
        if url.startswith("data:"):
            packed = url.split(",", 1)[-1]
            return {"b64_json": packed}
        try:
            blob, _ = self._fetch_attachment(url, "")
        except ValueError:
            # Скачать не вышло — честнее отдать ссылку, чем ничего.
            return {"url": url}
        import base64 as b64
        return {"b64_json": b64.b64encode(blob).decode()}

    # ── /v1/audio/transcriptions ─────────────────────────────────────

    def _transcribe(self) -> None:
        """Распознать речь через Groq Whisper.

        Принимает multipart/form-data с полем ``file`` (аудио) и
        необязательными ``model`` и ``language``. Формат совместим с
        OpenAI Audio API.
        """
        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" in content_type:
            # Разбираем сами, через email.parser: модуль ``cgi`` УДАЛЁН из
            # стандартной библиотеки в Python 3.13 — а распознавание речи
            # openai-python шлёт именно как multipart, то есть это основной
            # путь, и на новом Python он падал бы ModuleNotFoundError прямо
            # посреди обработчика.
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return self._error(400, "invalid_request",
                                   "кривой Content-Length")
            if length <= 0 or length > self.MAX_BODY_BYTES:
                return self._error(400, "invalid_request",
                                   "пустое или слишком большое тело")
            parts = self._multipart(self.rfile.read(length), content_type)
            audio = parts.get("file")
            if not audio or not audio.get("data"):
                return self._error(400, "invalid_request",
                                   "file field is required")
            audio_data = audio["data"]
            filename = audio.get("filename") or "audio.wav"
            model = (parts.get("model") or {}).get("text")                 or "whisper-large-v3-turbo"
            language = (parts.get("language") or {}).get("text") or None
        elif "application/json" in content_type:
            import base64 as b64
            body = self._read_body()
            if body is None:
                return
            raw = body.get("file", "")
            if not raw:
                return self._error(400, "invalid_request",
                                   "file (base64) is required")
            try:
                audio_data = b64.b64decode(raw)
            except (ValueError, binascii.Error):
                return self._error(400, "invalid_request",
                                   "file: повреждённый base64")
            filename = body.get("filename", "audio.wav")
            model = body.get("model", "whisper-large-v3-turbo")
            language = body.get("language")
        else:
            return self._error(400, "invalid_request",
                               "Content-Type must be multipart/form-data "
                               "or application/json")

        # openai-python по умолчанию шлёт model="whisper-1" (или
        # "gpt-4o-transcribe") — у Groq таких нет, и дословный проброс давал
        # 400→502, то есть стоковый клиент не работал. Чужие/неизвестные имена
        # сводим к модели Groq.
        if model not in ("whisper-large-v3", "whisper-large-v3-turbo",
                         "distil-whisper-large-v3-en"):
            model = "whisper-large-v3-turbo"

        try:
            provider = open_provider("groq", store=store,
                                     allow_anonymous=True)
        except (AuthError, ProviderError) as exc:
            return self._error(502, "provider_error", str(exc))

        try:
            result = provider.transcribe(
                audio_data=audio_data,
                filename=filename,
                model=model,
                language=language if language else None,
            )
        except ProviderError as exc:
            return self._error(502, "provider_error", str(exc))
        finally:
            self._quiet_close(provider)

        self._json(200, result)

    # ── /v1/audio/speech ─────────────────────────────────────────────

    def _speak(self) -> None:
        """Озвучить текст. Первым идёт тот, у кого нет суточной нормы.

        Порядок не случайный. Orpheus на Groq даёт сотню запросов в сутки
        на ключ, да ещё требует отдельно принять условия модели — принято
        не везде, поэтому пул приходится перебирать. OpenAI.fm не просит
        ничего и не считает запросы, так что он основной, а Groq — запасной
        и заодно источник голосов, которых там нет.

        Тип содержимого берём у провайдера: OpenAI.fm отдаёт MP3, Orpheus —
        WAV. Проставить наугад значит получить неиграющий файл.
        """
        body = self._read_body()
        if body is None:
            return
        text = (body.get("input") or body.get("text") or "").strip()
        if not text:
            return self._error(400, "invalid_request", "input is required")
        voice = str(body.get("voice") or "").strip()
        style = str(body.get("style") or "friendly").strip()

        from foxroute.providers.api.openai_compat import GroqProvider
        from foxroute.providers.web.openaifm import OpenAIFMProvider

        errors: list[str] = []
        audio: bytes | None = None
        mime = "audio/mpeg"

        # Голос сам говорит, к кому идти: списки не пересекаются.
        wants_groq = voice in GroqProvider.TTS_VOICES

        if not wants_groq:
            speaker = None
            try:
                speaker = OpenAIFMProvider(Credential(provider="openai_fm",
                                                      value=""))
                audio = speaker.synthesize(
                    text, voice=voice or OpenAIFMProvider.DEFAULT_VOICE,
                    style=style)
                mime = speaker.TTS_MIME
            except ProviderError as exc:
                errors.append(f"openai.fm: {exc}")
            finally:
                self._quiet_close(speaker)

        if audio is None:
            for cred in store.usable("groq"):
                provider = None
                try:
                    provider = GroqProvider(cred)
                    audio = provider.synthesize(
                        text, voice=voice if wants_groq else "autumn")
                    mime = "audio/wav"
                    break
                except ProviderError as exc:
                    errors.append(f"groq/{cred.account}: {exc}")
                    continue
                finally:
                    # finally срабатывает и при break — успешный провайдер
                    # тоже закроется.
                    self._quiet_close(provider)

        if audio is None:
            return self._error(
                502, "provider_error",
                "озвучка недоступна: " + "; ".join(errors))

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)

    # ── кадры ответа ──────────────────────────────────────────────────

    @staticmethod
    def _quiet_close(provider) -> None:
        """Закрыть провайдер, не роняя обработчик уборкой.

        Веб-сессия (Gemini, curl_cffi) без close оставляет поток и
        соединение — закрывать нужно на всех путях (картинки, озвучка,
        распознавание), не только в чате.
        """
        if provider is not None:
            try:
                provider.close()
            except Exception:  # noqa: BLE001 — уборка не важнее ответа
                pass

    @staticmethod
    def _completion_body(text: str, model: str,
                         conversation=None, tools=None,
                         prompt_chars: int = 0) -> dict:
        # Вызов инструмента вылавливаем из текста и отдаём так, как ждёт
        # клиент: полем ``tool_calls`` и с ``finish_reason: tool_calls``.
        # Без этого агентные обвязки (LangChain, openai-agents) видят
        # обычный текст и ``tool_calls: null`` — и либо падают, либо
        # крутятся впустую.
        call = parse_tool_call(text, tools or [])
        message = {"role": "assistant", "content": None if call else text}
        finish = "stop"
        if call:
            message["tool_calls"] = [call]
            finish = "tool_calls"

        body = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }],
            # Токены веб-сессии не сообщают, поэтому ОЦЕНИВАЕМ их по длине
            # (~4 символа на токен — общепринятая грубая эвристика). Ноль
            # тоже неверен и вдобавок ломает клиентов, которые делят на
            # total_tokens (скорость в токенах/с) или показывают «0
            # tokens» как явную поломку. Оценка приблизительная; для
            # бесплатного шлюза биллинга нет, а деления на ноль не будет.
            "usage": {
                "prompt_tokens": max(0, prompt_chars // 4),
                "completion_tokens": (max(1, len(text) // 4) if text else 0),
                "total_tokens": (max(0, prompt_chars // 4)
                                 + (max(1, len(text) // 4) if text else 0)),
            },
        }
        # Метка серверной беседы — своё поле рядом со стандартными. Без
        # неё продолжить разговор через API было нельзя: в потоке метка
        # возвращалась, а в обычном ответе терялась.
        if conversation is not None and getattr(conversation, "chat_id", ""):
            body["conversation"] = {
                "provider": conversation.provider,
                "chat_id": conversation.chat_id,
                "last_message_id": conversation.last_message_id,
            }
        return body

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Именно close, а не keep-alive. Длину содержимого мы не знаем, и
        # без закрытия соединения клиент не понимает, что поток кончился:
        # браузер ждёт вечно, кнопка отправки остаётся заблокированной.
        self.send_header("Connection", "close")
        allowed = self._origin_ok()
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
        self.end_headers()

    def _send_raw(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_chunk(self, completion_id: str, model: str, delta: dict,
                    finish: str | None = None, extra: dict | None = None) -> None:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish,
            }],
        }
        # Своё кладём ПОЛЕМ в обычный кусок, а не отдельным кадром.
        # Отдельным было нельзя: в кадре без ``choices`` строгий клиент
        # (тот же openai-python) спотыкается — он разбирает каждый
        # ``data:`` как кусок ответа, и обращение к ``choices[0]``
        # роняет чужой код. Лишнее поле, наоборот, безобидно.
        if extra:
            chunk.update(extra)
        # Для stream_options.include_usage копим длину ответа и запоминаем
        # id/model — финальный usage-чанк соберём из них перед [DONE].
        u = getattr(self, "_usage", None)
        if u is not None:
            u["id"], u["model"] = completion_id, model
            piece = delta.get("content") if isinstance(delta, dict) else ""
            if piece:
                u["chars"] += len(piece)
        self._send_raw(chunk)

    def _send_usage_chunk(self) -> None:
        """Финальный usage-чанк при stream_options.include_usage — как у
        OpenAI: отдельный кадр с пустым ``choices`` и полем ``usage``. Токены
        оцениваем по длине (та же эвристика //4, что в непотоковом ответе)."""
        u = getattr(self, "_usage", None)
        if not (u and u.get("include") and u.get("id")):
            return
        pt = max(0, u["prompt_chars"] // 4)
        ct = max(1, u["chars"] // 4) if u["chars"] else 0
        self._send_raw({
            "id": u["id"], "object": "chat.completion.chunk",
            "created": int(time.time()), "model": u["model"], "choices": [],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                      "total_tokens": pt + ct},
        })

    def _end_stream(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_broke(self, exc: Exception) -> None:
        """Сырое исключение ПОСРЕДИ стрима (обрыв соединения в iter_lines,
        падение библиотеки провайдера). Заголовки 200 уже ушли — HTTP-статус
        не поменять, иначе `HTTP 500` + JSON писались бы ВНУТРЬ тела SSE
        без финального [DONE], и клиент читал бы молча оборванный поток.
        Отдаём ошибку IN-BAND кадром и закрываем поток по-людски."""
        try:
            self._send_raw({"error": {
                "message": f"{type(exc).__name__}: {str(exc)[:200]}",
                "type": "provider_error"}})
            self._end_stream()
        except Exception:  # noqa: BLE001 — клиент мог уже отвалиться
            pass

    # ── утилиты ───────────────────────────────────────────────────────

    #: Потолок на тело запроса. Вложения едут в base64 (+33%), поэтому
    #: щедро — но не безгранично: без потолка один запрос мог занять
    #: столько памяти, сколько объявит в заголовке.
    MAX_BODY_BYTES = 256 * 1024 * 1024

    def _read_body(self) -> dict | None:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            # Кривой Content-Length иначе уронил бы обработчик здесь, и
            # клиент получил бы оборванное соединение вместо объяснения.
            self._error(400, "invalid_request",
                        f"кривой Content-Length: {raw_length!r}")
            return None
        if length <= 0:
            self._error(400, "invalid_request", "empty body")
            return None
        if length > self.MAX_BODY_BYTES:
            self._error(413, "invalid_request",
                        f"тело больше {self.MAX_BODY_BYTES // 1024 // 1024} МБ")
            return None
        try:
            parsed = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "invalid_request", "invalid JSON")
            return None
        # Верхний уровень обязан быть объектом: тело вида [...] или скаляр
        # прошло бы разбор, но затем body.get(...) упало бы AttributeError
        # → 500 вместо внятного 400.
        if not isinstance(parsed, dict):
            self._error(400, "invalid_request",
                        "тело должно быть JSON-объектом")
            return None
        return parsed


    # ── откуда пускаем ────────────────────────────────────────────────
    #
    # Отвечать на КАЖДЫЙ запрос ``Access-Control-Allow-Origin: *`` —
    # включая ``/api/accounts/*``, которые заводят и удаляют доступы, —
    # нельзя: тогда любая открытая в браузере страница могла бы
    # постучаться на наш адрес и прочитать ответ. Шлюз слушает петлю, но
    # браузер человека — тоже на этой петле. Ни пароля, ни ключа тут нет,
    # так что защита одна — не отвечать чужому origin.

    LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

    def _origin_ok(self) -> str:
        """Origin, которому можно ответить. Пусто — нельзя.

        Запросы без Origin (curl, python-скрипт, openai-python) проходят:
        заголовка нет — и проверять нечего, это не браузер.
        """
        origin = self.headers.get("Origin") or ""
        if not origin:
            return ""
        from urllib.parse import urlparse

        host = urlparse(origin).hostname or ""
        return origin if host in self.LOCAL_HOSTS else ""

    def _origin_hostile(self) -> bool:
        """Origin ЕСТЬ и он чужой — значит запрос шлёт браузерная страница
        с другого сайта. Без Origin (curl, openai-python) — не браузер,
        пропускаем."""
        origin = self.headers.get("Origin") or ""
        if not origin:
            return False
        from urllib.parse import urlparse

        host = urlparse(origin).hostname or ""
        return host not in self.LOCAL_HOSTS

    @staticmethod
    def _multipart(body: bytes, content_type: str) -> dict:
        """Разобрать multipart/form-data в ``{имя: {...}}``.

        Файловые части отдаются как ``{"filename", "data"}``, обычные —
        как ``{"text"}``. Через ``email.parser``: он в стандартной
        библиотеке всерьёз и надолго, в отличие от ``cgi``.
        """
        from email.parser import BytesParser
        from email.policy import HTTP

        head = ("Content-Type: " + content_type
                + "\r\nMIME-Version: 1.0\r\n\r\n")
        message = BytesParser(policy=HTTP).parsebytes(
            head.encode("utf-8") + body)
        out: dict[str, dict] = {}
        if not message.is_multipart():
            return out
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                out[name] = {"filename": filename, "data": payload}
            else:
                out[name] = {"text": payload.decode("utf-8", "replace").strip()}
        return out

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        allowed = self._origin_ok()
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, error_type: str, message: str) -> None:
        self._json(code, {
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": error_type,
            }
        })

    def do_OPTIONS(self) -> None:
        allowed = self._origin_ok()
        self.send_response(200 if allowed or not
                           self.headers.get("Origin") else 403)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            # Authorization перечисляем ЯВНО: по спецификации Fetch «*» в
            # Allow-Headers его НЕ покрывает, и браузерный SDK, шлющий
            # Authorization, провалил бы preflight.
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, *")
        self.end_headers()

    def log_message(self, fmt, *args) -> None:
        import sys
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}", file=sys.stderr,
              flush=True)


def serve(host: str = "127.0.0.1", port: int = 8777) -> None:
    from foxroute import settings

    # Ключ инстанса: env → сохранённый → сгенерировать при первом запуске.
    # Так каждый, кто развернёт проект у себя, получает свой уникальный ключ.
    key = settings.ensure_api_key()
    server = ThreadedHTTPServer((host, port), Handler)
    print(f"foxroute server @ http://{host}:{port}/v1")
    print(f"провайдеров: {len(implemented())}")
    print(f"хранилище: {store.path}")
    if key:
        print(f"API-ключ (для программных клиентов): {key}")
        print("  интерфейс за туннелем ходит без ключа")
    else:
        print("API-ключ НЕ задан — авторизация выключена (FOXROUTE_API_KEY='')")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="foxroute OpenAI-compatible server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    args = parser.parse_args()
    serve(args.host, args.port)
