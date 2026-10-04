// Tiny helpers. DOM is built with h() (text nodes only) - no raw HTML from the network ever reaches innerHTML.
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, kids);
  return el;
}
export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return el;
}
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
export const setText = (el, t) => { t = String(t); if (el.textContent !== t) el.textContent = t; };
export const setClass = (el, c) => { if (el.className !== c) el.className = c; };

export const fmtNum = (n) => {
  n = Number(n) || 0;
  return n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k' : String(n);
};
export const fmtBytes = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? (n / 1024).toFixed(0) + ' KB' : n + ' B');
export function rel(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
export const mmss = (sec) => { sec = Math.max(0, Math.round(sec)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); };
// Show the END of a path (the useful part): last two folders, e.g. "…/demo_workspaces/jyoti".
export function shortPath(p, n = 34) {
  if (!p) return '';
  const parts = p.split(/[\\/]+/).filter(Boolean);
  const tail = parts.slice(-2).join('/');
  const s = parts.length > 2 ? '…/' + tail : p.split('\\').join('/');
  return s.length > n ? '…' + s.slice(-(n - 1)) : s;
}

// Keyed reconcile: create/update/reorder/remove children of `parent` so element identity (and any terminal inside) survives updates.
export function reconcile(parent, items, keyOf, create, update) {
  const map = parent._rk || (parent._rk = new Map());
  const seen = new Set();
  let prev = null;
  for (const it of items) {
    const k = keyOf(it);
    seen.add(k);
    let el = map.get(k);
    if (!el) { el = create(it); map.set(k, el); }
    update(el, it);
    const want = prev ? prev.nextSibling : parent.firstChild;
    if (el !== want) parent.insertBefore(el, want);
    prev = el;
  }
  for (const [k, el] of Array.from(map)) {
    if (!seen.has(k)) { if (el._destroy) el._destroy(); el.remove(); map.delete(k); }
  }
}

// ── icons (stroke icons, 24px grid). Static markup only. ──
const I = {
  brain: '<circle cx="12" cy="12" r="3"/><circle cx="4.5" cy="6" r="1.8"/><circle cx="19.5" cy="6" r="1.8"/><circle cx="4.5" cy="18" r="1.8"/><circle cx="19.5" cy="18" r="1.8"/><path d="M9.6 10.4 5.9 7.3M14.4 10.4l3.7-3.1M9.6 13.6l-3.7 3.1M14.4 13.6l3.7 3.1"/>',
  grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
  laptop: '<rect x="4.5" y="5" width="15" height="10.5" rx="2"/><path d="M2.5 19h19"/>',
  chart: '<path d="M5 20V11M12 20V4M19 20v-6"/>',
  pulse: '<path d="M3 12h4l2.2-6 4 12 2.3-6H21"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h11M19 17h1"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', x: '<path d="M6 6l12 12M18 6 6 18"/>', check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  play: '<path d="M7 5.5v13l11-6.5z"/>', stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2.2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>', key: '<circle cx="8" cy="15" r="3.5"/><path d="M10.5 12.5 19 4M16 7l2.5 2.5M14 9l2 2"/>',
  shield: '<path d="M12 3l7.5 3v5.5c0 4.4-3 7.8-7.5 9.5-4.5-1.7-7.5-5.1-7.5-9.5V6z"/>', eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  hand: '<path d="M9 11V5.5a1.5 1.5 0 0 1 3 0V10m0-2.5a1.5 1.5 0 0 1 3 0V11m0-2a1.5 1.5 0 0 1 3 0v6.5c0 3-2.3 5-5 5h-1.5c-2 0-3.3-1-4.5-3L4.5 14a1.5 1.5 0 0 1 2.4-1.7L9 14.5"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2.2"/><path d="M15.5 8.5V6.2a1.7 1.7 0 0 0-1.7-1.7H6.2a1.7 1.7 0 0 0-1.7 1.7v7.6a1.7 1.7 0 0 0 1.7 1.7h2.3"/>',
  alert: '<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4.5M12 17.2v.1"/>', send: '<path d="M4 12 20 4l-5.5 16-3-6.5z"/>',
  terminal: '<path d="M4.5 17l5.5-5-5.5-5M12.5 19h7"/>', user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c.8-3.6 3.6-5.5 7-5.5s6.2 1.9 7 5.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/>', moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l.8 12.5h9.4L17.5 7"/>', link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7L11.5 6.8M14 10a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 11 18.7l1.5-1.5"/>',
  bolt: '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>', clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>', folder: '<path d="M3.5 7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  branch: '<circle cx="6.5" cy="5.5" r="2"/><circle cx="6.5" cy="18.5" r="2"/><circle cx="17.5" cy="8.5" r="2"/><path d="M6.5 7.5v9M17.5 10.5c0 4-4 4.5-11 6"/>', cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>', list: '<path d="M9 6.5h11M9 12h11M9 17.5h11"/><circle cx="4.8" cy="6.5" r=".9"/><circle cx="4.8" cy="12" r=".9"/><circle cx="4.8" cy="17.5" r=".9"/>',
  expand: '<path d="M14 4.5h5.5V10M10 19.5H4.5V14M19.5 4.5 13.5 10.5M4.5 19.5l6-6"/>', wifi: '<path d="M3 9.5a14 14 0 0 1 18 0M6 13a9.5 9.5 0 0 1 12 0M9.2 16.4a5 5 0 0 1 5.6 0"/><circle cx="12" cy="19.2" r=".8"/>',
};
export function icon(name, size = 18, cls = '') {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size); s.setAttribute('height', size);
  s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', '1.7');
  s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round'); s.setAttribute('aria-hidden', 'true');
  if (cls) s.setAttribute('class', cls);
  s.innerHTML = I[name] || '';
  return s;
}

export const PROVIDERS = {
  claude: { name: 'Claude Code', color: '#c15f3c', letter: 'C' },
  codex: { name: 'Codex', color: '#3b3a36', letter: 'X' },
  opencode: { name: 'OpenCode', color: '#4b6a88', letter: 'O' },
  hermes: { name: 'Hermes', color: '#9a7316', letter: 'H' },
  agy: { name: 'Antigravity', color: '#3a6ea5', letter: 'A' },
  shell: { name: 'Shell', color: '#6b675d', letter: '>' },
};
export const providerMeta = (id, label) => ({ ...(PROVIDERS[id] || { name: label || id, color: '#6b675d', letter: (label || id || '?')[0].toUpperCase() }), name: label || (PROVIDERS[id] || {}).name || id });
export const MODE_INFO = {
  watch: { label: 'Watch', short: 'WATCH', hint: 'Read-only', cls: '' },
  suggest: { label: 'Suggest', short: 'SUGGEST', hint: 'Advise only', cls: 'info' },
  copilot: { label: 'Co-pilot', short: 'CO-PILOT', hint: 'Both can type', cls: 'warn' },
  takeover: { label: 'Takeover', short: 'TAKEOVER', hint: 'Exclusive', cls: 'bad' },
};
