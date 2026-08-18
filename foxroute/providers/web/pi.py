"""Pi — Inflection Pi (pi.ai).

Доступ — куки ``pi.ai`` строкой «a=b; c=d», нужна ``__Host-session``.

**Cloudflare здесь пускает по набору заголовков, а не по отпечатку TLS.**
Первоначальный вывод «без браузера не пройти» оказался неверным: челлендж
ловит только HTML-страница ``/talk``, а API отвечает, если запрос выглядит
как обращение приложения — ``X-Api-Version``, ``Accept: application/json``
и клиентские подсказки. На HTML мы просто не ходим.

Один из немногих, кто **сам называет остаток**: ``remainingTrialTurns``
приходит в ответе на начало беседы, то есть ДО того, как потратишь запрос.

Заодно ловушка: соседнее поле ``quotaRestorationDate`` — **не обратный
отсчёт**. Указанное время проходит, лимит держится, а значение ползёт
следом. В сообщения его тащить нельзя, будет врать.
"""
from __future__ import annotations

import json
import uuid
from typing import Iterator

from foxroute.errors import ProviderError, RateLimited
from foxroute.providers import _http
from foxroute.providers.base import (
    Capabilities, Conversation, Credential, Provider, Request)

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


class PiProvider(Provider):
    name = "pi"
    capabilities = Capabilities(text=True, conversations=True)

    BASE = "https://pi.ai"

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        # Сервис отвечает и анонимному гостю, просто норма у него своя.
        # Проверено сверкой.
        self.authorized = bool(credential.value)
        #: Остаток бесплатных ходов по словам сервиса. -1 — ещё не спрашивали
        #: или сервис его не назвал (поле есть не у всех аккаунтов).
        self.remaining = -1

    def _headers(self) -> dict:
        """Заголовки, снятые с живого Chrome.

        Состав менять нельзя: именно по нему Cloudflare отличает обращение
        приложения от бота.
        """
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"{self.BASE}/talk",
            "Origin": self.BASE,
            "X-Api-Version": "5",
            "X-Supports-Null-Main-Conversation": "1",
            "x-client-timezone": "Europe/Moscow",
            "sec-ch-ua": ('"Not=A?Brand";v="99", "Google Chrome";v="151", '
                          '"Chromium";v="151"'),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _session(self):
        session = _http.session()
        for part in self.credential.value.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            session.cookies.set(key, value, domain="pi.ai")
        return session

    def _start(self, session) -> str:
        """Начать беседу и вернуть её идентификатор.

        Здесь же узнаём остаток ходов — до того, как потратим запрос. Это
        редкость: из двадцати сервисов остаток отдают семеро.
        """
        response = _http.request(
            session, "POST", f"{self.BASE}/api/chat/start",
            provider=self.name, headers=self._headers(),
            json={"distinctId": str(uuid.uuid4())}, timeout=60)
        _http.check(self.name, response)
        try:
            payload = response.json() or {}
        except ValueError as exc:
            raise ProviderError(
                "не JSON в ответе на начало беседы", self.name) from exc

        left = payload.get("remainingTrialTurns")
        if isinstance(left, int):
            self.remaining = left
            if left <= 0:
                # Знаем заранее — не тратим запрос впустую. Срок не
                # указываем намеренно: quotaRestorationDate врёт.
                raise RateLimited(
                    "бесплатные ходы кончились", self.name)

        conversations = payload.get("conversations") or []
        conversation = ((conversations[0].get("sid") if conversations else "")
                        or payload.get("sid", ""))
        if not conversation:
            raise ProviderError("сервер не вернул беседу", self.name)
        return conversation

    def _begin(self, session, req: Request) -> str:
        """Продолжить беседу или начать новую. Pi держит контекст на уровне
        conversation-sid, отдельная цепочка сообщений не нужна."""
        conv = req.conversation
        if conv and conv.chat_id:
            return conv.chat_id
        return self._start(session)

    def _remember(self, req: Request, conversation: str) -> None:
        if not conversation:
            return
        if req.conversation is None:
            req.conversation = Conversation(provider=self.name,
                                            chat_id=conversation)
        req.conversation.chat_id = conversation

    def _stream(self, req: Request) -> Iterator[str]:
        with self._session() as session:
            conversation = self._begin(session, req)
            response = _http.request(
                session, "POST", f"{self.BASE}/api/chat",
                provider=self.name,
                headers={**self._headers(), "Accept": "text/event-stream"},
                json={"text": req.prompt, "conversation": conversation},
                timeout=req.timeout, stream=True)
            _http.check(self.name, response)
            self._remember(req, conversation)

            # Поток размечен ТИПАМИ событий, и разбирать их обязательно: под
            # `progress` едут служебные размышления по-английски («Let me
            # spend a little time on this»), и без фильтра ответы начинались
            # с этой болтовни.
            #
            # Куски текста приезжают под `partial`. Раньше их нёс `message`,
            # но сервис это поменял: теперь `message` содержит только
            # метаданные (sid, sentAt) без текста. Держим оба — старое
            # поведение может вернуться, а лишний тип ничего не стоит.
            produced = False
            event = ""
            for raw in response.iter_lines():
                if raw is None:
                    continue
                line = (raw.decode("utf-8", "replace")
                        if isinstance(raw, bytes) else raw)
                if line.startswith("event: "):
                    event = line[7:].strip()
                    continue
                if not line.startswith("data: "):
                    continue
                if event not in ("partial", "message", ""):
                    continue
                try:
                    payload = json.loads(line[6:])
                except ValueError:
                    continue
                piece = payload.get("text") if isinstance(payload, dict) else None
                if piece:
                    produced = True
                    yield piece

            if not produced:
                raise ProviderError("пустой ответ", self.name)
