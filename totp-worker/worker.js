addEventListener('fetch', (event) => {
  event.respondWith(handle(event.request));
});

const GUI_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ig-totp — shared 2FA codes</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0b0e14; color: #e6e9f0; font-family: system-ui, -apple-system, sans-serif; }
  .card { width: min(92vw, 380px); background: #151a26; border: 1px solid #2a3348; border-radius: 14px; padding: 24px; }
  h1 { margin: 0 0 4px; font-size: 20px; }
  p.sub { margin: 0 0 16px; color: #8b94a7; font-size: 13px; }
  label { display: block; font-size: 12px; color: #8b94a7; margin: 12px 0 4px; }
  input { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #2a3348;
    background: #0b0e14; color: #e6e9f0; font-size: 15px; }
  .row { display: flex; gap: 8px; margin-top: 14px; }
  button { flex: 1; padding: 11px; border: 0; border-radius: 8px; font-size: 15px; cursor: pointer; }
  #show { background: #3b82f6; color: #fff; }
  #copy { background: #232b3d; color: #e6e9f0; flex: 0 0 84px; }
  #code { margin: 18px 0 4px; font-size: 46px; letter-spacing: 8px; text-align: center;
    font-family: ui-monospace, monospace; min-height: 60px; }
  #meta { text-align: center; color: #8b94a7; font-size: 13px; min-height: 20px; }
  #bar { height: 6px; background: #232b3d; border-radius: 3px; margin-top: 10px; overflow: hidden; }
  #fill { height: 100%; width: 0%; background: #22c55e; }
  #err { color: #f87171; font-size: 13px; text-align: center; min-height: 20px; margin-top: 8px; }
  .remember { display: flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 13px; color: #8b94a7; }
  .remember input { width: auto; }
</style>
</head>
<body>
<div class="card">
  <h1>ig-totp</h1>
  <p class="sub">Shared 2FA codes — pick a slot, grab the live code at login.</p>
  <label for="key">Provider key</label>
  <input id="key" type="password" placeholder="TOTP_KEY" autocomplete="off">
  <label for="slot">Slot (one per project)</label>
  <input id="slot" type="text" placeholder="default" autocomplete="off">
  <div class="row"><button id="show">Show code</button><button id="copy">Copy</button></div>
  <div id="code">••••••</div>
  <div id="meta"></div>
  <div id="bar"><div id="fill"></div></div>
  <div id="err"></div>
  <label class="remember"><input id="remember" type="checkbox"> remember key on this device</label>
</div>
<script>
const $ = (id) => document.getElementById(id);
let timer = null, left = 0, span = 30;
try {
  $('slot').value = localStorage.getItem('ig_slot') || 'default';
  if (localStorage.getItem('ig_key')) { $('key').value = localStorage.getItem('ig_key'); $('remember').checked = true; }
} catch (e) {}
async function fetchCode() {
  const key = $('key').value.trim(), slot = ($('slot').value.trim() || 'default');
  $('err').textContent = '';
  if (!key) { $('err').textContent = 'enter provider key'; return; }
  try {
    const r = await fetch('/code?key=' + encodeURIComponent(key) + '&slot=' + encodeURIComponent(slot));
    const d = await r.json();
    if (!r.ok || !d.ok) { $('err').textContent = r.status === 403 ? 'wrong key' : (d.error || 'failed'); return; }
    $('code').textContent = d.code;
    left = d.expires_in_sec; span = 30;
    try {
      localStorage.setItem('ig_slot', slot);
      if ($('remember').checked) localStorage.setItem('ig_key', key);
      else localStorage.removeItem('ig_key');
    } catch (e) {}
    tick();
  } catch (e) { $('err').textContent = 'network error'; }
}
function tick() {
  if (timer) clearInterval(timer);
  const draw = () => {
    $('meta').textContent = left > 0 ? left + 's left' : 'refreshing…';
    $('fill').style.width = Math.max(0, (left / span) * 100) + '%';
    $('fill').style.background = left <= 5 ? '#ef4444' : '#22c55e';
  };
  draw();
  timer = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(timer); fetchCode(); return; }
    draw();
  }, 1000);
}
$('show').onclick = fetchCode;
$('copy').onclick = async () => {
  const t = $('code').textContent;
  if (/^\d{6}$/.test(t)) { try { await navigator.clipboard.writeText(t); $('meta').textContent = 'copied'; } catch (e) {} }
};
$('key').addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchCode(); });
</script>
</body>
</html>`;

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
