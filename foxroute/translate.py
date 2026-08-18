"""Трансляция OpenAI-запроса в промпт провайдера и обратно.

Два превращения, оба неочевидные.

**messages[] → один промпт.** Веб-чаты одноразовые: у них нет истории на
нашей стороне, каждый вызов — новый чат. Поэтому вся история ролей
схлопывается в одну строку: ``system:`` как преамбула, ``user:`` и
``assistant:`` как диалог. Это грубо, но на таком контексте провайдеры
доводят задачу до верного ответа.

**tools[] → построчный формат.** На промпты с JSON- и XML-схемами
инструментов ChatGPT, MS Copilot, Meta AI и Manus **отказываются**, а Meta
AI прямо пишет, что это «попытка подменить среду выполнения». Построчный
формат (``READ path``) те же модели выполняют.
Это самый ценный кусок ноу-хау проекта — от него зависит, работает ли
агентный цикл вообще.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
import uuid


def history_chars(messages: list[dict]) -> int:
    """Сколько символов займёт история — для отбора провайдера по окну."""
    return sum(len(_content(m)) for m in messages)


def trim_history(messages: list[dict], max_chars: int) -> list[dict]:
    """Ужать историю под окно провайдера, сохранив смысл.

    Наивная отправка всей истории упирается в лимит слабых провайдеров:
    Groq на 48k отдаёт HTTP 413, DeepAI теряет маркеры уже на 4k. Режем
    по-умному, а не обрубаем хвост:

    - **Последние ходы — дословно.** Свежий контекст важнее всего.
    - **Первое сообщение — сохраняем.** Там часто постановка задачи, и
      без неё модель отвечает не на то.
    - **Середину выкидываем**, отметив пропуск, чтобы модель понимала, что
      беседа была длиннее, а не начиналась с середины.

    ``max_chars`` — это окно провайдера; берём с запасом (0.9), потому что
    к промпту ещё добавится разметка ролей и место под сам ответ.
    """
    if max_chars <= 0 or history_chars(messages) <= max_chars:
        return messages
    if len(messages) <= 1:
        return messages

    budget = int(max_chars * 0.9)
    first = messages[0]
    used = len(_content(first))

    tail: list[dict] = []
    # Идём с конца, набираем последние ходы, пока влезают.
    for msg in reversed(messages[1:]):
        size = len(_content(msg))
        if used + size > budget and tail:
            break
        tail.insert(0, msg)
        used += size

    dropped = len(messages) - 1 - len(tail)
    if dropped <= 0:
        return [first] + tail

    gap = {"role": "system",
           "content": f"[…{dropped} ранних сообщений опущено для краткости…]"}
    return [first, gap] + tail


def messages_to_prompt(messages: list[dict]) -> str:
    """Схлопнуть ``messages[]`` в один промпт.

    Формат простой: каждое сообщение начинается с метки роли, содержимое
    как есть. Последнее ``user:`` сообщение идёт без метки — многие модели
    отвечают естественнее, когда промпт не начинается со служебного слова.
    """
    if not messages:
        return ""

    # Единственное сообщение — отдаём как есть, без разметки.
    if len(messages) == 1:
        return _content(messages[0])

    parts = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        text = _content(msg)
        # Ход ассистента может нести не текст, а вызовы инструментов
        # (content=null, tool_calls=[…]). Без этого он терялся целиком, и в
        # переигранной агентной истории пропадал сам факт вызова.
        if not text and role == "assistant" and msg.get("tool_calls"):
            text = _render_tool_calls(msg["tool_calls"])
        if not text:
            continue

        # Последнее user-сообщение без метки: модель отвечает в продолжение.
        if i == len(messages) - 1 and role == "user":
            parts.append(text)
        elif role == "system":
            parts.append(text)
        elif role == "assistant":
            parts.append(f"assistant: {text}")
        elif role == "tool":
            # Результат инструмента — не реплика пользователя, метим иначе,
            # иначе беседа выглядит так, будто это сказал человек.
            parts.append(f"tool_result: {text}")
        else:
            parts.append(f"user: {text}")

    return "\n\n".join(parts)


def _render_tool_calls(tool_calls: list) -> str:
    """Вызовы инструментов из хода ассистента — в тот же построчный вид,
    в котором модель их и звала (``NAME arg1 arg2``), чтобы переигранная
    история осталась связной."""
    lines = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        func = call.get("function") or {}
        name = func.get("name")
        if not name:
            continue
        raw = func.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = {}
        vals = (" ".join(str(v) for v in args.values())
                if isinstance(args, dict) else "")
        lines.append(f"{name.upper()} {vals}".strip())
    return "\n".join(lines)


def tools_to_instructions(tools: list[dict]) -> str:
    """Описать инструменты ПОСТРОЧНО, не JSON-схемой.

    Формат выбран не для красоты: JSON-схемы инструментов запускают защиту
    от перехвата у ChatGPT, Copilot, Meta AI и Manus — они отвечают отказом
    «инструменты недоступны» или «попытка подменить среду выполнения».
    Построчный формат ту же проверку не запускает.

    На выходе блок вида::

        Доступные команды (каждый ответ — ровно ОДНА строка-команда):
        READ <path>   — прочитать файл
        WRITE <path> <content>   — записать файл
        RUN <command>   — выполнить команду

    Вызывающий (сервер) добавляет этот блок к системному промпту.
    """
    if not tools:
        return ""

    lines = ["Доступные команды (каждый ответ — ровно ОДНА строка-команда):"]
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function") or {}
        name = func.get("name", "").upper()
        desc = func.get("description", "")
        params = func.get("parameters", {}).get("properties", {})

        args = " ".join(f"<{p}>" for p in params)
        line = f"{name} {args}".strip()
        if desc:
            line += f"   — {desc}"
        lines.append(line)

    return "\n".join(lines)


def parse_tool_call(text: str, tools: list[dict]) -> dict | None:
    """Разобрать построчный вызов инструмента из текста модели.

    Возвращает dict в формате OpenAI ``tool_calls[]`` или None.

    **Совпадение регистрозависимое, и это принципиально.** Команды в
    промпте набраны ЗАГЛАВНЫМИ (``READ``, ``RUN``, см. tools_to_instructions),
    поэтому модель, вызывая инструмент, начинает строку заглавным именем.
    Сравнение через ``.upper()`` ловило бы обычную прозу «Run the tests…»,
    «Read the file…» как вызов функции, ломая агентный цикл на ровном
    месте. Поэтому ``Run`` (не целиком заглавное) вызовом не считается.
    """
    if not tools or not text:
        return None

    # {ИМЯ_ЗАГЛАВНЫМИ: (оригинальное имя, свойства параметров)}. Только
    # function-инструменты; прочие типы (в т.ч. без ключа ``function``)
    # пропускаем, а не падаем на индексации.
    known: dict[str, tuple[str, dict]] = {}
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function") or {}
        name = func.get("name")
        if not name:
            continue
        props = (func.get("parameters") or {}).get("properties", {})
        known[name.upper()] = (name, props)
    if not known:
        return None

    for line in text.strip().split("\n"):
        words = line.strip().split()
        if not words:
            continue
        first = words[0]                       # РЕГИСТРОЗАВИСИМО, см. докстроку
        if first not in known:
            continue
        original_name, properties = known[first]
        rest = line.strip()[len(first):].strip()
        param_names = list(properties.keys())

        if len(param_names) == 1:
            arguments = json.dumps({param_names[0]: rest})
        elif not param_names:
            arguments = "{}"
        else:
            # Несколько параметров — разбиваем по пробелам, последний
            # забирает остаток строки.
            parts = rest.split(None, len(param_names) - 1)
            arguments = json.dumps({
                n: (parts[i] if i < len(parts) else "")
                for i, n in enumerate(param_names)})

        return {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": original_name, "arguments": arguments},
        }
    return None


def _content(message: dict) -> str:
    """Текстовое содержимое сообщения.

    OpenAI допускает и строку, и массив ``content_parts`` — приводим к
    строке. Части с картинками и файлами сюда не попадают: их разбирает
    ``attachments_from_messages``.
    """
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in (
                    "text", "input_text", "output_text"):
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return str(content)


# ── Вложения из стандартного тела OpenAI ──────────────────────────────

#: Разбор строки ``data:<mime>;base64,<данные>``.
_DATA_URI = re.compile(r"^data:([^;,]+)?(;[^,]*)?,(.*)$", re.S)


def _from_data_uri(uri: str) -> tuple[bytes | None, str]:
    """``(байты, mime)`` из ``data:``-строки. ``(None, "")`` — не она."""
    match = _DATA_URI.match(uri or "")
    if not match:
        return None, ""
    mime = (match.group(1) or "application/octet-stream").strip()
    body = match.group(3) or ""
    if ";base64" in (match.group(2) or ""):
        try:
            return base64.b64decode(body), mime
        except (ValueError, binascii.Error):
            return None, ""
    return urllib.parse.unquote_to_bytes(body), mime


def attachments_from_messages(messages: list[dict]) -> list[dict]:
    """Вложения из ПОСЛЕДНЕГО сообщения человека, как их шлёт OpenAI.

    Клиент по спецификации кладёт картинку не отдельным полем, а частью
    сообщения::

        {"role": "user", "content": [
            {"type": "text", "text": "что тут"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}]}

    Без разбора таких частей ``_content`` взял бы только текст, и запрос
    ушёл бы к провайдеру БЕЗ картинки — модель отвечает «не вижу
    изображения», а причина на нашей стороне. Понимаем все распространённые
    виды:
    ``image_url`` (Chat Completions), ``input_image`` (Responses),
    ``file``/``input_file`` (файлы), и значение либо строкой, либо объектом
    с ключом ``url``/``file_data``.

    Берём только ПОСЛЕДНЕЕ сообщение человека: вложения относятся к
    текущему ходу. Тянуть картинки из всей истории значило бы слать их
    заново на каждом шаге беседы.

    Возвращаем список словарей: ``data`` (байты) ЛИБО ``url`` (что качать),
    плюс ``mime``, ``filename`` и ``kind``. Скачивание — забота
    вызывающего: у сети свои потолки и свои отказы.
    """
    last = None
    for message in reversed(messages or []):
        if message.get("role") == "user":
            last = message
            break
    if not last or not isinstance(last.get("content"), list):
        return []

    found: list[dict] = []
    for item in last["content"]:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or ""
        if kind in ("image_url", "input_image", "image"):
            holder = item.get("image_url") or item.get("image") or ""
            source = holder.get("url", "") if isinstance(holder, dict) else holder
            name, wanted = "", "image"
        elif kind in ("file", "input_file", "document"):
            holder = item.get("file") or item.get("input_file") or {}
            if not isinstance(holder, dict):
                holder = {"file_data": holder}
            source = (holder.get("file_data") or holder.get("file_url")
                      or holder.get("url") or "")
            name, wanted = str(holder.get("filename") or ""), "file"
        else:
            continue

        source = str(source or "").strip()
        if not source:
            continue

        data, mime = _from_data_uri(source)
        if data is not None:
            found.append({"kind": wanted, "data": data, "mime": mime,
                          "filename": name or _guess_name(mime, wanted)})
        elif source.startswith(("http://", "https://")):
            found.append({"kind": wanted, "url": source, "mime": "",
                          "filename": name})
        elif source.startswith("data:"):
            # data:-строка, которую не удалось разобрать (повреждённый
            # base64) — это ошибка запроса. Молча выбросить нельзя: модель
            # ответит «не вижу изображения», а причина у нас. Отдаём наверх
            # как ValueError → сервер превратит в понятный 400.
            raise ValueError(
                f"вложение {name or wanted}: data-URI не разобран "
                "(повреждённый base64?)")
    return found


def _guess_name(mime: str, kind: str) -> str:
    """Имя файла, когда клиент его не назвал."""
    tail = (mime.split("/")[-1] or "bin").split("+")[0]
    return f"{'picture' if kind == 'image' else 'file'}.{tail}"
