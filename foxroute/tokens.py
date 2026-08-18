"""Разбор токенов доступа без обращения к сервису.

Нужно, чтобы отвечать на вопрос «когда это протухнет» до того, как оно
протухнет посреди задачи.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone


def jwt_expiry(token: str) -> datetime | None:
    """Срок годности JWT из поля ``exp``. None, если это не JWT или срока нет.

    Подпись не проверяется намеренно: ключ не наш, и вопрос здесь не
    «подлинный ли токен», а «не пора ли его менять».

    Практический смысл: у Qwen обновления токена нет вообще, его
    перевставляют руками, и нигде не написано, сколько он живёт. А срок
    лежит прямо внутри. У Manus по той же причине видно, что кука
    ``session_id`` — это JWT на 90 дней.
    """
    if not token or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # base64url приходит без набивки
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    exp = data.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def expires_in_seconds(token: str) -> float | None:
    """Сколько секунд осталось. Отрицательное — уже протух."""
    exp = jwt_expiry(token)
    if exp is None:
        return None
    return (exp - datetime.now(timezone.utc)).total_seconds()
