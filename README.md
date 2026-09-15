# InstaWard IG Repost Bot

Instagram repost bot: Python + aiograpi + FastAPI (Render free) + Turso.

- `GET /health?cronjob=1` keep-warm
- `GET /upload?key=&amount=&comment=&cronjob=` background repost job, poll `GET /a_job?key=&id=`
- `GET /archive?key=&time=&views=&all=&cronjob=` retire stale/low-view own reposts
- `GET /reconnect?key=` IG login, `GET /login_status?key=` circuit breaker
- `GET /turso_check?key=` DB diagnostics, `GET /live?key=` recent reposts

Cron: `/health` every 10 min; `/upload?...&cronjob=1` every 35 min; `/archive?...&cronjob=1` slower.
