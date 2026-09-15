# ig-totp — shared 2FA codes for every project/deployment

Live: `https://ig-totp.tanbirst2st2.workers.dev`

## GUI (login-time codes)

Open `/` in a browser → enter provider **key** + **slot** → live 6-digit code
with countdown, auto-refresh and copy button. Wrong key → clear message
(seed pasted as key is detected and explained); `forget saved` wipes stored
key/slot.

Deep links that start showing codes immediately:

- `/?seed=<BASE32>` — **local mode**: codes computed in the browser, no key
  needed. Seed stays in your address bar — private devices only.
- `/?slot=<name>&key=<TOTP_KEY>` — server mode, pre-filled and auto-started.
  Note: the key lands in browser history — prefer typing it.

## API (bots)

`GET /code?key=<TOTP_KEY>&slot=<slot>` →
`{"ok": true, "code": "123456", "expires_in_sec": 27, "slot": "..."}`

Matches `app/totp_client.py` (cache + single-flight + silent fallback to the
local seed). `GET /health` → `{"ok": true}`. Unknown slot → 404.

## Sharing: one slot per project

- Slot `default` reads the `TOTP_SEED` secret.
- Any other slot reads the `TOTP_SLOTS` JSON secret, e.g.
  `{"acc2": "SEED...", "shopbot": "SEED..."}`.
- One master `TOTP_KEY` gates all slots; isolation between projects is by
  slot name. Rotate the key by updating the Worker secret + every consumer.
- Add a slot: `PUT .../workers/scripts/ig-totp/secrets`
  `{"name": "TOTP_SLOTS", "text": "{...merged...}", "type": "secret_text"}`
  (merge with existing JSON first). Remove: `DELETE .../secrets/TOTP_SLOTS`.
- Consumer env per project: `TOTP_PROVIDER_URL` (Worker URL),
  `TOTP_KEY` (same value), `TOTP_SLOT` (its slot name).

## Deploy notes (learned the hard way)

- Upload this file **raw** (`Content-Type: application/javascript`), NOT as
  multipart — the API silently stores multipart envelopes as the script.
- Then enable the route: `POST .../workers/scripts/ig-totp/subdomain`
  `{"enabled": true}` — without it the URL serves `error code: 1042`.
- Secrets persist across uploads; allow ~1 min propagation after changes.
