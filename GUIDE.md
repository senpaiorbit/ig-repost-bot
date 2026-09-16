# ig-repost — Operator Guide

Instagram repost bot (FastAPI). One cron GET queues a background upload — no polling needed.

## Endpoints

All `key`-authed endpoints take `?key=YOUR_API_KEY`. No-auth endpoints are marked `-`.

| Method | Path | Params | Purpose |
| ------ | ---- | ------ | ------- |
| GET | `/` | — | Service info (`service`, `status`, `docs`) |
| GET | `/health` | — | Render health check. Always 200: `{"status":"ok","db":"ok\|failed:…","login":"ok:user\|failed:…\|skipped:…"}` (login cached 300s) |
| GET | `/upload` | `key`, `target_url?`, `post_type=reel`, `comment`, `cover`, `sync=0`, `amount=1`, `cronjob=0` | Cron entry. With http `target_url` → queued 202 (`sync=1` = inline). Without → AUTO feed mode 202 |
| POST | `/upload` | `key`, body `{"target_url":"…"}`, `post_type`, `comment`, `cover`, `sync=1` | Manual/explicit upload (inline by default) |
| GET | `/job?id=` | `id` | Job status: `queued/running/success/error` + `media_id`/`error` + recent `logs` |
| GET | `/a_job?id=` | `id` | Alias of `/job` (old cron compat) |
| POST | `/archive` | `key`, `max_age_hours=24`, `min_views=1000`, `force=false` | Archive old low-view posts |
| GET | `/check_db` | — | DB health (`ok` / 500) |
| GET | `/check_login` | — | IG login check (real login attempt) |
| GET | `/check_totp` | — | TOTP seed check (code prefix only, never full code) |
| GET | `/turso_check` | — | DB diagnostics: backend, path, writability, tables, counts. Never 500 |
| GET | `/reconnect` | `key`, `reset=0` | Login test; `reset=1` deletes session file first (fresh login) |
| GET | `/live` | `key`, `n=100`, `clear=0` | Last `n` log lines + in-memory job states; `clear=1` wipes buffer |

## Cron setup (cron-jobs.org)

Upload (AUTO, 30s-safe — returns 202 in <2s, work continues server-side):

```text
https://<app>.onrender.com/upload?key=KEY&amount=1&cronjob=1
```

With cover/comment:

```text
https://<app>.onrender.com/upload?key=KEY&amount=1&cover=https://…/c.jpg&comment=TEXT&cronjob=1
```

Explicit URL instead of feed pick:

```text
https://<app>.onrender.com/upload?key=KEY&target_url=https://www.instagram.com/reel/XXXX/
```

Archive daily (separate job, once/day):

```text
https://<app>.onrender.com/archive?key=KEY&max_age_hours=24&min_views=1000
```

Notes: `amount` clamps to 1–3. Daily cap applies per upload (`429 daily limit reached`). Bare `GET /upload?key=KEY` also runs AUTO `amount=1`.

## Environment variables

| Var | Default | Purpose / notes |
| --- | ------- | --------------- |
| `API_KEY` | `changeme` | Required. `?key=` auth for all write/diagnostic endpoints. Empty = dev mode (accepts any non-empty key — never use in prod) |
| `IG_USERNAME` / `IG_PASSWORD` | `` | Login fallback when `IG_SESSIONID` absent/expired |
| `IG_TOTP_SEED` | `` | Base32 2FA seed for password logins |
| `IG_SESSIONID` | `` | Preferred login path (no password/2FA round-trip) |
| `TOTP_PROVIDER_URL` | `https://ig-totp.tanbirst2st2.workers.dev` | 2FA code worker (`GET ?seed=…&json=1`); any failure falls back to local pyotp |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | `` | Remote Turso DB (needs `libsql` installed). Empty/`file:` → local sqlite (`local.db`, or tmpdir on Render) |
| `SESSION_FILE` | `session.json` | Device/session persistence (load before login, dump after) |
| `MAX_UPLOADS_PER_DAY` | `2` | Daily upload cap. `0` = unlimited. Exceeded → HTTP 429 |
| `AUTO_FEED_LIMIT` | `20` | Feed candidates fetched per auto job |
| `SOURCE_USERNAMES` | `` | Comma-separated IG accounts to repost from when own timeline is empty (`@` optional) |
| `COVER_IMAGE_URL` | `https://i.ibb.co/…/1.jpg` | Default reel cover |
| `COVER_IMAGE_URLS` | `` | Opt-in cover pool (comma/newline separated) — random pick per upload when set |
| `THUMBNAIL_URL` | `` | Legacy cover override (beats `COVER_IMAGE_URL`) |
| `COMMENT_TEXT` | `follow me 🔥` | Default comment |
| `COMMENT_ENABLED` | `0` | `0` = no default comment; explicit `?comment=` still posts |
| `IG_PROXY` | `` | Optional `http(s)://…` proxy for IG traffic |
| `LOG_LEVEL` | `INFO` | Log level |

## Render blueprint (`render.yaml`)

Provisions one Python web service (`ig-repost`): `pip install -r requirements.txt`, starts `uvicorn main:app`, Python pinned via `PYTHON_VERSION=3.11.0` (+ `.python-version`). `sync: false` envVars are set from the Render dashboard (never committed); every `Settings` field has an entry so the dashboard shows the full list. `GET /health` is the health check (always 200 by design). Entry `app.main:app` also works (thin shim re-exporting the root app).

## Troubleshooting

| Symptom | Look at |
| ------- | ------- |
| Job failed, why? | `GET /job?id=…` (`error` + `logs` tail) or `GET /live?key=…&n=200` |
| `0/N candidates ok` | `/live` feed lines: `raw=` items seen, `skipped_ad`/`skipped_nocode`, per-source `items -> candidates`; empty timeline + no `SOURCE_USERNAMES` = nothing to repost |
| `daily limit reached` (429) | `/turso_check` `active_media` count vs `MAX_UPLOADS_PER_DAY` |
| Login/challenge errors | `GET /reconnect?key=…` to test; `&reset=1` for a fresh session; check `/live` for `challenge/rate-limit` lines |
| DB confusion (sqlite vs Turso) | `GET /turso_check` — backend, path, writability, table presence, job counts |

## Anti-ban operating notes

- Default cap is **2 uploads/day** (`MAX_UPLOADS_PER_DAY`); uploads are serialized (single-upload lock, no parallels).
- Jitter everywhere: request `delay_range [1,8]`, 1–10s pause before commenting, 15–90s human pause after upload, 15–60s between auto uploads.
- Session/device reuse via `SESSION_FILE` — avoids a fresh login handshake per upload.
- Comments off by default; never reposts own posts; ads/suggested-users filtered from candidates.
- Keep `SOURCE_USERNAMES` to 1–3 stable public accounts; don't raise the cap aggressively on a new account.
