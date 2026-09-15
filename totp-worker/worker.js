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
  #mode { text-align: center; font-size: 12px; color: #8b94a7; margin-top: 6px; min-height: 16px; }
  .remember { display: flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 13px; color: #8b94a7; }
  .remember input { width: auto; }
  .links { text-align: center; margin-top: 10px; font-size: 13px; }
  .links a { color: #3b82f6; cursor: pointer; text-decoration: none; margin: 0 6px; }
  .warn { font-size: 12px; color: #8b94a7; text-align: center; margin-top: 10px; display: none; }
</style>
</head>
<body>
<div class="card">
  <h1>ig-totp</h1>
  <p class="sub">Shared 2FA codes — pick a slot, grab the live code at login.</p>
  <div id="serverBox">
    <label for="key">Provider key <span style="color:#5b6577">(not the seed)</span></label>
    <input id="key" type="password" placeholder="TOTP_KEY" autocomplete="off">
    <label for="slot">Slot (one per project)</label>
    <input id="slot" type="text" placeholder="default" autocomplete="off">
  </div>
  <div id="seedBox" style="display:none">
    <label for="seed">2FA seed (local mode — never sent anywhere)</label>
    <input id="seed" type="password" placeholder="AAAA BBBB CCCC ..." autocomplete="off">
  </div>
  <div class="row"><button id="show">Show code</button><button id="copy">Copy</button></div>
  <div id="code">••••••</div>
  <div id="meta"></div>
  <div id="bar"><div id="fill"></div></div>
  <div id="mode"></div>
  <div id="err"></div>
  <div class="links"><a id="swapLink">use seed instead</a><a id="forgetLink">forget saved</a></div>
  <div class="warn" id="seedWarn">Local mode: codes are computed in this browser from the seed in the address bar. Use only on a private device.</div>
  <label class="remember"><input id="remember" type="checkbox"> remember on this device</label>
</div>
<script>
const $ = (id) => document.getElementById(id);
let timer = null, left = 0, span = 30, localSeed = '';
const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
function cleanSeed(s) { return String(s || '').toUpperCase().replace(/[^A-Z2-7]/g, ''); }
function looksLikeSeed(s) { return cleanSeed(s).length >= 16; }
async function sha1hmac(keyBytes, msg) {
  const k = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', k, msg));
}
function b32dec(s) {
  s = cleanSeed(s);
  let bits = 0, value = 0; const out = [];
  for (const ch of s) {
    value = (value << 5) | B32.indexOf(ch); bits += 5;
    if (bits >= 8) { out.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
  }
  return new Uint8Array(out);
}
async function localCode(seed) {
  const kb = b32dec(seed);
  if (!kb.length) throw new Error('bad seed');
  const now = Math.floor(Date.now() / 1000), ctr = Math.floor(now / 30);
  const msg = new Uint8Array(8);
  let c = ctr;
  for (let i = 7; i >= 0; i--) { msg[i] = c & 0xff; c = Math.floor(c / 256); }
  const sig = await sha1hmac(kb, msg);
  const o = sig[sig.length - 1] & 0x0f;
  const bin = ((sig[o] & 0x7f) << 24) | (sig[o+1] << 16) | (sig[o+2] << 8) | sig[o+3];
  return { code: String(bin % 1000000).padStart(6, '0'), left: 30 - (now % 30) };
}
function showCode(code, remain, modeLabel) {
  $('code').textContent = code;
  left = remain; span = 30;
  $('mode').textContent = modeLabel;
  tick();
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
    if (left <= 0) { clearInterval(timer); refresh(); return; }
    draw();
  }, 1000);
}
async function refresh() {
  if (localSeed) {
    try { const r = await localCode(localSeed); showCode(r.code, r.left, 'local mode — computed in this browser'); }
    catch (e) { $('err').textContent = 'bad seed'; }
    return;
  }
  fetchCode();
}
async function fetchCode() {
  const key = $('key').value.trim(), slot = ($('slot').value.trim() || 'default');
  $('err').textContent = '';
  if (!key) { $('err').textContent = 'enter provider key (or use seed instead)'; return; }
  try {
    const r = await fetch('/code?key=' + encodeURIComponent(key) + '&slot=' + encodeURIComponent(slot));
    const d = await r.json();
    if (!r.ok || !d.ok) {
      if (r.status === 403) {
        try { localStorage.removeItem('ig_key'); } catch (e) {}
        $('err').textContent = looksLikeSeed(key)
          ? 'that looks like a SEED, not the key — use “use seed instead” below'
          : 'wrong key — saved key cleared, re-enter it';
      } else { $('err').textContent = d.error || 'failed'; }
      return;
    }
    showCode(d.code, d.expires_in_sec, '');
    try {
      localStorage.setItem('ig_slot', slot);
      if ($('remember').checked) localStorage.setItem('ig_key', key);
      else localStorage.removeItem('ig_key');
    } catch (e) {}
    tick();
  } catch (e) { $('err').textContent = 'network error'; }
}
function setSeedMode(seed) {
  localSeed = cleanSeed(seed || $('seed').value);
  if (!localSeed || !looksLikeSeed(localSeed)) { $('err').textContent = 'paste a valid seed first'; return; }
  $('err').textContent = '';
  $('seedBox').style.display = 'none';
  $('serverBox').style.display = '';
  $('seedWarn').style.display = 'block';
  $('swapLink').textContent = 'use provider key instead';
  refresh();
}
function setServerMode() {
  localSeed = '';
  $('seedWarn').style.display = 'none';
  $('swapLink').textContent = 'use seed instead';
  $('mode').textContent = '';
}
$('show').onclick = () => {
  if ($('seedBox').style.display !== 'none' && !localSeed) { setSeedMode(); return; }
  refresh();
};
$('swapLink').onclick = () => {
  if (localSeed) { setServerMode(); return; }
  if ($('seedBox').style.display === 'none') { $('seedBox').style.display = ''; $('serverBox').style.display = 'none'; }
  else { setSeedMode(); }
};
$('forgetLink').onclick = () => {
  try { localStorage.removeItem('ig_key'); localStorage.removeItem('ig_slot'); } catch (e) {}
  $('key').value = ''; setServerMode(); $('err').textContent = 'saved data cleared';
};
$('copy').onclick = async () => {
  const t = $('code').textContent;
  if (/^\d{6}$/.test(t)) { try { await navigator.clipboard.writeText(t); $('meta').textContent = 'copied'; } catch (e) {} }
};
$('key').addEventListener('keydown', (e) => { if (e.key === 'Enter') refresh(); });
(function init() {
  try {
    $('slot').value = localStorage.getItem('ig_slot') || 'default';
    if (localStorage.getItem('ig_key')) { $('key').value = localStorage.getItem('ig_key'); $('remember').checked = true; }
  } catch (e) {}
  const q = new URLSearchParams(location.search);
  const s = q.get('seed'), sl = q.get('slot'), k = q.get('key');
  if (s && looksLikeSeed(s)) { setSeedMode(s); return; }
  if (sl) $('slot').value = sl;
  if (k) { $('key').value = k; fetchCode(); }
})();
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
