"""Хранилище учёток: пул аккаунтов на провайдера.

Одна сессия на сервис — естественный потолок ёмкости. Квота у веб-сессий
считается в сообщениях, поэтому ёмкость растёт ровно с числом аккаунтов, а
не с изяществом кода. Хранилище поэтому с самого начала думает пулом, а не
единственным ключом.

Что здесь решается, кроме собственно хранения:

**Один писатель.** Kimi и MS Copilot обновляют свои токены сами, и два
процесса, пишущих один файл, затирают друг друга. Запись идёт под файловой
блокировкой и атомарно — во временный файл с последующей заменой, чтобы
оборванная запись не оставила обрубок.

**Ротация не затирает исходное.** У Kimi исходный токен из куки продолжает
приниматься, а сервис выдаёт каждый раз новый и каждый раз другой — то есть
в куке корневой, а выдаются временные.
Перезаписать хранилище ротацией значило бы променять месяцы жизни доступа на
часы. Поэтому храним оба: исходный как ``value``, последнюю ротацию как
``rotated``. Используется первый; второй повышается в основные, только когда
первый отвергнут.

**Ключ — не весь доступ.** У Gemini Web рабочая вторая кука живёт в каталоге
кеша, а не в настройках; у Meta AI и Copilot доступ вообще лежит файлами.
Хранилище знает про это (``registry.AUTH_FILES``, ``AUTH_STATE_DIRS``) и
умеет проверить, что всё на месте.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


def _process_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID — БЕЗ вреда для него.

    ``os.kill(pid, 0)`` для проверки живости годится только на POSIX. На
    Windows Python реализует ``os.kill`` через ``TerminateProcess``, и
    сигнал 0 не проверяет процесс, а УБИВАЕТ его — то есть проверка
    брошенной блокировки прибила бы либо зависший писатель, либо
    посторонний процесс с переиспользованным PID. Поэтому на Windows
    спрашиваем ядро через ``OpenProcess``.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False          # процесса нет
        ctypes.windll.kernel32.CloseHandle(handle)
        return True               # есть процесс с этим PID — считаем живым
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False              # процесса нет
    except PermissionError:
        return True               # чужой, но живой
    except OSError:
        return True               # сомнение толкуем в пользу «жив»
    return True

from foxroute.errors import AuthError
from foxroute.paths import app_dir
from foxroute.providers.base import Credential
from foxroute.registry import (
    AUTH_FILE,
    AUTH_FILES,
    AUTH_NONE,
    AUTH_OPTIONAL,
    AUTH_STATE_DIRS,
    auth_kind,
    is_api,
)


def word(provider: str, count: int = 1) -> str:
    """Как называть единицу доступа у этого провайдера, с нужным окончанием.

    Не придирка к словам: у веб-сессии это залогиненный аккаунт со своей
    историей и куками, у официального API — просто кошелёк с квотой.
    Разговаривать о них одним словом значит путать самого себя.
    """
    forms = (("ключ", "ключа", "ключей") if is_api(provider)
             else ("учётка", "учётки", "учёток"))
    tail, tens = count % 10, count % 100
    if tail == 1 and tens != 11:
        return forms[0]
    if 2 <= tail <= 4 and not 12 <= tens <= 14:
        return forms[1]
    return forms[2]

STORE_NAME = "accounts.json"
STORE_VERSION = 1

#: Сколько ждём чужую блокировку, прежде чем счесть её брошенной. Записи
#: короткие, так что секунды хватает с запасом; больший срок значил бы, что
#: процесс упал, не убрав за собой.
LOCK_STALE_SECONDS = 20


# ── блокировка файла ──────────────────────────────────────────────────

class FileLock:
    """Межпроцессная блокировка через файл, создаваемый исключительно.

    В стандартной библиотеке переносимой блокировки нет: ``fcntl`` есть не
    везде, ``msvcrt`` работает иначе. Атомарное создание файла с ``O_EXCL``
    ведёт себя одинаково всюду, а брошенную блокировку узнаём по возрасту.
    """

    def __init__(self, target: Path, timeout: float = 10.0):
        self.path = target.with_suffix(target.suffix + ".lock")
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if self._is_stale():
                    # Владелец не убрал за собой — забираем блокировку.
                    self.path.unlink(missing_ok=True)
                    continue
                if time.time() > deadline:
                    raise TimeoutError(
                        f"не дождались блокировки {self.path} за "
                        f"{self.timeout:.0f} с") from None
                time.sleep(0.05)

    def _is_stale(self) -> bool:
        """Брошена ли блокировка.

        Одного возраста файла МАЛО: владелец мог просто задуматься
        (сборка мусора, своп, отладчик), и тогда отбор блокировки даёт
        потерянную правку — оба процесса пишут поверх друг друга. Поэтому
        сначала спрашиваем систему, жив ли записанный в файле процесс, и
        только мёртвого раскулачиваем.
        """
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if age <= LOCK_STALE_SECONDS:
            return False

        try:
            owner = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            # Файл без внятного владельца и старый — считаем брошенным.
            return True
        if owner <= 0 or owner == os.getpid():
            return True
        # Старый файл + мёртвый владелец = брошенная блокировка. Проверку
        # живости делаем безвредно, см. _process_alive (на Windows os.kill
        # убил бы процесс).
        return not _process_alive(owner)

    def __exit__(self, *_exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Снимаем блокировку ТОЛЬКО если в файле всё ещё наш PID. Если нас
        # сочли зависшими и другой процесс забрал её (переписав PID на
        # свой), удалить её — значит впустить третьего писателя и потерять
        # чужую правку. В таком случае не трогаем.
        try:
            owner = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return
        if owner == os.getpid():
            self.path.unlink(missing_ok=True)


# ── учётка ────────────────────────────────────────────────────────────

@dataclass
class Account:
    """Один аккаунт провайдера.

    ``value`` — доступ как его дал человек. ``rotated`` — то, что сервис
    выдал взамен; хранится отдельно и в дело идёт только если исходный
    отвергнут (см. заметку о ротации в шапке модуля).
    """

    provider: str
    account: str = "main"
    value: str = ""
    rotated: str = ""
    rotated_at: str = ""
    enabled: bool = True
    #: Почему выключен. Пусто — выключен вручную.
    disabled_reason: str = ""
    added_at: str = ""
    #: Прокси для этого аккаунта. Десять аккаунтов с одного адреса —
    #: классический повод для антифрода, так что поле заведено заранее.
    proxy: str = ""
    note: str = ""

    def __repr__(self) -> str:
        # Значение доступа не должно попасть ни в лог, ни в трейсбек.
        shown = f"{len(self.value)} симв" if self.value else "без ключа"
        state = "вкл" if self.enabled else f"выкл ({self.disabled_reason})"
        return f"Account({self.provider}/{self.account}, {shown}, {state})"

    def credential(self, use_rotated: bool = False) -> Credential:
        value = self.rotated if (use_rotated and self.rotated) else self.value
        return Credential(provider=self.provider, value=value,
                          account=self.account)


# ── хранилище ─────────────────────────────────────────────────────────

class Accounts:
    """Пул учёток на диске.

    Все изменения проходят через ``_update``: он берёт блокировку,
    перечитывает файл, применяет правку и пишет атомарно. Перечитывание
    обязательно — между нашими операциями файл мог поменять другой процесс,
    и записать поверх своей устаревшей копией значит потерять чужую правку.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (app_dir() / STORE_NAME)
        #: Сопутствующие файлы доступа ищутся рядом с самим хранилищем, а не
        #: в глобальном каталоге: иначе два экземпляра (например, в тестах)
        #: смотрели бы на разные наборы и расходились в оценке готовности.
        self.data_dir = self.path.parent
        self._lock = threading.Lock()

    # ── чтение ────────────────────────────────────────────────────────

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": STORE_VERSION, "providers": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Битый файл не должен ронять запрос: работаем с пустым пулом,
            # а исходный не трогаем, чтобы человек мог его починить руками.
            return {"version": STORE_VERSION, "providers": {}}
        if not isinstance(data.get("providers"), dict):
            data["providers"] = {}
        return data

    def _write(self, data: dict) -> None:
        """Атомарная запись: во временный файл, затем замена.

        Прямая запись в целевой файл оставляет обрубок, если процесс упал на
        середине, — а обрубок здесь означает потерянные доступы.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".accounts-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        # Файл с доступами не должен быть читаем кем попало.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # на Windows прав в этом виде нет

    def _update(self, change) -> None:
        with self._lock, FileLock(self.path):
            data = self._read()
            change(data)
            self._write(data)

    def all(self, provider: str) -> list[Account]:
        """Все учётки провайдера, включая выключенные."""
        raw = self._read()["providers"].get(provider) or []
        fields = set(Account.__dataclass_fields__)
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            known = {k: v for k, v in item.items() if k in fields}
            known["provider"] = provider
            out.append(Account(**known))
        return out

    def usable(self, provider: str) -> list[Account]:
        """Учётки, которыми можно работать прямо сейчас."""
        return [a for a in self.all(provider) if a.enabled]

    def get(self, provider: str, account: str) -> Account | None:
        return next((a for a in self.all(provider) if a.account == account),
                    None)

    def providers(self) -> list[str]:
        return sorted(self._read()["providers"])

    # ── правки ────────────────────────────────────────────────────────

    def _next_name(self, taken: set[str]) -> str:
        if "main" not in taken:
            return "main"
        return str(next(n for n in range(2, 100000) if str(n) not in taken))

    def add(self, provider: str, value: str, account: str = "",
            proxy: str = "", note: str = "") -> list[Account]:
        """Добавить доступ. Возвращает добавленное — его может быть несколько.

        У официальных API строка с ``|`` означает НЕСКОЛЬКО ключей и
        разворачивается в пул: ключей можно вбить сколько угодно, каждый со
        своим кошельком и своей нормой. У веб-сессий та же строка наоборот
        склеивает части одного доступа и не трогается (см.
        ``registry.splits_pool``).

        Имя задавать необязательно; при добавлении нескольких разом оно
        игнорируется, потому что одно имя на всех бессмысленно.
        """
        values = [item.value for item in Credential.expand(provider, value)
                  if item.value]
        if not values:
            raise ValueError("пустой доступ")

        # Имена подбираются ВНУТРИ блокировки, вместе с записью. Если
        # подбирать до неё, два одновременных добавления увидят один и
        # тот же занятый набор — оба возьмут имя «2» и заведут две записи
        # с одним именем, после чего учётки станут неразличимы.
        entries: list[Account] = []

        def change(data: dict) -> None:
            bucket = data["providers"].setdefault(provider, [])
            taken = {row.get("account") for row in bucket
                     if isinstance(row, dict)}
            if account and len(values) == 1:
                if account in taken:
                    raise ValueError(
                        f"{provider}/{account} уже есть — сначала удали")
                names = [account]
            else:
                names = []
                for _ in values:
                    name = self._next_name(taken)
                    taken.add(name)
                    names.append(name)

            for name, item in zip(names, values):
                entry = Account(provider=provider, account=name, value=item,
                                proxy=proxy, note=note,
                                added_at=time.strftime("%Y-%m-%d"))
                entries.append(entry)
                bucket.append({k: v for k, v in asdict(entry).items()
                               if k != "provider"})

        self._update(change)
        return entries

    def remove(self, provider: str, account: str) -> bool:
        removed = False

        def change(data: dict) -> None:
            nonlocal removed
            bucket = data["providers"].get(provider) or []
            kept = [x for x in bucket if x.get("account") != account]
            removed = len(kept) != len(bucket)
            data["providers"][provider] = kept

        self._update(change)
        return removed

    def set_enabled(self, provider: str, account: str, enabled: bool,
                    reason: str = "") -> None:
        """Включить или выключить учётку.

        Выключать стоит при отвергнутом доступе: иначе маршрутизатор будет
        ходить в неё снова и снова, тратя время на заведомый отказ.
        """
        def change(data: dict) -> None:
            for item in data["providers"].get(provider) or []:
                if item.get("account") == account:
                    item["enabled"] = enabled
                    item["disabled_reason"] = "" if enabled else reason

        self._update(change)

    def set_proxy(self, provider: str, account: str, proxy: str) -> bool:
        """Задать (или снять — пустой строкой) прокси для одной учётки.

        Возвращает ``False``, если такой учётки нет: интерфейс тогда покажет
        честную ошибку, а не молчаливый успех над пустотой.
        """
        found = False

        def change(data: dict) -> None:
            nonlocal found
            for item in data["providers"].get(provider) or []:
                if item.get("account") == account:
                    item["proxy"] = proxy
                    found = True

        self._update(change)
        return found

    #: Провайдеры, у которых ротация ОДНОКРАТНАЯ: сервис отдаёт новый
    #: доступ в обмен на старый и старый тут же перестаёт приниматься.
    #: Для них «положить рядом» — верный способ потерять учётку: следующий
    #: запуск возьмёт исходное значение и получит отказ. Так устроен, к
    #: примеру, вход через Auth0 — в отличие от Kimi, где исходный токен
    #: продолжает работать месяцами. Сейчас пусто — механизм оставлен на
    #: случай следующего такого сервиса.
    ROTATION_REPLACES: frozenset[str] = frozenset()

    def record_rotation(self, credential: Credential) -> None:
        """Запомнить доступ, который сервис выдал взамен старого.

        Обычно исходное значение НЕ трогаем: см. заметку о ротации в шапке
        модуля. Новое кладётся рядом и пойдёт в дело, только если исходное
        отвергнут. Исключение — сервисы из ``ROTATION_REPLACES``.
        """
        replaces = credential.provider in self.ROTATION_REPLACES

        def change(data: dict) -> None:
            for item in data["providers"].get(credential.provider) or []:
                if item.get("account") != credential.account:
                    continue
                if item.get("value") == credential.value:
                    return  # ничего не менялось
                if replaces:
                    item["value"] = credential.value
                    item["rotated"] = ""
                else:
                    item["rotated"] = credential.value
                item["rotated_at"] = time.strftime("%Y-%m-%d %H:%M")

        self._update(change)

    def promote_rotation(self, provider: str, account: str) -> bool:
        """Повысить последнюю ротацию в основные.

        Делается, когда исходный доступ отвергнут, а ротация есть: это
        единственный случай, когда менять ``value`` оправданно.
        """
        promoted = False

        def change(data: dict) -> None:
            nonlocal promoted
            for item in data["providers"].get(provider) or []:
                if item.get("account") != account:
                    continue
                if not item.get("rotated"):
                    return
                item["value"] = item["rotated"]
                item["rotated"] = ""
                item["rotated_at"] = ""
                promoted = True

        self._update(change)
        return promoted

    # ── целостность доступа ───────────────────────────────────────────

    def missing_parts(self, provider: str) -> list[str]:
        """Чего не хватает провайдеру помимо ключа.

        Ключ — не весь доступ. У Meta AI и Copilot он лежит файлами, а у
        Gemini Web рабочая вторая кука живёт в каталоге кеша: без него
        библиотека молча уходит в анонимный режим и тратит ~180 секунд на
        попытки входа. Такое надо уметь заметить до запроса.
        """
        missing = []
        for name in AUTH_FILES.get(provider, []):
            if not (self.data_dir / name).exists():
                missing.append(name)
        for name in AUTH_STATE_DIRS.get(provider, []):
            directory = self.data_dir / name
            if not directory.is_dir() or not any(directory.iterdir()):
                missing.append(f"{name}/ (пусто)")
        return missing

    def ready(self, provider: str) -> bool:
        """Готов ли провайдер к работе: есть учётка и всё сопутствующее."""
        if self.missing_parts(provider):
            return False
        if auth_kind(provider) in (AUTH_NONE, AUTH_FILE):
            return True
        return bool(self.usable(provider))

    # ── перенос доступов ──────────────────────────────────────────────

    def import_settings(self, settings_path: str | Path,
                        overwrite: bool = False) -> dict[str, int]:
        """Забрать доступы из внешнего ``settings.json``.

        Строка ``|`` у AgentRouter означает ДВА независимых ключа и
        разворачивается в две учётки; у всех прочих она наоборот склеивает
        части одного доступа и трогать её нельзя.
        """
        path = Path(settings_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        added: dict[str, int] = {}

        for entry in data.get("ai_providers", []):
            provider = entry.get("name", "")
            value = (entry.get("key") or "").strip()
            if not provider or not value:
                continue
            if self.all(provider) and not overwrite:
                continue
            if overwrite:
                for old in self.all(provider):
                    self.remove(provider, old.account)

            # Разворачивание пула берёт на себя add(): у API строка с '|'
            # это несколько ключей, у веб-сессий — склейка одного доступа.
            entries = self.add(provider, value,
                               note="перенесено при импорте")
            added[provider] = len(entries)

        return added


#: Общее хранилище процесса. Отдельный экземпляр нужен только тестам.
default = Accounts()


def open_provider(provider: str, account: str = "", *, model: str = "",
                  allow_anonymous: bool = False, store: Accounts | None = None):
    """Поднять адаптер, взяв доступ из хранилища.

    Выбор учётки здесь простой — первая пригодная. Осмысленный выбор внутри
    пула (по остатку квоты, по паузам, по занятости) появится вместе с
    учётом квот: без него сравнивать учётки не по чему.

    Ротация подписывается на хранилище, а отвергнутый исходный доступ
    заменяется запасным, если тот есть. Это единственный случай, когда
    ``value`` меняется: политика описана в шапке модуля.
    """
    from foxroute import settings
    from foxroute.providers import build

    keeper = store or default

    if auth_kind(provider) in (AUTH_NONE, AUTH_FILE):
        # Аккаунт есть, но не в виде строки: у Copilot и Meta AI он лежит
        # файлами, у Z.ai его нет вовсе. Учётки в хранилище нет, поэтому
        # per-account прокси взять неоткуда — берём прокси провайдера, иначе
        # общий.
        settings.set_request_proxy(settings.provider_proxy(provider) or None)
        return build(provider, Credential(provider), model,
                     on_rotate=keeper.record_rotation,
                     allow_anonymous=allow_anonymous)

    pool = keeper.usable(provider)
    if account:
        pool = [a for a in pool if a.account == account]
    if not pool:
        # Необязательный вход (KEY_OPTIONAL: alice, deepai, perplexity, pi,
        # zai) работает и БЕЗ учётки — анонимно, урезанно. Адаптеры это умеют
        # (authorized = bool(value)), поэтому open_provider не кидает
        # AuthError до build, не глядя на allow_anonymous: иначе «живой»
        # optional-провайдер падал бы «нет учётки» и в чате, и в картинках.
        # Строим анонима, как и для AUTH_NONE.
        if allow_anonymous and auth_kind(provider) == AUTH_OPTIONAL:
            settings.set_request_proxy(settings.provider_proxy(provider) or None)
            return build(provider, Credential(provider), model,
                         on_rotate=keeper.record_rotation,
                         allow_anonymous=True)
        raise AuthError(
            f"в хранилище нет пригодной учётки для {provider}"
            + (f"/{account}" if account else ""), provider)

    chosen = pool[0]
    # Прокси этого запроса, по убыванию частности: свой у учётки → у
    # провайдера → общий (None проваливается к глобальному в current_proxy).
    settings.set_request_proxy(
        chosen.proxy or settings.provider_proxy(provider) or None)
    try:
        return build(provider, chosen.credential(), model,
                     on_rotate=keeper.record_rotation,
                     allow_anonymous=allow_anonymous)
    except AuthError:
        if not chosen.rotated:
            raise
        # Исходный доступ отвергнут, а запасной есть — вот теперь его время.
        keeper.promote_rotation(provider, chosen.account)
        refreshed = keeper.get(provider, chosen.account)
        if refreshed is None:
            raise
        return build(provider, refreshed.credential(), model,
                     on_rotate=keeper.record_rotation,
                     allow_anonymous=allow_anonymous)
