# API

The gateway impersonates the OpenAI API. Any client that can talk to OpenAI
works with it unchanged — only the address changes.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8777/v1", api_key="fox_…")
```

**A key is required for programmatic clients** — `Authorization: Bearer
<key>`, otherwise `401`. Your own interface behind a tunnel works without a
key (the gateway recognizes it by `Sec-Fetch-Site: same-origin`). The key is
generated on first run: printed to the log and stored in `settings.json`.

## Choosing a provider

The provider name goes in place of the model.

```python
client.chat.completions.create(model="chatgpt", messages=[...])
client.chat.completions.create(model="auto",    messages=[...])
```

* `auto` — the router picks for you and moves to the next one on failure.
  Fits almost every case: it knows who's alive, who has quota left, and who
  can do what's asked.
* `name` — this provider only, no substitutions. The gateway won't override
  a person's explicit choice: the model may have been picked deliberately.
* `name/model` — a provider and a specific model within it, where such a
  choice exists (`groq/qwen/qwen3.6-27b`).

For the list: `GET /v1/models` or the `/` page.

## Text

A plain call, no different from OpenAI:

```python
answer = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Explain the difference between a process and a thread"}],
)
print(answer.choices[0].message.content)
```

Streaming:

```python
for chunk in client.chat.completions.create(model="auto", messages=[...], stream=True):
    piece = chunk.choices[0].delta.content or ""
    print(piece, end="", flush=True)
```

The stream is real: chunks arrive as the service responds, not in one burst
at the end. Where a service returns cumulative text (Kimi, Mistral), the
adapter turns it into deltas itself.

## Image and file INPUT

Just like OpenAI — as message parts:

```python
import base64, pathlib

raw = base64.b64encode(pathlib.Path("shema.png").read_bytes()).decode()

client.chat.completions.create(model="chatgpt", messages=[{
    "role": "user",
    "content": [
        {"type": "text", "text": "What's in the diagram?"},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{raw}"}},
    ],
}])
```

Recognized:

| part | source | accepts |
|---|---|---|
| `image_url` | Chat Completions | a `data:` string or an `http(s)` link |
| `input_image` | Responses API | same |
| `file` / `input_file` | files | `file_data` with a `data:` string, name in `filename` |

Links are downloaded by the gateway, with a ceiling of 32 MB per attachment.
Attachments are taken from the **last** user message: they belong to the
current turn, not the whole conversation.

Who accepts files: `GET /api/status`, field `can.files`. A provider that
can't never receives the request: the refusal comes immediately, before
spending a message from quota.

## Image OUTPUT

```python
picture = client.images.generate(model="auto", prompt="a fox in Scandinavian style",
                                 size="1024x1024", response_format="b64_json")
```

* `size` is translated into an aspect ratio (`1024x1024` → `1:1`,
  `1792x1024` → `16:9`, `1024x1792` → `9:16`);
* `response_format` — `url` (default) or `b64_json`;
* `n` — how many to return; the gateway won't invent more than were drawn;
* the caption the service attached to the image arrives in `revised_prompt`
  on the first one.

Draws: qwen, alice, ms_copilot, deepai, gemini_web, chatgpt, grok, meta_ai —
plus the dedicated image services **bing_images** and **pollinations**.

## File OUTPUT

Two can do it: **chatgpt** (whatever the model created in its code sandbox)
and **gemini_web** (generated images, video, audio).

The file arrives **inside the response text** — as a markdown link where
the content itself sits in place of the address:

```
[otchet.csv](data:text/csv;base64,0LjQvNGP...) — 2 KB
```

This is deliberate: the real address at the service only works with our
authorization and won't open from outside. The client just needs to pick
the `data:` links out of the response and decode them.

## Capabilities: reasoning, search, research

Three optional request fields. For `curl` they're plain body fields; for
`openai-python`, via `extra_body`:

```python
client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "What's new in quantum computing this week?"}],
    extra_body={"deep_research": True},
)
```

| field | what it does | who can |
|---|---|---|
| `thinking` | think longer before answering | 8 providers |
| `web_search` | search the web and ground on the results | 6 |
| `deep_research` | many searches in a row plus a summary with links | 4 |

The exact list is in `GET /api/status`. A provider that lacks a capability
isn't sent the flag: the request is rejected immediately.

**Research takes minutes** (measured: Qwen — 310 seconds), so the gateway
waits up to 15 minutes instead of the usual five. Clients should raise their
own timeout.

## Server-side conversations

Eight providers keep the context on their side. Then you don't have to send
the whole history — it's enough to pass the chat marker:

```python
answer = client.chat.completions.create(
    model="chatgpt",
    messages=[{"role": "user", "content": "tell me more?"}],
    extra_body={"conversation": {"chat_id": "…", "last_message_id": "…"}},
)
```

The gateway returns the markers in its response. With `auto` the
conversation isn't used: the next turn may go to a different provider, and
the chat belongs to another one.

## Other endpoints

| path | what it does |
|---|---|
| `GET /v1/models` | list of providers and models |
| `POST /v1/chat/completions` | text, streamed or whole |
| `POST /v1/images/generations` | image generation |
| `POST /v1/audio/speech` | text-to-speech |
| `POST /v1/audio/transcriptions` | speech transcription (Groq Whisper) |
| `GET /api/status` | pool state and each provider's capabilities — ours, not OpenAI's |

## Failures

Same shape as OpenAI: `{"error": {"message", "type", "code"}}`. The types
distinguish the cause, not just the code:

| code | type | what happened |
|---|---|---|
| 400 | `invalid_request` | malformed request body |
| 400 | `unsupported` | the provider can't do this |
| 401 | `auth_error` | credential rejected — refresh the cookie |
| 429 | `rate_limited` | quota exhausted |
| 502 | `provider_error` | the service returned a failure |

`unsupported` comes **before** any call to the service — asking Kimi to
parse an image doesn't spend a message from quota.

## Speech

Both directions are compatible with OpenAI's Audio API.

```python
# text-to-speech
speech = client.audio.speech.create(model="tts-1", voice="alloy",
                                    input="Hi, this is the little fox")

# transcribe
with open("zapis.mp3", "rb") as source:
    said = client.audio.transcriptions.create(
        model="whisper-large-v3-turbo", file=source, language="en")
print(said.text)
```

In the interface, transcription is the microphone button in the input
field: press it, dictate, press again — the text lands in the field. It's
appended to what's already typed rather than replacing it.

Verified round-trip: a phrase was voiced by our own `/v1/audio/speech` and
transcribed back through `/v1/audio/transcriptions` — it matched.
