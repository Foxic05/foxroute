"""Реестр провайдеров: как их звать и что они умеют.

``PROVIDER_CONFIGS`` — это модели и служебные флаги, выверенные на живых
сервисах. Менять значения наугад нельзя: имя модели, которого сервис не
знает, у части провайдеров молча возвращает пустоту вместо ошибки — правим
только после сверки на живом.

Замеренные характеристики (потолок контекста, скорость, пригодность к
агентному циклу) сюда НЕ кладутся: они протухают, и место им в данных, а не
в исходнике — см. ``foxroute/measurements.py``. Здесь только переэкспорт, чтобы
потребителю не приходилось знать про два модуля сразу.
"""
from __future__ import annotations

# ── База: модели и флаги ──────────────────────────────────────────────

PROVIDER_CONFIGS = {
    "groq": {
        "base_url":       "https://api.groq.com/openai/v1",
        # Groq убрал Llama-модели (обе прежние отдавали 404). Дефолт —
        # быстрая gpt-oss-20b, качественная — 120b.
        "model":          "openai/gpt-oss-20b",
        "model_quality":  "openai/gpt-oss-120b",
        "label":          "Groq",
        "daily_limit":    1000,
        # У каждой модели СВОЯ норма и она не пересекается: один аккаунт
        # Groq по ёмкости как несколько отдельных провайдеров.
        #
        # НО считается она на ОРГАНИЗАЦИЮ, а не на ключ (их документация,
        # раздел rate-limits). Два ключа из одного кабинета ёмкости не
        # удваивают — для этого нужен отдельный аккаунт.
        #
        # Цифры — по их документации https://console.groq.com/docs/rate-limits
        # Поля: req_day — запросов в сутки, tok_min и tok_day — токенов в
        # минуту и в сутки, ctx — окно.
        "models": {
            "openai/gpt-oss-20b":          {"req_day": 1000,  "tok_min": 8000,  "tok_day": 200000, "ctx": 131072},
            "openai/gpt-oss-120b":         {"req_day": 1000,  "tok_min": 8000,  "tok_day": 200000, "ctx": 131072},
            # Единственная зрячая из бесплатных. На неё же уходит запрос с
            # картинкой, какая бы модель ни была выбрана (``VISION_MODEL``).
            "qwen/qwen3.6-27b":            {"req_day": 1000,  "tok_min": 8000,  "tok_day": 200000, "ctx": 131072, "vision": True},
            # compound — не модель, а их агент с поиском и запуском кода.
            "groq/compound":               {"req_day": 250,   "tok_min": 70000, "ctx": 131072},
            "groq/compound-mini":          {"req_day": 250,   "tok_min": 70000, "ctx": 131072},
            # Арабская, 4k контекста. В общий выбор не идёт как экзотика.
            "allam-2-7b":                  {"req_day": 7000,  "tok_min": 6000,  "ctx": 4096, "chat": False},
            "whisper-large-v3":            {"req_day": 200,   "audio": True, "chat": False},
            "whisper-large-v3-turbo":      {"req_day": 2000,  "audio": True, "chat": False},
            # Orpheus TTS: условия приняты не на всех ключах — сервер
            # перебирает пул и берёт рабочий.
            "canopylabs/orpheus-v1-english": {"req_day": 100, "tok_min": 1200, "tts": True, "chat": False},
            "canopylabs/orpheus-arabic-saudi": {"req_day": 100, "tok_min": 1200, "tts": True, "chat": False},
        },
    },
    "openrouter": {
        "base_url":       "https://openrouter.ai/api/v1",
        "model":          "openrouter/free",
        "model_quality":  "openrouter/free",
        "label":          "OpenRouter",
        "daily_limit":    0,
    },
    "chatgpt": {
        # Ключ = кука __Secure-next-auth.session-token. Может быть разбита
        # на .0 + .1 — склеить через |.
        "model":          "auto",
        "model_quality":  "auto",
        "label":          "ChatGPT",
        "daily_limit":    0,
        "web":            True,
    },
    "gemini_web": {
        # Flash намеренно: Pro у бесплатного аккаунта режется до ~5 запросов
        # в день, на объём он не годится.
        "model":          "gemini-3-flash",
        "model_quality":  "gemini-3-flash",
        "label":          "Gemini (Web)",
        "daily_limit":    0,
        "web":            True,
    },
    "qwen": {
        # chat.qwenlm.ai — тот же бэкенд, что chat.qwen.ai, но без антибота TMD.
        "model":          "qwen3.8-max",
        "model_quality":  "qwen3.8-max",
        "label":          "Qwen",
        "daily_limit":    0,
        "web":            True,
    },
    "kimi": {
        "model":          "k2.6",
        "model_quality":  "k2.6",
        "label":          "Kimi",
        "daily_limit":    0,
        "web":            True,
        "models": {"k2.5": {}, "k2.6": {}},
    },
    "perplexity": {
        "model":          "turbo",
        "model_quality":  "turbo",
        "label":          "Perplexity",
        "daily_limit":    0,
        "web":            True,
    },
    "ms_copilot": {
        # Мобильный API Android-приложения, device code flow. Ключ не нужен.
        "model":          "smart",
        "model_quality":  "smart",
        "label":          "MS Copilot",
        "daily_limit":    0,
        "web":            True,
        "no_key":         True,
        # Только smart. Старые стили Bing (balanced/creative/precise) Microsoft
        # убрал — сервис отвечает на них invalid-event за 0 секунд. Режимы
        # reasoning/researcher/research включаются ФЛАГАМИ (thinking/
        # web_search/deep_research), а не выбором модели.
        "models": {"smart": {}},
    },
    "venice": {
        "model":          "venice-uncensored-1-2",
        "model_quality":  "venice-uncensored-1-2",
        "label":          "Venice",
        "daily_limit":    10,
        "web":            True,
    },
    "yqcloud": {
        # Витрина chat9.yqcloud.top, отвечает api.binjie.fun. Модель заявлена
        # как gpt-4; проверить это нечем, поэтому имя берём как есть.
        "model":          "gpt-4",
        "model_quality":  "gpt-4",
        "label":          "Yqcloud",
        "daily_limit":    0,
        "web":            True,
        "no_key":         True,
    },
    "pollinations": {
        # Только картинки: их текстовый API отдаёт 402 уже на втором
        # запросе и объявлен устаревающим.
        "model":          "flux",
        "model_quality":  "flux",
        "label":          "Pollinations",
        "daily_limit":    0,
        "web":            True,
        "no_key":         True,
        "images_only":    True,
    },
    "openai_fm": {
        "model":          "coral",
        "model_quality":  "coral",
        "label":          "OpenAI.fm (озвучка)",
        "daily_limit":    0,
        "web":            True,
        "no_key":         True,
    },
    "opera_aria": {
        "model":          "aria",
        "model_quality":  "aria",
        "label":          "Opera Aria",
        # Сервис сам сообщает остаток в потоке: 200 на свежую регистрацию.
        "daily_limit":    200,
        "web":            True,
        "no_key":         True,
    },
    "claude_web": {
        "model":          "claude-sonnet-5",
        "model_quality":  "claude-sonnet-5",
        "label":          "Claude Web",
        "daily_limit":    0,
        "web":            True,
        "models": {
            "claude-sonnet-5":  {},
            "claude-sonnet-4-6": {},
            "claude-haiku-4-5": {},
        },
    },
    "cloudflare": {
        "base_url":       "https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1",
        "model":          "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "model_quality":  "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "label":          "Cloudflare",
        "daily_limit":    1300,
        "models": {
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast":     {},
            "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b": {},
            "@cf/qwen/qwen2.5-coder-32b-instruct":          {},
        },
    },
    "cohere": {
        "base_url":       "https://api.cohere.ai/compatibility/v1",
        "model":          "command-a-03-2025",
        "model_quality":  "command-a-plus-05-2026",
        "label":          "Cohere",
        # Тысяча вызовов в МЕСЯЦ, не в сутки: суточного предела у них нет,
        # и выставлять его тут значило бы врать счётчику.
        "daily_limit":    0,
        "models": {
            "command-a-03-2025":         {},
            "command-a-plus-05-2026":    {},
            "command-a-reasoning-08-2025": {},
            "command-r7b-12-2024":       {},
            "command-a-vision-07-2025":  {},
        },
    },
    "llm7": {
        "base_url":       "https://api.llm7.io/v1",
        "model":          "gpt-oss:20b",
        "model_quality":  "gemma4:31b",
        "label":          "LLM7",
        "daily_limit":    0,
        # Ключа нет ВООБЩЕ — ни регистрации, ни почты. В пуле это делает
        # его последней опорой: протухнуть тут нечему.
        "no_key":         True,
        "models": {
            "gpt-oss:20b":   {},
            "gemma4:31b":    {},
            "minimax-m2.7":  {},
        },
    },
    "agentrouter": {
        # ВНИМАНИЕ: протокол ANTHROPIC (/v1/messages), не OpenAI. База БЕЗ
        # /v1 — адаптер добавляет /v1/messages сам. Их OpenAI-путь закрыт
        # (пустой 402).
        "base_url":       "https://agentrouter.org",
        # gpt-5.6-sol сидит на budget pool с остатком и работает; opus-модели
        # токену выданы, но их общий пул часто исчерпан (402). Дефолт — на
        # рабочую.
        "model":          "gpt-5.6-sol",
        "model_quality":  "gpt-5.6-sol",
        "label":          "AgentRouter",
        "daily_limit":    0,
        # Релей к фронтир-моделям: тут выбор и есть смысл провайдера.
        "models": {
            "gpt-5.6-sol":      {},
            "claude-opus-4-8":  {},
            "claude-opus-5":    {},
        },
    },
    "gemini_api": {
        # Плавающие алиасы, а не прибитое поколение: конкретный
        # gemini-2.5-flash однажды стал недоступен новым ключам, и провайдер
        # отвалился с 404 на каждом запросе.
        "model":          "gemini-flash-lite-latest",
        "model_quality":  "gemini-flash-latest",
        "label":          "Gemini (API)",
        "daily_limit":    0,
        "models": {
            "gemini-flash-lite-latest": {},
            "gemini-flash-latest":      {},
        },
    },
    "zai": {
        # GLM-5.1 — то, чем отвечает их собственная страница, работает и
        # текстом, и файлами. glm-4.7 тоже жив и оставлен на выбор.
        # Прочие имена сервис принимает и молча отдаёт пустоту или 500.
        "model":          "GLM-5.1",
        "model_quality":  "GLM-5.1",
        "models":         {"GLM-5.1": {}, "glm-4.7": {}},
        "label":          "Z.ai",
        "daily_limit":    0,
        "web":            True,
        # no_key здесь НЕ ставим: у chat.z.ai обычный вход есть. С этим
        # флагом open_provider подставил бы пустой доступ, и сохранённый
        # токен до адаптера не дошёл бы вовсе — сервис отвечал бы, но
        # гостем, а гостю, например, запрещена загрузка файлов.
    },
    "meta_ai": {
        "model":          "meta-ai",
        "model_quality":  "meta-ai",
        "label":          "Meta AI",
        "daily_limit":    0,
        "web":            True,
        "no_key":         True,
    },
    "deepseek": {
        "model":          "deepseek-chat",
        "model_quality":  "deepseek-chat",
        "label":          "DeepSeek",
        "daily_limit":    0,
        "web":            True,
        "models": {"deepseek-chat": {}, "deepseek-reasoner": {}},
    },
    "grok": {
        "model":          "fast",
        "model_quality":  "fast",
        "label":          "Grok",
        "daily_limit":    0,
        "web":            True,
        "models": {"fast": {}, "default": {}},
    },
    "alice": {
        "model":          "yandexgpt-pro",
        "model_quality":  "yandexgpt-pro",
        "label":          "Алиса",
        "daily_limit":    0,
        "web":            True,
    },
    "mistral": {
        "model":          "mistral",
        "model_quality":  "mistral",
        "label":          "Mistral",
        "daily_limit":    0,
        "web":            True,
    },
    "deepai": {
        "model":          "standard",
        "model_quality":  "standard",
        "label":          "DeepAI",
        "daily_limit":    0,
        "web":            True,
    },
    "pi": {
        "model":          "pi",
        "model_quality":  "pi",
        "label":          "Pi",
        "daily_limit":    0,
        "web":            True,
    },
    "manus": {
        # Не чат-модель, а облачный агент: каждое сообщение поднимает
        # виртуальный компьютер. Медленный и на кредитах.
        "model":          "manus",
        "model_quality":  "manus",
        "label":          "Manus",
        "daily_limit":    0,
        "web":            True,
    },
    "poe": {
        "model":          "Assistant",
        "model_quality":  "Assistant",
        "label":          "Poe",
        "daily_limit":    0,
        "web":            True,
    },
    "bing_images": {
        "model":          "MAI-Image-2.5-Flash",
        "model_quality":  "MAI-Image-2.5-Flash",
        "label":          "Bing Images",
        "daily_limit":    0,
        "web":            True,
        "images_only":    True,
    },
}


# ── Измерения ─────────────────────────────────────────────────────────

# Характеристики, снятые на живых сервисах, лежат отдельно и в JSON:
# они протухают, и обновлять их должна канарейка, а не человек правкой
# кода. Здесь только переэкспорт, чтобы не заставлять всех знать про два
# модуля сразу.

from foxroute.measurements import (          # noqa: E402  (после конфигов)
    AGENTIC_GOOD,
    AGENTIC_NO,
    AGENTIC_SHORT,
    AGENTIC_UNKNOWN,
    Measured,
    get as measured,
    measured_at,
)

# ── Производные сведения ──────────────────────────────────────────────

AUTH_KEY = "key"        # строка в хранилище учёток
AUTH_FILE = "file"      # отдельные файлы (meta_*.json, copilot_tokens.json)
AUTH_NONE = "none"      # не требует ничего
AUTH_OPTIONAL = "optional"  # работает и без доступа, но урезанно

#: Отвечают и без авторизации, просто хуже: лимиты ниже, памяти чатов нет.
#: Важно не отвергать запрос из-за отсутствия ключа — это отняло бы рабочий,
#: пусть и урезанный, режим. И важно не считать «ответил без ключа» признаком
#: поломки при диагностике.
#:
#: Отвечают даже с заведомо негодным доступом. У DeepAI это прямо следует
#: из протокола — ключ ``tryit-…`` считается на нашей стороне, а кука лишь
#: привязывает бесплатные кредиты к аккаунту.
KEY_OPTIONAL = {"alice", "perplexity", "deepai", "pi", "zai"}

#: Отвечают даже на ОТВЕРГНУТЫЙ доступ, молча свалившись в гостевой режим.
#:
#: Отличие от KEY_OPTIONAL тонкое, но важное. Там анонимный режим — законный
#: выбор, который включают сознательно. Здесь же ключ ЕСТЬ, он просто не
#: принят, а сервис вместо отказа тихо обслуживает как гостя.
#:
#: Практическое следствие: «ответил» перестаёт быть доказательством того, что
#: аккаунт жив. Канарейке нужен другой признак — у Gemini Web наличие кеша
#: кук, у Mistral то, что гостю отвечают ровно на одно сообщение, а дальше
#: молчат. С испорченным ключом оба отвечают как ни в чём не бывало.
SILENT_DEGRADE = {"gemini_web", "mistral"}

#: Провайдеры, авторизующиеся файлами, и какими именно.
AUTH_FILES = {
    "ms_copilot": ["copilot_tokens.json"],
    "meta_ai":    ["meta_cookies.json", "meta_proto_template.json",
                   "meta_ws_auth.json"],
}

#: Дополнительное состояние доступа, которое обязано ПЕРЕЕЗЖАТЬ ВМЕСТЕ С КЛЮЧОМ.
#:
#: Ключ в настройках — не весь доступ. У Gemini Web рабочая
#: ``__Secure-1PSIDTS`` живёт только в кеше библиотеки, и без него запрос
#: уходит анонимно, потратив ~180 секунд на попытки входа. Со стороны это
#: выглядит как протухшая сессия, хотя сессия жива.
AUTH_STATE_DIRS = {
    "gemini_web": ["cache/gemini_web"],
}

#: Сами обновляют свой токен на диске. Читать/писать только под блокировкой:
#: два потребителя одного файла ломают друг друга — один получает свежий
#: токен, у второго остаётся протухший.
SELF_REFRESHING = {"kimi", "ms_copilot"}


# ── Где человеку взять доступ ─────────────────────────────────────────
#
# Сведения собраны с живых сервисов. Человеку, добавляющему учётку, нужно
# видеть домен, имя куки и тонкости прямо в форме, а не искать их в
# исходниках.
#
# Поля намеренно раздельные, а не одна строка текста: ``site`` идёт ссылкой,
# ``what`` подставляется в подсказку поля ввода, ``note`` показывается только
# когда есть. Собирать их обратно в предложение — забота интерфейса.

#: Откуда берётся значение: кука браузера, localStorage, консоль сервиса.
FROM_COOKIE = "cookie"
FROM_STORAGE = "storage"
FROM_CONSOLE = "console"

ACCESS_HINTS = {
    # ── веб-сессии по кукам ──
    "chatgpt": {
        "site": "https://chatgpt.com", "source": FROM_COOKIE,
        "what": "кука __Secure-next-auth.session-token",
        "note": "Может быть разбита на .0 и .1 — склей их через | в одну "
                "строку, по порядку.",
    },
    "gemini_web": {
        "site": "https://gemini.google.com", "source": FROM_COOKIE,
        "what": "куки __Secure-1PSID и __Secure-1PSIDTS через |",
        "note": "Вторая нужна не всем аккаунтам, но с ней стабильнее.",
    },
    "qwen": {
        "site": "https://chat.qwen.ai", "source": FROM_STORAGE,
        "what": "поле token из localStorage (JWT целиком)",
        "note": "Сам не обновляется — срок зашит внутрь токена.",
    },
    "deepseek": {
        "site": "https://chat.deepseek.com", "source": FROM_STORAGE,
        "what": "поле userToken из localStorage",
    },
    "kimi": {
        "site": "https://www.kimi.com", "source": FROM_COOKIE,
        "what": "кука refresh_token",
        "note": "Живёт месяцами. Сервис выдаёт новый токен в ответах, "
                "хранилище подхватывает его само.",
    },
    "alice": {
        "site": "https://alice.yandex.ru", "source": FROM_COOKIE,
        "what": "кука Session_id (домен .yandex.ru)",
        "note": "Без неё Алиса отвечает анонимно: лимиты ниже, память чатов "
                "не работает.",
    },
    "grok": {
        "site": "https://grok.com", "source": FROM_COOKIE,
        "what": "куки sso и sso-rw через |",
    },
    "pi": {
        "site": "https://pi.ai", "source": FROM_COOKIE,
        "what": "куки строкой «a=b; c=d», обязательна __Host-session",
    },
    "poe": {
        "site": "https://poe.com", "source": FROM_COOKIE,
        "what": "куки строкой «a=b; c=d», обязательна p-b",
    },
    "perplexity": {
        "site": "https://www.perplexity.ai", "source": FROM_COOKIE,
        "what": "кука __Secure-next-auth.session-token",
        "note": "Работает и без неё, просто с меньшими лимитами.",
    },
    "manus": {
        "site": "https://manus.im", "source": FROM_COOKIE,
        "what": "кука session_id",
    },
    "venice": {
        "site": "https://venice.ai", "source": FROM_COOKIE,
        "what": "кука __session (или весь набор кук объектом JSON)",
        "note": "Бесплатно 10 текстовых запросов в день.",
    },
    "mistral": {
        "site": "https://chat.mistral.ai", "source": FROM_COOKIE,
        "what": "куки строкой «a=b; c=d», нужна в том числе csrftoken",
    },
    "bing_images": {
        "site": "https://www.bing.com/images/create", "source": FROM_COOKIE,
        "what": "куки строкой «a=b; c=d», обязательна _U",
    },
    "deepai": {
        "site": "https://deepai.org", "source": FROM_COOKIE,
        "what": "куки строкой «a=b; c=d»",
        "note": "Работает и без них — кука лишь привязывает бесплатные "
                "кредиты к аккаунту.",
    },
    # ── ключи официальных API ──
    "groq": {
        "site": "https://console.groq.com/keys", "source": FROM_CONSOLE,
        "what": "API-ключ",
        "note": "У каждой модели своя квота, они не пересекаются.",
    },
    "openrouter": {
        "site": "https://openrouter.ai/keys", "source": FROM_CONSOLE,
        "what": "API-ключ",
    },
    "gemini_api": {
        "site": "https://aistudio.google.com/apikey", "source": FROM_CONSOLE,
        "what": "API-ключ",
    },
    "agentrouter": {
        "site": "https://agentrouter.org", "source": FROM_CONSOLE,
        "what": "API-ключ",
    },
    "claude_web": {
        "site": "https://claude.ai", "source": FROM_COOKIE,
        "what": "вся строка кук (sessionKey обязательна, "
                "cf_clearance и __cf_bm тоже — бери из Network → Headers)",
    },
    "cloudflare": {
        "site": "https://dash.cloudflare.com/profile/api-tokens",
        "source": FROM_CONSOLE,
        "what": "API-токен Workers AI (шаблон «Workers AI»)",
    },
    "cohere": {
        "site": "https://dashboard.cohere.com/api-keys", "source": FROM_CONSOLE,
        "what": "пробный ключ (Trial) — бесплатный, 1000 вызовов в месяц",
    },
    "llm7": {
        "site": "https://api.llm7.io", "source": FROM_CONSOLE,
        "what": "ничего не нужно",
    },
    # ── особые случаи ──
    "yqcloud": {
        "site": "https://chat9.yqcloud.top", "source": FROM_CONSOLE,
        "what": "ничего не нужно",
        "note": "Работает анонимно, без ключа и кук.",
    },
    "pollinations": {
        "site": "https://pollinations.ai", "source": FROM_CONSOLE,
        "what": "ничего не нужно",
        "note": "Только картинки, анонимно. Текст у них платный.",
    },
    "opera_aria": {
        "site": "https://www.opera.com/features/aria",
        "source": FROM_CONSOLE, "what": "ничего не нужно",
        "note": "Регистрируется сам, анонимно. 200 запросов на аккаунт, "
                "остаток сервис сообщает в ответе.",
    },
    "openai_fm": {
        "site": "https://www.openai.fm", "source": FROM_CONSOLE,
        "what": "ничего не нужно",
        "note": "Озвучка одиннадцатью голосами, анонимно и без суточной "
                "нормы — в отличие от Orpheus на Groq.",
    },
    "zai": {
        "site": "https://chat.z.ai", "source": FROM_STORAGE,
        "what": "token из localStorage",
        "note": "Ответит и анонимно, но урезанно: гостю запрещена загрузка "
                "файлов. Ещё нужен сценарий обхода капчи (zai_captcha.js) "
                "в каталоге данных.",
    },
    "ms_copilot": {
        "site": "https://copilot.microsoft.com", "source": FROM_CONSOLE,
        "what": "вход по коду устройства, как в мобильном приложении",
        "note": "Токены лежат файлом copilot_tokens.json и обновляются сами.",
    },
    "meta_ai": {
        "site": "https://www.meta.ai", "source": FROM_CONSOLE,
        "what": "файлы meta_cookies.json, meta_proto_template.json, "
                "meta_ws_auth.json",
        "note": "Шаблон запроса снимается один раз с живого клиента — без "
                "него запрос собрать нельзя.",
    },
}


#: Насколько доступ привязан к IP-адресу — для подсказки в «Доступах».
#: Определяется механизмом авторизации, а не догадкой:
#:   good — ключ/токен API или аноним: адрес не проверяется, работает откуда
#:          угодно;
#:   warn — сессионный токен/JWT: к IP не привязан, переезд обычно переживает,
#:          но анти-фрод сервиса может насторожиться при смене СТРАНЫ;
#:   crit — кука cf_clearance (Cloudflare, считается по IP+отпечатку) или
#:          строгий гео-анти-фрод Google/Яндекс/Meta: снимать надо ТАМ ЖЕ, где
#:          доступ будет работать.
IP_RISK = {
    "claude_web": "crit", "gemini_web": "crit", "alice": "crit",
    "mistral": "crit", "venice": "crit", "meta_ai": "crit",
    "chatgpt": "warn", "qwen": "warn", "deepseek": "warn", "kimi": "warn",
    "grok": "warn", "perplexity": "warn", "manus": "warn", "poe": "warn",
    "pi": "warn", "deepai": "warn", "bing_images": "warn", "ms_copilot": "warn",
}


def ip_risk(name: str) -> str:
    """Привязка доступа к IP: good | warn | crit. Умолчание good —
    провайдеры без куки (ключ API, аноним) адреса не проверяют."""
    return IP_RISK.get(name, "good")


def access_hint(name: str) -> dict:
    """Как человеку добыть доступ к провайдеру.

    Пустой словарь означает «подсказки нет», а не «доступ не нужен»: за
    последнее отвечает ``auth_kind``.
    """
    return ACCESS_HINTS.get(name, {})

def is_api(name: str) -> bool:
    """Официальный API по ключу, а не веб-сессия по кукам.

    Разница не в вежливости названия. У ключа нет аккаунта в том же смысле:
    это кошелёк с собственной квотой, он не обновляет себя сам, ему не нужен
    отдельный прокси, и параллельные запросы им допустимы. Отсюда и разное
    обращение с пулом.
    """
    return not PROVIDER_CONFIGS.get(name, {}).get("web", False)


def splits_pool(name: str) -> bool:
    """У кого символ ``|`` разделяет НЕСКОЛЬКО ключей, а не склеивает один.

    Различие принципиальное и неочевидное. У веб-сессий ``|`` наоборот
    СКЛЕИВАЕТ части одного доступа: у ChatGPT браузер режет длинную куку на
    ``.0`` и ``.1``, у Gemini Web это две разные куки (``__Secure-1PSID`` и
    ``__Secure-1PSIDTS``), у Grok — ``sso`` и ``sso-rw``. Перепутать значит
    либо потерять половину доступа, либо принять пул за один битый ключ,
    который даёт 401, неотличимый от протухшего.

    У официальных API таких составных доступов не бывает: ключ там —
    непрозрачная строка без разделителей (``gsk_…``, ``AIza…``, ``sk-…``).
    Поэтому правило выводится из природы провайдера, а не перечисляется
    списком: добавить новый API-сервис и забыть внести его в список нельзя.
    """
    return is_api(name)


#: Совместимость: оставлено, чтобы существующие проверки продолжали
#: читаться, но опираться следует на splits_pool().
MULTI_KEY = {name for name in PROVIDER_CONFIGS if is_api(name)}

#: Отдают одноразовый токен, который нельзя кешировать между запросами.
#: Симптом при ошибке обманчив: «работал, потом резко отвалился, наверное
#: куки протухли», хотя на деле кеш отдавал уже использованный токен.
ONE_SHOT_TOKEN = {"chatgpt", "zai", "ms_copilot"}


def config(name: str) -> dict:
    return PROVIDER_CONFIGS.get(name, {})


def auth_kind(name: str) -> str:
    cfg = PROVIDER_CONFIGS.get(name, {})
    if name in AUTH_FILES:
        return AUTH_FILE
    if cfg.get("no_key"):
        return AUTH_NONE
    if name in KEY_OPTIONAL:
        return AUTH_OPTIONAL
    return AUTH_KEY


def all_names() -> list[str]:
    return list(PROVIDER_CONFIGS)


def text_names() -> list[str]:
    """Все, кроме рисовальщиков."""
    return [n for n, c in PROVIDER_CONFIGS.items() if not c.get("images_only")]


def web_names() -> list[str]:
    """Только веб-сессии — то, ради чего всё затевалось."""
    return [n for n, c in PROVIDER_CONFIGS.items()
            if c.get("web") and not c.get("images_only")]


def agentic_names(min_level: str = AGENTIC_GOOD) -> list[str]:
    """Кого можно пускать в агентный цикл."""
    levels = {AGENTIC_GOOD: [AGENTIC_GOOD],
              AGENTIC_SHORT: [AGENTIC_GOOD, AGENTIC_SHORT]}
    allowed = levels.get(min_level, [AGENTIC_GOOD])
    return [n for n in text_names() if measured(n).agentic in allowed]
