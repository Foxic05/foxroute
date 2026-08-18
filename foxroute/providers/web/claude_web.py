"""Claude.ai — веб-сессия бесплатного тарифа.

Доступ — кука ``sessionKey``. Бесплатный тариф даёт три модели:
Sonnet 5, Sonnet 4.6, Haiku 4.5 — с переключением effort.

Протокол снят перехватом живого трафика:

* ``thinking_mode`` и ``effort`` идут В ТЕЛЕ ``/completion``, а не в
  настройках беседы. Значения: ``thinking_mode`` = ``auto`` | ``off``,
  ``effort`` = ``low`` | ``medium`` | ``high``.
* Файлы загружаются через ``/conversations/{id}/wiggle/upload-file``
  (multipart), после чего появляются в ``attachments`` тела. Файл
  привязывается к беседе, ``file_id`` берётся из ответа.
* Web search включается инструментом ``web_search`` с типом
  ``web_search_v0``.

Поток — стандартный SSE с событиями ``content_block_delta``.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import uuid
from typing import Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)

MODEL = "claude-sonnet-5"
MAX_UPLOAD = 64 * 1024 * 1024
MAX_MADE_FILES = 5
MAX_MADE_BYTES = 12 * 1024 * 1024


class ClaudeWebProvider(Provider):
    name = "claude_web"
    capabilities = Capabilities(
        text=True, streaming=True, conversations=True,
        web_search=True, thinking=True, vision=True,
        files_in=True, files_out=True)

    BASE = "https://claude.ai"

    def __init__(self, credential: Credential, model: str = "",
                 on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if not credential.value:
            raise ProviderError(
                "нужна кука sessionKey от claude.ai", self.name)
        self._org: str = ""
        self._device_id = str(uuid.uuid4())

    @property
    def authorized(self) -> bool:
        return bool(self.credential.value)

    def _session(self):
        s = _http.session(impersonate="chrome")
        s.headers["cookie"] = self.credential.value
        return s

    def _headers(self, accept: str = "application/json") -> dict:
        return {
            "accept": accept,
            "content-type": "application/json",
            "origin": self.BASE,
            "referer": f"{self.BASE}/",
            "anthropic-client-platform": "web_claude_ai",
            "anthropic-client-version": "1.0.0",
            "anthropic-device-id": self._device_id,
            "x-activity-session-id": str(uuid.uuid4()),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

    def _ensure_org(self, session) -> str:
        if self._org:
            return self._org
        resp = _http.request(session, "GET",
                             f"{self.BASE}/api/organizations",
                             provider=self.name,
                             headers=self._headers(),
                             timeout=30)
        if resp.status_code == 403:
            raise AuthError("sessionKey протухла", self.name)
        _http.check(self.name, resp)
        # Тело бывает НЕ списком: при протухшей куке Cloudflare отдаёт HTML
        # (json() кинет ValueError), а сервис — {"error": …} (dict пройдёт
        # проверку на пустоту, а orgs[0] упадёт). Разбираем аккуратно.
        try:
            orgs = resp.json()
        except ValueError as exc:
            raise AuthError("организации не в JSON — вероятно, кука "
                            "протухла или Cloudflare выдал челлендж",
                            self.name) from exc
        if not isinstance(orgs, list) or not orgs:
            raise AuthError("нет организаций — аккаунт пуст или доступ "
                            "отклонён", self.name)
        first = orgs[0]
        if not isinstance(first, dict) or not first.get("uuid"):
            raise AuthError("в ответе нет uuid организации", self.name)
        self._org = first["uuid"]
        return self._org

    def _api(self, path: str) -> str:
        return f"{self.BASE}/api/organizations/{self._org}/{path}"

    def _upload(self, session, conv_id: str, item) -> dict:
        """Залить файл через wiggle. Возвращает описание для attachments."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("вложение пустое", self.name)
        if len(raw) > MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        mime = item.mime or "application/octet-stream"
        name = item.filename or "file.bin"

        body, ctype = _http.multipart(
            name="file", filename=name, data=raw, content_type=mime)
        headers = self._headers()
        headers["content-type"] = ctype

        resp = _http.request(
            session, "POST",
            self._api(f"conversations/{conv_id}/wiggle/upload-file"),
            provider=self.name, headers=headers,
            data=body, timeout=120)
        _http.check(self.name, resp)
        info = resp.json()
        file_id = info.get("file_uuid") or info.get("uuid") or info.get("id")
        if not file_id:
            raise ProviderError("загрузка не выдала id файла", self.name)

        return {
            "file_name": name,
            "file_type": mime,
            "file_size": len(raw),
            "extracted_content": "",
            "file_uuid": file_id,
        }

    def _stream(self, req: Request) -> Iterator[str]:
        # Только ответ: мысли отфильтрованы, как требует контракт.
        for kind, piece in self._events(req):
            if kind == "text":
                yield piece

    def stream_rich(self, req: Request) -> Iterator[tuple[str, str]]:
        # Ответ И мысли — для тех, кто хочет показать ход размышления
        # отдельным потоком (кнопка thinking в интерфейсе).
        self.validate(req)
        yield from self._events(req)

    def _events(self, req: Request) -> Iterator[tuple[str, str]]:
        """Поток пар (тип, кусок): ``text`` — ответ, ``thinking`` — мысли.

        Общее тело для ``_stream`` и ``stream_rich``. Мысли идут отдельным
        типом, а НЕ строкой ``<think>`` в тексте: иначе ``complete()`` вернул
        бы служебное рассуждение в ответе, что запрещено контрактом
        (base.py ``_stream``).
        """
        with self._session() as session:
            self._ensure_org(session)

            conv_id = ""
            parent_uuid = ""
            if req.conversation and req.conversation.chat_id:
                conv_id = req.conversation.chat_id
                # parent_message_uuid держим в last_message_id, а НЕ в extra:
                # extra не переживает передачу через API (сервер отдаёт
                # клиенту только provider/chat_id/last_message_id). Без
                # этого второй ход шёл с пустым parent, и на длинных
                # беседах claude.ai ветвил дерево от корня.
                parent_uuid = req.conversation.last_message_id

            human_uuid = str(uuid.uuid4())
            assistant_uuid = str(uuid.uuid4())
            model = self.model or MODEL

            body: dict = {
                "prompt": req.prompt,
                "timezone": "UTC",
                "locale": "en-US",
                "model": model,
                "rendering_mode": "messages",
                "thinking_mode": "auto" if req.thinking else "off",
                "effort": "high" if req.thinking else "medium",
                "turn_message_uuids": {
                    "human_message_uuid": human_uuid,
                    "assistant_message_uuid": assistant_uuid,
                },
                "attachments": [],
                "files": [],
                "sync_sources": [],
                "tools": [],
            }

            body["tools"].extend([
                {"name": "artifacts", "type": "artifacts_v0"},
                {"name": "repl", "type": "repl_v0"},
            ])
            if req.web_search:
                body["tools"].append(
                    {"name": "web_search", "type": "web_search_v0"})

            if conv_id:
                body["parent_message_uuid"] = parent_uuid
            else:
                conv_id = str(uuid.uuid4())
                body["create_conversation_params"] = {
                    "uuid": conv_id, "name": ""}

            if req.attachments:
                for item in req.attachments:
                    att = self._upload(session, conv_id, item)
                    body["attachments"].append(att)

            headers = self._headers(accept="text/event-stream")
            response = _http.request(
                session, "POST",
                self._api(f"chat_conversations/{conv_id}/completion"),
                provider=self.name,
                headers=headers,
                data=json.dumps(body).encode(),
                timeout=req.timeout,
                stream=True)

            if response.status_code == 403:
                err = ""
                try:
                    err = response.json().get("error", {}).get("message", "")
                except Exception:
                    pass
                if "not available" in err.lower():
                    raise ProviderError(
                        f"модель {model} недоступна на этом тарифе",
                        self.name)
                raise AuthError(
                    f"403: {err or response.text[:200]}", self.name)
            if response.status_code == 429:
                raise RateLimited("квота claude.ai исчерпана", self.name)
            _http.check(self.name, response)

            got_text = False
            file_paths: list[str] = []
            current_tool = ""
            tool_json = ""

            for event in _http.sse_events(response):
                etype = event.get("type", "")

                if etype == "content_block_start":
                    block = event.get("content_block", {})
                    current_tool = block.get("name", "")
                    tool_json = ""

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    dt = delta.get("type", "")
                    if dt == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            got_text = True
                            yield ("text", text)
                    elif dt == "thinking_delta":
                        thinking = delta.get("thinking", "")
                        if thinking:
                            yield ("thinking", thinking)
                    elif dt == "input_json_delta":
                        tool_json += delta.get("partial_json", "")

                elif etype == "content_block_stop":
                    if current_tool == "present_files" and tool_json:
                        try:
                            parsed = json.loads(tool_json)
                            paths = parsed.get("filepaths", [])
                            file_paths.extend(paths)
                        except ValueError:
                            pass
                    current_tool = ""
                    tool_json = ""

                elif etype == "error":
                    emsg = event.get("error", {}).get("message", str(event))
                    if "rate" in emsg.lower() or "limit" in emsg.lower():
                        raise RateLimited(emsg, self.name)
                    raise ProviderError(emsg, self.name)

                elif etype == "message_limit":
                    info = event.get("message_limit", {})
                    resolved = info.get("resolved", {})
                    limit_info = resolved.get("limit", {})
                    pct = limit_info.get("percent", 0)
                    if pct >= 95:
                        raise RateLimited(
                            f"квота claude.ai {pct}%", self.name)

            if file_paths:
                made = self._fetch_made_files(session, conv_id, file_paths)
                if made:
                    got_text = True
                    yield ("text", made)

            if not got_text:
                raise ProviderError("пустой ответ", self.name)

            if req.conversation is None:
                req.conversation = Conversation(
                    provider=self.name, chat_id=conv_id)
            req.conversation.chat_id = conv_id
            # parent следующего хода = uuid ЭТОГО ответа. Кладём в
            # last_message_id, чтобы метка целиком доехала до клиента и
            # вернулась в следующем запросе (extra отбрасывается).
            req.conversation.last_message_id = assistant_uuid

    def _download_file(self, session, conv_id: str,
                       path: str) -> tuple[str, str, int] | None:
        """Скачать файл из wiggle-песочницы.

        Возвращает ``(имя, data:-ссылка, размер в байтах)`` — размер отдаём
        отдельно, чтобы вызывающему не пришлось декодировать base64 обратно
        ради подсчёта килобайт.
        """
        import mimetypes

        resp = _http.request(
            session, "GET",
            self._api(f"conversations/{conv_id}/wiggle/download-file"
                      f"?path={urllib.parse.quote(path)}"),
            provider=self.name,
            headers=self._headers(),
            timeout=60)
        if resp.status_code != 200:
            return None
        body = resp.content or b""
        if not body or len(body) > MAX_MADE_BYTES:
            return None
        name = path.rsplit("/", 1)[-1]
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        packed = base64.b64encode(body).decode()
        return name, f"data:{mime};base64,{packed}", len(body)

    def _fetch_made_files(self, session, conv_id: str,
                          file_paths: list[str]) -> str:
        """Забрать файлы, созданные Claude, и вернуть разметку со ссылками."""
        links = []
        for path in file_paths[:MAX_MADE_FILES]:
            result = self._download_file(session, conv_id, path)
            if result is None:
                continue
            name, data_url, size = result
            links.append(f"[{name}]({data_url}) — {size // 1024 or 1} КБ")
        if not links:
            return ""
        return "\n\nФайлы: " + " · ".join(links)

    def close(self) -> None:
        pass
