"""Пересъём замеров для всех текстовых провайдеров: СКОРОСТЬ и КОНТЕКСТ.

Агентность здесь не перемеряем — остаются две живые величины:

* **скорость хода** (``median_turn_sec``) — медиана времени полного ответа
  на короткий промпт, 2 пробы;
* **окно контекста** (``context_chars``) — по определению из
  ``measurements.py``: наибольший размер входа, который провайдер ПРИНЯЛ и
  на который ВЕРНУЛ ответ. Шлём наполнитель нужной длины с короткой
  инструкцией в ХВОСТЕ (переживает обрезку с головы) и смотрим, пришёл ли
  непустой ответ. Идём по лестнице ОТ ТЕКУЩЕГО значения: подтверждаем его
  и пробуем ступень выше, а не прошло — спускаемся. 1-3 запроса на
  провайдера, без двоичного поиска от нуля.

  Оговорка: провайдер, который молча срезает вход до своего окна и всё
  равно отвечает, покажет высокое число — это заложено в само определение
  метрики (важно, что он «взял и ответил»), и так же считались заводские.
  Кто отбивает лишнее ошибкой/413 — тому находим реальный потолок.

Мерим на живых, параллельно (разные сессии не мешают). Провайдер, который
не поднялся или исчерпал норму, сохраняет прежние числа.

    python bench/measure.py            # все текстовые
    python bench/measure.py qwen kimi  # выбранные
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foxroute import measurements                       # noqa: E402
from foxroute.accounts import open_provider             # noqa: E402
from foxroute.errors import (  # noqa: E402
    AuthError, ContextTooLarge, ProviderError, RateLimited)
from foxroute.providers import capabilities_of, implemented  # noqa: E402
from foxroute.providers.base import Request              # noqa: E402

#: Тот же промпт, что в smoke.py — чтобы числа были сопоставимы с заводскими.
PROMPT = ("Перечисли пять планет Солнечной системы. "
          "Каждую с новой строки, с одним фактом о ней.")
SAMPLES = 2
WORKERS = 4

#: Лестница размеров входа (символы). Совпадает с корзинами, в которых
#: заведены заводские значения, — чтобы числа были сопоставимы.
LADDER = [4000, 16000, 48000, 100000, 200000]


def measure_turn(provider_factory) -> tuple[float | None, str]:
    """Медиана времени полного ответа за SAMPLES проб."""
    times: list[float] = []
    for _ in range(SAMPLES):
        provider = provider_factory()
        try:
            t0 = time.time()
            got = False
            for _piece in provider.stream(Request(prompt=PROMPT, timeout=120)):
                got = True
            if got:
                times.append(time.time() - t0)
        except RateLimited:
            return (round(statistics.median(times), 1) if times else None,
                    "норма исчерпана")
        except Exception as exc:  # noqa: BLE001
            return (round(statistics.median(times), 1) if times else None,
                    f"отказ: {type(exc).__name__}")
        finally:
            try:
                provider.close()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)
    return (round(statistics.median(times), 1) if times else None, "ok")


def _big_input(size: int) -> str:
    """Наполнитель на ``size`` символов с инструкцией в хвосте.

    Хвост — чтобы у провайдера, срезающего вход с головы, инструкция
    уцелела и он всё-таки ответил (нам важно, что размер он ПРИНЯЛ).
    """
    tail = "\n\nВыше — длинный текст. Ответь одним коротким словом: ок."
    filler = "Текст для проверки размера окна контекста. " * (size // 42 + 60)
    body = filler[: max(1, size - len(tail))]
    return body + tail


def _accepts(provider_factory, size: int) -> bool:
    """Принял ли провайдер вход в ``size`` символов и вернул непустой ответ."""
    provider = provider_factory()
    try:
        out = ""
        for piece in provider.stream(Request(prompt=_big_input(size),
                                             timeout=180)):
            out += piece
            if len(out) >= 2:
                return True
        return len(out.strip()) > 0
    except ContextTooLarge:
        # Вход длиннее окна — это и есть потолок, а не сбой: «не принял».
        return False
    finally:
        try:
            provider.close()
        except Exception:  # noqa: BLE001
            pass


def measure_context(provider_factory, current: int) -> tuple[int | None, str]:
    """Окно контекста «принял/не принял», адаптивно от текущего значения."""
    start = min(range(len(LADDER)), key=lambda i: abs(LADDER[i] - current))
    best = 0
    try:
        if _accepts(provider_factory, LADDER[start]):
            best = LADDER[start]
            for j in range(start + 1, len(LADDER)):    # вдруг подросло
                if _accepts(provider_factory, LADDER[j]):
                    best = LADDER[j]
                else:
                    break
        else:
            for j in range(start - 1, -1, -1):          # спускаемся
                if _accepts(provider_factory, LADDER[j]):
                    best = LADDER[j]
                    break
    except RateLimited:
        return (best or None), "норма исчерпана"
    except Exception as exc:  # noqa: BLE001
        return (best or None), f"отказ: {type(exc).__name__}"
    if not best:
        return None, "не принял ни одного размера"
    return best, "ok"


def measure_one(name: str) -> dict:
    """Замерить скорость и контекст одного провайдера. Ничего не сохраняет."""
    def factory():
        return open_provider(name, allow_anonymous=True)

    try:
        factory().close()
    except AuthError:
        return {"skip": "нет учётки"}
    except ProviderError as exc:
        return {"skip": f"не поднялся: {type(exc).__name__}"}
    except Exception as exc:  # noqa: BLE001
        return {"skip": f"не поднялся: {type(exc).__name__}"}

    cur = measurements.get(name)
    turn, turn_note = measure_turn(factory)
    ctx, ctx_note = measure_context(factory, cur.context_chars or 16000)
    return {"turn": turn, "turn_note": turn_note,
            "ctx": ctx, "ctx_note": ctx_note}


def main(argv: list[str]) -> int:
    want = [a for a in argv if not a.startswith("-")]
    targets = want or [n for n in sorted(implemented())
                       if capabilities_of(n).text]
    print(f"замеры: {len(targets)} провайдеров — скорость ({SAMPLES} пробы) + "
          f"контекст (принял/не принял), {WORKERS} параллельно\n", flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(measure_one, n): n for n in targets
                if capabilities_of(n).text}
        for fut in as_completed(futs):
            name = futs[fut]
            done += 1
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[{done}/{len(futs)}] {name}: ошибка {exc}", flush=True)
                continue
            cur = measurements.get(name)
            if "skip" in res:
                print(f"[{done}/{len(futs)}] {name}: {res['skip']} → оставляю "
                      f"{cur.context_chars}сим/{cur.median_turn_sec}s",
                      flush=True)
                continue

            new = cur
            bits = []
            if res["turn"] is not None:
                new = replace(new, median_turn_sec=res["turn"])
                tail = "" if res["turn_note"] == "ok" else f" ({res['turn_note']})"
                bits.append(f"{res['turn']}s (было {cur.median_turn_sec}s){tail}")
            else:
                bits.append(f"скорость: {res['turn_note']}")
            if res["ctx"] is not None:
                new = replace(new, context_chars=res["ctx"])
                tail = "" if res["ctx_note"] == "ok" else f" ({res['ctx_note']})"
                bits.append(f"{res['ctx']}сим (было {cur.context_chars}){tail}")
            else:
                bits.append(f"контекст: {res['ctx_note']}")

            if new != cur:
                measurements.save(name, new)
            print(f"[{done}/{len(futs)}] {name}: " + "; ".join(bits), flush=True)

    print("\nготово — measurements.json обновлён (measured_at = сегодня).",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
