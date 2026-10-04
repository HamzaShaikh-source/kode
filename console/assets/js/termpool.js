// One persistent xterm instance per session. It lives in a hidden parking element and is re-parented into whichever
// tile / focus view shows it, scaled with CSS (the PTY geometry never changes just because someone is looking).
const THEME = {
  background: '#1c1b19', foreground: '#ebe7dc', cursor: '#d97a52', cursorAccent: '#1c1b19', selectionBackground: '#514c42',
  black: '#2a2824', red: '#e0675c', green: '#8cc08a', yellow: '#e0b457', blue: '#7fa7d9', magenta: '#c892b8', cyan: '#78c2bf', white: '#d9d4c6',
  brightBlack: '#6f6a5e', brightRed: '#f08b80', brightGreen: '#a7d6a4', brightYellow: '#f0cb78', brightBlue: '#a1c0e8', brightMagenta: '#dcb0cd', brightCyan: '#99d8d4', brightWhite: '#f4f0e4',
};
// Must stay identical in spirit to hub/protocol.py REPLY_RE: terminal-generated answers, never user typing.
const REPLY = /^(?:\x1b\[(?:\d+;\d+R|\?[\d;]*c|>[\d;]*c|[\d;]*n|\?[\d;]*\$y|[\d;]*t|\?[\d;]*u)|\x1b\](?:4|10|11|12|104);[^\x07\x1b]*(?:\x07|\x1b\\)|\x1bP[^\x1b]*\x1b\\)+$/;
const BUF_CAP = 320000;
const pool = new Map();
const io = { sendReply() {}, sendInput() {}, canType: () => false, blocked() {} };
export const setIO = (x) => Object.assign(io, x);

let parking = null;
function park() {
  if (!parking) {
    parking = document.createElement('div');
    parking.setAttribute('aria-hidden', 'true');
    parking.style.cssText = 'position:fixed;left:-99999px;top:0;width:1100px;height:700px;visibility:hidden;pointer-events:none';
    document.body.appendChild(parking);
  }
  return parking;
}

export function ensure(sid, cols = 100, rows = 28) {
  let e = pool.get(sid);
  if (e) {
    if ((e.cols !== cols || e.rows !== rows) && cols >= 20 && rows >= 5) { e.cols = cols; e.rows = rows; e.term.resize(cols, rows); e.dirty = true; schedule(e); }
    return e;
  }
  const wrap = document.createElement('div');
  wrap.className = 'term-scale';
  park().appendChild(wrap);
  const term = new window.Terminal({ cols, rows, fontFamily: '"JetBrains Mono", Consolas, monospace', fontSize: 12, lineHeight: 1.15, scrollback: 1500,
    cursorBlink: false, allowProposedApi: true, theme: THEME, letterSpacing: 0 });
  term.open(wrap);
  const ta = wrap.querySelector('textarea');
  if (ta) { ta.tabIndex = -1; ta.setAttribute('aria-label', 'terminal input'); }
  e = { sid, cols, rows, term, wrap, pending: '', buf: '', responder: false, interactive: false, host: null, mode: 'tile', raf: 0, ro: null, dirty: true, ended: false };
  term.onData((d) => {
    if (REPLY.test(d)) { if (e.responder) io.sendReply(sid, d); return; }          // handshake answers: only the elected responder forwards them
    if (!e.interactive) return;
    if (io.canType(sid)) io.sendInput(sid, d); else io.blocked(sid);
  });
  pool.set(sid, e);
  return e;
}
export const has = (sid) => pool.has(sid);
export const entry = (sid) => pool.get(sid);

function schedule(e) {
  if (e.raf) return;
  e.raf = (document.hidden ? setTimeout : requestAnimationFrame)(() => { e.raf = 0; flush(e); }, 30);
}
function flush(e) {
  if (e.pending) { e.term.write(e.pending); e.pending = ''; }
  if (e.dirty) { e.dirty = false; fit(e); }
}
export function feed(sid, d) {
  const e = pool.get(sid);
  if (!e) return;
  e.buf = (e.buf + d).slice(-BUF_CAP);
  e.pending += d;
  schedule(e);
}
export function replay(sid, d, responder) {
  const e = pool.get(sid) || ensure(sid);
  e.responder = !!responder;
  e.buf = (d || '').slice(-BUF_CAP);
  e.term.reset();
  e.pending = e.buf;
  e.dirty = true;
  schedule(e);
}
export const setResponder = (sid, on) => { const e = pool.get(sid); if (e) e.responder = on; };
export function markExit(sid) {
  const e = pool.get(sid);
  if (e && !e.ended) { e.ended = true; e.pending += '\r\n\x1b[2m[session ended]\x1b[0m\r\n'; schedule(e); }
}

export function fit(e) {
  const host = e.host;
  const x = e.wrap.querySelector('.xterm');
  if (!host || !x || !x.offsetWidth) { if (host) setTimeout(() => { e.dirty = true; schedule(e); }, 120); return; }
  const natW = x.offsetWidth, natH = x.offsetHeight;
  e.wrap.style.width = natW + 'px'; e.wrap.style.height = natH + 'px';
  const hw = host.clientWidth || natW;
  let scale;
  if (e.mode === 'tile') {
    scale = hw / natW;
    host.style.height = Math.max(70, Math.ceil(natH * scale)) + 'px';
    e.wrap.style.left = '0px'; e.wrap.style.top = '0px';
  } else {
    const maxH = Math.max(320, window.innerHeight * 0.68);
    scale = Math.min(hw / natW, maxH / natH, 1.7);
    host.style.height = Math.ceil(natH * scale) + 'px';
    e.wrap.style.left = Math.max(0, (hw - natW * scale) / 2) + 'px';
    e.wrap.style.top = '0px';
  }
  e.wrap.style.transform = `scale(${scale})`;
}

export function attach(sid, host, mode = 'tile', interactive = false) {
  const e = pool.get(sid);
  if (!e) return null;
  if (e.host !== host) {
    if (e.ro) e.ro.disconnect();
    host.appendChild(e.wrap);
    e.host = host;
    e.ro = new ResizeObserver(() => { e.dirty = true; schedule(e); });
    e.ro.observe(host);
  }
  e.mode = mode; e.interactive = interactive;
  const ta = e.wrap.querySelector('textarea');
  if (ta) ta.tabIndex = interactive ? 0 : -1;
  e.wrap.style.pointerEvents = interactive ? 'auto' : 'none';
  e.dirty = true; schedule(e);
  return e;
}
export function detach(sid, host) {
  const e = pool.get(sid);
  if (!e || (host && e.host !== host)) return;
  if (e.ro) e.ro.disconnect();
  e.ro = null; e.host = null; e.interactive = false;
  park().appendChild(e.wrap);
}
export function dispose(sid) {
  const e = pool.get(sid);
  if (!e) return;
  if (e.ro) e.ro.disconnect();
  e.term.dispose(); e.wrap.remove(); pool.delete(sid);
}
export function visibleText(sid) {
  const e = pool.get(sid);
  if (!e) return '';
  const b = e.term.buffer.active, out = [];
  for (let i = 0; i < b.length; i++) { const l = b.getLine(i); if (l) out.push(l.translateToString(true).trimEnd()); }
  return out.join('\n').replace(/\n+$/, '');
}
export const focusTerm = (sid) => { const e = pool.get(sid); if (e) e.term.focus(); };
export const selection = (sid) => { const e = pool.get(sid); return e ? e.term.getSelection() : ''; };
