"""Перепроверка окна контекста ИГОЛКОЙ и откат липовых значений.

Accept-тест (в measure.py) считает контекстом наибольший вход, на который
провайдер ВООБЩЕ ответил. Кто молча срезает вход до своего окна и всё же
отвечает — получает завышенное число. Здесь отделяем реальный контекст от
обрезки:

* кладём 6-значное ЧИСЛО-МЕТКУ в САМОЕ НАЧАЛО входа, а вопрос про неё — в
  ХВОСТ (он переживает обрезку с головы, которой режут почти все);
* если модель вернула метку — она прочитала вход целиком, размер реальный;
* если нет — спускаемся по лестнице до размера, где метка удерживается.

Страховка от занижения: если метку не вернул даже на 4k, значит иголка для
этой модели ненадёжна (не выполняет инструкцию) — НЕ трогаем, оставляем как
есть. Перепроверяем только тех, кому accept намерил >= FLOOR.

    python bench/verify_context.py            # все с контекстом >= FLOOR
    python bench/verify_context.py deepai alice
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foxroute import measurements                       # noqa: E402
from foxroute.accounts import open_provider             # noqa: E402
from foxroute.errors import ContextTooLarge, RateLimited  # noqa: E402
from foxroute.providers import capabilities_of, implemented  # noqa: E402
from foxroute.providers.base import Request              # noqa: E402

LADDER = [4000, 16000, 48000, 100000, 200000]
MARK = "849271"
WORKERS = 4
FLOOR = 48000


def big(size: int) -> str:
    head = "Число-метка в начале документа: " + MARK + ".\n\n"
    tail = ("\n\nВопрос: какое число-метка указано в САМОМ НАЧАЛЕ документа? "
            "Ответь только этим числом.")
    filler = "Строка-наполнитель для проверки окна контекста, без чисел. " * (
        size // 60 + 80)
    body = filler[: max(1, size - len(head) - len(tail))]
    return head + body + tail


def retained(name: str, size: int) -> bool:
    """Вернул ли провайдер метку из начала при входе ``size`` символов."""
    provider = open_provider(name, allow_anonymous=True)
    try:
        out = ""
        for piece in provider.stream(Request(prompt=big(size), timeout=180)):
            out += piece
            if MARK in out:
                return True
        return MARK in out
    except ContextTooLarge:
        return False
    finally:
        try:
            provider.close()
        except Exception:  # noqa: BLE001
            pass


def verify(name: str) -> dict:
    measured = measurements.get(name).context_chars
    try:
        if not retained(name, 4000):
            return {"skip": "не вернул метку и на 4k — иголке не верю"}
    except RateLimited:
        return {"skip": "норма исчерпана"}
    except Exception as exc:  # noqa: BLE001
        return {"skip": f"отказ: {type(exc).__name__}"}

    real = 4000
    for size in reversed([s for s in LADDER if 4000 < s <= measured]):
        try:
            if retained(name, size):
                real = size
                break
        except RateLimited:
            return {"real": real, "partial": True}
        except Exception:  # noqa: BLE001
            continue
    return {"real": real}


def main(argv: list[str]) -> int:
    want = [a for a in argv if not a.startswith("-")]
    if want:
        targets = want
    else:
        targets = [n for n in sorted(implemented())
                   if capabilities_of(n).text
                   and measurements.get(n).context_chars >= FLOOR]
    print(f"перепроверка контекста иголкой: {len(targets)} провайдеров "
          f"(accept намерил >= {FLOOR}), {WORKERS} параллельно\n", flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(verify, n): n for n in targets}
        for fut in as_completed(futs):
            name = futs[fut]
            done += 1
            cur = measurements.get(name)
            measured = cur.context_chars
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[{done}/{len(futs)}] {name}: ошибка {exc}", flush=True)
                continue
            if "skip" in res:
                print(f"[{done}/{len(futs)}] {name}: {res['skip']} → "
                      f"оставляю {measured}", flush=True)
                continue
            real = res["real"]
            note = " (частично — норма исчерпана)" if res.get("partial") else ""
            if real < measured:
                measurements.save(name, replace(cur, context_chars=real))
                print(f"[{done}/{len(futs)}] {name}: иголка держит {real}, "
                      f"accept завысил до {measured} → ОТКАТ{note}", flush=True)
            else:
                print(f"[{done}/{len(futs)}] {name}: {measured} подтверждён "
                      f"иголкой{note}", flush=True)
    print("\nготово — measurements.json поправлен.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
