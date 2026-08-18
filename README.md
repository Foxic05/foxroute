# foxroute

An OpenAI-compatible gateway on top of free web sessions and chat-service APIs.
One command — 30 providers instead of a dozen separate protocols.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8777/v1", api_key="…")
client.chat.completions.create(model="auto", messages=[...])
```

**Documentation:** [API](docs/api.md) · [providers](docs/providers.md) ·
[getting credentials](docs/credentials.md) · [operations](docs/operations.md)

> This project started as something for personal use, and it's written
> entirely by Claude Code. It leans on other people's repositories — some of
> which no longer worked — and a handful of providers were reverse-engineered
> by hand. The main thing is that this AI slop actually works, and I hope it
> turns out useful to others too.

Point any OpenAI-compatible client at it and reach ChatGPT, Claude, Gemini,
DeepSeek, Grok and two dozen more — through your own free web logins, or the
providers that need no login at all. When one runs out of quota, the gateway
moves to the next on its own.

A single OpenAI interface hides the fact that every service does it its own
way: one speaks plain SSE, another a binary WebSocket with a home-grown
protobuf, a third a reverse-engineered request signature. The caller knows
none of this — it picks a `model` (or `auto`), and the gateway finds a live
provider, tracks quota, and normalizes the stream.

## Install

Python 3.10+. Install the dependencies:

```bash
pip install -r requirements.txt
```

The example client also needs the OpenAI SDK: `pip install openai`.

## Running

```bash
python -m foxroute.server --host 127.0.0.1 --port 8777
```

The web UI opens at the same address. The programmatic API is protected by a
key (`Authorization: Bearer`, generated on first run); the interface behind a
tunnel works without one. No database, no Docker — all you need are
credentials to the services, and you add them right on the page. The UI has a
dark/light theme and a RU/EN language toggle in the header.

Several providers answer with no login at all (llm7, Pi, Perplexity, Yqcloud…),
so you can start chatting the moment the server is up; add accounts for the
rest — the pool marks who still needs one.

Next: [getting credentials](docs/credentials.md) and
[operations](docs/operations.md).

## Web interface

The gateway ships with a web UI at the same address — no separate app to run.
From the browser you can:

- **chat** with any provider (or `auto`, which picks a live one for you), with
  real streaming, code highlighting, and message actions;
- **add credentials** right on the page — including one-click *Sign in via
  browser*, which grabs the cookies for you;
- watch the **provider pool** live: who's ready, who's paused, plus context
  size and speed per provider;
- **generate images** and have answers **read aloud** (text-to-speech);
- switch **theme** (dark/light) and **language** (RU / EN).

## Providers

Around thirty in all.

**Chat / text** — ChatGPT, Claude, Gemini, DeepSeek, Grok, Qwen, Kimi,
Perplexity, Mistral, Meta AI, MS Copilot, Alice (YandexGPT), Z.ai, Pi, Poe,
Manus, Venice, Opera Aria, DeepAI, Yqcloud, plus the key-based APIs Groq,
Gemini API, Cohere, OpenRouter, Cloudflare Workers AI, AgentRouter and LLM7.

**Image generation** — Qwen, ChatGPT, Gemini, Grok, Alice, Meta AI, MS Copilot
and DeepAI, plus the dedicated Bing Images and Pollinations.

**Voice** — text-to-speech through OpenAI.fm (eleven voices, no daily limit) and
Groq / Orpheus; speech-to-text through Groq Whisper.

The full table — reasoning, web search, deep research, context window and speed
for each — is in [docs/providers.md](docs/providers.md).

## Capabilities

| capability | how many |
|---|---|
| streaming text | 22 |
| reasoning | 8 |
| web search | 6 |
| deep research | 4 |
| image/file input | 18 |
| file output | 2 |
| image generation | 8 |

A dash in the capability table means "verified absent," not "never got to
it": a provider that lacks a capability never receives the request — the
refusal comes back immediately, before spending a message from quota. The
full table is in [docs/providers.md](docs/providers.md).

## Design

```
foxroute/
  errors.py          typed failures
  registry.py        who's who, how each authenticates, what it spends
  measurements.py    provider characteristics (context window, speed)
  router.py          provider selection, fallback down the chain
  server.py          OpenAI-compatible HTTP layer
  providers/
    base.py          the contract: a stream of deltas
    _http.py         session, SSE parsing, response code → error type
    _async.py        bridge from WebSocket to the synchronous contract
    web/             cookie-based web sessions — the core of the pool
    api/             official key-based APIs
bench/               check adapters against live services
```

Web and API aren't split for looks — they behave differently. A web session
breaks without warning, counts quota in messages (and usually doesn't show
it), and handles one conversation at a time. An official API is stable,
reports what's left in headers, and handles concurrency, but the key is
finite. So the plumbing around them differs, while the contract is one and
the same.

Three decisions shape this layer:

**Errors as types, not text.** Failures are classified by exception type
(`RateLimited`, `AuthError`, `ContextTooLarge` …) rather than by
substring-matching the text — a type won't drift when a service rewords its
message.

**A stream of deltas is the base primitive.** Every service streams under
the hood; assembling the answer into a string inside the adapter would mean
losing `stream=true` up top. Normalizing to deltas is the adapter's job
(some services send cumulative text, which you can't stitch together by
concatenation).

**An adapter does protocol only.** No statistics, no daily limits, no
logging — so a provider can be tested and reused on its own.

## Screenshots

The web interface — the chat, the live provider pool, and adding access:

<!-- drop the screenshots here -->

## Disclaimer

A project for personal and research use. The gateway reaches services using
your account — respect their terms of use.

Licensed under **AGPL-3.0** — see [LICENSE](LICENSE) and [CREDITS](CREDITS.md).
