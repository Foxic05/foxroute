"""Временная диагностика DeepSeek: сырые кадры /chat/completion.

Печатает первые кадры потока как есть (промпт нейтральный). Запуск:
python3 bench/_probe_deepseek.py
"""
from __future__ import annotations

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
        print("беседа создана, id есть:", bool(chat_id))
        pow_response = prov.solver.solve(prov._challenge(session))
        print("POW решён, длина:", len(pow_response or ""))

        resp = _http.request(
            session, "POST", f"{prov.BASE}/chat/completion",
            provider="deepseek", headers=prov._headers(pow_response),
            json={
                "chat_session_id": chat_id,
                "parent_message_id": None,
                "prompt": "Назови три планеты, каждую с новой строки.",
                "ref_file_ids": [],
                "thinking_enabled": False,
                "search_enabled": False,
            },
            cookies=prov._cookies, timeout=(15, 120), stream=True)
        print("HTTP", resp.status_code,
              " content-type:", resp.headers.get("content-type"))
        print("--- первые кадры ---")
        shown = 0
        for line in resp.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            print(repr(text[:220]))
            shown += 1
            if shown >= 30:
                break
        resp.close()


if __name__ == "__main__":
    main()
