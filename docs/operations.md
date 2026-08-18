# Operations

## Running

The usual case — on your own machine:

```bash
python -m foxroute.server --host 127.0.0.1 --port 8777
```

The interface opens at the same address: `http://127.0.0.1:8777/`. Nothing
else is needed — no database, no Docker, no configuration.

Where data is stored is set by `FOXROUTE_HOME` (the application directory by
default): it holds accounts, cookie caches, frame templates, the API key.

**The port listens on the loopback only, and it's best not to change that.**
The gateway holds live sessions of third-party services. The programmatic
API is protected by a key (`Authorization: Bearer`), but the interface
behind a tunnel works without one — a port open to the outside still means
anyone could use the interface, and with it the sessions.

### On a separate machine

The gateway is kept on a server: sessions live longer that way, and requests
to services don't come from your address. Access is via a tunnel, not an
open port:

```bash
ssh -N -L 8777:127.0.0.1:8777 root@SERVER
```

Then `http://localhost:8777/` in the browser; a programmatic client uses the
same address in `base_url` plus the key.

## Command line

```bash
python -m foxroute list                      # the pool: who's set up, what's connected
python -m foxroute status                    # who's good for what right now
python -m foxroute add qwen "<credential>"   # add an account
python -m foxroute check qwen                # live check (spends a message)
python -m foxroute check                     # check them all
```

## Smoke-testing after changes

```bash
python bench/smoke.py --web     # all web sessions
python bench/smoke.py --all     # plus the official APIs
python bench/smoke.py qwen      # a single provider
```

The smoke test checks not "did something come back" but exactly what breaks
during a rewrite: whether the stream is real (chunks arrive as they go, not
in one burst at the end), whether service markup leaked through, whether the
failure is typed, whether unsupported requests are rejected without hitting
the network.

The smoke test reaches live services, so it has to be run where the
credentials live. If that's a separate machine:

```bash
scp -r foxroute bench root@SERVER:/opt/foxroute/
ssh root@SERVER "cd /opt/foxroute && FOXROUTE_HOME=/opt/foxroute/data \
  python3 bench/smoke.py --web"
```

## When something doesn't work

**An "empty response" almost never means "the cookie went stale."** Half of
those cases turned out to be our own parsing bugs. Before changing a
credential — look at what came over the wire.

Three cases that have already cost dearly and are sure to recur:

**Gemini "drops out every time."** The working `__Secure-1PSIDTS` lives
ONLY in the library's cache; it's not in the settings. The library saved the
cache by replacement, and a failed attempt would overwrite the working set
with a stub — after which every next start was worse than the last. Fixed by
merging instead of replacing (`_protect_cookie_cache` in
`providers/web/gemini_web.py`). If the symptom returns — compare the cookie
set in `data/cache/gemini_web/`: the working set has six, the corrupted one
four.

**Copilot "returns nothing" on hard questions.** The service itself decides
the question needs a different mode and sends `modeSelected` — there will be
no answer after that; it waits for the message to be resent in the chosen
mode. Handled in `copilot.py`; if something similar shows up elsewhere —
look for the same kind of frame.

**File upload "stopped working."** Manus recognizes a file by its content
and won't accept it a second time, reusing the previous upload. A benchmark
needs a NEW file, otherwise it looks like a bug.

## A browser alongside the gateway

Needed for two things: signing into a service by hand, and capturing a live
request when the protocol can't be guessed. On your own machine an ordinary
browser will do; on a headless machine a script brings one up.

```bash
bash bench/browser_session.sh start    # bring it up
bash bench/browser_session.sh stop     # stop it
```

View it at `http://localhost:6977/vnc.html` (from a separate machine —
through a tunnel to port 6977).

**The scribe** — when you need to capture the protocol:

```bash
python3 bench/watch_tabs.py            # all tabs
python3 bench/watch_tabs.py alice grok # only these
```

It keeps one tab per provider and records the entire exchange (requests with
headers, response bodies, socket frames) into `/tmp/watch/<name>.jsonl`. A
human clicks the buttons: the pop-up menus of Alice and Manus won't open by
a programmatic click, nor by a real mouse through the debugger.

To stop it: `pkill -f "watch""_tabs"` — exactly like that, with the break. A
whole word in the pattern would match the command line of the ssh session
itself, and it would kill itself.

## Proxy

Provider requests and the browser sign-in can go through a proxy. Set a
global one on **Settings → Proxy** (`socks5://…` or `http://…`), or override
it per account. Cookies are then grabbed under the proxy's IP, which matters
for IP-bound sessions (the 🔴 markers on the Credentials page).

## Quotas and pauses

Web sessions count quota in messages and usually don't show it. The gateway
keeps its own count (`quota.py`) and sets a pause when a service refuses.

Two kinds of refusal are distinguished, and it changes behavior:

* **throttle** — a short delay, the service will come back on its own
  (Copilot revived after 25 minutes). Waiting makes sense.
* **budget** — the window is exhausted (Poe went away for 17 hours). Waiting
  is pointless; you have to move to another.

Limits worth remembering: Copilot's research is **five runs per month**;
Venice — 10 messages per day; Perplexity has no file upload at all
(`upload_limit: 0`).
