# Providers

The pool: cookie-based web sessions (the core), official key-based APIs, and
llm7, which works with no credentials at all. Capabilities vary, and the
gateway knows it — the interface hides what a provider doesn't do, and the
API rejects such a request before ever calling the service.

The table is taken from a live `GET /api/status` (which always has the
current numbers). Context is in characters, turn is the median in seconds.
Context is measured in two passes — by accepting the input and with a
"needle" (verifying the input arrived intact): `bench/measure.py` and
`bench/verify_context.py`.

| provider | kind | thinking | search | research | file in | file out | draws | context | turn |
|---|---|---|---|---|---|---|---|---|---|
| alice | web | yes | — | — | yes | — | yes | 48,000 | 4.3 |
| chatgpt | web | yes | yes | yes | yes | **yes** | yes | 48,000 | 7.3 |
| claude_web | web | yes | yes | — | yes | **yes** | — | 200,000 | 4.7 |
| deepai | web | — | — | — | — | — | yes | 200,000 | 2.2 |
| deepseek | web | yes | yes | — | yes | — | — | 200,000 | 2.7 |
| gemini_web | web | yes | — | — | yes | **yes** | yes | 200,000 | 12.5 |
| grok | web | — | yes | yes | yes | — | yes | 200,000 | 8.5 |
| kimi | web | yes | — | — | yes | — | — | 200,000 | 10.9 |
| manus | web | — | — | — | yes | — | — | 200,000 | 12.1 |
| meta_ai | web | — | — | — | yes | — | yes | 200,000 | 8.5 |
| mistral | web | — | — | — | yes | — | — | 200,000 | 4.8 |
| ms_copilot | web | yes | yes | yes | yes | — | yes | 4,000 | 5.1 |
| opera_aria | web | — | — | — | yes | — | — | 48,000 | 7.5 |
| perplexity | web | — | yes | — | — | — | — | 200,000 | 4.6 |
| pi | web | — | — | — | — | — | — | 16,000 | 4.2 |
| poe | web | — | — | — | yes | — | — | 16,000 | 2.5 |
| qwen | web | yes | — | yes | yes | — | yes | 200,000 | 4.0 |
| venice | web | — | yes | — | — | — | — | 16,000 | 4.1 |
| yqcloud | web | — | — | — | — | — | — | 4,000 | 10.3 |
| zai | web | yes | — | — | yes | — | — | 200,000 | 6.1 |
| cloudflare | api | — | — | — | — | — | — | 48,000 | 2.4 |
| cohere | api | — | — | — | yes | — | — | 16,000 | 2.6 |
| llm7 | api | — | — | — | — | — | — | 16,000 | 3.3 |
| gemini_api | api | — | — | — | yes | — | — | 200,000 | 1.0 |
| groq | api | — | — | — | yes | — | — | 16,000 | 0.4 |
| openrouter | api | — | — | — | yes | — | — | 100,000 | 11.8 |
| agentrouter | api | — | — | — | yes | — | — | 200,000 | 6.6 |

Three specialists aren't in this text table: **bing_images** and
**pollinations** (image generation only) and **openai_fm** (speech only).
They show up in the interface's "draws"/"voice" bars. Counting them, the
startup banner reports 30 providers in all.

## Why some cells are dashes

A dash means "verified absent," not "never got to it." Each was
investigated separately, and the method is always the same: **look at the
button in the service's own interface**, not at the answer text. "Knows
today's date" is a worthless sign of search: the date is placed in the
system prompt.

**Reasoning** is built differently everywhere, and that's worth keeping in
mind:

* a flag with a separate stream of thoughts — deepseek, qwen, zai, kimi;
* a separate **model** — chatgpt (`gpt-5-4-t-mini`), gemini_web
  (`gemini-3-flash-thinking`); no thought stream, but it thinks longer;
* a separate **mode** — ms_copilot (`reasoning`, their "Think Deeper");
* a field in the frame **plus a handshake** — alice.

Not claimed: **grok** (it's their Expert mode, which is paid), **venice**
(the only free model can't), **opera_aria** (accepts the flag, behavior
doesn't change), **perplexity** (nothing to confirm it with).

**Search** is not claimed for **kimi** (the menu only has Auto/Off — no
controllable toggle), **qwen** (we go through a mirror, which ignores
search), **zai** (the flag changes nothing), **alice** (there's no button at
all).

**Research** is available on four. It's expensive: Copilot gets five runs
**per month**, Qwen thinks for 310 seconds. Kimi has the button, but it runs
into a subscription offer.

**File output** is only on two, and that's not an oversight. It needs a
sandbox that hands the file out: ChatGPT has one, Gemini has media. Qwen
genuinely executes code but won't release the file from the sandbox — its
"download" button just saves the code block. Venice's request schema has no
attachment fields at all, and Perplexity's free account is set to
`upload_limit: 0`.

## Quirks worth knowing up front

**meta_ai** — a reasoning model that burns through hundreds of frames, so it
can be slow. Don't keep it as your primary.

**manus** — not a chat model but an agent: every message spins up a virtual
computer, so it's slow and costs credits (8–16 out of three hundred per
day). Its role: backup.

**deepseek** reads documents but not images: their pipeline is text-only, a
PNG reaches the end of parsing and gets `CONTENT_EMPTY`.

**kimi** — the opposite: documents yes, images no.

**alice** — images yes, documents no.

**groq** — quotas are counted per model, but the allowance is shared **per
organization**: two keys from the same account don't double capacity.
Exactly one free model can see, and the gateway substitutes it automatically
for images.

**zai** requires solving a CAPTCHA with a Node script — it needs
`puppeteer-core`, and the path is set via `FOXROUTE_NODE_PATH`.

**cohere** — the trial key is free, no card needed. The service reports the
quota in headers: 1000 calls per month, 20 per minute. The sighted model is
separate and is substituted automatically for images.

**llm7** — the only one that needs NOTHING: no key, no registration, no
email. Three free models with a large window (`gpt-oss:20b` 128k,
`gemma4:31b` 262k, `minimax-m2.7` 180k); the other 31 are paid and answer
`model_unavailable`. In the pool it's valuable not for power but because it
has nothing to go stale: the last resort when every session is paused.

## How a provider is chosen for `auto`

The router looks at three things: who can do what's asked (capabilities,
context size), who isn't currently paused, and who's faster. On failure it
moves to the next one down the chain. Paid providers aren't taken into
"auto" — they have to be named explicitly.
