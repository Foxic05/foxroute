"""DeepSeek — веб-сессия chat.deepseek.com.

Доступ — ``userToken`` из ``localStorage``.

Каждое обращение к ``/chat/completion`` требует решить задачу
Proof-of-Work — она берётся у сервиса и считается их же модулем
WebAssembly (см. ``_deepseek_pow``).

**Поток устроен патчами, а не готовыми кусками.** Сервис перешёл на
протокол, где путь задаётся один раз, а дальше едут «голые» значения по
последнему пути::

    {"p": "response/content", "o": "APPEND", "v": "Б"}   путь и добавка
    {"v": "ит"}                                          продолжение по пути
    {"p": "response/status", "o": "SET", "v": "FINISHED"}
    {"v": [{…}, {…}]}                                    пачка операций

Рядом по пути ``response/thinking_content`` идут рассуждения модели — в
ответ они не годятся и отфильтровываются. Прежний формат (``choices[].delta``)
поддерживаем тоже: сервис уже переключался, может и откатить.

Держит 200 000 символов входа, медиана хода 2.8 секунды.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

from foxroute.errors import AuthError, ProviderError, RateLimited
from foxroute.paths import app_dir
from foxroute.providers import _http
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)
from foxroute.providers.web._deepseek_pow import Solver


class DeepSeekProvider(Provider):
    name = "deepseek"
    #: Картинки идут ТЕМ ЖЕ путём, что документы, никакого особого режима
    #: не нужно. Ловушка в другом: разбор возвращает ``CONTENT_EMPTY``,
    #: если на картинке нечего распознавать. Одноцветный квадрат и пара
    #: слов мелким шрифтом дают именно его — и это легко принять за
    #: «картинок не умеет». Настоящий снимок экрана разбирается сразу.
    capabilities = Capabilities(text=True, web_search=True,
                                conversations=True, thinking=True,
                                files_in=True, vision=True)

    BASE = "https://chat.deepseek.com/api/v0"
    #: Куда сервис просит нацелить решение задачи.
    POW_TARGET = "/api/v0/chat/completion"
    POW_UPLOAD = "/api/v0/file/upload_file"

    #: Куки нужны, только если Cloudflare выдал челлендж. Обычно файла нет,
    #: и это штатно — шуметь по этому поводу не надо.
    COOKIES_NAME = "deepseek_cookies.json"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        if not credential.value:
            raise ProviderError(
                "нужен userToken из localStorage chat.deepseek.com", self.name)
        self._solver: Solver | None = None
        self._cookies = self._load_cookies()

    def _load_cookies(self) -> dict:
        path = app_dir() / self.COOKIES_NAME
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        cookies = data.get("cookies")
        return cookies if isinstance(cookies, dict) else {}

    @property
    def solver(self) -> Solver:
        """Решатель задачи. Поднимается лениво — он дорогой в создании."""
        if self._solver is None:
            self._solver = Solver()
        return self._solver

    # ── протокол ──────────────────────────────────────────────────────

    def _headers(self, pow_response: str = "") -> dict:
        headers = {
            "accept": "*/*",
            "authorization": f"Bearer {self.credential.value}",
            "content-type": "application/json",
            "origin": "https://chat.deepseek.com",
            "referer": "https://chat.deepseek.com/",
            "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/132.0.0.0 Safari/537.36"),
            "x-app-version": "20241129.1",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-version": "1.0.0-always",
        }
        if pow_response:
            headers["x-ds-pow-response"] = pow_response
        return headers

    def _check_envelope(self, payload: dict) -> None:
        """Проверить конверт ответа.

        Сервис отвечает **HTTP 200 даже на отвергнутый токен**, а отказ
        кладёт в тело::

            {"code": 40003, "msg": "Authorization Failed (invalid token)",
             "data": null}

        Проверки по коду ответа тут недостаточно, и это не мелочь: без
        разбора конверта протухший доступ выглядит как «сервис не выдал
        беседу», то есть как поломка на нашей стороне. Ровно та же ловушка,
        что у Qwen.
        """
        code = payload.get("code")
        if not code:  # 0 или поля нет — всё в порядке
            return
        message = str(payload.get("msg") or "")
        lowered = message.lower()
        if code == 40003 or "authorization" in lowered or "token" in lowered:
            raise AuthError(f"токен отвергнут: {message}", self.name)
        if any(word in lowered for word in
               ("rate", "limit", "frequent", "busy")):
            raise RateLimited(message, self.name)
        raise ProviderError(f"отказ {code}: {message}", self.name)

    def _post(self, session, endpoint: str, body: dict) -> dict:
        response = _http.request(
            session, "POST", f"{self.BASE}{endpoint}", provider=self.name,
            headers=self._headers(), json=body, cookies=self._cookies,
            timeout=(15, 60))
        _http.check(self.name, response)
        try:
            payload = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                f"не JSON в ответе {endpoint}", self.name) from exc
        self._check_envelope(payload)
        return payload

    def _challenge(self, session, target: str = "") -> dict[str, Any]:
        payload = self._post(session, "/chat/create_pow_challenge",
                             {"target_path": target or self.POW_TARGET})
        challenge = (((payload.get("data") or {}).get("biz_data") or {})
                     .get("challenge"))
        if not isinstance(challenge, dict):
            raise ProviderError(
                "сервис не выдал задачу Proof-of-Work", self.name)
        return challenge

    def _open_session(self, session) -> str:
        payload = self._post(session, "/chat_session/create",
                             {"character_id": None})
        chat_id = (((payload.get("data") or {}).get("biz_data") or {})
                   .get("id"))
        if not chat_id:
            raise ProviderError("сервис не выдал беседу", self.name)
        return str(chat_id)

    def _begin(self, session, req: Request) -> tuple[str, Any]:
        """Продолжить беседу или открыть новую. Возвращает (chat_id, parent).

        parent_message_id у DeepSeek числовой. Храним его в last_message_id
        (строкой) — только это поле переживает передачу беседы через клиента;
        extra до провайдера не доходит.
        """
        conv = req.conversation
        if conv and conv.chat_id:
            parent = conv.last_message_id or None
            if parent and str(parent).isdigit():
                parent = int(parent)
            return conv.chat_id, parent
        return self._open_session(session), None

    def _remember(self, req: Request, chat_id: str, response_id: Any) -> None:
        if not chat_id:
            return
        if req.conversation is None:
            req.conversation = Conversation(provider=self.name, chat_id=chat_id)
        req.conversation.chat_id = chat_id
        if response_id is not None:
            req.conversation.last_message_id = str(response_id)

    # ── разбор потока ─────────────────────────────────────────────────

    @staticmethod
    def _apply(operation: Any, state: dict, out: list) -> None:
        """Применить одну операцию патча, сложив результат в ``out``.

        Элементы ``out`` — пары (тип, текст). Тип ``text`` идёт в ответ,
        ``thinking`` отбрасывается вызывающим, ``stop`` завершает поток.
        """
        if not isinstance(operation, dict):
            return

        if "p" in operation:
            state["path"] = operation["p"]
        path = operation.get("p", state.get("path"))
        value = operation.get("v")

        if isinstance(value, list):  # пачка вложенных операций
            for nested in value:
                DeepSeekProvider._apply(nested, state, out)
            return

        if isinstance(value, dict):  # начальный снимок ответа
            response = value.get("response")
            if not isinstance(response, dict):
                return
            # id ответа — им продолжаем беседу (parent следующего хода).
            if response.get("message_id") is not None:
                state["response_id"] = response["message_id"]
            if response.get("content"):
                out.append(("text", response["content"]))
            if response.get("status") in ("FINISHED", "STOP"):
                out.append(("stop", ""))
            return

        if not isinstance(value, str):
            return

        if path == "response/content":
            out.append(("text", value))
        elif path == "response/thinking_content":
            out.append(("thinking", value))
        elif path == "response/status" and value in ("FINISHED", "STOP"):
            out.append(("stop", ""))

    @classmethod
    def _parse(cls, line: bytes | str, state: dict) -> list:
        if isinstance(line, str):
            line = line.encode("utf-8", "replace")
        if not line or not line.startswith(b"data: "):
            return []
        body = line[6:].strip()
        if not body or body in (b"[DONE]", b'"[DONE]"'):
            return []
        try:
            data = json.loads(body)
        except ValueError:
            # Битый кадр пропускаем: рвать поток из-за него значило бы
            # потерять уже полученный ответ.
            return []

        # Прежняя схема, похожая на OpenAI. Сервис с неё ушёл, но откат
        # обошёлся бы нам молчащим провайдером.
        if isinstance(data, dict) and data.get("choices"):
            choice = data["choices"][0]
            delta = choice.get("delta") or {}
            out = []
            if delta.get("content"):
                kind = "thinking" if delta.get("type") == "thinking" else "text"
                out.append((kind, delta["content"]))
            if choice.get("finish_reason"):
                out.append(("stop", ""))
            return out

        out: list = []
        cls._apply(data, state, out)
        return out

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Проще, чем у прочих: один POST с файлом, в ответ идентификатор,
    # который кладётся в ``ref_file_ids`` запроса. Ни промежуточного
    # хранилища, ни подтверждения готовности.
    #
    # Тело собираем руками: наш HTTP-слой работает через ``curl_cffi``
    # (он нужен ради отпечатка браузера), а тот параметра ``files`` не
    # поддерживает вовсе.

    #: Потолок на файл. См. заметку у метода загрузки.
    MAX_UPLOAD = 64 * 1024 * 1024

    #: Сколько ждать готовности файла и с каким шагом. Сразу после загрузки
    #: он ``PENDING``, а чат берёт только готовый.
    READY_TRIES = 45
    READY_PAUSE = 2.0

    def _upload(self, item) -> str:
        """Залить файл. Возвращает его идентификатор у сервиса."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("вложение пустое", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)

        name = item.filename or "file.bin"
        mime = item.mime or "application/octet-stream"
        body, ctype = _http.multipart(filename=name, data=raw,
                                      content_type=mime)

        # Своя сессия и НИЖЕ протоколом. По HTTP/2 их приёмник рвёт поток на
        # теле файла: ответ приходит бодрый, а файл навсегда остаётся
        # ``PENDING``, и отказ вылезает потом у чата — «invalid ref file id».
        # Догадаться неоткуда, отсюда отдельная сессия ради одного запроса.
        with _http.session(http1=True) as session:
            # Ключи заголовков у нас в нижнем регистре, и добавить свой
            # "Content-Type" мало: в словаре это ДРУГОЙ ключ, уедут оба сразу
            # — и json, и multipart. Сервер на это отвечает «Invalid
            # boundary». Задача Proof-of-Work тут СВОЯ, под путь загрузки:
            # с решением от пути беседы приходит 40301 INVALID_POW_RESPONSE.
            proof = self.solver.solve(
                self._challenge(session, self.POW_UPLOAD))
            headers = {k: v for k, v in self._headers(proof).items()
                       if k.lower() != "content-type"}
            headers["content-type"] = ctype
            headers["accept"] = "application/json"

            response = _http.request(
                session, "POST", f"{self.BASE}/file/upload_file",
                provider=self.name, headers=headers, data=body,
                cookies=self._cookies, timeout=300)
            _http.check(self.name, response)

            try:
                payload = response.json() or {}
            except ValueError as exc:
                raise ProviderError(
                    "загрузка: ответ не JSON", self.name) from exc
            self._check_envelope(payload)

            file_id = ((payload.get("data") or {}).get("biz_data")
                       or payload.get("data") or {}).get("id")
            if not file_id:
                raise ProviderError(
                    f"сервис не вернул id файла: {str(payload)[:150]}",
                    self.name)

            self._await_file(session, str(file_id))
        return str(file_id)

    #: Состояния разбора. ``PARSING`` — промежуточное и появляется не сразу
    #: после ``PENDING``; считать его концом нельзя, но и ошибкой тоже.
    BUSY_STATES = ("", "PENDING", "PARSING")

    def _await_file(self, session, file_id: str) -> None:
        """Дождаться, пока файл станет ``SUCCESS``.

        Проверяется через ``fetch_files``, и ТОЛЬКО с параметром
        ``file_ids``: без него тот же путь честно отвечает пустым списком —
        выглядит как «файла нет», хотя файл есть.
        """
        status = ""
        for _ in range(self.READY_TRIES):
            response = _http.request(
                session, "GET", f"{self.BASE}/file/fetch_files",
                provider=self.name, headers=self._headers(),
                params={"file_ids": file_id}, cookies=self._cookies,
                timeout=60)
            _http.check(self.name, response)
            try:
                found = (((response.json() or {}).get("data") or {})
                         .get("biz_data") or {}).get("files") or []
            except ValueError:
                found = []
            status = (found[0].get("status") if found else "") or ""
            if status == "SUCCESS":
                return
            if status == "CONTENT_EMPTY":
                # Разбор прошёл и не нашёл ничего. Отличать это от поломки
                # важно: причина не наша и повтор не поможет.
                raise ProviderError(
                    "сервис не нашёл в файле ничего для чтения — пустой "
                    "документ или картинка без различимого содержимого",
                    self.name)
            if status not in self.BUSY_STATES:
                raise ProviderError(
                    f"сервис не смог разобрать файл: {status}", self.name)
            time.sleep(self.READY_PAUSE)
        raise ProviderError(
            f"файл так и не стал готов за "
            f"{int(self.READY_TRIES * self.READY_PAUSE)} с "
            f"(последнее состояние {status or 'неизвестно'})", self.name)

    def _pairs(self, req: Request) -> Iterator[tuple[str, str]]:
        """Общее тело потока: пары ``("text"|"thinking", кусок)``.

        ``_stream`` и ``stream_rich`` различались лишь тем, выбрасывать ли
        мысли, — весь сетевой обмен был скопирован дважды, и правку (напр.
        ``finally: response.close()``) приходилось зеркалить руками. Теперь
        обмен здесь один раз, а обёртки только фильтруют.
        """
        with _http.session() as session:
            chat_id, parent = self._begin(session, req)
            # Файлы заливаем ДО решения задачи: она одноразовая и
            # протухает, пока идёт закачка большого вложения.
            file_ids = [self._upload(item) for item in req.attachments]
            pow_response = self.solver.solve(self._challenge(session))

            response = _http.request(
                session, "POST", f"{self.BASE}/chat/completion",
                provider=self.name, headers=self._headers(pow_response),
                json={
                    "chat_session_id": chat_id,
                    "parent_message_id": parent,
                    "prompt": req.prompt,
                    "ref_file_ids": file_ids,
                    "thinking_enabled": req.thinking,
                    "search_enabled": req.web_search,
                },
                cookies=self._cookies,
                # Раздельные таймауты: read прерывает поток, если сервер
                # замолчал посреди ответа. Без него чтение блокировалось
                # навсегда и вешало вызывающий поток.
                timeout=(15, 120), stream=True)
            _http.check(self.name, response)

            state: dict = {}
            produced = False
            try:
                for line in response.iter_lines():
                    finished = False
                    for kind, text in self._parse(line, state):
                        if kind == "stop":
                            finished = True
                            break
                        if kind in ("text", "thinking") and text:
                            produced = True
                            yield (kind, text)
                    if finished:
                        break
                self._remember(req, chat_id, state.get("response_id"))
            finally:
                # На завершении мы делаем break, НЕ дочитав тело. Без явного
                # закрытия сокет и его поток остаются висеть и копятся,
                # доводя до «Too many open files».
                try:
                    response.close()
                except Exception:  # noqa: BLE001 — уборка не должна ронять ответ
                    pass

            if not produced:
                raise ProviderError("пустой ответ", self.name)

    def _stream(self, req: Request) -> Iterator[str]:
        for kind, piece in self._pairs(req):
            if kind == "text":
                yield piece

    def stream_rich(self, req: Request) -> Iterator[tuple[str, str]]:
        """Поток с размышлениями: ``("thinking", кусок)`` и ``("text", кусок)``.

        При ``req.thinking=True`` сервис присылает ``response/thinking_content``
        перед ответом.
        """
        self.validate(req)
        yield from self._pairs(req)
