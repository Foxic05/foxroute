"""DGW — двоичный протокол WebSocket у Meta AI.

Расшифровывается как Datagram WebSocket. Это их собственный формат поверх
обычного веб-сокета: кадр с двоичной шапкой, внутри JSON, внутри которого
лежит protobuf в base64.

Модуль самодостаточный и заменяет пакет ``metaai_api``. Причина не в
чистоте: **его ``build_data_frame`` считает длину неправильно** — берёт
``len(payload)``, тогда как сервер ждёт на два больше. С библиотечной
версией связь рвётся молча. Держать зависимость ради половины функций,
вторую половину переопределяя, смысла нет.

Формат кадра::

    EstablishStream, шапка 6 байт:
        [0x0F][stream_id:2 BE][payload_len:1][flags:2][JSON]

    DATA, шапка 8 байт:
        [0x0D][stream_id:2 BE][payload_len:3 LE][seq:1][flags:1=0x80][JSON]
"""
from __future__ import annotations

import enum
import json
import struct
from dataclasses import dataclass


class FrameType(enum.IntEnum):
    """Виды кадров DGW."""

    DRAIN = 3
    DEAUTH = 4
    SMALL_ACK = 7
    PING = 9
    PONG = 10
    ACK = 12
    DATA = 13
    END_OF_DATA = 14
    ESTAB_STREAM = 15


@dataclass
class Frame:
    kind: int
    stream_id: int
    payload: bytes

    @property
    def is_data(self) -> bool:
        return self.kind == FrameType.DATA

    @property
    def is_end(self) -> bool:
        return self.kind == FrameType.END_OF_DATA


def parse_frame(buffer: bytes) -> Frame:
    """Разобрать кадр. Непонятный кадр не ошибка — вернём что смогли."""
    if len(buffer) < 2:
        return Frame(0, 0, buffer)

    kind = buffer[0]
    stream_id = struct.unpack(">H", buffer[1:3])[0]

    if kind == FrameType.ESTAB_STREAM and len(buffer) >= 6:
        length = buffer[3]
        return Frame(kind, stream_id, buffer[6:6 + length])

    if kind in (FrameType.DATA, FrameType.ACK,
                FrameType.END_OF_DATA) and len(buffer) >= 8:
        length = int.from_bytes(buffer[3:6], "little")
        return Frame(kind, stream_id, buffer[8:8 + length])

    # Незнакомая шапка: вытаскиваем JSON, если он там есть.
    start = buffer.find(b"{")
    return Frame(kind, stream_id, buffer[start:] if start >= 0 else b"")


def build_estab_frame(conversation_id: str) -> bytes:
    """Кадр установки потока для беседы."""
    payload = json.dumps({
        "x-dgw-app-x-ecto-conversation-id": conversation_id,
        "x-dgw-app-client-payload-type": "PROTO_INSIDE_JSON",
    }, separators=(",", ":")).encode("utf-8")
    if len(payload) > 127:
        raise ValueError(
            f"полезная нагрузка кадра установки {len(payload)} байт, "
            "а длина в шапке однобайтовая (максимум 127)")
    return bytes([0x0F, 0x00, 0x00, len(payload), 0x00, 0x00]) + payload


def build_data_frame(payload: bytes, seq: int = 0, stream_id: int = 0) -> bytes:
    """Кадр с данными.

    **Длина в шапке на два больше тела.** Ровно так, не опечатка: с честной
    длиной сервер молча рвёт связь, без сообщения об ошибке. Догадаться по
    коду невозможно.
    """
    declared = len(payload) + 2
    return (bytes([0x0D])
            + struct.pack(">H", stream_id)
            + declared.to_bytes(3, "little")
            + bytes([seq & 0x7F, 0x80])
            + payload)


# ── protobuf вручную ──────────────────────────────────────────────────
#
# Схемы у нас нет и взять её негде, поэтому кодируем и разбираем поля сами.
# Это несложно: нужны только два типа из пяти — varint и length-delimited.

def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        seven = value & 0x7F
        value >>= 7
        out.append(seven | 0x80 if value else seven)
        if not value:
            return bytes(out)


def pb_bytes(field: int, value: str | bytes) -> bytes:
    """Поле с длиной: строка, вложенное сообщение, что угодно."""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return varint((field << 3) | 2) + varint(len(raw)) + raw


def pb_varint(field: int, value: int) -> bytes:
    return varint((field << 3) | 0) + varint(value)


def pb_parse(data: bytes) -> dict[int, list[tuple[str, object]]]:
    """Разобрать сообщение в ``{номер поля: [(тип, значение), …]}``.

    Типы: ``int`` для varint, ``bytes`` для полей с длиной. Поля с
    фиксированной длиной пропускаем — в ответах Meta их нет, а
    поддерживать неиспользуемое незачем.
    """
    fields: dict[int, list[tuple[str, object]]] = {}
    position = 0

    while position < len(data):
        tag = data[position]
        position += 1
        number, wire = tag >> 3, tag & 7
        if number == 0:
            break

        if wire == 0:  # varint
            value = 0
            shift = 0
            while position < len(data):
                byte = data[position]
                position += 1
                value |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            fields.setdefault(number, []).append(("int", value))

        elif wire == 2:  # длина, затем тело
            length = 0
            shift = 0
            while position < len(data):
                byte = data[position]
                position += 1
                length |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            fields.setdefault(number, []).append(
                ("bytes", data[position:position + length]))
            position += length

        elif wire == 5:  # 32 бита
            position += 4
        elif wire == 1:  # 64 бита
            position += 8
        else:
            break

    return fields


def pb_text(fields: dict, number: int) -> str:
    """Первое поле с указанным номером как строка. Пусто — нет такого."""
    for kind, value in fields.get(number, []):
        if kind != "bytes":
            continue
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return ""
