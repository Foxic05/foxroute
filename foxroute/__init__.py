"""foxroute — OpenAI-совместимый слой поверх веб-сессий чат-сервисов.

Одна команда на всех провайдеров. Под ней прячется то, что у каждого сервиса
своё: у одного обычный SSE, у другого бинарный WebSocket с самодельным
protobuf, у третьего реверснутая подпись запроса.
"""
from __future__ import annotations

__version__ = "0.0.1"

from foxroute.errors import (
    AuthError,
    ContextTooLarge,
    ProviderError,
    ProviderRefused,
    ProviderUnavailable,
    RateLimited,
    Unsupported,
)

__all__ = [
    "__version__",
    "AuthError",
    "ContextTooLarge",
    "ProviderError",
    "ProviderRefused",
    "ProviderUnavailable",
    "RateLimited",
    "Unsupported",
]
