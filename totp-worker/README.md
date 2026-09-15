# ig-totp — Cloudflare TOTP provider for the bot

Matches `app/totp_client.py`: `GET /code?key=<TOTP_KEY>&slot=default`
returns `{"ok": true, "code": "123456", "expires_in_sec": 27}`.

## Deploy (2 min, Dashboard — no CLI needed)

1. Cloudflare Dashboard → Workers & Pages → Create → Hello World → paste
   `totp-worker/worker.js` → Deploy.
2. Worker → Settings → Variables → Add Secret (NOT plain text):
   - `TOTP_KEY` = provider password (ask orchestrator — generated, 48 hex chars)
   - `TOTP_SEED` = Instagram 2FA seed (spaces ok, stripped automatically)
   - optional multi-account: `TOTP_SLOTS` = JSON like `{"acc2": "SEED..."}`
3. URL will be `https://ig-totp.<your-subdomain>.workers.dev`.
4. Test: `/health` → `{"ok": true}`; `/code?key=...&slot=default` → code.

## CLI alternative

```
cd totp-worker
npx wrangler login
npx wrangler secret put TOTP_KEY
npx wrangler secret put TOTP_SEED
npx wrangler deploy
```

## Bot wiring (Render env)

- `TOTP_PROVIDER_URL` = `https://ig-totp.<sub>.workers.dev`
- `TOTP_KEY` = same value as the Worker secret
- `TOTP_SLOT` = `default`

Provider failure falls back to local `INSTAGRAM_TOTP_SEED` automatically —
safe to enable anytime.
