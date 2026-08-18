"""Адаптеры провайдеров.

Разделены по природе доступа, потому что это разные звери:

* ``web/`` — веб-сессии по кукам. Протоколы сняты с живого трафика, ломаются
  без предупреждения, квота считается в сообщениях и обычно не видна.
  Это основа пула.
* ``api/`` — официальные API по ключу. Протокол опубликован, лимиты приходят
  заголовками, зато ключ конечен. Роль вспомогательная.

Каждый провайдер — свой модуль: протокол одного сервиса не должен зависеть
от протокола другого. Импорт ленивый, потому что у части адаптеров тяжёлые
или необязательные зависимости (у DeepSeek — своя библиотека обхода
Cloudflare, у Z.ai — Node с Puppeteer для капчи, у API — пакеты вендоров),
и отсутствие одной из них не повод ронять весь пакет.
"""
from __future__ import annotations

from typing import Callable

from foxroute.errors import AuthError, ProviderError
from foxroute.providers.base import (
    Attachment,
    Capabilities,
    Conversation,
    Credential,
    Provider,
    Request,
)

__all__ = ["Attachment", "Capabilities", "Conversation", "Credential",
           "Provider", "Request", "build", "capabilities_of", "implemented",
           "kind_of"]

WEB = "web"
API = "api"


def _web_qwen():
    from foxroute.providers.web.qwen import QwenProvider

    return QwenProvider


def _web_kimi():
    from foxroute.providers.web.kimi import KimiProvider

    return KimiProvider


def _web_alice():
    from foxroute.providers.web.alice import AliceProvider

    return AliceProvider


def _web_chatgpt():
    from foxroute.providers.web.chatgpt import ChatGPTProvider

    return ChatGPTProvider


def _web_mistral():
    from foxroute.providers.web.mistral import MistralProvider

    return MistralProvider


def _web_perplexity():
    from foxroute.providers.web.perplexity import PerplexityProvider

    return PerplexityProvider


def _web_gemini():
    from foxroute.providers.web.gemini_web import GeminiWebProvider

    return GeminiWebProvider


def _web_deepai():
    from foxroute.providers.web.deepai import DeepAIProvider

    return DeepAIProvider


def _web_bing():
    from foxroute.providers.web.bing import BingImagesProvider

    return BingImagesProvider


def _web_poe():
    from foxroute.providers.web.poe import PoeProvider

    return PoeProvider


def _web_manus():
    from foxroute.providers.web.manus import ManusProvider

    return ManusProvider


def _web_grok():
    from foxroute.providers.web.grok import GrokProvider

    return GrokProvider


def _web_meta():
    from foxroute.providers.web.meta import MetaAIProvider

    return MetaAIProvider


def _web_copilot():
    from foxroute.providers.web.copilot import MSCopilotProvider

    return MSCopilotProvider


def _web_deepseek():
    from foxroute.providers.web.deepseek import DeepSeekProvider

    return DeepSeekProvider


def _web_zai():
    from foxroute.providers.web.zai import ZaiProvider

    return ZaiProvider


def _web_pi():
    from foxroute.providers.web.pi import PiProvider

    return PiProvider


def _web_venice():
    from foxroute.providers.web.venice import VeniceProvider

    return VeniceProvider


def _web_yqcloud():
    from foxroute.providers.web.yqcloud import YqcloudProvider

    return YqcloudProvider


def _web_pollinations():
    from foxroute.providers.web.pollinations import PollinationsProvider

    return PollinationsProvider


def _web_openaifm():
    from foxroute.providers.web.openaifm import OpenAIFMProvider

    return OpenAIFMProvider


def _web_opera_aria():
    from foxroute.providers.web.opera_aria import OperaAriaProvider

    return OperaAriaProvider


def _api_groq():
    from foxroute.providers.api.openai_compat import GroqProvider

    return GroqProvider


def _api_openrouter():
    from foxroute.providers.api.openai_compat import OpenRouterProvider

    return OpenRouterProvider


def _web_claude():
    from foxroute.providers.web.claude_web import ClaudeWebProvider

    return ClaudeWebProvider


def _api_cohere():
    from foxroute.providers.api.openai_compat import CohereProvider

    return CohereProvider


def _api_llm7():
    from foxroute.providers.api.openai_compat import Llm7Provider

    return Llm7Provider


def _api_cloudflare():
    from foxroute.providers.api.openai_compat import CloudflareProvider

    return CloudflareProvider


def _api_agentrouter():
    from foxroute.providers.api.openai_compat import AgentRouterProvider

    return AgentRouterProvider


def _api_gemini():
    from foxroute.providers.api.gemini import GeminiAPIProvider

    return GeminiAPIProvider


#: Ключ реестра -> (природа, загрузчик класса).
#:
#: Список намеренно НЕ совпадает с registry.PROVIDER_CONFIGS: в нём только
#: реализованные адаптеры, а не всё, что описано в реестре.
_LOADERS: dict[str, tuple[str, Callable[[], type[Provider]]]] = {
    "qwen":        (WEB, _web_qwen),
    "kimi":        (WEB, _web_kimi),
    "alice":       (WEB, _web_alice),
    "chatgpt":     (WEB, _web_chatgpt),
    "mistral":     (WEB, _web_mistral),
    "perplexity":  (WEB, _web_perplexity),
    "gemini_web":  (WEB, _web_gemini),
    "deepai":      (WEB, _web_deepai),
    "pi":          (WEB, _web_pi),
    "zai":         (WEB, _web_zai),
    "deepseek":    (WEB, _web_deepseek),
    "ms_copilot":  (WEB, _web_copilot),
    "meta_ai":     (WEB, _web_meta),
    "grok":        (WEB, _web_grok),
    "manus":       (WEB, _web_manus),
    "poe":         (WEB, _web_poe),
    "bing_images": (WEB, _web_bing),
    "venice":      (WEB, _web_venice),
    "yqcloud":     (WEB, _web_yqcloud),
    "pollinations": (WEB, _web_pollinations),
    "openai_fm":   (WEB, _web_openaifm),
    "opera_aria":  (WEB, _web_opera_aria),
    "groq":            (API, _api_groq),
    "openrouter":      (API, _api_openrouter),
    "claude_web":      (WEB, _web_claude),
    "cohere":          (API, _api_cohere),
    "llm7":            (API, _api_llm7),
    "agentrouter":     (API, _api_agentrouter),
    "cloudflare":      (API, _api_cloudflare),
    "gemini_api":      (API, _api_gemini),
}


def implemented(kind: str | None = None) -> list[str]:
    """Реализованные провайдеры. Можно спросить только веб или только API."""
    return sorted(name for name, (which, _) in _LOADERS.items()
                  if kind is None or which == kind)


def kind_of(name: str) -> str:
    """Природа провайдера: веб-сессия или официальный API."""
    entry = _LOADERS.get(name)
    return entry[0] if entry else ""


#: Разобранные возможности — чтобы не грузить модуль адаптера на каждый
#: вопрос «умеет ли он искать в сети».
_CAPABILITIES: dict[str, Capabilities] = {}


def capabilities_of(name: str) -> Capabilities:
    """Что провайдер умеет — со СЛОВ самого адаптера, а не из списка.

    Возможности объявлены атрибутом класса, поэтому читаются без создания
    экземпляра: сеть не трогается, доступ не нужен. Перечислять умения
    списком в другом модуле нельзя — он разойдётся с адаптерами при первой
    же правке.
    """
    if name in _CAPABILITIES:
        return _CAPABILITIES[name]
    entry = _LOADERS.get(name)
    if entry is None:
        return Capabilities(text=False)
    try:
        found = entry[1]().capabilities
    except Exception:  # noqa: BLE001 — нет зависимости, значит не умеет
        found = Capabilities(text=False)
    _CAPABILITIES[name] = found
    return found


def build(name: str, credential: Credential, model: str = "",
          on_rotate=None, allow_anonymous: bool = False) -> Provider:
    """Поднять адаптер по ключу реестра.

    ``on_rotate`` вызывается, когда сервис сам выдал новый доступ взамен
    старого (так делают Kimi и MS Copilot). Хранилище подписывается сюда,
    чтобы решить, записывать ли новое значение.

    ``allow_anonymous`` — разрешить работу БЕЗ аккаунта. По умолчанию нет, и
    это осознанное умолчание. Часть сервисов (Alice, Perplexity, DeepAI, Pi,
    Copilot) отвечает и анонимному гостю, но там урезаны лимиты, нет памяти
    чатов, а у некоторых недоступна часть моделей. Ради лимитов всё и
    затевалось, поэтому свалиться в анонимный режим молча — худшее, что слой
    может сделать: он продолжит отвечать, а ёмкость просядет незаметно.

    Исключение — провайдеры, у которых аккаунта нет как понятия (Z.ai
    выдаёт токен анонимно каждому). Там это не урезанный режим, а
    единственный, и запрет на него бессмыслен.
    """
    entry = _LOADERS.get(name)
    if entry is None:
        raise ProviderError(
            f"адаптер {name!r} не реализован; готовы: "
            f"веб — {', '.join(implemented(WEB)) or 'нет'}; "
            f"API — {', '.join(implemented(API)) or 'нет'}", name)

    provider = entry[1]()(credential, model, on_rotate)

    from foxroute.registry import AUTH_NONE, auth_kind

    if (not provider.authorized and not allow_anonymous
            and auth_kind(name) != AUTH_NONE):
        raise AuthError(
            "нет доступа к аккаунту, а анонимный режим урезан (лимиты ниже, "
            "памяти чатов нет). Дай ключ или разреши явно: "
            "build(..., allow_anonymous=True)", name)
    return provider
