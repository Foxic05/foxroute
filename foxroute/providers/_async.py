"""Мост из асинхронного мира в синхронный контракт.

Часть сервисов говорит по WebSocket: Алиса, Meta AI, Manus, Poe. Библиотеки
для этого асинхронные, а ``Provider.stream`` синхронный — и это осознанно,
потому что синхронный итератор одинаково удобен и в скрипте, и в потоке
обработки HTTP-запроса, а обратное неверно.

Наивное решение — собрать весь ответ через ``asyncio.run`` и отдать строкой.
Но это убивает ``stream=true`` наверху: куски уже пришли, но потребитель
узнает о них только в конце.

Здесь корутина крутится в отдельном потоке и складывает куски в очередь,
а вызывающий разбирает её по мере поступления. Стриминг сохраняется,
исключения переносятся через границу потока.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import AsyncIterator, Callable, Iterator

#: Кладётся в очередь, когда корутина закончила.
_DONE = object()


def to_sync(make_agen: Callable[[], AsyncIterator[str]],
            timeout: float = 300.0) -> Iterator[str]:
    """Синхронный итератор поверх асинхронного генератора.

    ``make_agen`` — фабрика, а не готовый генератор: создавать его нужно уже
    внутри цикла событий, иначе он окажется привязан к чужому.

    ``timeout`` считается на ожидание ОЧЕРЕДНОГО куска, а не на весь ответ.
    Так правильнее: длинный ответ не обрывается на середине из-за того, что
    он длинный, а вот молчащий сервис ловится.
    """
    bucket: queue.Queue = queue.Queue()

    # Прокси текущего запроса лежит потоко-локально (settings.set_request_proxy
    # в потоке HTTP-запроса), а корутина крутится в ЭТОМ, отдельном потоке —
    # и потоко-локального там нет. Снимаем значение здесь, в родительском
    # потоке, и переставляем в воркере ДО старта корутины. Иначе прокси
    # учётки/провайдера у async-провайдеров (Copilot и др.) молча терялся —
    # трафик шёл мимо прокси, хотя логинились под ним.
    from foxroute import settings

    parent_proxy = settings.current_proxy()

    async def pump() -> None:
        async for piece in make_agen():
            bucket.put(piece)

    def run() -> None:
        settings.set_request_proxy(parent_proxy or None)
        try:
            asyncio.run(pump())
        except BaseException as exc:  # noqa: BLE001 — переносим через границу
            bucket.put(exc)
        finally:
            bucket.put(_DONE)

    worker = threading.Thread(target=run, daemon=True,
                              name="foxroute-async-bridge")
    worker.start()

    try:
        while True:
            try:
                item = bucket.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(
                    f"сервис молчит дольше {timeout:.0f} с") from None
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        # Поток демонский и умрёт с процессом, но дождаться его дешевле, чем
        # оставлять недозакрытый сокет висеть до сборки мусора.
        worker.join(timeout=5)
