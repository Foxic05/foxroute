"""Z.ai — международный GLM от Zhipu (chat.z.ai).

**Ключ не нужен вовсе.** Токен выдаётся анонимно на запрос ``/api/v1/auths/``,
и это делает провайдера единственным в пуле, который масштабируется без
регистрации аккаунтов.

Плата за это — капча Aliyun. Решается автоматически: рядом лежит сценарий
для Node с Puppeteer, headless-браузер поднимается только на время решения
(секунды), а не держится постоянно.

**Captcha-токен одноразовый.** Второй запрос с тем же значением сервер молча
отдаёт пустотой: со свежей капчей проходят все запросы, с кешированной — ни
одного. Кешировать токен нельзя, иначе работает только первый запрос в окне,
и это выглядит как поломка провайдера. Та же болезнь, что у ``access_token``
ChatGPT.

**Файлы работают только по полной цепочке.** Загрузка — обычный multipart
на ``/api/v1/files/`` (гостю запрещена), но одной её мало: чат надо завести
С ПУСТЫМ ``id`` (идентификатор выдаёт сервер), положить туда вопрос, а файл
отправить отдельным полем ``files`` верхнего уровня, сославшись на это
сообщение через ``ref_user_msg_id`` и ``current_user_message_id``. Свой
сгенерированный ``id`` чата сервис принимает молча, но ссылку внутри такого
чата уже не разрешает: приходит ``INTERNAL_ERROR`` либо ответ «вы ничего не
прикрепили». Форма снята с живой страницы.

Держит 200 000 символов входа, медиана хода 4.8 секунды, агентную задачу
отладки решает за 6 ходов.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Iterator

from foxroute.errors import (
    AuthError, ProviderError, ProviderUnavailable)
from foxroute.paths import app_dir
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

#: Имя сценария решения капчи. Ищется в каталоге данных.
CAPTCHA_SCRIPT = "zai_captcha.js"


def solve_captcha(timeout: float = 90) -> str:
    """Решить капчу Aliyun и вернуть токен.

    Отдельной функцией, а не методом, потому что это внешний процесс, а не
    часть протокола: его удобно подменить в тестах и вызвать вручную при
    разборе поломок.
    """
    script = Path(app_dir()) / CAPTCHA_SCRIPT
    if not script.exists():
        raise ProviderError(
            f"нет сценария решения капчи ({script}). Нужен он и установленный "
            "Node с Puppeteer", "zai")

    environment = dict(os.environ)
    # DISPLAY нужен, если Puppeteer поднимается не в headless-режиме:
    # тогда рядом должен работать виртуальный экран.
    environment.setdefault("DISPLAY", ":99")

    # Node ищет пакеты, поднимаясь по каталогам ОТ СЦЕНАРИЯ. Сценарий лежит
    # в каталоге данных, а puppeteer-core может стоять где угодно, поэтому
    # путь задаётся явно: FOXROUTE_NODE_PATH, иначе node_modules рядом.
    node_path = os.environ.get("FOXROUTE_NODE_PATH", "").strip()
    if not node_path:
        nearby = script.parent / "node_modules"
        node_path = str(nearby) if nearby.exists() else ""
    if node_path:
        environment["NODE_PATH"] = node_path
    try:
        finished = subprocess.run(
            ["node", str(script)], capture_output=True, text=True,
            timeout=timeout, env=environment)
    except FileNotFoundError as exc:
        raise ProviderError("не найден node", "zai") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderUnavailable(
            f"капча не решилась за {timeout:.0f} с", "zai") from exc

    if finished.returncode != 0:
        stderr = finished.stderr[:300]
        if "Cannot find module" in stderr:
            raise ProviderError(
                "сценарию капчи не хватает пакетов Node. Укажи, где они, "
                "через FOXROUTE_NODE_PATH, либо поставь puppeteer-core рядом "
                f"со сценарием ({script.parent}/node_modules)", "zai")
        raise ProviderUnavailable(f"капча не решилась: {stderr}", "zai")
    token = finished.stdout.strip()
    if not token:
        raise ProviderUnavailable("сценарий капчи вернул пустоту", "zai")
    return token


class ZaiProvider(Provider):
    name = "zai"
    #: Файлы — только от аккаунта: гостю загрузка запрещена (401).
    capabilities = Capabilities(text=True, thinking=True,
                                files_in=True, vision=True)

    BASE = "https://chat.z.ai"
    #: Версия фронтенда в заголовке — сервис её сверяет.
    FRONTEND_VERSION = "prod-fe-1.1.82"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # Отвечает и анонимно: токен выдаётся любому, кто попросит. Но это
        # УРЕЗАННЫЙ режим — анонимному, например, запрещена загрузка файлов
        # (401 на /api/v1/files/). Со своим токеном сервис узнаёт учётку.
        self._token = (credential.value or "").strip()
        self.authorized = True
        self._captcha_lock = threading.Lock()

    # ── доступ ────────────────────────────────────────────────────────

    def _authorize(self, session) -> tuple[str, str]:
        """Bearer-токен и идентификатор пользователя.

        Со своим токеном идём тем же путём, только представившись: ответ
        того же вида, в нём и ``id``. Отдельной ветки не заводим намеренно
        — иначе анонимный и именной режимы разойдутся и один из них тихо
        сгниёт.
        """
        headers = {"Referer": f"{self.BASE}/"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = _http.request(
            session, "GET", f"{self.BASE}/api/v1/auths/", provider=self.name,
            headers=headers, timeout=60)
        if self._token and response.status_code in (401, 403):
            raise AuthError(
                "токен z.ai отвергнут — обновить его в localStorage "
                "chat.z.ai, поле token", self.name)
        _http.check(self.name, response)
        try:
            payload = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на выдачу токена", self.name) from exc
        # Свой токен держим свой: в ответе на именной запрос его может и не
        # быть, а перейти с него на выданный значило бы тихо соскользнуть
        # обратно в анонимный режим.
        token = self._token or payload.get("token", "")
        if not token:
            raise ProviderError("сервис не выдал токен", self.name)
        return token, str(payload.get("id", ""))

    def _captcha(self) -> str:
        """Свежая капча. Кешировать её нельзя — см. заметку в шапке."""
        with self._captcha_lock:
            return solve_captcha()

    # ── протокол ──────────────────────────────────────────────────────

    @staticmethod
    def _signature(request_id: str, timestamp: str, user_id: str) -> str:
        """Подпись запроса.

        Ключ HMAC пустой — так это устроено у них, не опечатка. Подписывается
        отсортированный по имени список пар «ключ=значение» через запятую.
        """
        payload = ",".join(
            f"{key}={value}" for key, value in sorted(
                {"requestId": request_id, "timestamp": timestamp,
                 "user_id": user_id}.items()))
        return hmac.new(b"", payload.encode(), hashlib.sha256).hexdigest()

    def _drop_chat(self, session, token: str, chat_id: str) -> None:
        """Убрать чат, заведённый ради одного ответа.

        Без этого каждый запрос оставляет в аккаунте пустой «New Chat»: мы
        заводим чат ради идентификатора, а историю в него не пишем — она
        живёт у нас. Человек, зайдя на сайт, видит ленту мусора, и это
        не косметика: среди сотен пустышек не найти собственные беседы.

        Ошибка уборки ответ не рушит — он уже получен.
        """
        if not chat_id:
            return
        try:
            _http.request(
                session, "DELETE", f"{self.BASE}/api/v1/chats/{chat_id}",
                provider=self.name,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json",
                         "Referer": f"{self.BASE}/"},
                timeout=30)
        except Exception:  # noqa: BLE001 — уборка не должна ломать ответ
            pass

    def _ask_body(self, model: str, req: Request, chat_id: str, msg_id: str,
                  captcha: str, files: list[dict]) -> dict:
        """Тело просьбы ответить.

        Списан с живой страницы. Важное здесь — ``current_user_message_id``:
        файлы едут ОТДЕЛЬНЫМ полем верхнего уровня и ссылаются на это
        сообщение через ``ref_user_msg_id``. Без такой пары сервис файл
        просто не находит, а модель отвечает «вы ничего не прикрепили» —
        без единой ошибки в ответе.
        """
        return {
            "stream": True,
            "model": model,
            "messages": [{"role": "user", "content": req.prompt}],
            "signature_prompt": req.prompt[:500],
            "captcha_verify_param": captcha,
            "params": {}, "extra": {},
            "files": files,
            "features": {"image_generation": False,
                         "web_search": req.web_search,
                         "auto_web_search": False, "preview_mode": True,
                         "flags": [], "vlm_tools_enable": False,
                         "vlm_web_search_enable": False,
                         "vlm_website_mode": False,
                         "enable_thinking": req.thinking},
            "variables": {}, "chat_id": chat_id,
            "id": str(uuid.uuid4()),
            "current_user_message_id": msg_id,
            "current_user_message_parent_id": None,
            "background_tasks": {"title_generation": False,
                                 "tags_generation": False},
        }

    def _start(self, session, token: str, model: str, msg_id: str,
               prompt: str) -> str:
        """Завести чат с вопросом в истории. Возвращает id ОТ СЕРВЕРА.

        Две вещи, без которых вложения не работают, и обе неочевидны.

        **``id`` уходит ПУСТЫМ.** Идентификатор чата выдаёт сервер; свой
        сгенерированный он принимает, чат даже появляется в ленте — но
        ссылка на сообщение внутри такого чата потом не разрешается, и
        запрос с файлом отвечает ``INTERNAL_ERROR`` либо молча теряет файл.

        **Вопрос кладётся в историю здесь**, до просьбы ответить, и БЕЗ
        файлов: файл поедет отдельно, в самом запросе ответа, и сошлётся
        на этот идентификатор сообщения.
        """
        now = int(time.time())
        question = {"id": msg_id, "parentId": None, "childrenIds": [],
                    "role": "user", "content": prompt, "timestamp": now,
                    "models": [model]}
        response = _http.request(
            session, "POST", f"{self.BASE}/api/v1/chats/new",
            provider=self.name,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Referer": f"{self.BASE}/",
                     "x-fe-version": self.FRONTEND_VERSION},
            json={"chat": {
                "id": "", "title": "New Chat", "models": [model],
                "params": {},
                "history": {"messages": {msg_id: question},
                            "currentId": msg_id},
                "tags": [], "flags": [],
                "features": [{"server": "tool_selector_h",
                              "status": "hidden", "type": "tool_selector"}],
                "mcp_servers": [], "enable_thinking": True,
                "reasoning_effort": "max", "auto_web_search": False,
                "message_version": 1, "extra": {},
                "timestamp": int(time.time() * 1000), "type": "default"}},
            timeout=60)
        _http.check(self.name, response)
        try:
            chat_id = (response.json() or {}).get("id") or ""
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на создание чата", self.name) from exc
        if not chat_id:
            raise ProviderError("сервис не выдал беседу", self.name)
        return chat_id

    @staticmethod
    def _delta(event: dict) -> str:
        """Кусок ответа (без thinking)."""
        inner = event.get("data")
        if not isinstance(inner, dict):
            return ""
        phase = inner.get("phase", "")
        if phase in ("done", "thinking"):
            return ""
        content = inner.get("delta_content") or inner.get("edit_content") or ""
        return content if isinstance(content, str) else ""

    @staticmethod
    def _rich_delta(event: dict) -> tuple[str, str] | None:
        """Кусок с типом: thinking или text."""
        inner = event.get("data")
        if not isinstance(inner, dict):
            return None
        phase = inner.get("phase", "")
        if phase == "done":
            return None
        content = inner.get("delta_content") or inner.get("edit_content") or ""
        if not isinstance(content, str) or not content:
            return None
        if phase == "thinking":
            return ("thinking", content)
        return ("text", content)

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Один POST с файлом, в ответ запись — как у Qwen, движок под ними один
    # и тот же. Разница в доступе: **гостю загрузка запрещена** (401 с
    # «нет прав»), нужен свой токен. Ходи адаптер анонимно — файлы «не
    # работают» именно из-за этого.

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024

    def _upload(self, session, token: str, item) -> dict:
        """Положить вложение и собрать запись для поля ``files``."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        name = item.filename or "file.bin"
        mime = item.mime or "application/octet-stream"
        body, ctype = _http.multipart(filename=name, data=raw,
                                      content_type=mime)
        response = _http.request(
            session, "POST", f"{self.BASE}/api/v1/files/", provider=self.name,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": ctype, "Origin": self.BASE,
                     "Referer": f"{self.BASE}/",
                     "x-fe-version": self.FRONTEND_VERSION},
            data=body, timeout=300)
        if response.status_code in (401, 403):
            raise AuthError(
                "загрузка файлов запрещена — сервис принял нас гостем, "
                "нужен свой token из localStorage chat.z.ai", self.name)
        _http.check(self.name, response)
        try:
            saved = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на загрузку файла", self.name) from exc

        file_id = saved.get("id") or ""
        if not file_id:
            raise ProviderError(
                f"сервис не выдал идентификатор файла: {str(saved)[:200]}",
                self.name)
        shape = "image" if mime.startswith("image/") else "file"
        return {
            "type": shape,
            "file": saved,
            "id": file_id,
            "url": f"/api/v1/files/{file_id}",
            "name": name,
            "status": "uploaded",
            "size": len(raw),
            "error": "",
            "itemId": str(uuid.uuid4()),
            "media": shape,
            "uploadedAt": int(time.time() * 1000),
            # Заполняется вызывающим: файл обязан сослаться на сообщение,
            # иначе сервис его не найдёт.
            "ref_user_msg_id": "",
        }

    def _stream(self, req: Request) -> Iterator[str]:
        for kind, piece in self._pairs(req):
            if kind == "text":
                yield piece

    def stream_rich(self, req: Request) -> Iterator[tuple[str, str]]:
        self.validate(req)
        yield from self._pairs(req)

    def _pairs(self, req: Request) -> Iterator[tuple[str, str]]:
        """Общее тело потока: пары ``("text"|"thinking", кусок)``.

        ``_stream`` (только текст, ``sse_deltas``) и ``stream_rich`` (пары,
        ``sse_events``) различаются лишь разбором дельт; общий обмен —
        авторизация, капча, загрузка, запрос и уборка чата — живёт здесь,
        чтобы не дублироваться.
        """
        model = self.resolve_model(req)
        with _http.session() as session:
            token, user_id = self._authorize(session)
            msg_id = str(uuid.uuid4())
            files = [self._upload(session, token, item)
                     for item in req.attachments]
            for record in files:
                record["ref_user_msg_id"] = msg_id
            captcha = self._captcha()
            chat_id = self._start(session, token, model, msg_id, req.prompt)
            try:
                timestamp = str(int(time.time() * 1000))
                request_id = str(uuid.uuid4())
                query = urllib.parse.urlencode({
                    "timestamp": timestamp, "requestId": request_id,
                    "user_id": user_id, "version": "0.0.1"})

                body = json.dumps(
                    self._ask_body(model, req, chat_id, msg_id, captcha,
                                   files),
                    separators=(",", ":"))

                response = _http.request(
                    session, "POST",
                    f"{self.BASE}/api/v2/chat/completions?{query}",
                    provider=self.name, data=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Origin": self.BASE,
                        "Referer": f"{self.BASE}/",
                        "x-fe-version": self.FRONTEND_VERSION,
                        "x-region": "overseas",
                        "x-signature": self._signature(
                            request_id, timestamp, user_id),
                    },
                    timeout=req.timeout, stream=True)
                _http.check(self.name, response)

                produced = False
                for event in _http.sse_events(response):
                    pair = self._rich_delta(event)
                    if pair:
                        produced = True
                        yield pair

                if not produced:
                    raise ProviderError(
                        "пустой ответ — обычно это переиспользованная капча "
                        "или неизвестная сервису модель", self.name)

            finally:
                self._drop_chat(session, token, chat_id)