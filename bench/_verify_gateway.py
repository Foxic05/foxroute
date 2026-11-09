"""Проверка шлюза по проводу: реальный запрос к живому серверу на :8777.

Ключ в вывод НЕ печатается — только его наличие и длина. Запуск:
python3 bench/_verify_gateway.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foxroute import settings

BASE = "http://127.0.0.1:8777"


def ask(model: str) -> None:
    key = settings.api_key()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": "Ответь одним словом: столица Франции?"}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions",
                                data=data, headers=headers)
    print(f"\n=== model={model} ===")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        print("HTTP", exc.code, (exc.read() or b"")[:300].decode("utf-8", "replace"))
        return
    except Exception as exc:  # noqa: BLE001
        print("запрос упал:", type(exc).__name__, str(exc)[:200])
        return
    answered = body.get("model")
    choices = body.get("choices") or [{}]
    text = ((choices[0].get("message") or {}).get("content") or "")
    print("ответил провайдер:", answered)
    print("ответ:", text[:120].replace("\n", " "))


def main() -> None:
    key = settings.api_key()
    print("авторизация:",
          f"ключ задан, длина {len(key)}, префикс {key[:4]!r}" if key
          else "ВЫКЛЮЧЕНА (FOXROUTE_API_KEY пуст — клиенту ключ не нужен)")
    for model in ("auto", "deepseek", "chatgpt"):
        ask(model)


if __name__ == "__main__":
    main()
