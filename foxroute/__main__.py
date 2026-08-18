"""Управление учётками из командной строки.

    python -m foxroute list                     что есть в пуле
    python -m foxroute add qwen "<ключ>"        добавить учётку
    python -m foxroute add qwen "<ключ>" --account second
    python -m foxroute remove qwen second
    python -m foxroute disable qwen main "кука протухла"
    python -m foxroute enable qwen main
    python -m foxroute import /path/to/settings.json

Каталог хранилища берётся из ``FOXROUTE_HOME``.
"""
from __future__ import annotations

import argparse
import sys

from foxroute import accounts as store_module
from foxroute.paths import app_dir
from foxroute.providers import implemented, kind_of
from foxroute.registry import auth_kind


def _print_pool(store: store_module.Accounts) -> int:
    print(f"хранилище: {store.path}\n")
    known = sorted(set(implemented()) | set(store.providers()))
    if not known:
        print("пусто")
        return 0

    print(f"{'провайдер':<14} {'вид':<5} {'доступ':<9} {'в пуле':>7}  состояние")
    for provider in known:
        pool = store.all(provider)
        alive = sum(1 for a in pool if a.enabled)
        missing = store.missing_parts(provider)

        if missing:
            state = "НЕ ХВАТАЕТ: " + ", ".join(missing)
        elif store.ready(provider):
            state = "готов"
        else:
            state = "нет учётки"

        marks = []
        for item in pool:
            mark = item.account if item.enabled else f"{item.account}(выкл)"
            if item.rotated:
                mark += "+ротация"
            marks.append(mark)
        if marks:
            state += "  [" + ", ".join(marks) + "]"

        print(f"{provider:<14} {kind_of(provider) or '—':<5} "
              f"{auth_kind(provider):<9} {alive:>3}/{len(pool):<2}  {state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m foxroute", description="учётки провайдеров")
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("list", help="показать пул")

    adding = commands.add_parser(
        "add", help="добавить доступ (можно сразу несколько)")
    adding.add_argument("provider")
    adding.add_argument("value", nargs="+",
                        help="ключ или кука; у API можно несколько подряд "
                             "либо одной строкой через '|'")
    adding.add_argument("--account", default="",
                        help="имя; при нескольких значениях не применяется")
    adding.add_argument("--proxy", default="")
    adding.add_argument("--note", default="")

    removing = commands.add_parser("remove", help="удалить учётку")
    removing.add_argument("provider")
    removing.add_argument("account")

    disabling = commands.add_parser("disable", help="выключить учётку")
    disabling.add_argument("provider")
    disabling.add_argument("account")
    disabling.add_argument("reason", nargs="?", default="вручную")

    enabling = commands.add_parser("enable", help="включить учётку")
    enabling.add_argument("provider")
    enabling.add_argument("account")

    importing = commands.add_parser(
        "import", help="перенести доступы из внешнего settings.json")
    importing.add_argument("path")
    importing.add_argument("--overwrite", action="store_true",
                           help="заменить уже имеющиеся учётки")

    checking = commands.add_parser(
        "check", help="проверить живость (тратит по сообщению на провайдера)")
    checking.add_argument("provider", nargs="*",
                          help="кого проверять; пусто — всех")
    checking.add_argument("--deep", action="store_true",
                          help="трёхтестовая диагностика при непонятном "
                               "отказе (стоит трёх сообщений)")

    commands.add_parser("status", help="кто на что годен прямо сейчас")

    args = parser.parse_args(argv)
    store = store_module.default

    if args.command in (None, "list"):
        return _print_pool(store)

    if args.command == "add":
        added = []
        for number, value in enumerate(args.value):
            # Имя применяем только к единственному значению: одно имя на
            # несколько доступов бессмысленно.
            name = args.account if (args.account and len(args.value) == 1
                                    and number == 0) else ""
            added.extend(store.add(args.provider, value, name,
                                   args.proxy, args.note))
        noun = store_module.word(args.provider, len(added))
        print(f"добавлено {len(added)} {noun}:")
        for entry in added:
            print(f"  {entry!r}")
        missing = store.missing_parts(args.provider)
        if missing:
            print(f"ВНИМАНИЕ: не хватает ещё: {', '.join(missing)}")
        return 0

    if args.command == "remove":
        found = store.remove(args.provider, args.account)
        print("удалено" if found else "такой учётки нет")
        return 0 if found else 1

    if args.command == "disable":
        store.set_enabled(args.provider, args.account, False, args.reason)
        print(f"выключено: {args.provider}/{args.account} — {args.reason}")
        return 0

    if args.command == "enable":
        store.set_enabled(args.provider, args.account, True)
        print(f"включено: {args.provider}/{args.account}")
        return 0

    if args.command == "import":
        added = store.import_settings(args.path, overwrite=args.overwrite)
        if not added:
            print("нечего переносить (уже есть? тогда --overwrite)")
            return 0
        for provider, count in sorted(added.items()):
            print(f"  {provider}: {count} "
                  f"{store_module.word(provider, count)}")
        print(f"\nвсего провайдеров: {len(added)}, каталог: {app_dir()}")
        return 0

    if args.command == "check":
        from foxroute.health import default as canary

        names = args.provider or None
        print("проверяю (каждая проверка тратит сообщение из квоты)\n")
        verdicts = canary.sweep(names, on_verdict=lambda v: print(f"  {v}"))
        alive = sum(1 for v in verdicts if v.state == "жив")
        print(f"\nживых {alive} из {len(verdicts)}")
        hands = [v for v in verdicts if v.needs_hands]
        if hands:
            print("\nтребуют вмешательства:")
            for v in hands:
                print(f"  {v.provider}/{v.account}: {v.detail}")
        return 0

    if args.command == "status":
        from foxroute.router import default as router

        rows = router.status()
        print(f"{'провайдер':<14} {'вид':<4} {'учёток':>6} {'свободна':<10} "
              f"{'цикл':<8} {'контекст':>9} {'ход':>6}  паузы")
        for row in rows:
            print(f"{row['provider']:<14} {row['вид']:<4} "
                  f"{row['учёток']:>6} {str(row['свободна'] or '—'):<10} "
                  f"{row['цикл']:<8} {row['контекст'] // 1000:>7}k "
                  f"{row['ход']:>5.1f}s  "
                  f"{', '.join(row['паузы']) if row['паузы'] else ''}")
        return 0


    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
