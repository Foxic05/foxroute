/* foxroute — чат с выбором провайдера.

   Отличие от Open WebUI, LibreChat и прочих: там модель либо есть, либо
   нет, и состояние прятать незачем. У нас двадцать хрупких сессий с
   квотами и паузами, поэтому живость провайдера показывается прямо в
   выборе — вместе с контекстом и скоростью хода. Иначе человек не поймёт,
   почему ответа нет, и решит, что сломался интерфейс.

   Разговоры лежат в localStorage: серверная база под них не нужна, а
   веб-сессии всё равно одноразовые — историю мы шлём целиком каждый раз. */

const $ = (id) => document.getElementById(id);

/* ── Язык интерфейса (i18n) ─────────────────────────────────────────
 *
 * Простой словарь: ключ — исходная русская строка, значение — английский
 * перевод. t() возвращает перевод в режиме «en» и саму строку в «ru», так
 * что русский остаётся значением по умолчанию без единой записи. Язык
 * лежит в localStorage под "foxroute-lang" ("ru"|"en", по умолчанию "ru").
 * Переключатель в шапке меняет значение и перезагружает страницу — тогда
 * все t() и applyStaticI18n() отрабатывают заново в новом языке. */

const I18N = {
  // ── Строки, приходящие с сервера ─────────────────────────────
  // Вид провайдера (registry.py → router.py) — тег в пуле.
  "веб": "web",
  // Подсказки по доступу (registry.py ACCESS_HINTS) — что вставить.
  "кука __Secure-next-auth.session-token": "the __Secure-next-auth.session-token cookie",
  "куки __Secure-1PSID и __Secure-1PSIDTS через |": "the __Secure-1PSID and __Secure-1PSIDTS cookies, joined with |",
  "поле token из localStorage (JWT целиком)": "the token field from localStorage (the whole JWT)",
  "поле userToken из localStorage": "the userToken field from localStorage",
  "кука refresh_token": "the refresh_token cookie",
  "кука Session_id (домен .yandex.ru)": "the Session_id cookie (.yandex.ru domain)",
  "куки sso и sso-rw через |": "the sso and sso-rw cookies, joined with |",
  "куки строкой «a=b; c=d», обязательна __Host-session": "cookies as a string «a=b; c=d»; __Host-session is required",
  "куки строкой «a=b; c=d», обязательна p-b": "cookies as a string «a=b; c=d»; p-b is required",
  "кука session_id": "the session_id cookie",
  "кука __session (или весь набор кук объектом JSON)": "the __session cookie (or the whole cookie set as a JSON object)",
  "куки строкой «a=b; c=d», нужна в том числе csrftoken": "cookies as a string «a=b; c=d»; csrftoken is required too",
  "куки строкой «a=b; c=d», обязательна _U": "cookies as a string «a=b; c=d»; _U is required",
  "куки строкой «a=b; c=d»": "cookies as a string «a=b; c=d»",
  "API-ключ": "API key",
  "вся строка кук (sessionKey обязательна, cf_clearance и __cf_bm тоже — бери из Network → Headers)": "the full cookie string (sessionKey required, cf_clearance and __cf_bm too — take them from Network → Headers)",
  "API-токен Workers AI (шаблон «Workers AI»)": "a Workers AI API token (the «Workers AI» template)",
  "пробный ключ (Trial) — бесплатный, 1000 вызовов в месяц": "a Trial key — free, 1000 calls per month",
  "ничего не нужно": "nothing needed",
  "token из localStorage": "the token from localStorage",
  "вход по коду устройства, как в мобильном приложении": "device-code sign-in, like in the mobile app",
  "файлы meta_cookies.json, meta_proto_template.json, meta_ws_auth.json": "the files meta_cookies.json, meta_proto_template.json, meta_ws_auth.json",
  // Примечания к доступу (ACCESS_HINTS note).
  "Может быть разбита на .0 и .1 — склей их через | в одну строку, по порядку.": "May be split into .0 and .1 — join them with | into one string, in order.",
  "Вторая нужна не всем аккаунтам, но с ней стабильнее.": "The second isn't needed by every account, but it's more stable with it.",
  "Сам не обновляется — срок зашит внутрь токена.": "Doesn't refresh itself — the expiry is baked into the token.",
  "Живёт месяцами. Сервис выдаёт новый токен в ответах, хранилище подхватывает его само.": "Lasts for months. The service issues a new token in its responses, and the store picks it up automatically.",
  "Без неё Алиса отвечает анонимно: лимиты ниже, память чатов не работает.": "Without it Alice answers anonymously: lower limits, chat memory doesn't work.",
  "Работает и без неё, просто с меньшими лимитами.": "Works without it too, just with lower limits.",
  "Бесплатно 10 текстовых запросов в день.": "Free: 10 text requests per day.",
  "Работает и без них — кука лишь привязывает бесплатные кредиты к аккаунту.": "Works without them too — the cookie only ties the free credits to an account.",
  "У каждой модели своя квота, они не пересекаются.": "Each model has its own quota; they don't overlap.",
  "Работает анонимно, без ключа и кук.": "Works anonymously, without a key or cookies.",
  "Только картинки, анонимно. Текст у них платный.": "Images only, anonymous. Their text is paid.",
  "Регистрируется сам, анонимно. 200 запросов на аккаунт, остаток сервис сообщает в ответе.": "Registers itself, anonymously. 200 requests per account; the service reports the remainder in its response.",
  "Озвучка одиннадцатью голосами, анонимно и без суточной нормы — в отличие от Orpheus на Groq.": "Text-to-speech with eleven voices, anonymous and with no daily limit — unlike Orpheus on Groq.",
  "Ответит и анонимно, но урезанно: гостю запрещена загрузка файлов. Ещё нужен сценарий обхода капчи (zai_captcha.js) в каталоге данных.": "Answers anonymously too, but in a reduced mode: guests can't upload files. It also needs the CAPTCHA-bypass script (zai_captcha.js) in the data directory.",
  "Токены лежат файлом copilot_tokens.json и обновляются сами.": "Tokens live in the copilot_tokens.json file and refresh themselves.",
  "Шаблон запроса снимается один раз с живого клиента — без него запрос собрать нельзя.": "The request template is captured once from a live client — without it a request can't be assembled.",
  // Статусы проверки доступа (health.py verdict.state / статичные detail).
  "жив": "alive",
  "доступ отвергнут": "access rejected",
  "норма выбрана": "rate limit reached",
  "поломка адаптера": "adapter failure",
  "кеш одноразового токена": "one-time-token cache",
  "отвечает, но гостем": "answers, but as a guest",
  "не проверялся": "not checked",
  "пустой ответ при успешном запросе": "empty response despite a successful request",

  // Шапка, рельс, навигация
  "Новый чат": "New chat",
  "Поиск по чатам": "Search chats",
  "Пул провайдеров": "Provider pool",
  "Запросов сегодня": "Requests today",
  "Настройки": "Settings",
  "Настройки: доступы, прокси": "Settings: access, proxy",
  "Тема": "Theme",

  // Выбор провайдера
  "Авто": "Auto",
  "выберет сам": "auto-selects",
  "Маршрут": "Route",
  "выберет живого из": "picks a live one from",
  "обойдёт упавшего": "skips the down one",
  "Веб-сессии — вход логином": "Web sessions — login sign-in",
  "Официальные API — по ключу": "Official APIs — by key",
  "Без входа — анонимно": "No sign-in — anonymous",
  "живой": "live",
  "нужен вход": "sign-in needed",
  "пауза": "paused",
  "не залогинен — заведи доступ во вкладке «Настройки» или выбери «Авто»":
    "not signed in — set up access in the Settings tab or choose Auto",
  "цикл": "loop",
  "платно": "paid",
  "ключ": "key", "ключа": "keys", "ключей": "keys",
  "учётка": "account", "учётки": "accounts", "учёток": "accounts",
  "нужен ключ": "key needed",
  "аноним": "anonymous",
  "из": "of",
  "живых": "live",
  "живых нет": "none live",

  // Пул
  "готовы отвечать прямо сейчас": "ready to answer right now",
  "Все": "All",
  "Веб": "Web",
  "Картинки": "Images",

  // Список чатов
  "Сегодня": "Today",
  "Вчера": "Yesterday",
  "Ранее": "Earlier",
  "Без названия": "Untitled",

  // Сообщения и действия под ответом
  "вариант": "variant",
  "Прошлый вариант": "Previous variant",
  "Вы": "You",
  "Ассистент": "Assistant",
  "файл": "file",
  "Копировать": "Copy",
  "Заново другим": "Try another",
  "Свернуть": "Collapse",
  "Раскрыть полностью": "Expand fully",
  "Скопировано": "Copied",
  "Не вышло": "Failed",
  "Размышления": "Thinking",
  "Играть": "Play",
  "Скопировать код": "Copy code",

  // Пустой экран
  "О чём": "What should we",
  "поговорим?": "talk about?",
  "Хитрая лисичка сама выберет живую модель, обойдёт лимиты и озвучит ответ. Ты просто спрашивай.":
    "The clever little fox picks a live model itself, works around limits and voices the answer. Just ask.",

  // Подсказки на пустом экране
  "Объясни, зачем нужен индекс в базе данных": "Explain why a database index is needed",
  "Простыми словами": "In simple terms",
  "Напиши функцию на Python для разбора CSV": "Write a Python function to parse CSV",
  "С примерами": "With examples",
  "Чем отличается процесс от потока": "How a process differs from a thread",
  "Примеры и сравнение": "Examples and comparison",
  "Нарисуй кота в скандинавском стиле": "Draw a cat in Scandinavian style",
  "На Луне 🌙": "On the Moon 🌙",
  "Покажи, как работает бинарный поиск": "Show how binary search works",
  "С разбором шагов": "Step by step",
  "Придумай название для кофейни у моря": "Come up with a name for a seaside cafe",
  "Десять вариантов": "Ten options",
  "Объясни квантовую запутанность": "Explain quantum entanglement",
  "Без формул": "Without formulas",
  "Нарисуй город будущего на закате": "Draw a future city at sunset",
  "Неоновые огни": "Neon lights",
  "Как читать график котировок": "How to read a stock chart",
  "Для начинающего": "For a beginner",
  "Составь маршрут по Японии на неделю": "Plan a week-long route through Japan",
  "Бюджетный": "On a budget",
  "Посоветуй книги как «Дюна»": "Recommend books like «Dune»",
  "И почему именно эти": "And why these ones",
  "Придумай сюжет для короткого рассказа": "Come up with a plot for a short story",
  "Неожиданный финал": "A twist ending",
  "Отладь: почему рекурсия падает в переполнение": "Debug: why recursion overflows the stack",
  "Разбор причин": "Cause analysis",
  "Напиши письмо с отказом от встречи": "Write an email declining a meeting",
  "Вежливо и коротко": "Politely and briefly",
  "Что такое энтропия на бытовых примерах": "What entropy is, with everyday examples",
  "Понятно": "Clearly",
  "Нарисуй лису-космонавта": "Draw a fox astronaut",
  "В стиле акварели": "In watercolor style",

  // Поток и ошибки
  "ответ оборвался": "answer cut off",
  "лимит исчерпан": "limit reached",
  "отказ провайдера": "provider refused",

  // Генерация картинки
  "Картинка": "Image",
  "картинка": "image",
  "картинки не получены": "no images received",

  // Озвучка
  "Озвучка": "Speech",
  "Озвучка:": "Speech:",
  "Голос:": "Voice:",
  "Рисует:": "Draws:",
  "без суточной нормы": "no daily limit",
  "100 в сутки, нужен ключ": "100 per day, key needed",
  "Не залогинен — заведи доступ во вкладке «Настройки»":
    "Not signed in — set up access in the Settings tab",
  "Нужен вход — заведи доступ во вкладке «Настройки»":
    "Sign-in needed — set up access in the Settings tab",
  "не залогинен — выбери доступного или заведи доступ во вкладке «Настройки»":
    "not signed in — choose an available one or set up access in the Settings tab",
  "Этот движок озвучки не залогинен — оставь OpenAI.fm (без входа) или заведи доступ во вкладке «Настройки»":
    "This voice engine isn't signed in — keep OpenAI.fm (no sign-in) or set up access in the Settings tab",

  // Поле ввода, скрепка, кнопки под полем
  "Спроси что-нибудь…": "Ask anything…",
  "Опиши, что нарисовать…": "Describe what to draw…",
  "Что озвучить голосом…": "What to voice…",
  "Убрать": "Remove",
  "Прикрепить файл": "Attach file",
  "Отправить": "Send",
  "Глубокое размышление": "Deep thinking",
  "Искать в сети — ответ с опорой на живую выдачу":
    "Search the web — an answer grounded in live results",
  "Глубокое исследование — несколько поисков и сводка со ссылками. Идёт дольше обычного ответа":
    "Deep research — several searches and a summary with links. Takes longer than a normal answer",
  "Генерация картинки": "Image generation",
  "Озвучить текст голосом": "Read text aloud",
  "Поиск": "Search",
  "Исследовать": "Research",
  "отправить": "send",
  "перенос": "new line",

  // Подсказки на кнопках по умениям
  "Размышление есть, но провайдер с ним не залогинен — заведи доступ во вкладке «Настройки»":
    "Thinking is available, but no provider with it is signed in — set up access in the Settings tab",
  "Веб-поиск есть, но провайдер с ним не залогинен — заведи доступ во вкладке «Настройки»":
    "Web search is available, but no provider with it is signed in — set up access in the Settings tab",
  "Глубокое исследование умеют ": "Deep research is supported by ",
  " — залогинь кого-то из них во вкладке «Настройки»":
    " — sign one of them in on the Settings tab",
  "Приём файлов есть, но такой провайдер не залогинен — заведи доступ":
    "File uploads are available, but no such provider is signed in — set up access",
  "Нет залогиненного провайдера с этим умением — заведи доступ":
    "No signed-in provider with this ability — set up access",

  // Предохранители отправки
  "Нет доступного рисовальщика — залогинь провайдера или пользуйся анонимными (Pollinations, DeepAI, Алиса).":
    "No image generator available — sign in a provider or use anonymous ones (Pollinations, DeepAI, Alice).",
  "не залогинен — выбери доступного рисовальщика.":
    "not signed in — choose an available image generator.",
  "не залогинен — переключись на OpenAI.fm (без входа) или заведи доступ.":
    "not signed in — switch to OpenAI.fm (no sign-in) or set up access.",
  "Нет ни одного залогиненного провайдера — заведи доступ во вкладке «Настройки».":
    "No signed-in providers — set up access in the Settings tab.",
  ", но они не залогинены.": ", but they're not signed in.",
  "Нет залогиненного провайдера с размышлением.":
    "No signed-in provider with thinking.",
  "Нет залогиненного провайдера с веб-поиском.":
    "No signed-in provider with web search.",
  "не залогинен — выбери «Авто» или заведи доступ во вкладке «Настройки».":
    "not signed in — choose Auto or set up access in the Settings tab.",
  "некоторые провайдеры": "some providers",

  // Надиктовка
  "Микрофон запрещён — разреши доступ в настройках браузера":
    "Microphone blocked — allow access in your browser settings",
  "Микрофон не найден": "Microphone not found",
  "Остановить запись": "Stop recording",
  "Надиктовать — распознаем речь и вставим текстом":
    "Dictate — we'll recognize speech and insert it as text",
  "Ничего не расслышал": "Didn't catch anything",
  "не вышло": "failed",
  "Не распознал": "Couldn't recognize",

  // Счётчик запросов
  "Нет запросов": "No requests",

  // Настройки: панель, прокси, доступы
  "Всё лежит только у тебя на машине": "Everything stays only on your machine",
  "Доступы": "Access",
  "Прокси": "Proxy",
  "Через него пойдут и вход в браузере, и все запросы провайдеров. Куки снимаются под IP прокси — с него же и работают.":
    "Both browser sign-in and all provider requests go through it. Cookies are grabbed under the proxy's IP — and work from it too.",
  "socks5://user:pass@host:port или http://host:port":
    "socks5://user:pass@host:port or http://host:port",
  "Сохранить": "Save",
  "Проверить": "Check",
  "Форматы:": "Formats:",
  "Пусто — без прокси.": "Empty — no proxy.",
  "Нужен доступ": "Need access",
  "Подключены": "Connected",
  "Без доступа": "No access",
  "Как завести доступ и что за 🟢🟡🔴": "How to set up access and what 🟢🟡🔴 mean",
  "Два способа.": "Two ways.",
  "«Войти в браузере» откроет чистое окно — логинишься там, оставляешь окно открытым, жмёшь «Забрать куки»: доступ снимется и проверится сам. «Вручную ↗» — открыть сайт, войти в своём браузере и вставить куку из F12 (что копировать, написано у каждого).":
    "«Sign in via browser» opens a clean window — you log in there, leave the window open, and click Grab cookies: access is captured and verified automatically. «Manually ↗» — open the site, sign in in your own browser and paste the cookie from F12 (what to copy is noted for each).",
  "Строгие сервисы.": "Strict services.",
  "DeepSeek и местами Google на автоматический вход могут ругаться «подозрительная среда». Их проще заводить вручную: войти в обычном браузере и вставить куку. Регистрировать новый аккаунт всегда лучше в своём браузере, а не в окне входа.":
    "DeepSeek and sometimes Google may complain «suspicious environment» about automatic sign-in. They're easier to set up manually: sign in in a normal browser and paste the cookie. Registering a new account is always better in your own browser than in the sign-in window.",
  "Привязка к IP": "IP binding",
  "— маркер у провайдера:": "— provider marker:",
  "адрес не важен, снимай где угодно ·": "address doesn't matter, grab anywhere ·",
  "переезд обычно ок, но при смене страны сервис может переспросить ·":
    "moving is usually fine, but on a country change the service may re-ask ·",
  "привязан к адресу — снимай там же, где будешь запускать. Проще всего заводить доступ на той же машине, где крутится роутер: тогда IP совпадает у всех.":
    "tied to the address — grab it where you'll run it. Easiest to set up access on the same machine that runs the router: then the IP matches for everyone.",

  // Карточка доступа (keyRow)
  "Проверить на живом (тратит сообщение из квоты)":
    "Check live (spends a message from quota)",
  "проверить": "check",
  "Свой прокси для этой учётки — переопределяет общий":
    "Own proxy for this account — overrides the shared one",
  "прокси": "proxy",
  "выключить": "disable",
  "включить": "enable",
  "удалить": "delete",
  "выключена": "disabled",
  "socks5://user:pass@host:port — пусто = общий прокси":
    "socks5://user:pass@host:port — empty = shared proxy",
  "сохранить": "save",
  "вручную ↗": "manually ↗",
  "Войти ↗": "Sign in ↗",
  "Откроется чистое окно браузера — войди там, потом «Забрать куки»":
    "A clean browser window opens — sign in there, then Grab cookies",
  "Войти в браузере": "Sign in via browser",
  "Не привязан к IP — снимай и запускай где угодно":
    "Not tied to IP — grab and run anywhere",
  "Переезд на другой IP обычно переживает; при смене страны сервис может запросить подтверждение входа":
    "Usually survives a move to another IP; on a country change the service may ask to confirm sign-in",
  "Привязан к IP (Cloudflare или гео-защита) — снимай ТАМ ЖЕ, где доступ будет работать":
    "Tied to IP (Cloudflare or geo-protection) — grab it RIGHT WHERE access will run",
  "не хватает": "missing",
  "можно добавить несколько ключей — они работают как пул":
    "you can add several keys — they work as a pool",
  "доступ": "access",
  "Добавить": "Add",
  "прокси провайдера": "provider proxy",
  "Прокси этого провайдера — на вход и на запросы. Пусто — общий":
    "This provider's proxy — for sign-in and requests. Empty — shared",
  "socks5://user:pass@host:port — пусто = общий":
    "socks5://user:pass@host:port — empty = shared",
  "Работает без входа": "Works without sign-in",
  "Через свой прокси": "Via own proxy",

  // Группы в списке доступов
  "API-ключи": "API keys",
  "Здесь пусто": "Nothing here",

  // Прокси и действия с доступами: статусы
  "сохраняю…": "saving…",
  "✓ сохранено": "✓ saved",
  " (прокси выключен)": " (proxy off)",
  "введи адрес прокси": "enter a proxy address",
  "проверяю связь через прокси…": "checking connection through proxy…",
  "✓ работает — видимый IP: ": "✓ works — visible IP: ",
  "не отвечает: ": "not responding: ",
  "работаю…": "working…",
  "сервер не ответил": "server didn't respond",
  "вставь доступ": "paste the access",
  "добавлено": "added",
  "прокси задан": "proxy set",
  "прокси снят — пойдёт через общий": "proxy removed — will use the shared one",
  "впиши адрес прокси": "enter a proxy address",
  "✓ прокси провайдера задан": "✓ provider proxy set",
  "снят — пойдёт общий": "removed — will use shared",
  "не вышло сохранить": "couldn't save",
  "проверяю живость (тратится сообщение)…": "checking liveness (spends a message)…",
  "открываю окно браузера…": "opening browser window…",
  "забираю куки и проверяю…": "grabbing cookies and checking…",
  "✓ доступ работает": "✓ access works",
  "добавлено, проверка: ": "added, check: ",
  "Залогинься в открывшемся окне, потом нажми «Забрать куки». ":
    "Sign in the window that opened, then click Grab cookies. ",
  "⚠ Не закрывай окно браузера, пока не нажал «Забрать куки» — снятие идёт из живого окна.":
    "⚠ Don't close the browser window until you've clicked Grab cookies — extraction runs from the live window.",
  "Забрать куки": "Grab cookies",
};

function t(s) {
  return localStorage.getItem("foxroute-lang") === "en" ? (I18N[s] || s) : s;
}

// Детали проверки приходят строкой с сервера; часть — с динамическим
// хвостом (список, число), поэтому точный t() их не поймает: переводим по
// известному образцу, а чужой текст (ошибки адаптеров) отдаём как есть.
function tDetail(s) {
  if (!s) return "";
  if (localStorage.getItem("foxroute-lang") !== "en") return s;
  if (I18N[s]) return I18N[s];
  let m;
  if ((m = s.match(/^не хватает: (.+)$/))) return "missing: " + m[1];
  if ((m = s.match(/^на паузе ещё (\d+)\s*с$/))) return "paused for " + m[1] + "s more";
  return s;
}

// Паузы провайдера: «учётка: троттл 42с» — переводим вид паузы и суффикс
// секунд, имя учётки трогать нельзя.
function tPause(s) {
  if (localStorage.getItem("foxroute-lang") !== "en") return s;
  return s.replace(": бюджет ", ": budget ")
          .replace(": троттл ", ": throttle ")
          .replace(/(\d+)с$/, "$1s");
}

// Статические подписи в index.html: [data-i18n] получают перевод в
// textContent, [data-i18n-placeholder] — в placeholder, [data-i18n-title]
// — в подсказку title. Вызывается на DOMContentLoaded.
function applyStaticI18n() {
  const en = localStorage.getItem("foxroute-lang") === "en";
  document.documentElement.lang = en ? "en" : "ru";
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.getAttribute("data-i18n-title"));
  });
  // Кнопка-переключатель показывает язык, НА который переключит.
  const toggle = $("lang-toggle");
  if (toggle) toggle.textContent = en ? "RU" : "EN";
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", applyStaticI18n);
} else {
  applyStaticI18n();
}

/* Авторизация: интерфейс за туннелем ходит БЕЗ ключа — сервер узнаёт «свой»
 * UI по same-origin (Sec-Fetch-Site/Origin) и пропускает. Ключ требуется
 * только программным клиентам (чужой проект). Поэтому здесь ничего делать не
 * нужно — фетчи идут как есть. */

const state = {
  model: "auto",
  thinking: false,
  deepResearch: false,
  webSearch: false,
  voice: "coral",
  drawModel: "auto",
  providers: [],
  threads: [],
  threadFilter: "",
  current: null,
  busy: false,
};

/* ── Хранилище разговоров ───────────────────────────────────────── */

const STORE_KEY = "foxroute.threads";

function loadThreads() {
  try {
    state.threads = JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
  } catch {
    state.threads = [];
  }
}

function saveThreads() {
  // Держим последние 50: больше в localStorage складывать незачем, а
  // переполнение его молча ломает.
  state.threads = state.threads.slice(0, 50);
  try {
    // Пустой разговор хранить нечего: он живёт в памяти до первого
    // сообщения, а переживать перезагрузку ему незачем.
    const worth = state.threads.filter((t) => t.messages.length);
    localStorage.setItem(STORE_KEY, JSON.stringify(worth));
  } catch { /* переполнено — не страшно, разговор в памяти остаётся */ }
}

function newThread() {
  // Пустой разговор уже открыт — незачем плодить второй такой же.
  // Без этой проверки кнопка набивает список пустышками по одной на клик.
  if (state.current && !state.current.messages.length) {
    $("input").focus();
    return;
  }
  // Заодно подчищаем пустые, оставшиеся от прошлых сессий.
  state.threads = state.threads.filter((t) => t.messages.length);

  const thread = { id: Date.now().toString(36), title: "", messages: [] };
  state.threads.unshift(thread);
  state.current = thread;
  saveThreads();
  renderThreads();
  renderMessages();
  $("input").focus();
}

function pickThread(id) {
  state.current = state.threads.find((t) => t.id === id) || null;
  renderThreads();
  renderMessages();
}

function dropThread(id, event) {
  event.stopPropagation();
  state.threads = state.threads.filter((t) => t.id !== id);
  if (state.current && state.current.id === id) state.current = null;
  saveThreads();
  renderThreads();
  renderMessages();
}

/* ── Провайдеры ─────────────────────────────────────────────────── */

async function loadProviders() {
  try {
    const response = await fetch("/api/status");
    const data = await response.json();
    // Пустой список принимаем только если сервер правда так ответил.
    state.providers = data.providers || [];
    // Рисующие приходят отдельно (картиночные-без-текста в providers нет).
    if (data.draw) state.draw = data.draw;
  } catch {
    // Раньше здесь стояло обнуление, и одна неудачная попытка опроса
    // (а он идёт каждые 20 секунд) сносила всё: прятались ВСЕ кнопки
    // умений, сбрасывались включённые режимы и молча пропадали
    // прикреплённые файлы. Сеть моргнула — человек потерял вложение.
    // Держимся за прошлые данные: устаревшие лучше, чем никаких.
    if (!state.providers.length) return;
  } finally {
    // Умения знаем только из состояния пула — обновляем кнопки тут же.
    if (typeof syncAbilityButtons === "function") syncAbilityButtons();
  }
  renderPicker();
  renderPool();
  renderPoolMini();
  // Если бар картинки/озвучки открыт — обновим в нём пометки доступности,
  // иначе после логина/разлогина они остались бы устаревшими.
  if ($("btn-image")?.classList.contains("active")) renderImageBar();
  if ($("btn-audio")?.classList.contains("active")) renderVoiceBar();
}

function providerById(id) {
  return state.providers.find((p) => p.id === id) || null;
}

function providerLabel(id) {
  const provider = providerById(id);
  return provider ? provider.label : id;
}

/* ── Список выбора ──────────────────────────────────────────────── */

function renderPicker() {
  const menu = $("picker-menu");
  // «Анонимно» — это НЕ тип провайдера, а ФАКТ: сейчас идёт без учётки
  // (сервер ставит anon только когда реальной записи нет). С кукой/ключом
  // провайдер уже не аноним — ему место в веб-сессиях/API, даже если вход
  // у него необязательный. kind сервер шлёт по-русски («веб»/«api»).
  const web = state.providers.filter((p) => !p.anon && p.kind === "веб");
  const api = state.providers.filter((p) => !p.anon && p.kind === "api");
  const anon = state.providers.filter((p) => p.anon);

  const ready = state.providers.filter((p) => p.state === "ready").length;

  let html = `
    <div class="group-label">${t("Маршрут")}</div>
    ${optionHTML({
      id: "auto",
      label: t("Авто"),
      sub: `${t("выберет живого из")} ${ready}, ${t("обойдёт упавшего")}`,
      state: ready ? "ready" : "paused",
      facts: "",
    })}`;
  if (web.length) {
    html += `<div class="group-label">${t("Веб-сессии — вход логином")}</div>
             ${web.map(providerOption).join("")}`;
  }
  if (api.length) {
    html += `<div class="group-label">${t("Официальные API — по ключу")}</div>
             ${api.map(providerOption).join("")}`;
  }
  if (anon.length) {
    html += `<div class="group-label">${t("Без входа — анонимно")}</div>
             ${anon.map(providerOption).join("")}`;
  }
  // Что значат цвета точек — иначе серые «нужен вход» читаются как поломка.
  html += `
    <div class="picker-legend">
      <span><span class="dot ready"></span>${t("живой")}</span>
      <span><span class="dot locked"></span>${t("нужен вход")}</span>
      <span><span class="dot paused"></span>${t("пауза")}</span>
    </div>`;
  menu.innerHTML = html;

  menu.querySelectorAll(".option").forEach((node) => {
    node.addEventListener("click", () => {
      // Незалогиненного провайдера выбрать нельзя — запрос ему уйти не может.
      const base = node.dataset.id.split("/")[0];
      const chosen = state.providers.find((x) => x.id === base);
      if (chosen && chosen.state === "locked") {
        toast("«" + (chosen.label || base) + "» " +
              t("не залогинен — заведи доступ во вкладке «Настройки» или выбери «Авто»"));
        return;
      }
      state.model = node.dataset.id;
      if (typeof syncAbilityButtons === "function") syncAbilityButtons();
      // Есть из чего выбрать — оставляем меню открытым, чтобы человек
      // сразу увидел раскрывшийся список моделей.
      if (!chosen || (chosen.models || []).length < 2) closePicker();
      renderPicked();
      renderPicker();
    });
  });

  menu.querySelectorAll(".model-chip").forEach((node) => {
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      state.model = node.dataset.id;
      if (typeof syncAbilityButtons === "function") syncAbilityButtons();
      closePicker();
      renderPicked();
      renderPicker();
    });
  });
  renderPicked();
}

function providerOption(provider) {
  const facts = [];
  if (provider.context) {
    facts.push(`<span class="tag">${Math.round(provider.context / 1000)}k</span>`);
  }
  if (provider.turn) {
    facts.push(`<span class="tag">${provider.turn}s</span>`);
  }
  if (provider.agentic === "good") {
    facts.push(`<span class="tag good">${t("цикл")}</span>`);
  }
  // Платного «Авто» не берёт — без пометки непонятно, почему он молчит.
  if (provider.paid) {
    facts.push(`<span class="tag paid">${t("платно")}</span>`);
  }

  const who = `${provider.accounts} ${plural(provider.accounts,
    provider.kind === "api" ? [t("ключ"), t("ключа"), t("ключей")]
                            : [t("учётка"), t("учётки"), t("учёток")])}`;
  // Модель показываем всегда: спрашивают именно её, а не имя сервиса.
  // «Аноним» — работает без входа и учётки нет; «нужен вход» — без доступа
  // не работает вовсе; иначе — сколько реальных учёток/ключей.
  const lead = provider.state === "locked"
    ? (provider.kind === "api" ? t("нужен ключ") : t("нужен вход"))
    : (provider.anon ? t("аноним") : who);
  let sub = provider.model ? `${lead} · ${provider.model}` : lead;
  if (provider.paused && provider.paused.length) {
    sub = provider.paused.map(tPause).join(", ");
  }

  // Выбранный провайдер с несколькими моделями разворачивает их списком.
  // Что показывать, решает сервер: не-чатовое (распознавание речи,
  // озвучка) он в список не кладёт.
  const models = provider.models || [];
  const picked = state.model === provider.id ||
                 state.model.startsWith(provider.id + "/");
  let extra = "";
  if (picked && models.length > 1) {
    extra = '<div class="model-row">' + models.map((m) => {
      const id = `${provider.id}/${m.id}`;
      const on = state.model === id ||
                 (state.model === provider.id && m.id === provider.model);
      // Норма у каждой модели своя и различается в разы — без неё выбор
      // вслепую. Тысячи сокращаем, иначе чип не помещается.
      const day = m.req_day >= 1000
        ? `${Math.round(m.req_day / 1000)}k/сут`
        : (m.req_day ? `${m.req_day}/сут` : "");
      const marks = [m.vision ? "👁" : "", day].filter(Boolean).join(" ");
      return `<button class="model-chip${on ? " on" : ""}"
                      data-id="${escapeHTML(id)}"
                      title="${escapeHTML(m.id)}">${escapeHTML(m.id)}${
        marks ? `<span class="model-note">${escapeHTML(marks)}</span>` : ""
      }</button>`;
    }).join("") + "</div>";
  }

  return optionHTML({
    id: provider.id,
    label: provider.label,
    sub,
    state: provider.state,
    facts: facts.join(""),
  }) + extra;
}

function optionHTML({ id, label, sub, state: dotState, facts }) {
  const chosen = state.model === id ||
                 state.model.startsWith(id + "/") ? " chosen" : "";
  const dim = (dotState === "paused" || dotState === "locked") ? " unavailable" : "";
  return `
    <button class="option${chosen}${dim}" data-id="${id}">
      <span class="dot ${dotState}"></span>
      <span>
        <span class="option-name">${escapeHTML(label)}</span>
        <span class="option-sub">${escapeHTML(sub)}</span>
      </span>
      <span class="option-facts">${facts}</span>
    </button>`;
}

function renderPicked() {
  const dot = $("picked-dot");
  const name = $("picked-name");
  const note = $("picked-note");

  if (state.model === "auto") {
    const ready = state.providers.filter((p) => p.state === "ready").length;
    dot.className = "dot " + (ready ? "ready" : "paused");
    name.textContent = t("Авто");
    note.textContent = ready ? `${t("из")} ${ready} ${t("живых")}` : t("живых нет");
    return;
  }
  // Выбор бывает составным: «groq/llama-3.3-70b-versatile».
  const [id, sub] = state.model.split("/");
  const provider = providerById(id);
  dot.className = "dot " + (provider ? provider.state : "");
  name.textContent = provider ? provider.label : state.model;

  if (provider && provider.paused && provider.paused.length) {
    note.textContent = tPause(provider.paused[0]);
  } else {
    // В подписи держим модель: у Groq их десять, и знать, какая сейчас
    // выбрана, важнее, чем лишний раз прочесть имя сервиса.
    note.textContent = sub || (provider ? provider.model : "") || "";
  }
}

function openPicker() { $("picker-menu").hidden = false; }
function closePicker() { $("picker-menu").hidden = true; }

/* ── Пул справа ─────────────────────────────────────────────────── */

function renderPool(filter) {
  // Без явного фильтра держим текущую вкладку, а не сбрасываем на «all».
  // Опрос статуса раз в 20 с зовёт renderPool() без аргумента, и открытая
  // панель пула молча возвращалась к «Все», хотя подсвечена была веб/api.
  filter = filter
    || document.querySelector(".pool-tab.active")?.dataset.tab
    || "all";
  const filtered = state.providers.filter((p) => {
    if (filter === "web") return p.kind === "веб";
    if (filter === "api") return p.kind === "api";
    if (filter === "images") return p.id === "bing_images" ||
      ["qwen","chatgpt","grok","alice","ms_copilot","meta_ai"].includes(p.id);
    return true;
  });
  const note = $("pool-note");
  if (note) {
    const ready = state.providers.filter((p) => p.state === "ready").length;
    note.textContent =
      `${ready} ${t("из")} ${state.providers.length} ${t("готовы отвечать прямо сейчас")}`;
  }

  $("pool-list").innerHTML = filtered.map((provider) => {
    // В покое показываем модель — её и спрашивают. В паузе важнее, до
    // каких пор она держится.
    const sub = provider.paused && provider.paused.length
      ? provider.paused.map(tPause).join(", ")
      : [provider.model, `${Math.round(provider.context / 1000)}k`,
         `${provider.turn}s`].filter(Boolean).join(" · ");
    const paid = provider.paid
      ? '<span class="pool-kind paid">' + t("платно") + '</span>' : "";
    return `
      <div class="pool-row">
        <span class="dot ${provider.state}"></span>
        <span>
          <div class="pool-name">${escapeHTML(provider.label)}</div>
          <div class="pool-sub">${escapeHTML(sub)}</div>
        </span>
        <span class="pool-kinds">${paid}
          <span class="pool-kind">${t(provider.kind)}</span></span>
      </div>`;
  }).join("");
}

/* ── Отрисовка ──────────────────────────────────────────────────── */

function renderThreads() {
  const filter = state.threadFilter || "";
  let list = state.threads;
  if (filter) {
    list = list.filter((t) => (t.title || "").toLowerCase().includes(filter));
  }

  // Группировка по дате: id треда — Date.now() в base36, из него берём день.
  const dayOf = (id) => {
    const ms = parseInt(id, 36);
    if (!isFinite(ms)) return "ранее";
    const d = new Date(ms);
    const now = new Date();
    const sameDay = (a, b) => a.toDateString() === b.toDateString();
    if (sameDay(d, now)) return "Сегодня";
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (sameDay(d, y)) return "Вчера";
    return "Ранее";
  };

  const groups = {};
  for (const t of list) {
    const g = dayOf(t.id);
    (groups[g] = groups[g] || []).push(t);
  }

  const order = ["Сегодня", "Вчера", "Ранее"];
  let html = "";
  for (const g of order) {
    if (!groups[g]) continue;
    html += `<div class="thread-group-label">${t(g)}</div>`;
    html += groups[g].map((thread) => {
      const active = state.current && state.current.id === thread.id ? " active" : "";
      const title = thread.title || t("Без названия");
      return `<button class="thread-item${active}" data-id="${thread.id}">
        <span class="thread-title">${escapeHTML(title)}</span>
        <span class="thread-drop" data-drop="${thread.id}">×</span>
      </button>`;
    }).join("");
  }
  $("threads").innerHTML = html;

  $("threads").querySelectorAll(".thread-item").forEach((node) => {
    node.addEventListener("click", (event) => {
      if (event.target.dataset.drop) return dropThread(event.target.dataset.drop, event);
      pickThread(node.dataset.id);
    });
  });
}

// Пул-сетка в сайдбаре: квадратики, живые светятся.
function renderPoolMini() {
  const grid = $("pool-mini-grid");
  const count = $("pool-mini-count");
  if (!grid) return;
  const total = state.providers.length;
  const ready = state.providers.filter((p) => p.state === "ready").length;
  grid.innerHTML = state.providers.map((p) =>
    `<span class="pool-dot ${p.state === "ready" ? "live" : ""}"
           title="${escapeHTML(p.label)}"></span>`
  ).join("");
  if (count) count.textContent = total ? `${ready}/${total}` : "—";
}

function renderMessages() {
  const thread = $("thread");
  const messages = state.current ? state.current.messages : [];

  if (!messages.length) {
    // Каждый раз новый набор подсказок: пустой экран показывается часто,
    // и один и тот же список на нём быстро перестают замечать.
    thread.innerHTML = emptyHTML();
    wireSuggestions();
    return;
  }

  // Автоскролл вниз — только если человек уже был у низа. Иначе финальная
  // перерисовка после стрима сбивала бы того, кто отлистал вверх читать.
  const atBottom =
    thread.scrollHeight - thread.scrollTop - thread.clientHeight < 150;
  thread.innerHTML = messages.map(messageHTML).join("");
  wireMessageActions();
  applyCollapsible();
  if (atBottom) thread.scrollTop = thread.scrollHeight;
}

//: Выше этой высоты (px) ответ модели сворачивается: показываем начало под
//: затуханием и кнопку «Раскрыть». Так глубокое исследование на страницу
//: текста не заваливает ленту — как это делают и сами иишки.
const COLLAPSE_MAX = 460;

function applyCollapsible() {
  const msgs = state.current ? state.current.messages : [];
  $("thread").querySelectorAll(".answer-body[data-idx]").forEach((body) => {
    const idx = parseInt(body.dataset.idx, 10);
    const m = msgs[idx];
    // Складываем только готовые текстовые ответы модели: не реплики
    // пользователя, не ошибки, не картинки/озвучку, и не то, что сейчас
    // стримится (последнее при busy — пусть допишется).
    if (!m || m.role === "user" || m.error || m.isImage || m.audio) return;
    if (state.busy && idx === msgs.length - 1) return;

    const tall = body.scrollHeight > COLLAPSE_MAX + 90;
    let btn = body.parentElement.querySelector(":scope > .msg-expand");
    if (!tall) {                     // короткий — снять всё, если было
      body.classList.remove("collapsible", "expanded");
      if (btn) btn.remove();
      return;
    }
    const expanded = !!m.expanded;
    body.classList.toggle("collapsible", !expanded);
    body.classList.toggle("expanded", expanded);
    if (!btn) {
      btn = document.createElement("button");
      btn.className = "msg-expand";
      body.after(btn);
      btn.addEventListener("click", () => {
        m.expanded = !m.expanded;
        applyCollapsible();          // обновить состояние без перерисовки
      });
    }
    btn.textContent = expanded ? t("Свернуть") : t("Раскрыть полностью");
    btn.classList.toggle("is-open", expanded);
  });
}

//: Иконки режима для реплик пользователя (картинка/озвучка) — вместо
//: эмодзи, которые в шрифте интерфейса выглядят крякозяброй.
const MODE_ICONS = {
  image: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
  speech: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>',
};

function messageHTML(message, index) {
  // Свёрнутый прошлый вариант ответа — тонкая строка, клик разворачивает.
  if (message.collapsed) {
    const who = message.who || t("вариант");
    return `<div class="msg variant-collapsed" data-idx="${index}">
      <button class="variant-toggle" data-expand="${index}">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg>
        ${t("Прошлый вариант")} · ${escapeHTML(who)}
      </button>
    </div>`;
  }

  const isUser = message.role === "user";
  const role = isUser ? t("Вы") : (message.who || t("Ассистент"));

  // Исчерпанный лимит — своя плашка с часами, не красная ошибка.
  if (message.rateLimited) {
    const clock = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
    return `<div class="msg">
      <div class="msg-role">${escapeHTML(role)}</div>
      <div class="msg-limit">${clock}<span>${escapeHTML(message.content)}</span></div>
    </div>`;
  }

  const cls = message.error ? "msg error" : (isUser ? "msg user" : "msg");
  const timer = message.elapsed
    ? `<span class="msg-timer">${message.elapsed}с</span>` : "";
  let body = "";
  // Часть моделей (gpt-oss на Groq) кладёт в поле рассуждения сам ответ.
  // Тогда блок размышлений — это второй экземпляр того же текста, и
  // показывать его незачем.
  const think = (message.thinking || "").trim();
  const answer = (message.content || "").trim();
  if (think && think !== answer) {
    body += '<details class="thinking">'
          + '<summary>' + t("Размышления") + '</summary>'
          + '<div class="thinking-body">'
          + escapeHTML(message.thinking)
          + '</div></details>';
  }
  // Озвучка: свой плеер вместо нативного — звуковая дорожка, круглая
  // кнопка, время. Высоты полосок детерминированы из индекса, чтобы не
  // прыгали при перерисовке.
  if (message.audio) {
    const bars = Array.from({ length: 48 }, (_, i) => {
      const h = 30 + Math.abs(Math.sin(i * 0.9) * 45)
                   + (i * 13 % 25);
      return `<span style="height:${Math.min(92, Math.round(h))}%"></span>`;
    }).join("");
    // Без переносов/отступов вокруг: msg-body в pre-wrap, и пробельные
    // текстовые узлы превратились бы в пустую строку над плеером.
    body += `<div class="player">` +
      `<button class="player-play" aria-label="${t("Играть")}">` +
      `<svg class="ic-play" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15a1 1 0 001.5.87l12-7.5a1 1 0 000-1.74l-12-7.5A1 1 0 007 4.5z"/></svg>` +
      `<svg class="ic-pause" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4.5" width="4" height="15" rx="1.3"/><rect x="14" y="4.5" width="4" height="15" rx="1.3"/></svg>` +
      `</button>` +
      `<div class="player-wave">` +
      `<div class="wave-row wave-bg">${bars}</div>` +
      `<div class="wave-row wave-fg">${bars}</div>` +
      `</div>` +
      `<span class="player-time">0:00</span>` +
      `<audio preload="metadata" src="${message.audio}"></audio>` +
      `</div>`;
  }
  // Реплика пользователя из режима картинки/озвучки — иконка вместо
  // эмодзи (эмодзи рендерился крякозяброй в шрифте интерфейса).
  // Прикреплённые файлы — показываем в СВОЁМ сообщении: миниатюра у картинки,
  // плашка с именем у файла. Иначе непонятно, что именно ушло провайдеру.
  if (message.attachments && message.attachments.length) {
    body += '<div class="msg-attachments">' + message.attachments.map((a) =>
      (a.kind === "image" && a.url)
        ? `<img class="msg-attach-img" src="${a.url}" alt="${escapeHTML(a.name || "")}" title="${escapeHTML(a.name || "")}">`
        : `<span class="msg-attach-file">📎 ${escapeHTML(a.name || t("файл"))}</span>`
    ).join("") + "</div>";
  }
  if (isUser && message.mode && MODE_ICONS[message.mode]) {
    body += `<div class="answer-body" data-idx="${index}"><span class="msg-mode-ic">` +
      `${MODE_ICONS[message.mode]}</span>${formatBody(message.content)}</div>`;
  } else {
    body += '<div class="answer-body" data-idx="' + index + '">' +
      formatBody(message.content) + '</div>';
  }

  // Действия под готовым ответом модели (не у пользователя, не у ошибки,
  // не во время генерации). У картинки и озвучки действий нет — там плеер
  // или картинка самодостаточны.
  let actions = "";
  if (!isUser && !message.error && answer && !state.busy
      && !message.isImage && !message.audio) {
    const icCopy = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
    const icRegen ='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v5h-5"/></svg>';
    const icHide = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m18 15-6-6-6 6"/></svg>';

    const msgs = state.current ? state.current.messages : [];
    // «Заново» — одноразово и только у актуального последнего ответа:
    // не у прошлого варианта и не если перегенерация уже была (перед этим
    // ответом стоит свёрнутый вариант).
    const prev = msgs[index - 1];
    const isLast = index === msgs.length - 1;
    // У озвучки «Заново другим» не нужно — это не ответ модели, а синтез
    // речи, и перегенерация ушла бы в текстовый чат.
    const canRegen = isLast && !message.isVariant && !message.audio
      && !(prev && prev.collapsed);

    actions = `<div class="msg-actions" data-idx="${index}">
      <button class="msg-act" data-act="copy">${icCopy}<span>${t("Копировать")}</span></button>` +
      (canRegen ? `<button class="msg-act" data-act="regen">${icRegen}<span>${t("Заново другим")}</span></button>` : "") +
      // Развёрнутый прошлый вариант можно свернуть обратно.
      (message.isVariant ? `<button class="msg-act" data-act="hide">${icHide}<span>${t("Свернуть")}</span></button>` : "") +
    `</div>`;
  }

  return `
    <div class="${cls}">
      <div class="msg-role">${escapeHTML(role)}${timer}</div>
      <div class="msg-body">${body}</div>
      ${actions}
    </div>`;
}

/* ── Действия под ответом ───────────────────────────────────────── */

function wireMessageActions() {
  document.querySelectorAll(".msg-actions").forEach((row) => {
    const idx = parseInt(row.dataset.idx, 10);
    row.querySelectorAll(".msg-act").forEach((btn) => {
      btn.addEventListener("click", () => onMessageAction(btn.dataset.act, idx, btn));
    });
  });
  // Развернуть свёрнутый прошлый вариант обратно.
  document.querySelectorAll(".variant-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = parseInt(btn.dataset.expand, 10);
      const m = state.current?.messages?.[i];
      if (m) { m.collapsed = false; saveThreads(); renderMessages(); }
    });
  });
}

async function onMessageAction(act, idx, btn) {
  const msg = state.current?.messages?.[idx];
  if (!msg) return;

  if (act === "copy") {
    const text = msg.content || "";
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      // clipboard API недоступен (не HTTPS) — старый способ через textarea.
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand("copy");
        ta.remove();
      } catch { ok = false; }
    }
    const was = t("Копировать");
    btn.textContent = ok ? t("Скопировано") : t("Не вышло");
    if (ok) btn.classList.add("done");
    setTimeout(() => { btn.textContent = was; btn.classList.remove("done"); }, 1400);
    return;
  }

  if (act === "hide") {
    // Свернуть развёрнутый прошлый вариант обратно.
    msg.collapsed = true;
    saveThreads();
    renderMessages();
    return;
  }

  if (act === "regen") {
    // «Ответить другим»: старый вариант не удаляем, а сворачиваем, и просим
    // маршрут дать ДРУГОГО провайдера — исключаем того, кто уже отвечал.
    if (state.busy) return;
    let userText = "";
    for (let i = idx - 1; i >= 0; i--) {
      if (state.current.messages[i].role === "user") {
        userText = state.current.messages[i].content;
        break;
      }
    }
    if (!userText) return;

    // regen идёт через «auto» мимо submit — тот же предохранитель, но здесь:
    // проверяем, есть ли кому ответить, ДО сворачивания варианта, иначе
    // «некому» встанет ошибкой в пузырь вместо дружелюбного тоста.
    const savedModel = state.model;
    state.model = "auto";
    const blocked = chatBlocker();
    state.model = savedModel;
    if (blocked) { toast(blocked); return; }

    // Собираем всех, кто уже отвечал на этот вопрос (сам ответ + прошлые
    // свёрнутые варианты подряд), чтобы не выпал тот же самый.
    const exclude = [];
    for (let i = idx; i >= 0; i--) {
      const m = state.current.messages[i];
      if (m.role !== "assistant") break;
      if (m.providerId) exclude.push(m.providerId);
    }
    msg.collapsed = true;         // прячем текущий вариант
    msg.isVariant = true;         // помечаем как прошлый вариант ответа
    saveThreads();
    // Перегенерация идёт через «auto» (маршрут выберет другого).
    const prevModel = state.model;
    state.model = "auto";
    renderMessages();
    send(userText, { reuseUser: true, exclude }).finally(() => {
      state.model = prevModel;
    });
  }
}

/* ── Подсказки на пустом экране ─────────────────────────────────── */

//: Иконки по темам. Ключ — название набора, значение — путь в SVG.
const SUG_ICONS = {
  text:  '<path d="M4 7h16M4 12h16M4 17h10"/>',
  code:  '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
  brain: '<path d="M12 2a7 7 0 017 7c0 2.4-1.2 4.5-3 5.7V17a2 2 0 01-2 2h-4a2 2 0 01-2-2v-2.3C6.2 13.5 5 11.4 5 9a7 7 0 017-7z"/><path d="M9 22h6"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  chart: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 010 20 15 15 0 010-20"/>',
  book:  '<path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/>',
  spark: '<path d="M12 2l2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4z"/>',
};

//: Пул подсказок. Показываем четыре случайные — одни и те же на каждом
//: пустом экране приедаются, а разные подталкивают попробовать новое.
const SUGGESTIONS = [
  ["text",  "purple", "Объясни, зачем нужен индекс в базе данных", "Простыми словами"],
  ["code",  "green",  "Напиши функцию на Python для разбора CSV", "С примерами"],
  ["brain", "pink",   "Чем отличается процесс от потока", "Примеры и сравнение"],
  ["image", "amber",  "Нарисуй кота в скандинавском стиле", "На Луне 🌙"],
  ["code",  "green",  "Покажи, как работает бинарный поиск", "С разбором шагов"],
  ["text",  "purple", "Придумай название для кофейни у моря", "Десять вариантов"],
  ["brain", "pink",   "Объясни квантовую запутанность", "Без формул"],
  ["image", "amber",  "Нарисуй город будущего на закате", "Неоновые огни"],
  ["chart", "green",  "Как читать график котировок", "Для начинающего"],
  ["globe", "purple", "Составь маршрут по Японии на неделю", "Бюджетный"],
  ["book",  "pink",   "Посоветуй книги как «Дюна»", "И почему именно эти"],
  ["spark", "amber",  "Придумай сюжет для короткого рассказа", "Неожиданный финал"],
  ["code",  "green",  "Отладь: почему рекурсия падает в переполнение", "Разбор причин"],
  ["text",  "purple", "Напиши письмо с отказом от встречи", "Вежливо и коротко"],
  ["brain", "pink",   "Что такое энтропия на бытовых примерах", "Понятно"],
  ["image", "amber",  "Нарисуй лису-космонавта", "В стиле акварели"],
];

function pickSuggestions(count = 4) {
  // Тасуем копию: править исходный пул незачем.
  const pool = SUGGESTIONS.slice();
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  return pool.slice(0, count);
}

function suggestionsHTML() {
  return pickSuggestions().map(([icon, tone, title, note]) => `
    <button class="sug-card">
      <span class="sug-icon sug-${tone}">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.5">${SUG_ICONS[icon]}</svg>
      </span>
      <span class="sug-text">
        <strong>${escapeHTML(t(title))}</strong>
        <small>${escapeHTML(t(note))}</small>
      </span>
    </button>`).join("");
}

function emptyHTML() {
  return `
    <div class="empty" id="empty">
      <svg class="hero-tail" viewBox="0 0 200 120" fill="none" aria-hidden="true"><path d="M10 114C10 62 44 20 100 8c42-9 78 8 96 38-30-12-58-6-80 12-24 20-40 38-72 52-12 5-34 8-34 4Z" fill="#FF8A3D"/></svg>
      <svg class="hero-fox" width="58" height="58" viewBox="0 0 32 32" fill="none"><path d="M2.8 3.2 12.4 9.6h7.2L29.2 3.2v13.6c0 6.9-5.9 12.4-13.2 12.4S2.8 23.7 2.8 16.8Z" fill="#FF8A3D"/><path d="M9.6 14.6 13.8 16.4 9.6 18.2Z" fill="#191210"/><path d="M22.4 14.6 18.2 16.4 22.4 18.2Z" fill="#191210"/><path d="M12.6 21h6.8L16 27.2Z" fill="#FFE9D6"/><circle cx="16" cy="22.4" r="1.4" fill="#191210"/></svg>
      <h1>${t("О чём")} <span class="gradient-text">${t("поговорим?")}</span></h1>
      <p>${t("Хитрая лисичка сама выберет живую модель, обойдёт лимиты и озвучит ответ. Ты просто спрашивай.")}</p>
      <div class="suggestions" id="suggestions">${suggestionsHTML()}</div>
    </div>`;
}

function wireSuggestions() {
  document.querySelectorAll("#suggestions .sug-card").forEach((node) => {
    node.addEventListener("click", () => {
      const title = node.querySelector("strong");
      if (!title) return;
      $("input").value = title.textContent.trim();
      $("composer").requestSubmit();
    });
  });
}

/* ── Печатная машинка ───────────────────────────────────────────── */

/** Плавная выдача текста независимо от того, как его отдал провайдер.
 *
 * Половина веб-сессий не стримит вовсе: ответ приходит одним куском, и без
 * этой прослойки он возникал бы на экране целиком. Остальные шлют куски
 * рывками — по фразе, по абзацу. И то и другое читается как поломка.
 *
 * Поэтому пришедший текст копится в `full`, а на экран попадает по мере
 * отрисовки кадров. Скорость привязана к отставанию: чем длиннее хвост,
 * тем быстрее догоняем, иначе ответ на три экрана печатался бы минуту.
 */
class Typewriter {
  constructor(paint) {
    this.full = "";
    this.shown = 0;
    this.paint = paint;
    this.running = false;
    this.ended = false;
    this.settle = null;
    this.last = 0;
  }

  push(text) {
    this.full = text;
    this._run();
  }

  /** Досказать остаток и дождаться, пока он окажется на экране. */
  finish() {
    this.ended = true;
    if (this.shown >= this.full.length) return Promise.resolve();
    return new Promise((resolve) => {
      this.settle = resolve;
      this._run();
    });
  }

  _run() {
    if (this.running) return;
    this.running = true;
    this.last = performance.now();
    this._tick(this.last);
  }

  _tick(now) {
    const dt = Math.min(now - this.last, 100);  // вкладку сворачивали — не рвём
    this.last = now;
    const left = this.full.length - this.shown;

    if (left > 0) {
      // В скрытой вкладке анимировать не для кого — дорисовываем разом.
      // Иначе ответ так и останется недописанным: пока вкладка не на
      // экране, кадров нет, а значит нет и продолжения.
      let step;
      if (document.hidden) {
        step = left;
      } else {
        // База — скорость беглого чтения. Хвост длиннее пары строк
        // ускоряет: иначе непотоковый провайдер, отдавший ответ целиком,
        // печатался бы минуту.
        const perSec = Math.max(180, left * 4);
        step = Math.max(1, Math.round(perSec * dt / 1000));
      }
      this.shown = Math.min(this.full.length, this.shown + step);
      try {
        this.paint(this.full.slice(0, this.shown));
      } catch {
        // Разметка не должна ронять цикл: иначе ожидающий finish()
        // никогда не дождётся и поле ввода останется заблокированным.
      }
    }

    if (this.shown < this.full.length) {
      this._schedule();
      return;
    }

    this.running = false;
    if (this.ended && this.settle) { this.settle(); this.settle = null; }
  }

  _schedule() {
    // requestAnimationFrame в скрытой вкладке не вызывается вовсе,
    // поэтому там переходим на таймер: он продолжает тикать.
    if (document.hidden) {
      setTimeout(() => this._tick(performance.now()), 16);
    } else {
      requestAnimationFrame((now) => this._tick(now));
    }
  }
}

/* ── Отправка ───────────────────────────────────────────────────── */

async function send(text, opts = {}) {
  if (!state.current) newThread();
  const thread = state.current;

  // reuseUser — перегенерация: вопрос уже в ленте, второй раз не добавляем.
  if (!opts.reuseUser) {
    // Превью вложений (уменьшенные) — показать в СВОЁМ сообщении то, что
    // прикрепил. Полные файлы уходят провайдеру отдельно (drainAttachments).
    const shown = await attachmentPreviews();
    thread.messages.push({ role: "user", content: text,
                           attachments: shown.length ? shown : undefined });
    if (!thread.title) {
      thread.title = text.slice(0, 40) + (text.length > 40 ? "…" : "");
    }
  }
  saveThreads();
  renderThreads();
  renderMessages();

  // Имя подставляем сразу: при выбранной модели оно известно заранее, и
  // ждать первого куска, чтобы подписать ответ, незачем. Для «Авто» его
  // перезапишет сервер, когда маршрутизатор определится с выбором.
  const reply = {
    role: "assistant",
    content: "",
    thinking: "",
    who: state.model === "auto" ? "" : providerLabel(state.model),
    providerId: state.model === "auto" ? "" : state.model.split("/")[0],
  };
  thread.messages.push(reply);
  renderMessages();

  const node = document.querySelector(".thread .msg:last-child .msg-body");
  // Пока ответа нет — пульсирующие точки «лисичка думает», не голый курсор.
  if (node) node.innerHTML =
    '<span class="dots"><i></i><i></i><i></i></span>';

  // Машинка пишет в свой div и подкручивает ленту, пока человек у низа:
  // если он отлистал вверх читать, дёргать его обратно нельзя.
  const typer = new Typewriter((visible) => {
    if (!node) return;
    // Первый кусок пришёл — убираем индикатор ожидания.
    const dots = node.querySelector(".dots");
    if (dots) dots.remove();
    let answer = node.querySelector(".answer-body");
    if (!answer) {
      answer = document.createElement("div");
      answer.className = "answer-body";
      const caret = node.querySelector(".caret");
      node.insertBefore(answer, caret || null);
    }
    answer.innerHTML = formatBody(visible);
    const feed = $("thread");
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
    if (atBottom) feed.scrollTop = feed.scrollHeight;
  });

  state.busy = true;
  $("send").disabled = true;
  const t0 = Date.now();
  // Счётчик идёт в подписи самого сообщения: смотреть в угол экрана,
  // пока читаешь ответ, неудобно.
  const head = document.querySelector(".thread .msg:last-child .msg-role");
  const timer = setInterval(() => {
    if (!head) return;
    const sec = ((Date.now() - t0) / 1000).toFixed(1);
    let badge = head.querySelector(".msg-timer");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "msg-timer";
      head.appendChild(badge);
    }
    badge.textContent = `${sec}с`;
  }, 100);

  try {
    // Историю шлём целиком: веб-сессии одноразовые, своей памяти у них нет.
    // Свёрнутые прошлые варианты в контекст не берём — это альтернативы
    // одного ответа, а не ход диалога.
    const history = thread.messages
      .filter((m) => m.content && !m.error && !m.collapsed && m !== reply)
      .map((m) => ({ role: m.role, content: m.content }));

    // Вложения СНИМАЕМ, но плашки убираем только после удачной отправки:
    // раньше очистка шла до запроса, и любая ошибка (сеть, отказ
    // провайдера) уносила выбранные файлы безвозвратно — человеку
    // приходилось искать и прикреплять их заново.
    const attachments = await drainAttachments({ keepChips: true });

    // Серверная беседа: если тред уже ведётся у того же выбранного
    // провайдера, шлём её ручку — сервер отправит только новое сообщение,
    // а контекст удержит провайдер. При «Авто» бесед нет (чат чужой).
    const provId = state.model === "auto" ? null : state.model.split("/")[0];
    let conv;
    if (provId && thread.conv && thread.conv.provider === provId) {
      conv = { chat_id: thread.conv.chatId,
               last_message_id: thread.conv.lastId };
    }

    const response = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: state.model,
        messages: history,
        stream: true,
        thinking: state.thinking,
        web_search: state.webSearch,
        deep_research: state.deepResearch,
        attachments: attachments.length ? attachments : undefined,
        exclude: opts.exclude && opts.exclude.length ? opts.exclude : undefined,
        conversation: conv,
      }),
    });

    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail?.error?.message || `HTTP ${response.status}`);
    }

    // Запрос принят — вот теперь плашки вложений можно убрать.
    dropAttachments();

    await readStream(response, reply, node, typer, thread);
    // Поток кончился, но хвост может быть ещё не напечатан: дожидаемся,
    // иначе финальная перерисовка оборвёт машинку на середине фразы.
    await typer.finish();
  } catch (error) {
    // Уже пришедший текст НЕ затираем. Раньше строка ошибки вставала на
    // его место, и обрыв на середине длинного ответа терял всё, что
    // успело прийти, — а заодно выбрасывал ход из контекста, потому что
    // сообщения с error в историю не идут.
    const note = String(error.message || error);
    if (reply.content) {
      reply.content += `

_[${t("ответ оборвался")}: ${note}]_`;
    } else {
      reply.error = true;
      reply.content = note;
    }
  }

  clearInterval(timer);
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  reply.elapsed = elapsed;
  state.busy = false;
  $("send").disabled = false;
  $("who").textContent = "";
  if (!reply.error && !reply.rateLimited) bumpCounter(reply.who || state.model);
  saveThreads();
  renderMessages();
  loadProviders();
}

async function readStream(response, reply, node, typer, thread) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6).trim();
      if (!payload) continue;
      // Выходим по [DONE], а не ждём закрытия соединения: так делают все
      // клиенты OpenAI, и это спасает, если сервер соединение придержит.
      if (payload === "[DONE]") return;

      let event;
      try { event = JSON.parse(payload); } catch { continue; }

      // Ручка серверной беседы — запоминаем В СВОЁМ треде, а не в том,
      // что открыт сейчас: человек мог переключить чат, пока шёл ответ,
      // и тогда ручка уезжала в чужой разговор.
      //
      // И не пропускаем кусок: сервер кладёт метку ПОЛЕМ в обычный кусок
      // (отдельный кадр без choices ломал строгих клиентов), так что в
      // нём может быть и текст.
      if (event.conversation && event.conversation.chat_id) {
        const c = event.conversation;
        const owner = thread || state.current;
        if (owner) {
          owner.conv = {
            provider: c.provider,
            chatId: c.chat_id,
            lastId: c.last_message_id,
          };
        }
      }

      if (event.error) {
        // Исчерпанный лимит — не «ошибка», а ожидаемое состояние: своя
        // плашка со временем сброса, а не красный текст отказа.
        if (event.error.type === "rate_limited") {
          reply.rateLimited = true;
          reply.content = event.error.message || t("лимит исчерпан");
          reply.retryAfter = event.error.retry_after || null;
        } else {
          reply.error = true;
          reply.content = event.error.message || t("отказ провайдера");
        }
        return;
      }
      // При «auto» сервер кладёт в model, кто в итоге ответил: при фолбэке
      // это уже не тот, кого просили, и человеку это важно видеть.
      if (event.model && event.model.includes(":")) {
        // id провайдера — для «ответить другим»: его исключаем из повтора.
        reply.providerId = event.model.split(":")[1];
        const chosen = providerLabel(reply.providerId);
        if (chosen !== reply.who) {
          reply.who = chosen;
          // Подпись уже нарисована — обновляем на месте, иначе имя
          // появится только после конца ответа. Правим только первый
          // узел: рядом тикает таймер, и затирать его нельзя.
          // Берём подпись ОТ захваченного node этого ответа, а НЕ живым
          // querySelector по видимому .thread: если человек переключил чат
          // посреди потока, имя провайдера уехало бы в последнее сообщение
          // ЧУЖОГО чата.
          const head = node.closest(".msg")?.querySelector(".msg-role");
          if (head) {
            const label = head.firstChild;
            if (label && label.nodeType === Node.TEXT_NODE) {
              label.nodeValue = chosen;
            } else {
              head.insertBefore(document.createTextNode(chosen),
                                head.firstChild);
            }
          }
        }
      }
      const delta = event.choices?.[0]?.delta || {};
      const thinkPiece = delta.thinking;
      const textPiece = delta.content;

      if (thinkPiece) {
        reply.thinking = (reply.thinking || "") + thinkPiece;
      }
      if (textPiece) {
        reply.content += textPiece;
      }

      if ((thinkPiece || textPiece) && node) {
        // DOM, а не innerHTML: пересборка строки на каждый кусок ломает
        // details/summary, потому что браузер разбирает незакрытый тег
        // и вытаскивает атрибуты как текст.
        if (reply.thinking) {
          let details = node.querySelector(".thinking");
          if (!details) {
            node.innerHTML = "";
            details = document.createElement("details");
            details.className = "thinking";
            details.setAttribute("open", "");
            const summary = document.createElement("summary");
            summary.textContent = t("Размышления");
            details.appendChild(summary);
            const body = document.createElement("div");
            body.className = "thinking-body";
            details.appendChild(body);
            node.appendChild(details);
          }
          details.querySelector(".thinking-body").textContent = reply.thinking;
        }
        // Текст ответа отдаём машинке: она сама разложит его по кадрам.
        if (textPiece) typer.push(reply.content);
        if (!node.querySelector(".caret")) {
          const caret = document.createElement("span");
          caret.className = "caret";
          node.appendChild(caret);
        }
      }
    }
  }
}

/* ── Генерация картинки ─────────────────────────────────────────── */

async function sendImage(prompt) {
  if (!state.current) newThread();
  const thread = state.current;

  thread.messages.push({ role: "user", content: prompt, mode: "image" });
  if (!thread.title) thread.title = prompt.slice(0, 38);
  saveThreads();
  renderThreads();
  renderMessages();

  const drawer = state.drawModel || "auto";
  const drawerLabel = drawer === "auto"
    ? t("Картинка") : `${t("Картинка")} · ${providerLabel(drawer)}`;
  const reply = { role: "assistant", content: "", who: drawerLabel, isImage: true };
  thread.messages.push(reply);
  renderMessages();

  state.busy = true;
  $("send").disabled = true;
  const t0 = Date.now();
  const imgHead = document.querySelector(".thread .msg:last-child .msg-role");
  const timer = setInterval(() => {
    if (!imgHead) return;
    let badge = imgHead.querySelector(".msg-timer");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "msg-timer";
      imgHead.appendChild(badge);
    }
    badge.textContent = `${((Date.now() - t0) / 1000).toFixed(1)}с`;
  }, 100);

  try {
    const response = await fetch("/v1/images/generations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, model: drawer }),
    });
    const data = await response.json();
    if (data.error) throw new Error(data.error.message);
    const urls = (data.data || []).map((d) => d.url);
    // Сервис часто присылает картинку с комментарием («вот милая
    // лисичка, хотите мультяшнее?»). Он приходит в revised_prompt —
    // стандартном поле Images API — и место ему под картинкой.
    const caption = (data.data || [])
      .map((d) => d.revised_prompt).filter(Boolean)[0] || "";
    reply.content = urls.map((url) =>
      `![${t("картинка")}](${url})`
    ).join("\n\n") + (caption ? "\n\n" + caption : "");
    if (!reply.content) throw new Error(t("картинки не получены"));
    bumpCounter("Картинка");
  } catch (error) {
    // Уже пришедший текст НЕ затираем. Раньше строка ошибки вставала на
    // его место, и обрыв на середине длинного ответа терял всё, что
    // успело прийти, — а заодно выбрасывал ход из контекста, потому что
    // сообщения с error в историю не идут.
    const note = String(error.message || error);
    if (reply.content) {
      reply.content += `

_[${t("ответ оборвался")}: ${note}]_`;
    } else {
      reply.error = true;
      reply.content = note;
    }
  }

  clearInterval(timer);
  reply.elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  state.busy = false;
  $("send").disabled = false;
  $("who").textContent = "";
  saveThreads();
  renderMessages();
}

/* ── Мелочи ─────────────────────────────────────────────────────── */

function escapeHTML(text) {
  // Через textContent кавычки НЕ экранируются: браузеру они внутри текста
  // не мешают. А у нас результат подставляется и в АТРИБУТЫ — там кавычка
  // закрывает значение, и дальше можно дописать что угодно, вплоть до
  // обработчика события. Текст к нам приходит от модели, а модель могла
  // взять его из веб-поиска, то есть с чужой страницы. Поэтому кавычки
  // экранируем сами.
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Подсветка синтаксиса — свой лёгкий токенайзер, без библиотек (их
// заблокировал бы CSP). Один проход с приоритетом: комментарий, строка,
// число, ключевое слово, встроенное имя.
const CODE_KW = "import|from|as|def|class|return|if|elif|else|for|while|" +
  "in|not|and|or|is|None|True|False|try|except|finally|raise|with|lambda|" +
  "yield|async|await|pass|break|continue|global|nonlocal|del|assert|" +
  "const|let|var|function|new|typeof|export|default|extends|super|this|" +
  "null|undefined|void|public|private|protected|static|interface|enum|" +
  "type|namespace|of|instanceof|throw|switch|case|do";
const CODE_BUILTIN = "print|len|range|str|int|float|list|dict|set|tuple|" +
  "bool|self|open|isinstance|Enum|Protocol|ABC|abstractmethod|dataclass|" +
  "auto|property|staticmethod|classmethod|console|log|require|module|" +
  "Math|Object|Array|String|Number|Boolean|JSON|Promise|map|filter|" +
  "reduce|forEach|length|append|format";
const CODE_RE = new RegExp(
  "(#[^\\n]*|\\/\\/[^\\n]*)" +
  "|(\"[^\"\\n]*\"|'[^'\\n]*'|`[^`]*`)" +
  "|(\\b\\d+\\.?\\d*\\b)" +
  "|(\\b(?:" + CODE_KW + ")\\b)" +
  "|(\\b(?:" + CODE_BUILTIN + ")\\b)",
  "g");
function highlightCode(escaped) {
  return escaped.replace(CODE_RE, (m, com, str, num, kw, fn) => {
    if (com) return `<span class="tok-com">${com}</span>`;
    if (str) return `<span class="tok-str">${str}</span>`;
    if (num) return `<span class="tok-num">${num}</span>`;
    if (kw) return `<span class="tok-kw">${kw}</span>`;
    if (fn) return `<span class="tok-fn">${fn}</span>`;
    return m;
  });
}
//: Блок кода с шапкой (язык + кнопка копии) и подсветкой. open=true —
//: ещё стримится (нет закрывающих ```), у него курсор и нет кнопки.
function codeBlock(code, lang, open) {
  const clean = code.replace(/\n$/, "");
  const body = highlightCode(clean);
  const label = (lang || "code").toLowerCase();
  const copy = open ? "" :
    `<button class="code-copy" title="${t("Скопировать код")}">` +
    `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>` +
    `<span>${t("Копировать")}</span></button>`;
  const caret = open ? '<span class="caret"></span>' : "";
  return `<div class="code-block"><div class="code-head">` +
    `<span class="code-lang">${escapeHTML(label)}</span>${copy}</div>` +
    `<pre><code>${body}${caret}</code></pre></div>`;
}

function formatBody(text) {
  // Свой разбор вместо библиотеки: нужна ровно та разметка, которой
  // пользуются модели, и без неё ответ читается как мусор со звёздочками.
  //
  // Порядок важен. Сначала экранируем — иначе модель, ответившая тегом,
  // вставит его в страницу. Потом вырезаем блоки кода в заглушки, чтобы
  // разметка внутри них не трогалась: звёздочки в коде это звёздочки.
  const blocks = [];
  let safe = escapeHTML(text);

  // Закрытые блоки кода — с подсветкой и кнопкой копирования.
  safe = safe.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(codeBlock(code, lang, false));
    return ` ${blocks.length - 1} `;
  });
  // Незакрытый блок в конце — код ещё стримится. Оформляем сразу как
  // код-блок, чтобы подсветка не появлялась лишь в самом конце.
  safe = safe.replace(/```(\w*)\n?([\s\S]*)$/, (_, lang, code) => {
    blocks.push(codeBlock(code, lang, true));
    return ` ${blocks.length - 1} `;
  });
  safe = safe.replace(/`([^`\n]+)`/g, (_, code) => {
    blocks.push(`<code>${code}</code>`);
    return ` ${blocks.length - 1} `;
  });

  // Заголовки, списки и цитаты — построчно.
  const lines = safe.split("\n");
  const out = [];
  let list = null;  // "ul" | "ol" | null

  const closeList = () => {
    if (list) { out.push(`</${list}>`); list = null; }
  };

  // Ячейки строки таблицы. Внешние черты необязательны, экранированная
  // «\|» — это символ, а не разделитель.
  const cells = (line) => {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  };

  // Разделитель шапки: |---|:--:|---:| и любые его вариации.
  const isSeparator = (line) => {
    if (!line || !line.includes("|") || !line.includes("-")) return false;
    const parts = cells(line);
    return parts.length > 1 && parts.every((c) => /^:?-+:?$/.test(c));
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Таблица: строка с чертой, а под ней разделитель. Модели отвечают
    // таблицами постоянно, и без разбора они высыпались в чат сырыми
    // строками с палками.
    if (line.includes("|") && isSeparator(lines[i + 1])) {
      closeList();
      const head = cells(line);
      const align = cells(lines[i + 1]).map((c) => {
        const left = c.startsWith(":"), right = c.endsWith(":");
        if (left && right) return "center";
        if (right) return "right";
        return "";
      });

      const body = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        body.push(cells(lines[i]));
        i++;
      }
      i--;  // последнюю несъеденную строку вернём внешнему циклу

      const at = (n) => (align[n] ? ` style="text-align:${align[n]}"` : "");
      const th = head.map((c, n) => `<th${at(n)}>${inline(c)}</th>`).join("");
      // Ряды равняем по шапке: модели нередко теряют ячейку в хвосте.
      const rows = body.map((row) =>
        "<tr>" + head.map((_, n) =>
          `<td${at(n)}>${inline(row[n] || "")}</td>`).join("") + "</tr>"
      ).join("");

      out.push('<div class="table-wrap"><table>'
             + `<thead><tr>${th}</tr></thead>`
             + (rows ? `<tbody>${rows}</tbody>` : "")
             + "</table></div>");
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const quote = line.match(/^&gt;\s?(.*)$/);

    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
    } else if (bullet) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(bullet[1])}</li>`);
    } else if (numbered) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(numbered[1])}</li>`);
    } else if (quote) {
      closeList();
      out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
    } else {
      closeList();
      out.push(inline(line));
    }
  }
  closeList();

  safe = out.join("\n");
  return safe.replace(/ (\d+) /g, (_, index) => blocks[index]);
}

function inline(text) {
  return text
    // Картинки: ![alt](url). Кроме обычных ссылок принимаем data: —
    // ChatGPT отдаёт файл байтами, потому что его собственная ссылка
    // живёт только с авторизацией и браузеру человека вернёт 403.
    .replace(/!\[([^\]]*)\]\(((?:https?:\/\/|data:image\/)[^\s)]+)\)/g,
             '<img src="$2" alt="$1" class="msg-image" loading="lazy">')
    // Жирный до курсива
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    // ChatGPT ссылается на собранный файл адресом sandbox:/mnt/data/… —
    // это не адрес, а путь внутри их песочницы, никуда не ведущий. Сам
    // файл приезжает ниже отдельной плашкой; здесь оставляем только имя,
    // иначе в тексте торчат сырые скобки разметки. Чистить на стороне
    // сервера нельзя: ответ идёт накопленным, и вырезание посреди потока
    // сбивает подсчёт приращений — текст задваивается.
    .replace(/\[([^\]]+)\]\(sandbox:[^\s)]*\)/g, "$1")
    // Файлы, собранные моделью: приезжают байтами по той же причине, что и
    // картинки. Нужен `download` — без него браузер по data: не сохраняет
    // файл, а пытается его открыть, и таблица уходит в пустую вкладку.
    .replace(/\[([^\]]+)\]\((data:[^\s)]+)\)/g,
             '<a class="msg-file" href="$2" download="$1">📎 $1</a>')
    // Ссылки
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function plural(count, forms) {
  const tail = count % 10;
  const tens = count % 100;
  if (tail === 1 && tens !== 11) return forms[0];
  if (tail >= 2 && tail <= 4 && !(tens >= 12 && tens <= 14)) return forms[1];
  return forms[2];
}

/* ── Запуск ─────────────────────────────────────────────────────── */

$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});

$("input").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = Math.min(event.target.scrollHeight, 180) + "px";
});

/* ── Кнопки-тогглы ─────────────────────────────────────────────── */

// Заблокированное умение (умеет только незалогиненный) не включаем — иначе
// запрос уйдёт с флагом, который выполнить некому. Говорим, что делать.
function abilityBlocked(el) {
  if (el.classList.contains("ability-off")) {
    toast(el.title || t("Нет залогиненного провайдера с этим умением — заведи доступ"));
    return true;
  }
  return false;
}

$("btn-web")?.addEventListener("click", () => {
  if (abilityBlocked($("btn-web"))) return;
  state.webSearch = !state.webSearch;
  $("btn-web").classList.toggle("active", state.webSearch);
});

$("btn-think").addEventListener("click", () => {
  if (abilityBlocked($("btn-think"))) return;
  state.thinking = !state.thinking;
  $("btn-think").classList.toggle("active", state.thinking);
});

$("btn-deep")?.addEventListener("click", () => {
  if (abilityBlocked($("btn-deep"))) return;
  state.deepResearch = !state.deepResearch;
  $("btn-deep").classList.toggle("active", state.deepResearch);
});

// Короткие подписи рисовальщиков (в реестре имена длиннее). Сам список
// рисующих приходит с сервера по способности images_out — не хардкодим,
// иначе разъезжается с реальностью (так пропал Pollinations).
const DRAW_LABELS = {
  qwen: "Qwen", bing_images: "Bing", chatgpt: "ChatGPT", grok: "Grok",
  alice: "Алиса", ms_copilot: "Copilot", meta_ai: "Meta AI",
  pollinations: "Pollinations",
};

function renderImageBar() {
  const inner = $("image-inner");
  if (!inner) return;
  // Доступные вперёд, «нужен вход» — следом и приглушённо.
  const list = (state.draw || []).slice().sort(
    (a, b) => (a.state === "locked") - (b.state === "locked"));
  const chips = [["auto", t("Авто"), "ready"]].concat(
    list.map((p) => [p.id, DRAW_LABELS[p.id] || p.label, p.state]));
  inner.innerHTML = '<span class="voice-label">' + t("Рисует:") + '</span>' +
    chips.map(([id, label, st]) => {
      const on = (state.drawModel || "auto") === id ? " on" : "";
      const locked = st === "locked";
      const tip = locked
        ? ' title="' + t("Не залогинен — заведи доступ во вкладке «Настройки»") + '"' : "";
      return `<button type="button" class="voice-chip${on}${
        locked ? " unavail" : ""}" data-draw="${id}"${tip}>${
        escapeHTML(label)}${locked ? '<span class="chip-lock">🔒</span>' : ""}</button>`;
    }).join("");
  inner.querySelectorAll("[data-draw]").forEach((chip) => {
    chip.addEventListener("click", () => {
      // Незалогиненного рисовальщика не выбираем — отправить ему нечего.
      if (chip.classList.contains("unavail")) {
        toast("«" + chip.textContent.replace("🔒", "").trim() + "» " +
              t("не залогинен — выбери доступного или заведи доступ во вкладке «Настройки»"));
        return;
      }
      state.drawModel = chip.dataset.draw;
      renderImageBar();
    });
  });
}

$("btn-image").addEventListener("click", () => {
  const on = $("btn-image").classList.toggle("active");
  if (on) {
    renderImageBar();
    openBar($("image-bar"));
    $("btn-audio").classList.remove("active");
    closeBar($("voice-bar"));
  } else {
    closeBar($("image-bar"));
  }
  const input = $("input");
  input.placeholder = on ? t("Опиши, что нарисовать…") : t("Спроси что-нибудь…");
  input.focus();
});

// Загрузка файлов — хранятся в памяти до отправки
const pendingFiles = [];

$("file-input").addEventListener("change", (event) => {
  const container = $("attachments");
  container.hidden = false;
  for (const file of event.target.files) {
    const entry = { file, id: Date.now() + Math.random() };
    pendingFiles.push(entry);

    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    const isImage = file.type.startsWith("image/");
    const icon = isImage ? "🖼" : "📎";
    chip.innerHTML = `${icon} ${escapeHTML(file.name.slice(0, 20))}
      <button class="attachment-remove" title="${t("Убрать")}">×</button>`;
    chip.querySelector(".attachment-remove").addEventListener("click", () => {
      const idx = pendingFiles.findIndex((e) => e.id === entry.id);
      if (idx >= 0) pendingFiles.splice(idx, 1);
      chip.remove();
      if (!container.children.length) container.hidden = true;
    });
    container.appendChild(chip);
  }
  event.target.value = "";
});

/** Забыть выбранные файлы и убрать их плашки. */
function dropAttachments() {
  if (!pendingFiles.length) return;
  pendingFiles.length = 0;
  const container = $("attachments");
  if (container) {
    container.innerHTML = "";
    container.hidden = true;
  }
  $("attach")?.classList.remove("has-files");
}

/** Уменьшенная копия картинки для показа в ленте (не для отправки): полный
 *  файл уехал бы в localStorage и распух бы. Ужимаем до 512px в webp. */
async function imageThumb(file, max = 512) {
  const objUrl = URL.createObjectURL(file);
  try {
    const img = await new Promise((res, rej) => {
      const im = new Image();
      im.onload = () => res(im);
      im.onerror = rej;
      im.src = objUrl;
    });
    const scale = Math.min(1, max / Math.max(img.width, img.height) || 1);
    const w = Math.max(1, Math.round(img.width * scale));
    const h = Math.max(1, Math.round(img.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    canvas.getContext("2d").drawImage(img, 0, 0, w, h);
    return canvas.toDataURL("image/webp", 0.8);
  } finally {
    URL.revokeObjectURL(objUrl);
  }
}

/** Превью вложений для показа в сообщении пользователя: у картинок —
 *  уменьшенный thumbnail, у файлов — имя. pendingFiles не расходуем. */
async function attachmentPreviews() {
  const out = [];
  for (const { file } of pendingFiles) {
    const isImage = file.type.startsWith("image/");
    let url = "";
    if (isImage) {
      try { url = await imageThumb(file); } catch { url = ""; }
    }
    out.push({ kind: isImage ? "image" : "file",
               name: file.name, mime: file.type, url });
  }
  return out;
}

/** Превращает pendingFiles в массив {data, filename, mime} для JSON. */
async function drainAttachments({ keepChips = false } = {}) {
  if (!pendingFiles.length) return [];
  const out = [];
  for (const { file } of pendingFiles) {
    const buf = await file.arrayBuffer();
    const b64 = btoa(
      new Uint8Array(buf).reduce((s, b) => s + String.fromCharCode(b), "")
    );
    out.push({ data: b64, filename: file.name, mime: file.type });
  }
  // keepChips — файлы прочитаны, но со стола не убраны: их уберёт
  // отправитель, когда запрос уйдёт успешно. Иначе ошибка сети уносит
  // выбранные файлы, и человек ищет их заново.
  if (!keepChips) dropAttachments();
  return out;
}

// Озвучка — режим TTS: кнопка переключает, выбор голоса появляется
// Плавное сворачивание ряда: класс closing играет анимацию выхода,
// потом снимаем open (мгновенное скрытие без closing выглядело бы резко).
function closeBar(el) {
  if (!el.classList.contains("open")) return;
  el.classList.remove("open");
  el.classList.add("closing");
  setTimeout(() => el.classList.remove("closing"), 260);
}
function openBar(el) {
  el.classList.remove("closing");
  el.classList.add("open");
}

$("btn-audio").addEventListener("click", () => {
  const on = $("btn-audio").classList.toggle("active");
  if (on) {
    renderVoiceBar();
    openBar($("voice-bar"));
    $("btn-image").classList.remove("active");
    closeBar($("image-bar"));
  } else {
    closeBar($("voice-bar"));
  }
  const input = $("input");
  input.placeholder = on ? t("Что озвучить голосом…") : t("Спроси что-нибудь…");
});

/* ── Выбор озвучки ──────────────────────────────────────────────────
 *
 * Два ряда, а не один общий список голосов: сперва движок, под ним его
 * голоса. Сваленные в кучу одиннадцать имён не говорят, кто из них чей, а
 * движки различаются по существу — у одного нет суточной нормы, у второго
 * сотня запросов и другой тембр. Выбор голоса без понимания, чей он,
 * ничего не значит.
 *
 * Маршрут на сервере определяется САМИМ голосом: списки не пересекаются
 * (server.py, _speak). Поэтому достаточно отправить голос, движок
 * передавать не нужно.
 */

const TTS_ENGINES = [
  {
    id: "openai_fm",
    label: "OpenAI.fm",
    note: "без суточной нормы",
    voices: [
      ["coral", "Coral", "♀"], ["nova", "Nova", "♀"],
      ["shimmer", "Shimmer", "♀"], ["sage", "Sage", "♀"],
      ["ballad", "Ballad", "♀"], ["alloy", "Alloy", "♂"],
      ["onyx", "Onyx", "♂"], ["echo", "Echo", "♂"],
      ["ash", "Ash", "♂"], ["fable", "Fable", "♂"],
      ["verse", "Verse", "♂"],
    ],
  },
  {
    id: "groq",
    label: "Orpheus · Groq",
    note: "100 в сутки, нужен ключ",
    voices: [
      ["autumn", "Autumn", "♀"], ["diana", "Diana", "♀"],
      ["hannah", "Hannah", "♀"], ["austin", "Austin", "♂"],
      ["daniel", "Daniel", "♂"], ["troy", "Troy", "♂"],
    ],
  },
];

function ttsEngineOf(voice) {
  return TTS_ENGINES.find((e) => e.voices.some(([v]) => v === voice))
         || TTS_ENGINES[0];
}

function renderVoiceBar() {
  const engines = $("tts-engines");
  const voices = $("tts-voices");
  if (!engines || !voices) return;

  const current = ttsEngineOf(state.voice || "coral");

  // openai_fm анонимный — всегда доступен; groq и прочие с ключом доступны
  // только залогиненными. Недоступный движок помечаем и не даём выбрать.
  const engineLocked = (engine) => {
    if (engine.id === "openai_fm") return false;
    const p = state.providers.find((x) => x.id === engine.id);
    return !p || p.state !== "ready";
  };
  engines.innerHTML = '<span class="voice-label">' + t("Озвучка:") + '</span>' +
    TTS_ENGINES.map((engine) => {
      const on = engine.id === current.id ? " on" : "";
      const locked = engineLocked(engine);
      const tip = locked
        ? t("Нужен вход — заведи доступ во вкладке «Настройки»")
        : t(engine.note);
      return `<button type="button" class="voice-chip${on}${
        locked ? " unavail" : ""}" data-engine="${engine.id}"
                      title="${escapeHTML(tip)}"
              >${escapeHTML(engine.label)}${locked ? '<span class="chip-lock">🔒</span>' : ""}
               <em class="voice-note">${escapeHTML(t(engine.note))}</em></button>`;
    }).join("");

  voices.innerHTML = '<span class="voice-label">' + t("Голос:") + '</span>' +
    current.voices.map(([id, label, sex]) => {
      const on = id === state.voice ? " on" : "";
      return `<button type="button" class="voice-chip${on}"
                      data-voice="${id}">${escapeHTML(label)} <em>${sex}</em></button>`;
    }).join("");

  engines.querySelectorAll("[data-engine]").forEach((chip) => {
    chip.addEventListener("click", () => {
      // Незалогиненный движок не выбираем — синтезировать через него нечем.
      if (chip.classList.contains("unavail")) {
        toast(t("Этот движок озвучки не залогинен — оставь OpenAI.fm (без входа) или заведи доступ во вкладке «Настройки»"));
        return;
      }
      const engine = TTS_ENGINES.find((e) => e.id === chip.dataset.engine);
      // Переключая движок, берём его первый голос: прежний принадлежал
      // другому и на сервере ушёл бы обратно к старому движку.
      state.voice = engine.voices[0][0];
      renderVoiceBar();
    });
  });
  voices.querySelectorAll("[data-voice]").forEach((chip) => {
    chip.addEventListener("click", () => {
      state.voice = chip.dataset.voice;
      renderVoiceBar();
    });
  });
}

async function sendSpeech(text) {
  if (!state.current) newThread();
  const thread = state.current;

  thread.messages.push({ role: "user", content: text, mode: "speech" });
  if (!thread.title) thread.title = text.slice(0, 38);
  saveThreads();
  renderThreads();
  renderMessages();

  const voice = state.voice || "coral";
  const reply = { role: "assistant", content: "", who: `${t("Озвучка")} · ${voice}` };
  thread.messages.push(reply);
  renderMessages();

  state.busy = true;
  $("send").disabled = true;
  const t0 = Date.now();
  const head = document.querySelector(".thread .msg:last-child .msg-role");
  const timer = setInterval(() => {
    if (!head) return;
    let badge = head.querySelector(".msg-timer");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "msg-timer";
      head.appendChild(badge);
    }
    badge.textContent = `${((Date.now() - t0) / 1000).toFixed(1)}с`;
  }, 100);

  try {
    const response = await fetch("/v1/audio/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: text, voice }),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err?.error?.message || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    // data: URL, чтобы плеер пережил перерисовку ленты из localStorage.
    reply.audio = await blobToDataURL(blob);
    reply.content = text;
    bumpCounter("Озвучка");
  } catch (error) {
    // Уже пришедший текст НЕ затираем. Раньше строка ошибки вставала на
    // его место, и обрыв на середине длинного ответа терял всё, что
    // успело прийти, — а заодно выбрасывал ход из контекста, потому что
    // сообщения с error в историю не идут.
    const note = String(error.message || error);
    if (reply.content) {
      reply.content += `

_[${t("ответ оборвался")}: ${note}]_`;
    } else {
      reply.error = true;
      reply.content = note;
    }
  }

  clearInterval(timer);
  reply.elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  state.busy = false;
  $("send").disabled = false;
  $("who").textContent = "";
  saveThreads();
  renderMessages();
}

function blobToDataURL(blob) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result);
    reader.readAsDataURL(blob);
  });
}

// Вкладки пула
document.querySelectorAll(".pool-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".pool-tab").forEach((t) =>
      t.classList.remove("active"));
    tab.classList.add("active");
    renderPool(tab.dataset.tab);
  });
});

/* ── Счётчик запросов ──────────────────────────────────────────── */

/* ── Свой аудиоплеер ────────────────────────────────────────────── */

// Делегирование на ленту: сообщения пересобираются через innerHTML, так
// что вешать обработчики на каждый плеер нельзя — они бы отваливались.
function fmtTime(sec) {
  if (!isFinite(sec)) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function paintPlayer(player) {
  const audio = player.querySelector("audio");
  const fg = player.querySelector(".wave-fg");
  const time = player.querySelector(".player-time");
  const dur = audio.duration || 0;
  const ratio = dur ? audio.currentTime / dur : 0;
  // Обрезаем справа: видно левую часть до прогресса.
  fg.style.clipPath = `inset(0 ${(1 - ratio) * 100}% 0 0)`;
  // Пока играет — текущее время, на паузе в начале — полная длина.
  time.textContent = audio.paused && !audio.currentTime
    ? fmtTime(dur) : fmtTime(audio.currentTime);
}

// Копирование кода из блока по кнопке в его шапке.
document.addEventListener("click", async (event) => {
  const copy = event.target.closest(".code-copy");
  if (!copy) return;
  const pre = copy.closest(".code-block")?.querySelector("pre");
  const text = pre ? pre.textContent : "";
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      ok = document.execCommand("copy"); ta.remove();
    } catch { ok = false; }
  }
  const span = copy.querySelector("span");
  if (span) {
    const was = span.textContent;
    span.textContent = ok ? t("Скопировано") : t("Не вышло");
    copy.classList.toggle("done", ok);
    setTimeout(() => { span.textContent = was; copy.classList.remove("done"); }, 1400);
  }
});

document.addEventListener("click", (event) => {
  const btn = event.target.closest(".player-play");
  if (btn) {
    const audio = btn.parentElement.querySelector("audio");
    // Перед стартом глушим остальные плееры — два голоса разом не нужны.
    if (audio.paused) {
      document.querySelectorAll(".player audio").forEach((a) => {
        if (a !== audio) a.pause();
      });
      audio.play();
    } else {
      audio.pause();
    }
    return;
  }
  const wave = event.target.closest(".player-wave");
  if (wave) {
    const player = wave.closest(".player");
    const audio = player.querySelector("audio");
    const rect = wave.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    if (audio.duration) {
      audio.currentTime = Math.max(0, Math.min(1, ratio)) * audio.duration;
      paintPlayer(player);
    }
  }
});

// Состояние иконки play/pause и прогресс — на событиях самого audio.
document.addEventListener("play", (event) => {
  event.target.closest?.(".player")?.classList.add("playing");
}, true);

document.addEventListener("pause", (event) => {
  event.target.closest?.(".player")?.classList.remove("playing");
}, true);

document.addEventListener("timeupdate", (event) => {
  const player = event.target.closest?.(".player");
  if (player) paintPlayer(player);
}, true);

document.addEventListener("loadedmetadata", (event) => {
  const player = event.target.closest?.(".player");
  if (player) paintPlayer(player);
}, true);

/* ── Счётчик запросов по провайдерам ── */
let requestStats = {};
try { requestStats = JSON.parse(localStorage.getItem("foxroute.stats") || "{}"); } catch {}

function bumpCounter(provider) {
  const key = provider || "unknown";
  requestStats[key] = (requestStats[key] || 0) + 1;
  try { localStorage.setItem("foxroute.stats", JSON.stringify(requestStats)); } catch {}
  renderCounter();
}

function totalRequests() {
  return Object.values(requestStats).reduce((a, b) => a + b, 0);
}

function renderCounter() {
  const count = $("plan-count");
  if (count) count.textContent = totalRequests();
}

// Раскрытие статистики по клику на счётчике запросов.
document.querySelector(".plan-toggle")?.addEventListener("click", () => {
  const foot = document.querySelector(".pool-mini");
  let details = foot.querySelector(".plan-details");
  if (details) { details.remove(); return; }
  details = document.createElement("div");
  details.className = "plan-details";
  const sorted = Object.entries(requestStats).sort((a, b) => b[1] - a[1]);
  details.innerHTML = sorted.map(([name, count]) =>
    `<div class="plan-detail-row">
      <span>${escapeHTML(t(providerLabel(name)))}</span>
      <span class="plan-detail-num">${count}</span>
    </div>`
  ).join("") || '<div class="plan-detail-row"><span>' + t("Нет запросов") + '</span></div>';
  foot.appendChild(details);
});

/* ── Подключение ───────────────────────────────────────────────── */

$("new-chat").addEventListener("click", newThread);
$("toggle-rail").addEventListener("click", () => $("rail").classList.toggle("hidden"));
function showPool(on) {
  $("pool").hidden = !on;
  $("pool-backdrop").hidden = !on;
  if (on) renderPool(document.querySelector(".pool-tab.active")?.dataset.tab);
}
$("pool-mini-grid").addEventListener("click", () => showPool($("pool").hidden));
$("close-pool").addEventListener("click", () => showPool(false));
$("pool-backdrop").addEventListener("click", () => showPool(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("pool").hidden) showPool(false);
});

// Поиск по чатам в сайдбаре.
$("thread-search").addEventListener("input", (event) => {
  state.threadFilter = event.target.value.trim().toLowerCase();
  renderThreads();
});

// Переключатель темы: светлая / тёмная, выбор запоминается.
if (localStorage.getItem("foxroute.theme") === "light") {
  document.documentElement.classList.add("light");
}
$("theme-toggle").addEventListener("click", () => {
  const light = document.documentElement.classList.toggle("light");
  localStorage.setItem("foxroute.theme", light ? "light" : "dark");
});

// Переключатель языка RU⇄EN: меняем значение в localStorage и перезагружаем
// страницу — так все t() и applyStaticI18n() отработают заново в новом языке.
$("lang-toggle")?.addEventListener("click", () => {
  const en = localStorage.getItem("foxroute-lang") === "en";
  localStorage.setItem("foxroute-lang", en ? "ru" : "en");
  location.reload();
});

$("picker-button").addEventListener("click", (event) => {
  event.stopPropagation();
  const menu = $("picker-menu");
  menu.hidden ? openPicker() : closePicker();
});
document.addEventListener("click", closePicker);
$("picker-menu").addEventListener("click", (event) => event.stopPropagation());

// Единый предохранитель отправки: есть ли кому выполнить запрос при текущем
// выборе. Возвращает причину отказа или "" если всё в порядке. Это последний
// рубеж: даже если выбор locked-цели как-то проскочил, отсюда он не уйдёт на
// заведомо мёртвого провайдера (и заодно ни один запрос не летит зря).
function routeBlocker() {
  if ($("btn-image").classList.contains("active")) {
    const d = state.drawModel || "auto";
    if (d === "auto") {
      return (state.draw || []).some((p) => p.state === "ready") ? ""
        : t("Нет доступного рисовальщика — залогинь провайдера или пользуйся анонимными (Pollinations, DeepAI, Алиса).");
    }
    const p = (state.draw || []).find((x) => x.id === d);
    return (p && p.state === "ready") ? ""
      : "«" + (DRAW_LABELS[d] || d) + "» " + t("не залогинен — выбери доступного рисовальщика.");
  }
  if ($("btn-audio").classList.contains("active")) {
    const engine = ttsEngineOf(state.voice || "coral");
    if (engine.id === "openai_fm") return "";  // анонимный, всегда доступен
    const p = state.providers.find((x) => x.id === engine.id);
    return (p && p.state === "ready") ? ""
      : "«" + engine.label + "» " + t("не залогинен — переключись на OpenAI.fm (без входа) или заведи доступ.");
  }
  return chatBlocker();
}

// Чат-часть предохранителя, отдельно — ею пользуется и «Заново другим»,
// которое шлёт мимо submit. Есть ли кому ответить при текущем/«auto» выборе.
function chatBlocker() {
  const id = (state.model || "auto").split("/")[0];
  if (id === "auto") {
    const ready = state.providers.filter((p) => p.state === "ready");
    if (!ready.length) return t("Нет ни одного залогиненного провайдера — заведи доступ во вкладке «Настройки».");
    if (state.deepResearch && !ready.some((p) => p.can && p.can.deep_research))
      return t("Глубокое исследование умеют ") + capProviders("deep_research") + t(", но они не залогинены.");
    if (state.thinking && !ready.some((p) => p.can && p.can.thinking))
      return t("Нет залогиненного провайдера с размышлением.");
    if (state.webSearch && !ready.some((p) => p.can && p.can.web_search))
      return t("Нет залогиненного провайдера с веб-поиском.");
    return "";
  }
  const p = state.providers.find((x) => x.id === id);
  return (p && p.state === "locked")
    ? "«" + (p.label || id) + "» " + t("не залогинен — выбери «Авто» или заведи доступ во вкладке «Настройки».")
    : "";
}

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.busy) return;

  // Последний рубеж: не отправляем запрос, который некому выполнить.
  const blocked = routeBlocker();
  if (blocked) { toast(blocked); return; }

  // Режим картинки: отправляем на /v1/images/generations. Режим НЕ
  // сбрасываем — рисуют обычно сериями, а слетавший режим отправлял
  // следующий «нарисуй…» в чат (текстовой модели, которая рисовать не
  // умеет). Выход из режима — повторным кликом по «Картинка».
  if ($("btn-image").classList.contains("active")) {
    input.value = "";
    input.style.height = "auto";
    sendImage(text);
    return;
  }

  // Режим озвучки: пользователь САМ попросил синтез — отправляем текст на TTS.
  if ($("btn-audio").classList.contains("active")) {
    input.value = "";
    input.style.height = "auto";
    sendSpeech(text);
    return;
  }

  input.value = "";
  input.style.height = "auto";
  send(text);
});

loadThreads();
renderThreads();
wireSuggestions();
loadProviders();
renderCounter();
setInterval(loadProviders, 20000);

/* ── Доступы ────────────────────────────────────────────────────────
 *
 * Экран, ради которого затевался слой: до него единственным способом
 * завести учётку была командная строка сервера. Здесь тот же набор
 * действий плюс подсказка, где именно брать доступ у каждого сервиса —
 * знание, которое раньше жило в комментариях внутри адаптеров.
 *
 * Значение доступа НИКОГДА не приходит с сервера целиком: в списке только
 * последние четыре символа. Их хватает отличить свою учётку от чужой и не
 * хватает воспользоваться.
 */

const KEYS = { data: [], filter: "need" };

async function loadKeys() {
  try {
    const answer = await fetch("/api/accounts").then((r) => r.json());
    KEYS.data = answer.providers || [];
  } catch (err) {
    KEYS.data = [];
  }
  const need = KEYS.data.filter(keyNeeded).length;
  const count = $("keys-count");
  if (count) {
    // В месте под число длинной фразе не место: показываем счёт, а
    // спокойное состояние отмечаем галочкой и приглушённым цветом.
    count.textContent = need ? String(need) : "✓";
    count.classList.toggle("keys-count-ok", !need);
  }
  renderKeys();
}

// «Нужен доступ» — это не «нет учёток», а «без них не работает»: часть
// сервисов (Z.ai, Opera Aria, Yqcloud) обходится вовсе без доступа.
function keyNeeded(provider) {
  return !provider.ready && provider.auth !== "none";
}

// Домен из ссылки — для показа адреса кликабельным (без протокола и www).
function siteHost(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return String(url).replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  }
}

function keysFiltered() {
  if (KEYS.filter === "ready") return KEYS.data.filter((p) => p.ready);
  if (KEYS.filter === "free") {
    return KEYS.data.filter((p) => p.auth === "none" || p.auth === "optional");
  }
  return KEYS.data.filter(keyNeeded);
}

function keyRow(provider) {
  const hint = provider.hint || {};
  const accounts = (provider.accounts || []).map((account) => `
    <div class="key-account">
      <span class="key-acc-name">${escapeHTML(account.account)}${account.proxy
        ? ` <span class="key-acc-proxied" title="${t("Через свой прокси")}: ${escapeHTML(account.proxy)}">🌐</span>`
        : ""}</span>
      <span class="key-acc-tail">…${escapeHTML(account.tail || "")}</span>
      ${account.enabled ? "" : '<span class="key-off">' + t("выключена") + '</span>'}
      <span class="key-acc-actions">
        <button class="key-mini" data-act="check"
                data-p="${provider.id}" data-a="${escapeHTML(account.account)}"
                title="${t("Проверить на живом (тратит сообщение из квоты)")}">${t("проверить")}</button>
        <button class="key-mini" data-act="proxy-edit"
                data-p="${provider.id}" data-a="${escapeHTML(account.account)}"
                title="${t("Свой прокси для этой учётки — переопределяет общий")}">${t("прокси")}</button>
        <button class="key-mini" data-act="toggle"
                data-p="${provider.id}" data-a="${escapeHTML(account.account)}"
                data-on="${account.enabled ? "0" : "1"}">${account.enabled ? t("выключить") : t("включить")}</button>
        <button class="key-mini danger" data-act="remove"
                data-p="${provider.id}" data-a="${escapeHTML(account.account)}">${t("удалить")}</button>
      </span>
      <div class="key-acc-proxy" hidden>
        <input type="text" class="key-acc-proxy-input" spellcheck="false"
               autocomplete="off" value="${escapeHTML(account.proxy || "")}"
               placeholder="${t("socks5://user:pass@host:port — пусто = общий прокси")}">
        <button class="key-mini" data-act="set-proxy"
                data-p="${provider.id}" data-a="${escapeHTML(account.account)}">${t("сохранить")}</button>
      </div>
    </div>`).join("");

  // Вход через браузер: кнопка открывает чистое окно Chrome, потом
  // «Забрать куки» снимает доступ автоматически. Запасом — ссылка на сайт
  // для тех, кто предпочитает завести доступ руками.
  const manual = hint.site
    ? `<a class="key-site" href="${escapeHTML(hint.site)}" target="_blank"
          rel="noopener noreferrer">${t("вручную ↗")}</a>`
    : "";
  // У анонимных (auth none) логина нет — «Войти» врёт: показываем сам адрес
  // сайта кликабельным. У тех, кому нужен ключ/логин, — «Войти ↗».
  const siteLink = hint.site
    ? `<a class="key-site" href="${escapeHTML(hint.site)}" target="_blank"
          rel="noopener noreferrer">${
      provider.auth === "none"
        ? escapeHTML(siteHost(hint.site)) + " ↗"
        : t("Войти ↗")}</a>`
    : "";
  const site = provider.browser_login
    ? `<button class="key-login" data-act="login" data-p="${provider.id}"
               title="${t("Откроется чистое окно браузера — войди там, потом «Забрать куки»")}">${t("Войти в браузере")}</button>${manual}`
    : siteLink;

  // Маркер привязки доступа к IP-адресу — коротко, подробность в тултипе.
  const IP_INFO = {
    good: ["🟢", t("Не привязан к IP — снимай и запускай где угодно")],
    warn: ["🟡", t("Переезд на другой IP обычно переживает; при смене страны сервис может запросить подтверждение входа")],
    crit: ["🔴", t("Привязан к IP (Cloudflare или гео-защита) — снимай ТАМ ЖЕ, где доступ будет работать")],
  };
  // Маркер IP нужен только там, где доступ — кука/сессия (её привязка к
  // адресу и правда играет). У ключей API он бессмыслен: ключу всё равно,
  // откуда его прислали.
  const ipm = provider.browser_login ? IP_INFO[provider.ip] : null;
  const ip = ipm
    ? `<span class="key-ip ${provider.ip}" title="${escapeHTML(ipm[1])}">${ipm[0]}</span>`
    : "";
  const note = hint.note ? `<div class="key-note">${escapeHTML(t(hint.note))}</div>` : "";
  const missing = (provider.missing || []).length
    ? `<div class="key-missing">${t("не хватает")}: ${escapeHTML(provider.missing.join(", "))}</div>`
    : "";

  // Ключей у API может быть много — это пул, и добавлять их можно подряд.
  // У веб-сессии несколько значений через | означают склейку одного
  // доступа, поэтому подсказка разная.
  const many = provider.multi_key
    ? t("можно добавить несколько ключей — они работают как пул")
    : "";

  const form = provider.auth === "none" ? "" : `
    <div class="key-form">
      <input type="password" class="key-input" placeholder="${escapeHTML(hint.what ? t(hint.what) : t("доступ"))}"
             data-p="${provider.id}" autocomplete="off" spellcheck="false">
      <button class="key-add" data-act="add" data-p="${provider.id}">${t("Добавить")}</button>
    </div>
    ${many ? `<div class="key-note">${many}</div>` : ""}`;

  // Прокси на всего провайдера: и на «Войти в браузере», и на его запросы.
  // Пароль в подписи маскируем, полный адрес — только в поле при раскрытии.
  const pmask = provider.proxy
    ? provider.proxy.replace(/\/\/([^:@/]+):[^@/]*@/, "//$1:•••@")
    : "";
  const proxyCtl = `
    <div class="key-proxy">
      <button class="key-mini key-proxy-toggle" data-act="pproxy-edit"
              data-p="${provider.id}"
              title="${t("Прокси этого провайдера — на вход и на запросы. Пусто — общий")}">
        🌐 ${provider.proxy
          ? t("прокси") + ": " + escapeHTML(pmask)
          : t("прокси провайдера")}</button>
      <div class="key-proxy-edit" hidden>
        <input type="text" class="key-proxy-input" spellcheck="false"
               autocomplete="off" value="${escapeHTML(provider.proxy || "")}"
               placeholder="${t("socks5://user:pass@host:port — пусто = общий")}">
        <button class="key-mini" data-act="pproxy-test" data-p="${provider.id}">${t("Проверить")}</button>
        <button class="key-mini" data-act="pproxy-save" data-p="${provider.id}">${t("Сохранить")}</button>
        <div class="key-proxy-result"></div>
      </div>
    </div>`;

  return `
    <div class="key-card" data-card="${provider.id}">
      <div class="key-head">
        <span class="dot ${provider.ready ? "ready" : (provider.auth === "none" ? "ready" : "paused")}"></span>
        <span class="key-title">${escapeHTML(provider.label)}</span>
        <span class="pool-kind">${t(provider.kind)}</span>
        ${ip}
        <span class="key-head-spacer"></span>
        ${site}
      </div>
      ${provider.auth === "none"
        ? '<div class="key-note">' + t("Работает без входа") + '</div>'
        : `<div class="key-what">${escapeHTML(hint.what ? t(hint.what) : "")}</div>`}
      ${note}${missing}
      ${accounts}
      ${form}
      ${proxyCtl}
      <div class="key-result" data-result="${provider.id}"></div>
    </div>`;
}

function renderKeys() {
  const list = $("keys-list");
  if (!list) return;
  const rows = keysFiltered();
  if (!rows.length) {
    list.innerHTML = '<div class="key-empty">' + t("Здесь пусто") + '</div>';
    return;
  }
  // Три разные природы доступа, показываем раздельно:
  //  • веб-сессии — вход кукой/логином (основной путь);
  //  • API-ключи — официальный ключ;
  //  • без входа — работает анонимно СЕЙЧАС (логин не нужен И учётки нет).
  // Ключевое: если учётка ЕСТЬ — провайдер ходит с ней, а не анонимно, даже
  // когда вход у него необязательный (alice/zai/perplexity с кукой). Тогда
  // ему место в веб-сессиях/API, а в «без входа» — только реально безучётные.
  const anonNow = (p) => (p.accounts || []).length === 0
    && (p.auth === "none" || p.auth === "optional");
  const web = rows.filter((p) => !anonNow(p) && p.kind === "web");
  const api = rows.filter((p) => !anonNow(p) && p.kind === "api");
  const anon = rows.filter(anonNow);
  const group = (title, items) => items.length
    ? `<div class="keys-group-h">${title}` +
      `<span class="keys-group-n">${items.length}</span></div>` +
      items.map(keyRow).join("")
    : "";
  list.innerHTML = group(t("Веб-сессии — вход логином"), web) +
                   group(t("API-ключи"), api) +
                   group(t("Без входа — анонимно"), anon);
}

function showKeys(on) {
  $("keys").hidden = !on;
  $("keys-backdrop").hidden = !on;
  if (on) {
    loadKeys();
    loadProxy();
    switchSettingsPane("access");  // всегда открываем на «Доступах»
  }
}

// Переключение верхних вкладок настроек: «Доступы | Прокси».
function switchSettingsPane(pane) {
  document.querySelectorAll(".settings-navtab").forEach((t) =>
    t.classList.toggle("active", t.dataset.pane === pane));
  document.querySelectorAll(".settings-pane").forEach((p) => {
    p.hidden = p.dataset.pane !== pane;
  });
}

async function loadProxy() {
  try {
    const r = await fetch("/api/settings");
    if (!r.ok) return;
    const d = await r.json();
    const input = $("proxy-input");
    if (input) input.value = d.proxy || "";
  } catch { /* оффлайн — оставим поле как есть */ }
}

async function settingsAction(action, body) {
  try {
    const r = await fetch(`/api/settings/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await r.json();
  } catch (e) {
    return { error: String(e) };
  }
}

$("proxy-save")?.addEventListener("click", async () => {
  const box = $("proxy-result");
  const proxy = ($("proxy-input").value || "").trim();
  box.textContent = t("сохраняю…");
  const answer = await settingsAction("proxy", { proxy });
  box.innerHTML = answer && !answer.error
    ? '<span class="proxy-ok">' + t("✓ сохранено") +
      (proxy ? "" : t(" (прокси выключен)")) + "</span>"
    : '<span class="proxy-bad">' + t("не вышло") + ": " +
      escapeHTML((answer && answer.error) || "?") + "</span>";
});

$("proxy-test")?.addEventListener("click", async () => {
  const box = $("proxy-result");
  const proxy = ($("proxy-input").value || "").trim();
  if (!proxy) { box.textContent = t("введи адрес прокси"); return; }
  box.innerHTML = loadingHTML(t("проверяю связь через прокси…"));
  const answer = await settingsAction("test-proxy", { proxy });
  box.innerHTML = answer && answer.ok
    ? '<span class="proxy-ok">' + t("✓ работает — видимый IP: ") +
      escapeHTML(answer.ip || "?") + "</span>"
    : '<span class="proxy-bad">' + t("не отвечает: ") +
      escapeHTML((answer && (answer.error || answer.ip)) || "?") + "</span>";
});

// Крутящийся индикатор + подпись на время долгой операции.
function loadingHTML(text) {
  return '<span class="spinner"></span><span class="is-working">' +
         escapeHTML(text || "") + "</span>";
}

async function keysAction(action, body, resultBox, loadingMsg) {
  if (resultBox) resultBox.innerHTML = loadingHTML(loadingMsg || t("работаю…"));
  try {
    const answer = await fetch(`/api/accounts/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json());
    if (answer.error) {
      if (resultBox) resultBox.textContent = answer.error.message || t("не вышло");
      return null;
    }
    return answer;
  } catch (err) {
    if (resultBox) resultBox.textContent = t("сервер не ответил");
    return null;
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-act]");
  if (!button || !button.closest("#keys")) return;

  const action = button.dataset.act;
  const provider = button.dataset.p;
  const card = button.closest(".key-card");
  const box = card ? card.querySelector(".key-result") : null;

  if (action === "add") {
    const input = card.querySelector(".key-input");
    const value = (input.value || "").trim();
    if (!value) { box.textContent = t("вставь доступ"); return; }
    const answer = await keysAction("add", { provider, value }, box);
    if (answer) {
      input.value = "";
      box.textContent = t("добавлено");
      loadKeys();
    }
    return;
  }

  const account = button.dataset.a;
  if (action === "remove") {
    if (await keysAction("remove", { provider, account }, box)) loadKeys();
    return;
  }
  if (action === "toggle") {
    const enabled = button.dataset.on === "1";
    if (await keysAction("toggle", { provider, account, enabled }, box)) loadKeys();
    return;
  }
  if (action === "proxy-edit") {
    // Показать/спрятать инлайн-поле прокси у этой учётки.
    const editor = button.closest(".key-account")?.querySelector(".key-acc-proxy");
    if (editor) {
      editor.hidden = !editor.hidden;
      if (!editor.hidden) editor.querySelector(".key-acc-proxy-input")?.focus();
    }
    return;
  }
  if (action === "set-proxy") {
    const field = button.closest(".key-account")?.querySelector(".key-acc-proxy-input");
    const proxy = (field?.value || "").trim();
    if (await keysAction("set-proxy", { provider, account, proxy }, box)) {
      box.textContent = proxy ? t("прокси задан") : t("прокси снят — пойдёт через общий");
      loadKeys();
    }
    return;
  }
  if (action === "pproxy-edit") {
    // Прокси на всего провайдера — раскрыть поле в карточке.
    const editor = card.querySelector(".key-proxy-edit");
    if (editor) {
      editor.hidden = !editor.hidden;
      if (!editor.hidden) editor.querySelector(".key-proxy-input")?.focus();
    }
    return;
  }
  if (action === "pproxy-test") {
    // Проверка прокси ДО логина/запросов: показываем видимый через него IP.
    const field = card.querySelector(".key-proxy-input");
    const res = card.querySelector(".key-proxy-result");
    const proxy = (field?.value || "").trim();
    if (!proxy) { res.textContent = t("впиши адрес прокси"); return; }
    res.innerHTML = loadingHTML(t("проверяю связь через прокси…"));
    const answer = await settingsAction("test-proxy", { proxy });
    res.innerHTML = answer && answer.ok
      ? '<span class="proxy-ok">' + t("✓ работает — видимый IP: ") +
        escapeHTML(answer.ip || "?") + "</span>"
      : '<span class="proxy-bad">' + t("не отвечает: ") +
        escapeHTML((answer && (answer.error || answer.ip)) || "?") + "</span>";
    return;
  }
  if (action === "pproxy-save") {
    const field = card.querySelector(".key-proxy-input");
    const res = card.querySelector(".key-proxy-result");
    const proxy = (field?.value || "").trim();
    const answer = await keysAction("provider-proxy", { provider, proxy });
    if (answer && !answer.error) {
      res.innerHTML = '<span class="proxy-ok">' +
        (proxy ? t("✓ прокси провайдера задан") : t("снят — пойдёт общий")) + "</span>";
      loadKeys();
    } else {
      res.textContent = t("не вышло сохранить");
    }
    return;
  }
  if (action === "check") {
    // Проверка стоит сообщения из квоты — говорим об этом до, а не после.
    const answer = await keysAction("check", { provider, account }, box,
                                    t("проверяю живость (тратится сообщение)…"));
    if (answer) {
      box.textContent = `${t(answer.state)}: ${tDetail(answer.detail)}`.trim();
    }
    return;
  }

  if (action === "login") {
    const answer = await keysAction("login", { provider }, box,
                                    t("открываю окно браузера…"));
    if (answer) {
      // Окно открыто — показываем кнопку «Забрать куки» прямо в карточке.
      box.innerHTML =
        '<div class="key-login-step">' +
        t("Залогинься в открывшемся окне, потом нажми «Забрать куки». ") +
        '<b class="key-login-warn">' +
        t("⚠ Не закрывай окно браузера, пока не нажал «Забрать куки» — снятие идёт из живого окна.") +
        '</b>' +
        '<button class="key-grab" data-act="grab" data-p="' + provider +
        '">' + t("Забрать куки") + '</button></div>';
    }
    return;
  }

  if (action === "grab") {
    const answer = await keysAction("grab", { provider }, box,
                                    t("забираю куки и проверяю…"));
    if (answer) {
      const chk = answer.check || {};
      const ok = chk.state === "ok" || chk.state === "ready";
      box.innerHTML = '<span class="' + (ok ? "grab-ok" : "grab-warn") +
        '">' + (ok ? t("✓ доступ работает") : t("добавлено, проверка: ") +
        escapeHTML(t(chk.state) || "?")) +
        (chk.detail ? " — " + escapeHTML(tDetail(chk.detail)) : "") + "</span>";
      loadKeys();
    }
    return;
  }
});

$("keys-toggle")?.addEventListener("click", () => showKeys(true));
$("close-keys")?.addEventListener("click", () => showKeys(false));
$("keys-backdrop")?.addEventListener("click", () => showKeys(false));
document.querySelectorAll(".keys-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".keys-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    KEYS.filter = tab.dataset.filter;
    renderKeys();
  });
});
document.querySelectorAll(".settings-navtab").forEach((tab) => {
  tab.addEventListener("click", () => {
    switchSettingsPane(tab.dataset.pane);
    if (tab.dataset.pane === "proxy") loadProxy();
  });
});

loadKeys();

/* ── Просмотр картинки ──────────────────────────────────────────────
 *
 * В ленте картинки приведены к одной высоте — иначе выдача разных
 * рисовальщиков скачет: у Bing один формат, у Pollinations другой, у
 * ChatGPT третий. Полный размер показывается по клику.
 *
 * Скачивание сделано ссылкой с download, а не программной: и обычный URL,
 * и data: она забирает одинаково, без обходных путей.
 */

function imageViewer() {
  let box = document.getElementById("viewer");
  if (box) return box;
  box = document.createElement("div");
  box.id = "viewer";
  box.className = "viewer";
  box.hidden = true;
  box.innerHTML = `
    <img class="viewer-img" alt="">
    <div class="viewer-bar">
      <a class="viewer-btn" id="viewer-save" download="foxygpt.png">Скачать</a>
      <button class="viewer-btn" id="viewer-close" type="button">Закрыть</button>
    </div>`;
  document.body.appendChild(box);

  const hide = () => { box.hidden = true; };
  box.addEventListener("click", (e) => {
    if (e.target === box || e.target.id === "viewer-close") hide();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hide();
  });
  return box;
}

// Картинка в сообщении не загрузилась (битый/протухший URL, 404 у
// Pollinations) — не оставляем огромный пустой бокс с иконкой «сломано»,
// а заменяем компактной ссылкой. Событие error у <img> не всплывает,
// поэтому слушаем в фазе capture.
document.addEventListener("error", (event) => {
  const img = event.target;
  if (!(img instanceof HTMLImageElement) || !img.classList.contains("msg-image")) return;
  const link = document.createElement("a");
  link.href = img.src;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.className = "msg-image-fail";
  link.textContent = "🖼 картинка не загрузилась — открыть по ссылке ↗";
  img.replaceWith(link);
}, true);

document.addEventListener("click", (event) => {
  const picture = event.target.closest("img.msg-image, img.msg-attach-img");
  if (!picture) return;
  const box = imageViewer();
  box.querySelector(".viewer-img").src = picture.src;
  const save = box.querySelector("#viewer-save");
  save.href = picture.src;
  // Имя файла из ссылки, если оно там есть; для data: своё.
  const guess = /\/([^/?#]+\.(?:png|jpe?g|webp|gif))(?:[?#]|$)/i.exec(picture.src);
  save.download = guess ? guess[1] : "foxygpt.png";
  box.hidden = false;
});

/* ── Кнопки по умениям провайдера ───────────────────────────────────
 *
 * Показываем только то, что выбранный провайдер действительно делает.
 * Кнопка «Думать» у того, кто размышлений не отдаёт, или скрепка там,
 * где вложения не уходят, обещают несуществующее — и заканчиваются
 * отказом уже ПОСЛЕ того, как сообщение списано из квоты.
 *
 * Умения приходят в /api/status полем can, а оно собрано из объявленных
 * возможностей адаптеров, а не из списка в интерфейсе: список разошёлся
 * бы с кодом при первой же правке.
 *
 * «Авто» — особый случай: там кнопка уместна, если умеет хоть кто-то в
 * пуле, потому что маршрутизатор выберет подходящего.
 */

// Имена провайдеров (любого состояния) с данным умением — для честных
// подсказок «умеют такие-то, залогинь кого-нибудь». Не хардкодим список:
// он разошёлся бы с реальными возможностями адаптеров.
function capProviders(key) {
  const names = state.providers
    .filter((p) => p.can && p.can[key]).map((p) => p.label);
  return names.length ? names.join(", ") : t("некоторые провайдеры");
}

// Статус умения при текущем выборе: "ready" — есть кому выполнить прямо
// сейчас; "locked" — умеет кто-то, но все не залогинены (в «Авто» маршрута
// нет); "none" — не умеет никто. Для «Авто» смотрим весь пул и ТОЛЬКО
// живых; для конкретного провайдера — его умения.
function abilitiesOf(model) {
  const id = (model || "auto").split("/")[0];
  if (id === "auto" || id === "best") {
    const status = (key) => {
      const has = state.providers.filter((p) => p.can && p.can[key]);
      if (!has.length) return "none";
      return has.some((p) => p.state === "ready") ? "ready" : "locked";
    };
    return { thinking: status("thinking"), web_search: status("web_search"),
             deep_research: status("deep_research"), files: status("files") };
  }
  const found = state.providers.find((p) => p.id === id);
  const can = (found && found.can) || {};
  // Выбранный провайдер разлогинился (кука протухла, поймано на опросе):
  // умение он формально имеет, но недоступен — "locked", а не "ready".
  const locked = !!found && found.state === "locked";
  const st = (v) => (!v ? "none" : (locked ? "locked" : "ready"));
  return { thinking: st(can.thinking), web_search: st(can.web_search),
           deep_research: st(can.deep_research), files: st(can.files) };
}

function syncAbilityButtons() {
  const can = abilitiesOf(state.model);

  // none → скрыть (умения нет ни у кого); locked → показать приглушённо и
  // недоступным (умеет только незалогиненный); ready → обычная кнопка.
  const apply = (el, status, hint) => {
    if (!el) return;
    el.hidden = status === "none";
    const off = status === "locked";
    el.classList.toggle("ability-off", off);
    el.title = off ? hint : "";
    if (status !== "ready") el.classList.remove("active");
  };

  apply($("btn-think"), can.thinking,
        t("Размышление есть, но провайдер с ним не залогинен — заведи доступ во вкладке «Настройки»"));
  apply($("btn-web"), can.web_search,
        t("Веб-поиск есть, но провайдер с ним не залогинен — заведи доступ во вкладке «Настройки»"));
  apply($("btn-deep"), can.deep_research,
        t("Глубокое исследование умеют ") + capProviders("deep_research") +
        t(" — залогинь кого-то из них во вкладке «Настройки»"));
  apply($("attach"), can.files,
        t("Приём файлов есть, но такой провайдер не залогинен — заведи доступ"));

  // Умение не готово (нет вовсе или не залогинен) — снимаем и флаг, и файлы:
  // иначе запрос уйдёт с признаком, который выполнить некому.
  if (can.thinking !== "ready" && state.thinking) {
    state.thinking = false;
    $("btn-think")?.classList.remove("active");
  }
  if (can.deep_research !== "ready" && state.deepResearch) {
    state.deepResearch = false;
    $("btn-deep")?.classList.remove("active");
  }
  if (can.web_search !== "ready" && state.webSearch) {
    state.webSearch = false;
    $("btn-web")?.classList.remove("active");
  }
  if (can.files !== "ready") dropAttachments();
}

/* ── Надиктовка ─────────────────────────────────────────────────── */
//
// Речь распознаётся у нас на сервере (Groq Whisper), а не браузерным
// движком: браузерный работает не везде и не понимает русский так же
// хорошо. Значит это обычная запись с микрофона плюс наш эндпоинт
// /v1/audio/transcriptions, совместимый с OpenAI.

const MIC = { recorder: null, chunks: [], stream: null };

/** Короткое сообщение человеку. Отдельной плашки в интерфейсе нет, а
 *  заводить её ради трёх случаев незачем — берём значок в шапке, тот же,
 *  что показывает отвечающего. Во время генерации не трогаем: там своё. */
function toast(text, seconds = 4) {
  const badge = $("who");
  if (!badge || state.busy) return;
  badge.textContent = text;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    if (!state.busy) badge.textContent = "";
  }, seconds * 1000);
}

async function startDictation() {
  const button = $("mic");
  let media;
  try {
    media = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    // Отказ в доступе или отсутствие микрофона — не повод молчать:
    // человек нажал кнопку и ждёт объяснения.
    toast(err && err.name === "NotAllowedError"
      ? t("Микрофон запрещён — разреши доступ в настройках браузера")
      : t("Микрофон не найден"));
    return;
  }

  MIC.stream = media;
  MIC.chunks = [];
  // Тип выбираем по тому, что браузер умеет: Chrome даёт webm/opus,
  // Safari — mp4. Whisper понимает оба, гадать не нужно.
  const kind = ["audio/webm", "audio/mp4", "audio/ogg"]
    .find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t));
  MIC.recorder = new MediaRecorder(media, kind ? { mimeType: kind } : undefined);
  MIC.recorder.ondataavailable = (e) => {
    if (e.data && e.data.size) MIC.chunks.push(e.data);
  };
  MIC.recorder.onstop = () => finishDictation(kind || "audio/webm");
  MIC.recorder.start();
  button.classList.add("recording");
  button.title = t("Остановить запись");
}

function stopDictation() {
  if (MIC.recorder && MIC.recorder.state !== "inactive") MIC.recorder.stop();
}

async function finishDictation(kind) {
  const button = $("mic");
  button.classList.remove("recording");
  button.title = t("Надиктовать — распознаем речь и вставим текстом");
  // Дорожку глушим сразу: иначе браузер держит значок записи и после
  // того, как мы закончили.
  if (MIC.stream) MIC.stream.getTracks().forEach((t) => t.stop());
  MIC.stream = null;

  const blob = new Blob(MIC.chunks, { type: kind });
  MIC.chunks = [];
  if (!blob.size) return;

  button.disabled = true;
  try {
    const form = new FormData();
    const ext = kind.includes("mp4") ? "mp4" : kind.includes("ogg") ? "ogg" : "webm";
    form.append("file", blob, `dictation.${ext}`);
    form.append("model", "whisper-large-v3-turbo");
    const response = await fetch("/v1/audio/transcriptions",
                                 { method: "POST", body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.error?.message || t("не вышло"));

    const said = (data.text || "").trim();
    if (!said) { toast(t("Ничего не расслышал")); return; }
    // Дописываем к тому, что уже набрано, а не заменяем: человек мог
    // начать печатать и продолжить голосом.
    const box = $("input");
    box.value = box.value ? `${box.value.trim()} ${said}` : said;
    box.dispatchEvent(new Event("input"));
    box.focus();
  } catch (err) {
    toast(`${t("Не распознал")}: ${err.message || err}`);
  } finally {
    button.disabled = false;
  }
}

$("mic")?.addEventListener("click", () => {
  if (MIC.recorder && MIC.recorder.state === "recording") stopDictation();
  else startDictation();
});
