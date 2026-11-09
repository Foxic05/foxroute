"""Временная диагностика DeepSeek: что отвечает /chat_session/create.

Печатает ТОЛЬКО форму ответа — строки как «<str N>», чтобы ни токен, ни
id беседы не попали в вывод. Запуск: python3 bench/_probe_deepseek.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foxroute.accounts import Accounts
from foxroute.providers import _http, build


def shape(x):
    if isinstance(x, dict):
        return {k: shape(v) for k, v in x.items()}
    if isinstance(x, list):
        return [shape(x[0]), f"...x{len(x)}"] if x else []
    if isinstance(x, str):
        return f"<str {len(x)}>"
    return x


def main() -> None:
    pool = Accounts().usable("deepseek")
    if not pool:
        print("нет учётки deepseek")
        return
    prov = build("deepseek", pool[0].credential())

    with _http.session() as session:
        for body in ({"character_id": None}, {}):
            print(f"\n=== POST /chat_session/create  тело={body} ===")
            try:
                resp = _http.request(
                    session, "POST", f"{prov.BASE}/chat_session/create",
                    provider="deepseek", headers=prov._headers(),
                    json=body, cookies=prov._cookies, timeout=(15, 60))
            except Exception as exc:  # noqa: BLE001
                print(f"запрос упал: {type(exc).__name__}: {exc}")
                continue
            print("HTTP", resp.status_code)
            try:
                payload = resp.json() or {}
            except ValueError:
                print("не JSON, первые 400 симв:", repr(resp.text[:400]))
                continue
            print("code:", payload.get("code"), " msg:", payload.get("msg"))
            print("форма:", json.dumps(shape(payload), ensure_ascii=False)[:900])


if __name__ == "__main__":
    main()
