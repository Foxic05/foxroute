"""Proof-of-Work для DeepSeek: sha3 в WebAssembly.

Сервис требует решить задачу перед каждым обращением к ``/chat/completion``
и кладёт ответ в заголовок ``x-ds-pow-response``. Считает её их собственный
модуль WebAssembly — переписывать этот алгоритм на Python бессмысленно:
он подобран так, чтобы браузер справлялся, а мы бы считали заметно дольше.
Поэтому исполняем их же модуль.

Двоичный файл ``deepseek_sha3.wasm`` лежит в данных пакета. Он от них и
менять его нельзя: под каждую версию модуля свои имена экспортов.

Отличия от исходной реализации (xtekky/deepseek4free):

* убран ``numpy`` — он там ради одной распаковки числа с плавающей точкой,
  ради чего тянуть зависимость незачем;
* добавлена блокировка: у ``wasmtime.Store`` состояние общее, и два потока,
  считающие задачу одновременно, портят друг другу память;
* убрана проверка версии ``curl-cffi``, которая при несовпадении писала
  предупреждение в stderr на каждый запуск.
"""
from __future__ import annotations

import base64
import json
import struct
import threading
from pathlib import Path
from typing import Any

from foxroute.errors import ProviderError
from foxroute.paths import app_dir

#: Имя двоичного модуля. Свой можно положить в каталог данных — тогда
#: возьмётся он: если сервис обновит модуль, замена не потребует правки кода.
WASM_NAME = "deepseek_sha3.wasm"

_BUILTIN = Path(__file__).resolve().parents[2] / "data" / WASM_NAME


def _wasm_path() -> Path:
    override = app_dir() / WASM_NAME
    if override.exists():
        return override
    if _BUILTIN.exists():
        return _BUILTIN
    raise ProviderError(
        f"не найден модуль {WASM_NAME} ни в {app_dir()}, ни в данных пакета",
        "deepseek")


class Solver:
    """Обёртка над модулем WebAssembly.

    Один экземпляр на провайдера. Дорогой в создании (разбор и компиляция
    модуля), дешёвый в использовании.
    """

    def __init__(self) -> None:
        try:
            import wasmtime
        except ImportError as exc:
            raise ProviderError(
                "нужен пакет wasmtime", "deepseek") from exc

        engine = wasmtime.Engine()
        module = wasmtime.Module(engine, _wasm_path().read_bytes())
        linker = wasmtime.Linker(engine)
        linker.define_wasi()

        self._store = wasmtime.Store(engine)
        self._instance = linker.instantiate(self._store, module)
        self._memory = self._instance.exports(self._store)["memory"]
        # Состояние модуля общее на экземпляр: считать из двух потоков сразу
        # значит портить друг другу память.
        self._lock = threading.Lock()

    def _export(self, name: str):
        return self._instance.exports(self._store)[name]

    def _write(self, text: str) -> tuple[int, int]:
        """Положить строку в память модуля, вернуть указатель и длину."""
        encoded = text.encode("utf-8")
        pointer = self._export("__wbindgen_export_0")(
            self._store, len(encoded), 1)
        view = self._memory.data_ptr(self._store)
        for offset, byte in enumerate(encoded):
            view[pointer + offset] = byte
        return pointer, len(encoded)

    def _hash(self, challenge: str, salt: str, difficulty: float,
              expire_at: int) -> int | None:
        prefix = f"{salt}_{expire_at}_"
        stack = self._export("__wbindgen_add_to_stack_pointer")
        result_ptr = stack(self._store, -16)
        try:
            challenge_ptr, challenge_len = self._write(challenge)
            prefix_ptr, prefix_len = self._write(prefix)

            self._export("wasm_solve")(
                self._store, result_ptr,
                challenge_ptr, challenge_len,
                prefix_ptr, prefix_len,
                float(difficulty))

            view = self._memory.data_ptr(self._store)
            status = int.from_bytes(bytes(view[result_ptr:result_ptr + 4]),
                                    byteorder="little", signed=True)
            if status == 0:
                return None
            # Ответ лежит числом с плавающей точкой двойной точности,
            # little-endian, сразу за статусом.
            raw = bytes(view[result_ptr + 8:result_ptr + 16])
            return int(struct.unpack("<d", raw)[0])
        finally:
            stack(self._store, 16)

    def solve(self, challenge: dict[str, Any]) -> str:
        """Решить задачу и собрать значение заголовка."""
        required = ("algorithm", "challenge", "salt", "difficulty",
                    "expire_at", "signature", "target_path")
        missing = [field for field in required if field not in challenge]
        if missing:
            raise ProviderError(
                f"в задаче нет полей: {', '.join(missing)}", "deepseek")

        with self._lock:
            answer = self._hash(
                challenge["challenge"], challenge["salt"],
                challenge["difficulty"], challenge["expire_at"])
        if answer is None:
            raise ProviderError("задача не решилась", "deepseek")

        payload = {
            "algorithm": challenge["algorithm"],
            "challenge": challenge["challenge"],
            "salt": challenge["salt"],
            "answer": answer,
            "signature": challenge["signature"],
            "target_path": challenge["target_path"],
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()
