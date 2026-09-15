# InstaWard — Instagram Repost Bot

Async repost bot: scrapes reels, re-uploads with custom cover + caption credit +
comment, retires low performers. **FastAPI** + **aiograpi** on Render,
**Turso (libSQL)** state, optional **Cloudflare TOTP Worker** (`totp-worker/`).

---

## 1. Prerequisites (10 min, one time)

| Need | How |
| --- | --- |
| Instagram posting account | Enable **app-based 2FA** (Authenticator app, NOT SMS) and **save the seed** (QR secret). Note username + password. |
| Turso account | `turso db create <name> --region <nearest>` → URL `libsql://<...>.turso.io` + `turso db tokens create <name> --full-access` (full JWT, not org key). Tables self-create on boot. |
| Render account + this repo fork | Below. |
| (Optional) Cloudflare account | Only for the TOTP Worker — bot works without it via local seed. |

## 2. Deploy (5 min)

1. Render → **New → Blueprint** → paste repo URL → Apply (uses `render.yaml`).
   Use Blueprint, not manual service creation — it pins `PYTHON_VERSION 3.11`,
   commands, region and health check together.
2. Fill prompted secrets: `ENV_KEY` (long random API key), `INSTAGRAM_USERNAME`,
   `INSTAGRAM_PASSWORD`, `INSTAGRAM_TOTP_SEED` (spaces ok), `TURSO_URL`,
   `TURSO_AUTH_TOKEN`.
3. Optional at deploy or later: `COMMENT_ENABLED=1`, `COMMENT_TEXT=follow me 🔥`,
   `THUMBNAIL_URL=<cover>`. Env edits auto-redeploy.
4. Open `https://<service>.onrender.com/health` → `{"ok":true}`.

> One repo → **one** service. Two services on the same repo double every build
> and duplicate failure mails.

## 3. First connect + verify (in order)

All key-protected: `?key=<ENV_KEY>`.

1. `GET /reconnect` → `{"ok":true,"user_id":...}` (first login uses
   one-shot code → TOTP provider → local seed; session caches to Turso after).
   Slow first time — may return a background `job_id`; poll `/a_job?id=`.
2. `GET /turso_check` → `{"ok":true}` (503 + hint = bad URL/token).
3. `GET /login_status` → `{"blocked":false,"failure_count":0}`.
4. Test post: `GET /upload_one?url=<reel>&cover=<img>&comment=<text>` →
   `configure_to_clips 200` in logs; row appears in DB.
5. Test delete: `GET /archive_one?code=<repost_code>` → `{"archived":1}`.

## 4. Endpoints

| Endpoint | Purpose | Key params |
| --- | --- | --- |
| `GET /health` | Uptime / keep-warm | `cronjob=1` echo |
| `GET /live?limit=` | Recent reposts from DB | `key` |
| `GET /upload?amount=&comment=` | Auto-curated reposts (background) | `key`, `cronjob=1` |
| `GET /upload_one?url=&comment=&cover=` | Repost one URL (**always background**) | `key`, `cronjob=1` |
| `GET /a_job?id=` | Job status/result | `key` |
| `GET /archive?time=&views=&all=` | Retire stale/low-view reposts | `key`, `cronjob=1` |
| `GET /archive_one?code=` | Retire one repost | `key` |
| `GET /reconnect` | IG login (28s budget, else background) | `key`, `cronjob=1` |
| `GET /login_status` | Breaker state | `key` |
| `GET /turso_check` | DB ping + schema | `key` |

`?cronjob=1` = answer in <2s, work continues in background. `upload_one`
is always background. Add a unique `&x=` when re-checking the same URL —
identical GETs can serve stale.

## 5. Cron (cron-jobs.org)

- Upload hourly: `/upload?key=...&amount=1&cronjob=1` (pacing 30 min + daily
  cap 10 self-enforce; don't schedule tighter than ~35 min).
- Archive daily: `/archive?key=...&time=24h&views=1000&cronjob=1`.
- Keep-warm every 10 min: `/health?cronjob=1` (free plan sleeps).
- Always `cronjob=1` on `/reconnect` (sync path waits up to 28s).

Rate buckets (per IP, 429 + `retry_after_sec`): normal 5/60s, cron 3/5min.
Writes only (`/upload*`, `/archive*`, `/reconnect`); reads unlimited.
Login 429s trip a 45-min circuit breaker persisted in Turso.

## 6. Behavior you should know

- **Caption** = original caption + `Credit=@author` (credit line always kept).
- **Cover** = per-upload `cover=` → sidecar → generated frame. No param +
  empty `THUMBNAIL_URL` = generated frame.
- **Hide-like is unsupported** by the IG library (zero API) — skipped cleanly.
- **Archive deletes clips** (IG can't archive clips), archives photos;
  owner-guard verifies each post is yours; views checked live.
- Archive only touches **DB-tracked reposts** — old organic posts need importing first.
- `/a_job` is **in-memory**: it 404s across deploys. Source of truth =
  DB flags + server logs.

## 7. Cloudflare TOTP Worker (optional, `totp-worker/`)

Shared 2FA codes for every project: `GET /code?key=&slot=` →
`{ok, code, expires_in_sec}`; browser GUI at `/` (key + slot picker,
countdown, copy). One slot per project (`default` + `TOTP_SLOTS` JSON map).
Bot env: `TOTP_PROVIDER_URL`, `TOTP_KEY`, `TOTP_SLOT`. Provider failure
falls back to local seed automatically. Deploy notes + API quirks are in
`totp-worker/README.md`.

## 8. Troubleshooting

| Symptom | Cause → fix |
| --- | --- |
| Build `maturin/cargo read-only`, exit 1 | Abandoned dep without wheels → fixed by maintained `libsql` + pinned Python 3.11. Don't add Rust-built deps. |
| 503 right after a push | Rolling deploy / mixed old+new code window → wait 2 min, re-check. |
| Login `ThrottledError`/breaker active | IG 429 → 45-min breaker; wait, don't hammer (hammering extends it). |
| 2FA rejected | Seed must be app-2FA base32 (spaces ok); SMS 2FA has no seed. |
| `/turso_check` 503 hint | Wrong token type — needs per-DB JWT, not org key; check URL scheme `libsql://`. |
| `user_id mismatch` | Session belongs to another account → clear `INSTAGRAM_SESSIONID`/state or fix `INSTAGRAM_DS_USER_ID`. |
| Post count unchanged after upload | Check `/live` + DB row + `configure_to_clips 200` in logs; IG can delay visibility. |
| `hidelike failed` in old logs | Pre-fix builds; current code skips silently. |
| Stuck `running` jobs | Rolling deploy killed the instance — jobs don't survive restarts; re-fire. |

## 9. Security

- `ENV_KEY` long + random; rotate if ever pasted anywhere.
- Secrets only via Render env (`sync: false`), never in repo/chat/logs.
- One master `TOTP_KEY` gates all Worker slots; rotate Worker secret +
  consumers together. Delete single-use Cloudflare tokens after use.
- API keys in cron URLs travel in server logs — acceptable, but prefer a
  dedicated cron key if `ENV_KEY` is shared with humans.

## 10. Layout

`app/main.py` routes · `app/upload_job.py` + `app/archive_job.py` pipelines ·
`app/ig_client.py` IG wrapper · `app/db.py` Turso · `app/job_registry.py` jobs ·
`app/rate_limit.py` · `app/totp_client.py` provider · `app/utils.py` ·
`totp-worker/` Cloudflare Worker · `render.yaml` blueprint.
