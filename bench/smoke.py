"""Сверка адаптера с живым сервисом.

Проверяем не «вернулось что-то», а именно то, что легко ломается:
настоящий ли стриминг, не просочились ли служебные потоки, типизированы ли
отказы, отвергается ли неподдержанное без обращения к сети.

    python bench/smoke.py qwen kimi
    python bench/smoke.py --all

Учётка берётся из хранилища (accounts.json); внешний settings.json —
запасной источник для обратной совместимости.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foxroute.errors import AuthError, ProviderError, RateLimited, Unsupported
from foxroute.providers import API, WEB, build, implemented, kind_of
from foxroute.providers.base import Credential, Request
from foxroute.registry import (
    AUTH_FILE,
    AUTH_NONE,
    AUTH_OPTIONAL,
    SILENT_DEGRADE,
    auth_kind,
    measured,
)

SETTINGS = os.environ.get("FOXROUTE_EXTRA_SETTINGS", "")

#: Служебные пометки, которые не должны просочиться в ответ. Каждая из них
#: реально встречалась в потоке: фазы Qwen, размышления Kimi, мысли Pi.
LEAKS = ("phase\":", "thinking_summary", "<think", "reasoning-", "op\":\"set")


def credential(name: str) -> Credential:
    """Первая учётка провайдера — из нового хранилища, иначе из запасного.

    Новое хранилище (accounts.json) — основное: туда добавляются и новые
    провайдеры вроде OpenRouter, которых во внешнем settings.json нет.
    Запасной источник используется ради обратной совместимости.
    """
    # Сначала новое хранилище
    from foxroute.accounts import Accounts
    store = Accounts()
    pool = store.usable(name)
    if pool:
        if len(pool) > 1:
            print(f"  (в пуле {len(pool)} учёток, берём первую)")
        return pool[0].credential()

    # Запасной — внешний settings.json
    if os.path.exists(SETTINGS):
        with open(SETTINGS, encoding="utf-8") as handle:
            data = json.load(handle)
        for entry in data.get("ai_providers", []):
            if entry.get("name") == name:
                expanded = Credential.expand(name, entry.get("key", ""))
                if len(expanded) > 1:
                    print(f"  (в настройках пул из {len(expanded)} ключей, "
                          f"берём первый)")
                return expanded[0]
    return Credential(provider=name)


def corrupt(value: str) -> str:
    """Испортить доступ, СОХРАНИВ его форму.

    Просто подсунуть строку, похожую на JWT, недостаточно: у провайдеров
    разная форма доступа, и адаптер справедливо пожалуется на формат, а не
    на отвергнутый ключ — то есть проверка выродится. Меняем только буквы и
    цифры, оставляя разделители на месте: ``sso|sso-rw`` останется парой,
    ``a=b; c=d`` останется куками, JWT останется трёхчастным.
    """
    if not value:
        return "eyJhbGciOiJIUzI1NiJ9.eyJpZCI6Inh4In0.bm9wZQ"
    return re.sub(r"[A-Za-z0-9]", "x", value)


def check_painter(name: str, provider) -> bool:
    """Сверка рисовальщика: у него нет текста, значит и проверки другие."""
    ok = True

    # 1. Текст обязан отвергаться СРАЗУ и без обращения к сети: иначе такой
    #    провайдер, попав в текстовую цепочку, съедал бы попытки впустую.
    print("\n  [1] отказ от текста")
    started = time.time()
    try:
        provider.complete(Request(prompt="привет"))
        print("      ПРОВАЛ: текстовый запрос не отвергнут")
        ok = False
    except Unsupported:
        print(f"      ОК: Unsupported за {time.time() - started:.3f}s")

    # 2. Рисование целиком: запуск, ожидание готовности, ссылки.
    print("\n  [2] рисование")
    started = time.time()
    try:
        links = provider.draw(Request(prompt="рыжий кот в скафандре на Луне",
                                      timeout=240))
    except RateLimited as exc:
        print(f"      НЕ ПРОВЕРИТЬ СЕЙЧАС: норма выбрана — {str(exc)[:110]}")
        return True
    except ProviderError as exc:
        print(f"      ПРОВАЛ: {type(exc).__name__}: {str(exc)[:170]}")
        return False

    spent = time.time() - started
    print(f"      {spent:.1f}s, картинок {len(links)}")
    for link in links[:2]:
        print(f"      {link[:100]}")
    if not links:
        print("      ПРОВАЛ: ссылок нет")
        return False
    if not all(link.startswith("http") for link in links):
        print("      ПРОВАЛ: среди ссылок есть не-ссылки")
        ok = False
    else:
        print("      ОК: все ссылки годные")
    return ok


def check_provider(name: str) -> bool:
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    rotations: list[Credential] = []
    try:
        provider = build(name, credential(name), on_rotate=rotations.append)
    except AuthError:
        # Аккаунта нет. Проверить провайдера всё равно надо, но отметить,
        # что это урезанный режим, а не штатная работа.
        print("  ВНИМАНИЕ: аккаунта нет, проверяю анонимно — "
              "лимиты там ниже, в бой так пускать не стоит")
        try:
            provider = build(name, credential(name),
                             on_rotate=rotations.append, allow_anonymous=True)
        except ProviderError as exc:
            print(f"  не поднялся: {type(exc).__name__}: {exc}")
            return False
    except ProviderError as exc:
        print(f"  не поднялся: {type(exc).__name__}: {exc}")
        return False

    caps = provider.capabilities
    stats = measured(name)
    print(f"  природа: {kind_of(name)}, потоков {provider.slots}")
    print(f"  доступ: {auth_kind(name)}, "
          f"{'ОТ АККАУНТА' if provider.authorized else 'анонимно (урезано)'}")
    print(f"  умеет: текст={caps.text} поиск={caps.web_search} "
          f"картинки={caps.images_out} зрение={caps.vision} "
          f"беседы={caps.conversations}")
    if caps.text:
        # Замеры контекста и скорости хода к рисовальщику неприменимы.
        print(f"  замер: {stats.context_chars // 1000}k контекста, "
              f"~{stats.median_turn_sec}s на ход, цикл={stats.agentic}")
    if hasattr(provider, "expires_at"):
        print(f"  доступ протухает: {provider.expires_at}")

    if not caps.text:
        return check_painter(name, provider)

    ok = True

    # 1. Стриминг: куски обязаны идти ПО ХОДУ, а не одной порцией в конце.
    #    Промпт намеренно просит длинный ответ: на коротком всё умещается в
    #    один кадр даже у тех, кто стримит, и проверка ничего не показывает.
    print("\n  [1] стриминг")
    started = time.time()
    marks, chars = [], 0
    try:
        for piece in provider.stream(Request(
                prompt="Перечисли пять планет Солнечной системы. "
                       "Каждую с новой строки, с одним фактом о ней.")):
            marks.append(time.time() - started)
            chars += len(piece)
    except RateLimited as exc:
        # Исчерпанная норма — не поломка адаптера, а состояние сервиса.
        # Считать это провалом значило бы ругаться на исправный код.
        print(f"      НЕ ПРОВЕРИТЬ СЕЙЧАС: норма выбрана ({exc.kind}) — "
              f"{str(exc)[:110]}")
        return True
    except ProviderError as exc:
        print(f"      ПРОВАЛ: {type(exc).__name__}: {str(exc)[:150]}")
        return False
    if not marks:
        print("      ПРОВАЛ: поток пуст")
        return False
    spread = marks[-1] - marks[0]
    print(f"      кусков {len(marks)}, символов {chars}, "
          f"первый через {marks[0]:.2f}s, последний через {marks[-1]:.2f}s")
    if not caps.streaming:
        print("      ОК: одной порцией — этот сервис потоком не отдаёт")
    elif len(marks) < 2 or spread < 0.05:
        print("      ВНИМАНИЕ: заявлен стриминг, но пришло одной порцией")
        ok = False
    else:
        print(f"      ОК: растянуто на {spread:.2f}s, стриминг настоящий")

    # 2. Целый ответ и чистота: служебные потоки не должны попадать в текст.
    print("\n  [2] целый ответ")
    started = time.time()
    try:
        text = provider.complete(Request(
            prompt="Одним предложением: зачем нужен индекс в базе данных?"))
    except ProviderError as exc:
        print(f"      ПРОВАЛ: {type(exc).__name__}: {str(exc)[:150]}")
        return False
    print(f"      {time.time() - started:.1f}s, {len(text)} символов")
    print(f"      {text[:160]}")
    leaked = [w for w in LEAKS if w in text]
    if leaked:
        print(f"      ПРОВАЛ: в тексте служебное — {leaked}")
        ok = False
    else:
        print("      ОК: служебных потоков нет")

    # 3. Отвергнутый доступ обязан давать AuthError, а не «пустой ответ»:
    #    иначе протухшая кука выглядит как поломка на своей стороне.
    #    Провайдеров, которым доступ не нужен вовсе или нужен лишь для
    #    расширенного режима, это не касается — там ответ без ключа штатен.
    print("\n  [3] реакция на отвергнутый доступ")
    kind_of_auth = auth_kind(name)
    if kind_of_auth == AUTH_FILE:
        # Такие авторизуются файлом токенов, а не строкой ключа: подменять
        # строку бессмысленно, адаптер её не читает.
        print("      пропущено: доступ файлом, строка ключа ни на что "
              "не влияет")
    elif name in SILENT_DEGRADE:
        print("      пропущено: сервис молча обслуживает гостем вместо отказа "
              "— «ответил» тут не доказывает, что аккаунт жив")
    elif kind_of_auth in (AUTH_NONE, AUTH_OPTIONAL):
        print(f"      пропущено: доступ {kind_of_auth} — ответ без ключа штатен")
    else:
        broken = Credential(provider=name, value=corrupt(credential(name).value))
        try:
            build(name, broken,
                  allow_anonymous=True).complete(Request(prompt="привет"))
            print("      ВНИМАНИЕ: битый доступ не вызвал ошибки")
            ok = False
        except ProviderError as exc:
            kind = type(exc).__name__
            good = kind == "AuthError"
            print(f"      {'ОК' if good else 'ВНИМАНИЕ'}: {kind}: {str(exc)[:110]}")
            if not good:
                print("      (ждали AuthError — иначе протухший доступ "
                      "не отличить от поломки)")

    # 4. Неподдержанное отвергается ДО сети: запрос стоит сообщения из квоты.
    print("\n  [4] отказ от неподдержанного без обращения к сети")
    try:
        started = time.time()
        provider.validate(Request(prompt="привет", web_search=True))
        if caps.web_search:
            print("      пропущено: поиск в сети поддерживается")
        else:
            print("      ПРОВАЛ: поиск не отвергнут")
            ok = False
    except Unsupported:
        print(f"      ОК: Unsupported за {time.time() - started:.3f}s")

    if rotations:
        print(f"\n  доступ обновился сам: {rotations[-1]!r} "
              f"(хранилище обязано это записать)")

    return ok


def main(argv: list[str]) -> int:
    if "--all" in argv:
        names = implemented()
    elif "--web" in argv:
        names = implemented(WEB)
    elif "--api" in argv:
        names = implemented(API)
    else:
        names = [a for a in argv[1:] if not a.startswith("-")]
    if not names:
        print("укажи провайдера, --all, --web или --api")
        print(f"  веб: {', '.join(implemented(WEB))}")
        print(f"  API: {', '.join(implemented(API))}")
        return 2
    results = {name: check_provider(name) for name in names}
    print(f"\n{'=' * 60}")
    for name, good in results.items():
        print(f"  {'ОК  ' if good else 'БЕДА'} {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
