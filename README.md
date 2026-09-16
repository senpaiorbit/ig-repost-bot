# ig-repost

FastAPI skeleton for IG repost bot (stubs only).

| Method | Endpoint | Params | Description |
| ------ | -------- | ------ | ----------- |
| GET | `/` | — | Service info |
| POST | `/upload` | `key`, `post_type`, body `target_url` | Repost media (TODO) |
| POST | `/archive` | `key`, `max_age_hours=24`, `min_views=1000`, `force=false` | Archive low-view old posts (TODO) |
| GET | `/check_db` | — | DB health check (TODO) |
| GET | `/check_login` | — | IG login check (TODO) |
| GET | `/check_totp` | — | TOTP check (TODO) |

Docs: `/docs`

Cron (single request — no polling needed):
- AUTO upload-from-feed (no `target_url`, cron-friendly, 30s-safe): `GET /upload?key=KEY&amount=1&cover=URL&comment=TEXT&cronjob=1` → `202 {"status":"queued","job_id":...,"mode":"auto"}` (upload continues server-side in background). Bare `GET /upload?key=KEY` (no `target_url`) also goes AUTO with `amount=1`.
- Explicit URL: `GET /upload?key=KEY&target_url=URL` → `202 {"status":"queued","job_id":...}` (upload continues server-side in background; `sync=0` default for GET).
- Optional status check: `GET /job?id=JOB_ID` (alias `GET /a_job?id=JOB_ID`).
- Manual/sync upload: `GET /upload?key=KEY&target_url=URL&sync=1` or `POST /upload?key=KEY&sync=1` with body `{"target_url": "..."}` (POST defaults to `sync=1` for back-compat).

Anti-ban defaults:
- Session reuse via `SESSION_FILE` (default `session.json`) — login session is loaded before and saved after login.
- Cap of `MAX_UPLOADS_PER_DAY` (default 2/day, HTTP 429 when reached) + single-upload lock (no parallel uploads).
- Comments off by default (`COMMENT_ENABLED=0`); explicit `?comment=` still posts.
- Jitter: `delay_range [2,5]`, 2–6s pause before commenting, 20–60s human pause after upload.

Note: manual API only — trigger via cron-jobs.org hitting `/upload` and `/archive`.
