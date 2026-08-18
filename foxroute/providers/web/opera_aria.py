"""Opera Aria — двести запросов на самостоятельно заведённый аккаунт.

Единственный в пуле, кто **регистрирует себя сам**. Остальным нужна кука из
залогиненного браузера; здесь адаптер проходит анонимную регистрацию Opera за
три шага и получает полноценный доступ, не спрашивая человека ни о чём.

Цепочка входа, все три шага обязательны:

1. токен приложения по ``client_credentials`` (идентификатор и секрет — от
   мобильного клиента Opera Mini, они постоянные);
2. анонимная регистрация этим токеном — выдаётся одноразовый ``auth_token``;
3. обмен его на ``refresh_token`` с областью ``shodan:aria``.

**Сервис сам сообщает остаток квоты** — в потоке приходит кадр
``throttling`` с ``requests_available`` и ``requests_max`` (на свежую
регистрацию — 200 запросов). Такое встречается редко: у большинства
веб-сессий остаток не виден вовсе, и о нём узнают, лишь упёршись. Забираем
его в ``remaining``, чтобы наверху было чем распоряжаться.

**Поле ``encryption.key`` обязательно**, хотя ничего у нас не шифрует: сервис
ждёт 32 случайных байта в base64 и без них запрос не принимает. Что он с ними
делает — снаружи не видно.

**Про исчерпание квоты.** Признака автоматического сброса сервис не сообщает
— в кадре только остаток и потолок. Зато каждая новая анонимная регистрация
выдаёт свежие 200: заводится другой аккаунт с другим ``refresh_token``.
Поэтому на исчерпании адаптер регистрируется заново, но РОВНО ОДИН раз за
запрос: превращать это в бесконечную молотилку нельзя, иначе сервис закроет
доступ по адресу целиком — и не только нам.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Iterator

from foxroute.errors import AuthError, ProviderError
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request

#: Постоянные мобильного клиента Opera. Не секрет в обычном смысле — они
#: одинаковы у всех и лежат в самом приложении.
CLIENT_ID = "mini-client"
CLIENT_SECRET = ("Pcc5NvlCrxl02pMw32kO6WrnhpS0pUZ95YrDP8XNKJJQvFht4wQD"
                 "kFJ7v9x5hn7C")

TOKEN_URL = "https://oauth2.opera-api.com/oauth2/v1/token/"
SIGNUP_URL = "https://auth.opera.com/account/v2/external/anonymous/signup"
CHAT_URL = "https://composer.opera-api.com/api/v2/a-chat"

#: Запас перед истечением: обновляемся заранее, чтобы токен не протух
#: посреди длинного ответа.
REFRESH_MARGIN = 60


class OperaAriaProvider(Provider):
    name = "opera_aria"
    #: Веб-поиска как УПРАВЛЯЕМОЙ функции нет: тумблера у Aria не найдено,
    #: а «знает дату» ничего не доказывает — это системный промпт. Кнопку
    #: не заявляем.
    capabilities = Capabilities(text=True, files_in=True, vision=True)

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # Доступ добывается сам, поэтому провайдер годен всегда.
        self.authorized = True
        self._access = ""
        self._until = 0.0
        self._refresh = credential.value or ""
        #: Остаток по данным сервиса. -1 — ещё не спрашивали.
        self.remaining = -1
        self.limit = -1

    # ── вход ──────────────────────────────────────────────────────────

    @staticmethod
    def _mobile_headers() -> dict:
        return {
            "User-Agent": "okhttp/5.3.2",
            "x-requested-with": "XMLHttpRequest",
            "x-opera-client-cache": "1",
        }

    def _register(self, session) -> str:
        """Пройти анонимную регистрацию. Возвращает ``refresh_token``."""
        head = self._mobile_headers()
        form = {**head, "Content-Type": "application/x-www-form-urlencoded"}

        response = _http.request(
            session, "POST", TOKEN_URL, provider=self.name, headers=form,
            data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                  "grant_type": "client_credentials",
                  "scope": "anonymous_account"}, timeout=30)
        _http.check(self.name, response)
        app_token = (response.json() or {}).get("access_token")
        if not app_token:
            raise AuthError("не выдан токен приложения", self.name)

        response = _http.request(
            session, "POST", SIGNUP_URL, provider=self.name,
            headers={**head, "Authorization": f"Bearer {app_token}",
                     "Accept": "application/json",
                     "Content-Type": "application/json; charset=utf-8"},
            json={"client_id": "mini"}, timeout=30)
        _http.check(self.name, response)
        once = (response.json() or {}).get("token")
        if not once:
            raise AuthError("анонимная регистрация не дала токен", self.name)

        response = _http.request(
            session, "POST", TOKEN_URL, provider=self.name, headers=form,
            data={"auth_token": once, "client_id": "mini",
                  "grant_type": "auth_token", "scope": "shodan:aria"},
            timeout=30)
        _http.check(self.name, response)
        granted = response.json() or {}
        self._remember_token(granted)
        refresh = granted.get("refresh_token", "")
        if not refresh:
            raise AuthError("не выдан refresh_token", self.name)
        return refresh

    def _remember_token(self, granted: dict) -> None:
        self._access = granted.get("access_token", "")
        self._until = time.time() + float(
            granted.get("expires_in") or 3600) - REFRESH_MARGIN

    def _token(self, session) -> str:
        """Свежий access-токен: из памяти, обновлением или регистрацией."""
        if self._access and time.time() < self._until:
            return self._access

        if self._refresh:
            response = _http.request(
                session, "POST", TOKEN_URL, provider=self.name,
                headers={**self._mobile_headers(),
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"client_id": "mini", "grant_type": "refresh_token",
                      "refresh_token": self._refresh,
                      "scope": "shodan:aria"}, timeout=30)
            if response.status_code == 200:
                self._remember_token(response.json() or {})
                if self._access:
                    return self._access
            # Обновление не вышло — регистрируемся заново. Аккаунт
            # анонимный и одноразовый, терять нечего.

        self._refresh = self._register(session)
        # Сохраняем добытый доступ, если хранилище подписано: повторная
        # регистрация обнуляет квоту в 200 запросов, и делать её на каждый
        # запуск — значит выбрасывать остаток.
        self.rotated(self._refresh)
        return self._access

    # ── разбор потока ─────────────────────────────────────────────────

    def _absorb_quota(self, frame: dict) -> None:
        """Забрать остаток квоты, если сервис его прислал."""
        throttling = frame.get("throttling")
        if not isinstance(throttling, dict):
            return
        for key, attr in (("requests_available", "remaining"),
                          ("requests_max", "limit")):
            value = throttling.get(key)
            if isinstance(value, int):
                setattr(self, attr, value)

    @staticmethod
    def _text_of(frame: dict) -> str:
        """Кусок ответа из кадра. Пусто — кадр служебный.

        Служебных здесь много: ``metadata`` с рубрикой запроса, ``throttling``
        с остатком, ``general_thinking`` с заглушкой «Just a sec». В текст из
        них не идёт ничего.
        """
        if frame.get("type") == "general_thinking":
            return ""
        answer = frame.get("response")
        if not isinstance(answer, dict):
            return ""
        if answer.get("content_type") != "text":
            return ""
        message = answer.get("message")
        return message if isinstance(message, str) else ""

    def _fresh_account(self, session) -> str:
        """Завести новый анонимный аккаунт вместо исчерпанного."""
        self._access = ""
        self._until = 0.0
        self._refresh = ""
        self.remaining = -1
        token = self._token(session)
        self.rotated(self._refresh)
        return token

    # ── вложения ──────────────────────────────────────────────────────
    #
    # Три шага: попросить подписанный адрес, положить байты в хранилище
    # Google, дождаться готовности опросом. Последний шаг не формальность —
    # сразу после PUT файл ещё ``in_progress``, и запрос с ним уйдёт
    # впустую.
    #
    # Заголовки для PUT сервис присылает сам, полем ``headers``: там среди
    # прочего ``X-Goog-Content-Length-Range``, без которого хранилище
    # отвергает запись. Свои туда подставлять нельзя.

    UPLOAD_URL = "https://composer.opera-api.com/api/v2/files/upload"
    FILES_URL = "https://composer.opera-api.com/api/v2/files/"

    #: Потолок на файл. Как у остальных: вложение едет к нам в base64
    #: (+33% к объёму) и целиком лежит в памяти.
    MAX_UPLOAD = 64 * 1024 * 1024
    #: Сколько ждать готовности. Опрос раз в секунду.
    READY_TRIES = 60

    def _upload(self, session, headers: dict, item) -> str:
        """Положить вложение и вернуть его идентификатор."""
        raw = item.data or b""
        if not raw:
            raise ProviderError("пустое вложение", self.name)
        if len(raw) > self.MAX_UPLOAD:
            raise ProviderError(
                f"файл больше {self.MAX_UPLOAD // 1024 // 1024} МБ", self.name)
        mime = item.mime or "application/octet-stream"

        asked = _http.request(
            session, "POST", self.UPLOAD_URL, provider=self.name,
            headers=headers, json={"mimetype": mime, "size": len(raw)},
            timeout=60)
        _http.check(self.name, asked)
        try:
            place = asked.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на запрос места под файл", self.name) from exc

        file_id = place.get("file_id") or ""
        target = place.get("upload_url") or ""
        if not file_id or not target:
            raise ProviderError(
                f"сервис не выдал место под файл: {str(place)[:200]}",
                self.name)

        given = place.get("headers") or {}
        put = _http.request(
            session, "PUT", target, provider=self.name,
            headers={
                "Content-Type": given.get("Content-Type", mime),
                "X-Goog-Content-Length-Range": given.get(
                    "X-Goog-Content-Length-Range", f"0,{len(raw)}"),
                "Origin": "https://composer.opera-api.com",
            },
            data=raw, timeout=300)
        _http.check(self.name, put)

        for attempt in range(self.READY_TRIES):
            state = _http.request(
                session, "GET", f"{self.FILES_URL}{file_id}",
                provider=self.name, headers=headers, timeout=60)
            _http.check(self.name, state)
            try:
                status = (state.json() or {}).get("upload_status", "")
            except ValueError:
                status = ""
            if status == "finished":
                return file_id
            if status in ("failed", "error"):
                raise ProviderError(
                    f"сервис не принял файл: {status}", self.name)
            time.sleep(min(1 + attempt * 0.3, 2))

        raise ProviderError("файл так и не стал готов", self.name)

    def _stream(self, req: Request) -> Iterator[str]:
        produced = False

        with _http.session() as session:
            token = self._token(session)
            # Остаток мы знаем с прошлого раза: если он на нуле, идти в
            # сервис бессмысленно — сразу берём новый аккаунт.
            if self.remaining == 0:
                token = self._fresh_account(session)
            headers = {
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Origin": "https://composer.opera-api.com",
                "Referer": ("https://composer.opera-api.com"
                            "/assets/aria/index.html"),
                "X-Opera-Timezone": "+02:00",
                "X-Opera-UI-Language": "en",
                "X-Requested-With": "com.opera.mini.native",
            }
            body = {
                "query": req.prompt,
                "sia": True,
                "think_harder": bool(req.thinking),
                "supported_features": [],
                "file_attachments": [self._upload(session, headers, item)
                                     for item in req.attachments],
                # Обязательное поле, см. шапку модуля.
                "encryption": {
                    "key": base64.b64encode(os.urandom(32)).decode()},
            }

            response = _http.request(
                session, "POST", CHAT_URL, provider=self.name,
                headers=headers, json=body, timeout=req.timeout, stream=True)

            # Норма выбрана — заводим новый анонимный аккаунт и повторяем.
            # Ровно один раз: см. шапку модуля про молотилку.
            if response.status_code == 429:
                headers["Authorization"] = f"Bearer {self._fresh_account(session)}"
                response = _http.request(
                    session, "POST", CHAT_URL, provider=self.name,
                    headers=headers, json=body, timeout=req.timeout,
                    stream=True)

            _http.check(self.name, response)

            for line in response.iter_lines():
                if not line:
                    continue
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if payload in ("", "[DONE]", "null"):
                    continue
                try:
                    frame = json.loads(payload)
                except ValueError:
                    continue

                self._absorb_quota(frame)
                piece = self._text_of(frame)
                if piece:
                    produced = True
                    yield piece

        if not produced:
            raise ProviderError("пустой ответ", self.name)
