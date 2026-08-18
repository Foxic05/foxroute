"""OpenAI.fm — озвучка одиннадцатью голосами без ключа.

Витрина голосов OpenAI, открытая для всех: обычный GET, текст и голос в
параметрах, в ответ MP3. Ни ключа, ни куки, ни очереди.

Ради чего берём: наш основной синтез идёт через Orpheus на Groq, а там сотня
запросов в сутки на аккаунт и отдельное принятие условий, которое сделано не
везде. Здесь потолка нет, и это делает озвучку в интерфейсе доступной всегда,
а не до полудня.

**Обычным ``curl`` его не проверить** — отдаёт страницу блокировки с кодом
429. Нужен отпечаток настоящего браузера, который даёт ``_http``; на этом
легко сделать ложный вывод, что сервис мёртв.

**Стилевые указания свои, а не их.** У витрины к каждому стилю приложен
абзац-описание манеры чтения; поле принимает произвольный текст, поэтому
пишем короткие свои формулировки вместо переноса чужих.
"""
from __future__ import annotations

import urllib.parse
from typing import Iterator

from foxroute.errors import ProviderError, Unsupported
from foxroute.providers import _http
from foxroute.providers.base import Capabilities, Credential, Provider, Request


class OpenAIFMProvider(Provider):
    name = "openai_fm"
    #: Только озвучка: текста этот сервис не порождает.
    capabilities = Capabilities(text=False, streaming=False)

    ENDPOINT = "https://www.openai.fm/api/generate"
    SITE = "https://www.openai.fm"

    #: Голоса витрины. Это имена параметров сервиса, менять нельзя.
    VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable",
              "onyx", "nova", "sage", "shimmer", "verse")
    DEFAULT_VOICE = "coral"

    #: Манера чтения. Поле свободное, поэтому формулировки короткие и свои.
    STYLES = {
        "friendly": "Читай тепло и приветливо, спокойным ровным темпом.",
        "calm": "Читай размеренно и негромко, с мягкими паузами.",
        "teacher": "Читай как терпеливый преподаватель: чётко, "
                   "с паузами после важных мест.",
        "news": "Читай как диктор новостей: собранно, внятно, без эмоций.",
        "story": "Читай как рассказчик: живо, с интонационными переходами.",
    }

    #: Отдаём MP3, а не WAV — вызывающему нужно знать, чтобы проставить тип.
    TTS_MIME = "audio/mpeg"

    #: Потолок длины. Сервис задуман под короткие пробы голоса, и на
    #: длинном тексте начинает отвечать долго либо обрывать.
    MAX_CHARS = 2000

    def __init__(self, credential: Credential, model: str = "", on_rotate=None):
        super().__init__(credential, model, on_rotate)
        self.authorized = True

    def _stream(self, req: Request) -> Iterator[str]:
        raise Unsupported("OpenAI.fm только озвучивает", self.name)
        yield  # pragma: no cover — делает функцию генератором

    def synthesize(self, text: str, voice: str = DEFAULT_VOICE,
                   style: str = "friendly") -> bytes:
        """Озвучить текст. Возвращает MP3."""
        if voice not in self.VOICES:
            voice = self.DEFAULT_VOICE
        clip = (text or "").strip()[:self.MAX_CHARS]
        if not clip:
            raise ProviderError("нечего озвучивать", self.name)

        query = urllib.parse.urlencode({
            "input": clip,
            "voice": voice,
            "prompt": self.STYLES.get(style, self.STYLES["friendly"]),
        })

        with _http.session() as session:
            response = _http.request(
                session, "GET", f"{self.ENDPOINT}?{query}", provider=self.name,
                headers={"Referer": f"{self.SITE}/"}, timeout=90)
            _http.check(self.name, response)

            kind = (response.headers.get("content-type") or "").lower()
            if not kind.startswith("audio/"):
                raise ProviderError(
                    f"вместо звука пришло {kind or 'непонятно что'}",
                    self.name)
            if len(response.content) < 500:
                raise ProviderError("запись подозрительно мала", self.name)
            return response.content
