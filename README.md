# OpenRouter farm — HTTP build (no browser)

Creates OpenRouter accounts, verifies the email, mints an `sk-or-v1-...` API key and pushes it
into a local **9Router**. Everything happens over plain HTTP: **no Chromium, no Playwright**,
~7–15 s per account.

```
pip install -r requirements.txt

python or_fast.py 10                                    # 10 accounts, 4 workers
python or_fast.py 200 --workers 10                      # volume run
python or_fast.py 5 --pool proxies_good.txt --smoke     # through proxies, prove a key works
python or_fast.py 3 --mail imap                         # your own mailbox (see below)
```

Each finished account is appended to `accounts.txt`:

```
email|password|key|status|note|proxy
```

## Why this works without a browser

The browser was only ever needed because *headless Chromium* trips Clerk's bot check. A plain
HTTP client with a Chrome TLS fingerprint (`curl_cffi`, `impersonate="chrome"`) is not challenged
on the signup POST, and every later step is a REST call:

```
GET   openrouter.ai/                                  -> Cloudflare sets __cf_bm / __cfuvid
GET   clerk.openrouter.ai/v1/client                   -> __client cookie (Clerk client)
POST  .../v1/client/sign_ups                          -> sign_up id   (no captcha)
PATCH .../v1/client/sign_ups/<sid>                    password + legal_accepted
POST  .../v1/client/sign_ups/<sid>/prepare_verification   strategy=email_code
POST  .../v1/client/sign_ups/<sid>/attempt_verification   code -> session created
POST  .../v1/client/sessions/<sess>/tokens            -> session JWT
GET   openrouter.ai/api/frontend/v1/private/user/workspaces        Bearer <jwt>
POST  openrouter.ai/api/frontend/v1/private/workspace-api-keys?workspace_id=<id>   {"name":"k1"}
```

The create-key response contains the **full** `sk-or-v1-...` key as a JSON string — the dialog in
the UI only shows it masked.

### Clerk's risk check

Sometimes the sign_up ends `missing_fields: ["protect_check"]`: Clerk wants its
`charon.protect.clerk.com` proof-of-transfer (a chunked SHA-256 speed bump) that a HTTP client
cannot satisfy. It is **risk based**, so `or_fast.py` reacts by redoing the signup **on a
different exit IP** (`--attempts`, default 3). With a residential pool roughly 1 in 2–4 attempt-1
signups hits it, and the retry usually clears. The inbox is reused, so retries cost no mail quota.

## Mail — this is the bottleneck, not the signup

OpenRouter refuses disposable domains at signup:

> Temporary email services are not supported. Please use your regular email address.

Measured 2026-10 by testing 1 signup POST per domain (rotating proxies):

| refused | accepted |
|---|---|
| mailto.plus, fexpost.com, fexbox.org, rover.info, tempmail.plus, yopmail.com, maildrop.cc, tmpmail.net/.org, moakt.cc, mohmal.com, tempmail.email, luckymail.org, inboxes.com, harakirimail.com, discard.email, mailcatch.com, tempr.email, mailsac.com, 10minutemail.com, mailmetrash.com, getnada.com, emailondeck.com, mailinator.com, sharklasers.com, guerrillamail.info, emlhub.com, emlpro.com, 10mail.org, dropmail.me, mail.tm, mail.gw, 1secmail.com, vjuum.com, laafd.com, txcct.com, lucktaro.com (tempmail.lol), tanpony.com, yzcalo.com | gmail.com, outlook.com, and the alias domains anonaddy.me / mozmail.com / 33mail.com |

tempmail.lol also answers `429 Rate limited (free)` after two inboxes. So the public backends in
`mail_providers.py` are there for testing, and a refused domain is written to
`blocked_domains.json` automatically. For real volume use one of these two:

**1. IMAP plus-addressing** (zero cost, no rate limit)

```bash
export OR_IMAP_HOST=imap.gmail.com
export OR_IMAP_USER=you@gmail.com
export OR_IMAP_PASS=app-password-16-chars     # Gmail: 2FA -> App passwords
python or_fast.py 100 --mail imap
```

`you+<tag>@gmail.com` is a normal address to OpenRouter and the mail lands in your inbox. Outlook,
Yandex and Fastmail work the same way.

**2. Your own domain + Cloudflare Email Worker** (unlimited, no inbound ports)

`cf_mail_worker.js` receives every address on your domain via Cloudflare Email Routing, keeps the
codes in KV and exposes `GET /code?to=<addr>&key=<secret>`. Deploy steps are in the file header.
Then:

```bash
export CF_MAIL_URL=https://mail-catcher.<you>.workers.dev
export CF_MAIL_KEY=<worker SECRET>
export CF_MAIL_DOMAIN=yourdomain.tld
python or_fast.py 1000 --workers 10
```

A freshly registered domain is not on the disposable list, so the signup passes.

**3. Your own mail API** — `OR_MAIL_CREATE_URL` / `OR_MAIL_POLL_URL` (+ optional
`OR_MAIL_FETCH_URL`, `OR_MAIL_HEADERS`) for anything else JSON-shaped.

Every provider that enforces a quota should be paced: `OR_MAIL_MIN_GAP` (seconds between calls,
default 2.0) is a global bucket shared by all workers, so 10 parallel workers stop 429-ing each
other. Kirin's tier, for example, allows 30 requests/minute → keep the gap ≥ 2.05.

## Proxies

```
python proxy_check.py proxies.txt proxies_good.txt     # CONNECT + geo per line
python or_fast.py 50 --pool proxies_good.txt
```

`proxy_check.py` keeps only lines whose `CONNECT openrouter.ai:443` answers 200 (`402` = the
credential is out of quota, `429` = busy) and records the exit country. The farm prefers US, then
GB/CA/AU, then the rest, skips a line that fails to open a tunnel, and remembers per-proxy
failures (`work/proxy_stats.json`). Without `--pool` everything goes out over your own IP.

## 9Router

Keys are pushed as `openrouter` **connections** on the built-in provider template — never as a
custom OpenAI-compatible node, which would leave a duplicate provider in the dashboard.

```bash
export NINE_ROUTER_URL=http://localhost:20128
export NINE_ROUTER_TOKEN=<x-9r-cli-token>       # dashboard -> CLI
# or NINE_ROUTER_PASSWORD=<dashboard password>
```

A new connection starts as `testStatus: unknown`; the script calls
`POST /api/providers/<id>/test` so it lands as `active`. `--no-router` writes keys to
`accounts.txt` only. If the router is unreachable the script says so once and keeps farming.

## What to expect

| setup | rate |
|---|---|
| IMAP or own domain, 10 workers, 200-proxy pool | ~6–9 accounts/min, >95 % success |
| public temp-mail backend | refused at signup — good for testing only |
| no proxies | works if your IP is not a flagged datacenter |

Failures to look for in `accounts.txt`:

- `protect_check required` — Clerk wanted the risk check on every retry; try more `--attempts` or
  a different proxy country.
- `disposable domain, blacklisted` — that domain is dead for future runs (persisted).
- `no code in mail` — the mail provider is rate limiting; raise `OR_MAIL_MIN_GAP`.
- `auth/key 401` — key revoked immediately, the account was probably flagged; rare.

## Files

| file | purpose |
|---|---|
| `or_fast.py` | the farm (HTTP only) |
| `mail_providers.py` | inbox backends, disposable-domain blocklist, request pacing |
| `router_push.py` | 9Router connection create/test |
| `proxy_check.py` | proxy pool health + geo filter |
| `cf_mail_worker.js` | Cloudflare Email Worker for your own domain (volume path) |
| `accounts.txt` | output rows |
| `blocked_domains.json` | domains the site refused, skipped next run |
| `work/` | proxy geo cache + per-proxy stats |

## Verify a key by hand

```bash
curl -s https://openrouter.ai/api/v1/auth/key -H "Authorization: Bearer sk-or-v1-..."      # 200
curl -s https://openrouter.ai/api/v1/chat/completions -H "Authorization: Bearer sk-or-v1-..." \
  -H 'Content-Type: application/json' \
  -d '{"model":"nvidia/nemotron-3.5-lightning:free","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
```

`:free` slugs come and go: `503 no upstreams available` or `429` still mean the key is valid —
only `401` means it is dead. `--smoke` walks the free models until one answers.
