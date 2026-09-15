// ig-totp — shared TOTP provider + login GUI (service-worker syntax).
// Live at https://ig-totp.<subdomain>.workers.dev — matches app/totp_client.py:
//   GET /code?key=<TOTP_KEY>&slot=<slot> -> {ok, code, expires_in_sec, slot}
addEventListener('fetch', (event) => {
  event.respondWith(handle(event.request));
});

async function handle(request) {
  const url = new URL(request.url);
  if (url.pathname === '/' || url.pathname === '/gui') {
    return new Response(GUI_HTML, { headers: { 'content-type': 'text/html; charset=utf-8' } });
  }
  if (url.pathname === '/health') {
    return Response.json({ ok: true });
  }
  if (url.pathname !== '/code') {
    return Response.json({ ok: false, error: 'not found' }, { status: 404 });
  }
  const key = url.searchParams.get('key') || '';
  const slot = (url.searchParams.get('slot') || 'default').slice(0, 64);
  if (!timingSafeEqual(key, currentKey())) {
    return Response.json({ ok: false, error: 'forbidden' }, { status: 403 });
  }
  const seed = resolveSeed(slot);
  if (!seed) {
    return Response.json({ ok: false, error: 'unknown slot' }, { status: 404 });
  }
  try {
    const out = await totpNow(seed);
    return Response.json({ ok: true, code: out.code, expires_in_sec: out.expiresIn, slot });
  } catch (e) {
    return Response.json({ ok: false, error: 'totp failure' }, { status: 500 });
  }
}

function currentKey() {
  return typeof TOTP_KEY !== 'undefined' ? String(TOTP_KEY) : '';
}

// Slots: 'default' reads TOTP_SEED; any other slot reads the TOTP_SLOTS
// JSON map secret, e.g. {"acc2": "SEED...", "proj": "SEED..."}.
function resolveSeed(slot) {
  if (slot === 'default' || slot === '') {
    return String(typeof TOTP_SEED !== 'undefined' ? TOTP_SEED : '').replace(/\s+/g, '');
  }
  try {
    const extra = JSON.parse(typeof TOTP_SLOTS !== 'undefined' ? TOTP_SLOTS : '{}');
    if (extra && extra[slot]) return String(extra[slot]).replace(/\s+/g, '');
  } catch (e) {}
  return '';
}

function timingSafeEqual(a, b) {
  a = String(a); b = String(b);
  if (!b || a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}

const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

function base32Decode(s) {
  s = String(s).toUpperCase().replace(/[^A-Z2-7]/g, '');
  let bits = 0, value = 0;
  const bytes = [];
  for (const ch of s) {
    value = (value << 5) | B32.indexOf(ch);
    bits += 5;
    if (bits >= 8) { bytes.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
  }
  return new Uint8Array(bytes);
}

async function totpNow(seed, step, digits) {
  step = step || 30; digits = digits || 6;
  const keyBytes = base32Decode(seed);
  if (keyBytes.length === 0) throw new Error('bad seed');
  const now = Math.floor(Date.now() / 1000);
  const counter = Math.floor(now / step);
  const msg = new Uint8Array(8);
  let c = counter;
  for (let i = 7; i >= 0; i--) { msg[i] = c & 0xff; c = Math.floor(c / 256); }
  const cryptoKey = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', cryptoKey, msg));
  const offset = sig[sig.length - 1] & 0x0f;
  const bin = ((sig[offset] & 0x7f) << 24) | (sig[offset + 1] << 16) | (sig[offset + 2] << 8) | sig[offset + 3];
  const mod = Math.pow(10, digits);
  const code = String(bin % mod).padStart(digits, '0');
  return { code, expiresIn: step - (now % step) };
}

const GUI_HTML = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>ig-totp</title></head><body><p>Open /gui — full login GUI is served from the deployed build.</p></body></html>';
