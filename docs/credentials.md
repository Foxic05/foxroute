# Getting credentials

Web providers work from your account: the gateway reaches the service the
same way a browser would. So you need a piece of your session — a cookie or
a token.

To add a credential: the `/` page → **Credentials** (a button in the header,
next to the theme toggle) → pick a provider → paste the string.

## The easy way: sign in through the browser

Most cookie-based providers have a **"Sign in via browser"** button on the
Credentials page. It opens a clean browser window — log into the service
there as usual, then click **"Grab cookies"**, and the gateway pulls the
session out for you. No `F12`, no copying strings. (Keep the window open
until you've grabbed.)

On your own machine this uses a local browser. On a headless server, bring
one up first with `bench/browser_session.sh` (see
[operations.md](operations.md)). If a service is fussy about automated
sign-in (DeepSeek, sometimes Google), fall back to the manual methods below.

## Three ways to get it by hand, and when to use which

**A token from localStorage** — the simplest. Open the site, `F12` →
**Console**, paste the line from the table, copy the output.

**Cookies a script can see.** In the same console, `document.cookie` — but
it doesn't always work, see below.

**Cookies with the HttpOnly flag.** The browser deliberately hides these
from scripts, and `document.cookie` will NOT show them — that's protection,
not a bug. Confirmed: Google's `__Secure-1PSID` is exactly this kind. For
them there's a reliable trick that works for absolutely everything:

> `F12` → the **Network** tab → reload the page or click something → pick
> any request to the service itself → **Headers** → the **Request Headers**
> section → the `cookie:` line → copy the whole thing.

That's the `a=b; c=d` string — exactly what the gateway asks for from most
providers. This method always works: the browser sends HttpOnly cookies to
the server, it just doesn't show them to scripts.

## Table

| Provider | Where to get it | What exactly |
|---|---|---|
| **chatgpt** | chatgpt.com | the `__Secure-next-auth.session-token` cookie |
| **claude_web** | claude.ai | the whole cookie string (`sessionKey`, plus `cf_clearance`/`__cf_bm` from Network → Headers) |
| **gemini_web** | google.com | `__Secure-1PSID` and `__Secure-1PSIDTS` joined with `\|` |
| **qwen** | chat.qwen.ai | `token` from localStorage |
| **deepseek** | chat.deepseek.com | `userToken` from localStorage |
| **kimi** | kimi.com | `refresh_token` from the cookies |
| **grok** | grok.com | the `sso` and `sso-rw` cookies joined with `\|` |
| **alice** | alice.yandex.ru | the `Session_id` cookie |
| **perplexity** | perplexity.ai | the whole cookie string |
| **mistral** | chat.mistral.ai | the whole cookie string |
| **manus** | manus.im | the `session_id` cookie (already a JWT) |
| **poe** | poe.com | the whole cookie string, `p-b` required |
| **pi** | pi.ai | the whole cookie string, `__Host-session` required |
| **deepai** | deepai.org | the whole cookie string, `sessionid` required |
| **bing_images** | bing.com | the whole cookie string, `_U` required |
| **venice** | venice.ai | cookies as JSON |
| **zai** | chat.z.ai | the whole cookie string |
| **groq** | console.groq.com | API key |
| **openrouter** | openrouter.ai | API key |
| **agentrouter** | agentrouter.org | API key |
| **gemini_api** | aistudio.google.com | API key |
| **cohere** | dashboard.cohere.com | a trial key (Trial), free |
| **cloudflare** | dash.cloudflare.com | a Workers AI API token |

Nothing needed: **llm7** (no key, no registration at all), **ms_copilot**
(device-code sign-in, the gateway will prompt you), **opera_aria**
(registers itself), **yqcloud**, **pollinations**, **openai_fm**.

A special case — **meta_ai**: it's not a string but files captured from a
live client (a frame template, a socket address, cookies). They're captured
once by a capture procedure, not typed by hand.

## Ready-made console snippets

Open the provider's site, sign in, `F12` → **Console**, paste, press Enter,
copy the output.

**Qwen** — token from localStorage:

```js
copy(JSON.parse(localStorage.getItem('token') ?? '""')); console.log('copied')
```

If `copy` isn't available, just:

```js
localStorage.getItem('token')
```

**DeepSeek**:

```js
JSON.parse(localStorage.getItem('userToken')).value
```

**Anything that asks for "the whole cookie string"** (perplexity, mistral,
poe, pi, deepai, bing_images, zai) — first try this:

```js
document.cookie
```

Empty or missing the name you need — that means the cookies are HttpOnly;
grab them from the Network tab as described above.

**A single cookie by name** (chatgpt, kimi, alice, manus):

```js
(name => document.cookie.split('; ').find(c => c.startsWith(name + '='))?.slice(name.length + 1)
  ?? 'not visible from script — take it from Network → Headers → cookie')('Session_id')
```

Substitute the name you need in place of `Session_id`.

**Grok** — two cookies joined with `|`:

```js
['sso', 'sso-rw'].map(n =>
  document.cookie.split('; ').find(c => c.startsWith(n + '='))?.slice(n.length + 1) ?? '?'
).join('|')
```

**Gemini** — both Google cookies joined with `|`. Almost certainly
HttpOnly, so it's more reliable to use **Application → Cookies →
google.com**: find `__Secure-1PSID` and `__Secure-1PSIDTS`, join them with
`|`.

**Venice** — cookies as JSON:

```js
JSON.stringify(Object.fromEntries(
  document.cookie.split('; ').map(c => [c.slice(0, c.indexOf('=')), c.slice(c.indexOf('=') + 1)])
))
```

## Checking that a credential is accepted

On the **Credentials** page, every account has a check button: the gateway
asks the service a trivial question and shows whether it answered. From the
command line:

```bash
python -m foxroute check qwen
```

## When a credential goes stale

It varies: Manus's JWT lives 90 days, Kimi's refresh token lasts months,
ChatGPT's cookie survives weeks, and Gemini's working part **refreshes on
the fly** and is stored in the library's cache rather than in the settings.

The sign of a stale credential is an `auth_error` in the interface. But
don't rush: "the service is silent" ≠ "the cookie is dead." With Gemini, a
similar picture came from a corrupted cookie cache rather than the session —
see [operations.md](operations.md).

Web-session accounts are set up by pulling a cookie or token from the
browser via `bench/grab_cookies.py` (on the server — over CDP from under
noVNC).
