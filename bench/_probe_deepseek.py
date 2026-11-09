"""Временная диагностика DeepSeek: структура фрагментов при thinking.

Печатает структурные кадры (с «p» или снимок) целиком, текстовые {"v":"…"}
сворачивает. Промпт нейтральный. Запуск: python3 bench/_probe_deepseek.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foxroute.accounts import Accounts
from foxroute.providers import _http, build


def main() -> None:
    pool = Accounts().usable("deepseek")
    if not pool:
        print("нет учётки deepseek")
        return
    prov = build("deepseek", pool[0].credential())

    with _http.session() as session:
        chat_id = prov._open_session(session)
        pow_response = prov.solver.solve(prov._challenge(session))
        resp = _http.request(
            session, "POST", f"{prov.BASE}/chat/completion",
            provider="deepseek", headers=prov._headers(pow_response),
            json={
                "chat_session_id": chat_id,
                "parent_message_id": None,
                "prompt": "Назови три планеты. Сначала коротко подумай.",
                "ref_file_ids": [],
                "thinking_enabled": True,
                "search_enabled": False,
            },
            cookies=prov._cookies, timeout=(15, 120), stream=True)
        print("HTTP", resp.status_code)
        text_run = 0
        for line in resp.iter_lines():
            if not line:
                continue
            s = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            if s.startswith("data: "):
                body = s[6:]
                try:
                    data = json.loads(body)
                except ValueError:
                    continue
                # Чистый {"v": "строка"} — это добавка текста, сворачиваем.
                if isinstance(data, dict) and set(data) == {"v"} and isinstance(data["v"], str):
                    text_run += 1
                    continue
                if text_run:
                    print(f"      …{text_run} кадров-добавок…")
                    text_run = 0
                print(json.dumps(data, ensure_ascii=False)[:600])
            else:
                if text_run:
                    print(f"      …{text_run} кадров-добавок…")
                    text_run = 0
                print(repr(s))
        resp.close()


if __name__ == "__main__":
    main()
