# FreeDeepSeekAPI

> **⚠️ Use at your own risk. Read [DISCLAIMER.md](DISCLAIMER.md) before
> running this.**
>
> This project talks to the **undocumented** web API of
> chat.deepseek.com. Using it **may violate DeepSeek's Terms of Service**
> and **get your account permanently banned**. The author takes **no
> responsibility** for banned accounts, blocked IPs, incorrect model
> output, or any other consequences. The software is provided **AS IS**.
> Use a throwaway account you don't mind losing.

An **OpenAI-compatible local proxy** in front of
[chat.deepseek.com](https://chat.deepseek.com). Drops any OpenAI SDK or
`curl` script straight onto DeepSeek's free web-API — the proxy handles
the proof-of-work challenge, AWS-WAF cookies and the streaming format
quirks for you.

```text
            ┌─────────────────────────────────────────┐
┌──────┐    │  POST /v1/chat/completions  (OpenAI)    │    ┌────────────┐
│ your │───▶│  POST /admin/refresh-cookies (admin)    │───▶│ chat.deep  │
│ app  │    │  GET  /v1/models                        │    │ seek.com   │
└──────┘    │                                         │    └────────────┘
            │   session pool, POW solver,             │
            │   auto cookie refresh (Chrome+xvfb)     │
            └─────────────────────────────────────────┘
```

## Features

- **OpenAI-compatible** `/v1/chat/completions` — blocking and SSE streaming.
- Supports both DeepSeek models:
  - `deepseek-chat` — V3, no reasoning
  - `deepseek-reasoner` — R1, with thinking traces
- **Session pool with rotation** — every DeepSeek completion normally
  needs a `create_chat_session` call first. We keep one session per
  `(token, thinking, search)` tuple and rotate after
  `DEEPSEEK_SESSION_REUSE_LIMIT` calls. Amortises RTT and keeps the
  model's context clean.
- **Correct system-prompt handling** — DeepSeek's web API has no `system`
  role; we prepend system messages into the prompt so SDK behaviour
  matches OpenAI.
- **Admin endpoints for self-healing** —
  `POST /admin/refresh-cookies` re-runs the Chrome-based WAF bypass and
  clears session caches; `GET /admin/health` exposes cookie freshness and
  pool sizes. Auth via the same Bearer token as the DeepSeek proxy.
- **Both SSE formats supported** — legacy OpenAI-style `choices[].delta`
  and the newer JSON-Patch operations
  (`{"p":"response/content","o":"APPEND","v":"tok"}`). If DeepSeek flips
  again, only `_iter_parsed` in `dsk/api.py` needs to learn the new shape.
- **Docker image with Chrome + Xvfb baked in** — cookie refresh works on
  a headless server without any host setup.
- Logging via the stdlib `logging` module, configurable via
  `LOG_LEVEL=DEBUG`.

## Quickstart

### 1. Grab your DeepSeek session token

1. Open <https://chat.deepseek.com> and log in.
2. DevTools (F12) → Console →
   ```js
   JSON.parse(localStorage.getItem("userToken")).value
   ```
3. Copy the ~60-character string.

### 2a. Run with Docker (recommended for servers)

```bash
git clone https://github.com/afterburnerr/FreeDeepSeekAPI.git
cd FreeDeepSeekAPI
cp .env.example .env
$EDITOR .env                  # set DEEPSEEK_AUTH_TOKEN

docker build -t freedeepseekapi .
docker run -d --name deepseek \
    --restart unless-stopped \
    -p 127.0.0.1:8080:8080 \
    --env-file .env \
    --shm-size=512m \
    -v deepseek-cookies:/app/dsk \
    freedeepseekapi

# smoke-test
curl http://127.0.0.1:8080/v1/models
```

The image ships Chrome + Xvfb + DrissionPage, so
`POST /admin/refresh-cookies` works out-of-the-box on any headless host.

### 2b. Run natively (local / dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env && $EDITOR .env

python main.py
# -> listens on http://127.0.0.1:8080
```

For cookie refresh on the host you'll also need Chrome and an X display
(real one or `xvfb-run`). `./refresh_cookies.sh` handles the rest.

### 3. Use it

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="unused")
resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Or via curl:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": false
      }'
```

Streaming works just like with OpenAI (`"stream": true`, SSE `data:` frames).

### 4. Verify

```bash
python smoke_test.py
# or
TOKEN="$DEEPSEEK_AUTH_TOKEN" BASE=http://127.0.0.1:8080 python smoke_test.py
```

## API reference

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | Bearer or `DEEPSEEK_AUTH_TOKEN` env | OpenAI-compatible completion (stream + non-stream). Extra non-OpenAI field: `search_enabled` (bool). |
| `GET`  | `/v1/models` | — | Lists `deepseek-chat` and `deepseek-reasoner`. |
| `POST` | `/admin/refresh-cookies` | Bearer = `ADMIN_SECRET` (fallback: `DEEPSEEK_AUTH_TOKEN`) | Run `refresh_cookies.sh`, clear session/client caches. |
| `GET`  | `/admin/health` | Bearer = `ADMIN_SECRET` | Cookie presence + pool sizes. |

Non-OpenAI response field: every `/v1/chat/completions` response includes
a `deepseek_session` object with `chat_id`, `use`, `limit` so callers can
tell whether they're on a fresh or reused session.

## Configuration

All configuration is via environment variables; see [`.env.example`](.env.example)
for the full list with defaults.

Key ones:

| Env | Default | Notes |
|---|---|---|
| `DEEPSEEK_AUTH_TOKEN` | *(empty)* | Fallback token if client omits `Authorization`. |
| `DEEPSEEK_SESSION_REUSE_LIMIT` | `30` | Completions per DeepSeek chat session before rotating. |
| `DEEPSEEK_SEARCH_DEFAULT` | `false` | Default for `search_enabled` if not in request. |
| `ADMIN_SECRET` | = `DEEPSEEK_AUTH_TOKEN` | Bearer secret for `/admin/*`. |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | In Docker usually `HOST=0.0.0.0`. |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows stream parsing. |

## Cookie refresh — the full story

DeepSeek's edge protection (was Cloudflare, now AWS WAF) hands out a
short-lived clearance cookie. When it expires, every completion starts
returning an empty body or a 403 challenge page.

This repo has two recovery paths:

1. **`./refresh_cookies.sh`** — spawns a one-shot `dsk/server.py` bypass
   server, solves the challenge via DrissionPage + Chrome, writes
   `dsk/cookies.json`, exits. Works on any host with Chrome + a display
   (physical or `xvfb-run`). The Docker image sets `DOCKERMODE=true` and
   bundles Xvfb, so nothing extra is required.
2. **`POST /admin/refresh-cookies`** — runs `refresh_cookies.sh` *inside*
   the proxy process and clears its caches, so callers that only have
   HTTP access (e.g. a Telegram bot in a separate container) can trigger
   recovery remotely.

Both flush `dsk/_session_pool` and `dsk/_client_cache` when done so the
next completion starts fresh.

## Architecture

```
main.py                 FastAPI app; env config; request → session pool
   │
   └── dsk/             "deepseek4free" core
         ├── api.py         DeepSeekAPI client (POW + streaming parser)
         ├── pow.py         Proof-of-work challenge solver (wasm)
         ├── wasm/sha3.wasm WASM module for POW
         ├── bypass.py      CLI wrapper around server.py → cookies.json
         ├── server.py      FastAPI service that uses DrissionPage+Chrome
         ├── CloudflareBypasser.py   DrissionPage-based CF/WAF solver
         └── cookies.json   (runtime, .gitignored)
```

## Troubleshooting

**`ответ AI не распознан` / empty completions.**
Cookies expired. Hit `POST /admin/refresh-cookies` or run
`./refresh_cookies.sh`.

**`401 Invalid or expired authentication token`.**
Your `DEEPSEEK_AUTH_TOKEN` is stale. Re-grab it from
chat.deepseek.com localStorage.

**`Warning: curl-cffi version mismatch`.**
DeepSeek browser-impersonates against `chrome120`, which `curl-cffi==0.8.1b9`
produces. Newer versions sometimes fail. Pin the version.

**Chrome crashes in Docker.**
Increase shared memory: `--shm-size=1g` (or `shm_size: "1g"` in compose).

**`Failed to bypass edge protection after multiple attempts`.**
The automatic refresh kicked in and still failed. Check
`/tmp/kwork-bot-logs/bypass-server.log` / container stdout. Common cause:
VPS in a geography blocked by DeepSeek — try a different region.

## Credits & upstream

This is a modernised fork of
[LeadroyaL/FreeDeepSeekAPI](https://github.com/LeadroyaL/FreeDeepSeekAPI),
which itself wraps [deepseek4free](https://github.com/xtekky/deepseek4free).
The `dsk/` module, the POW WASM and the Chrome-based bypass are all from
the upstream projects. My changes are in:

- `main.py` — session pool, system-prompt handling, admin endpoints,
  structured logging, FastAPI lifespan.
- `dsk/api.py` — support for the new JSON-Patch streaming format that
  DeepSeek switched to in late 2025, `importlib.metadata` instead of the
  deprecated `pkg_resources`, cleaner `_iter_parsed`, proper logging.
- `dsk/bypass.py` — accept `aws-waf-token` (in addition to the now-gone
  `cf_clearance`) as a valid clearance cookie.
- `refresh_cookies.sh` — portable one-shot wrapper (works inside Docker
  too).
- `Dockerfile` — Chrome + Xvfb + Drission stack for autonomous cookie
  refresh on headless servers.

## License

MIT (see [LICENSE](LICENSE)). Keeps upstream attribution to
`deepseek4free` and `FreeDeepSeekAPI`.

## Disclaimer

**Please read [DISCLAIMER.md](DISCLAIMER.md) before using this project.**

Short version:

- This project uses the **undocumented** web API of chat.deepseek.com
  and is **not affiliated with DeepSeek**.
- Using it **may violate DeepSeek's Terms of Service** and **DeepSeek
  may ban your account** at any time without notice.
- DeepSeek may change or remove the API at any time, breaking this
  project without warning.
- Provided **AS IS**, with **no warranty** and **no liability** for
  banned accounts, blocked IPs, incorrect output, financial loss, or
  any other consequences.
- Use a **throwaway DeepSeek account** you're OK with losing.
- Do not use this for commercial resale of DeepSeek access, spam,
  disinformation, or anything illegal in your jurisdiction.

If you run this software, you do so at your own risk.
