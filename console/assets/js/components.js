// Shared UI pieces: toast, modal, chips, the minimized live-console tile.
import { h, icon, setText, setClass, fmtNum, shortPath, providerMeta, MODE_INFO, $ } from './util.js';
import * as pool from './termpool.js';
import * as hub from './hubclient.js';

export function toast(msg, kind = '') {
  const t = h('div', { class: 'toast ' + kind }, icon(kind === 'bad' ? 'alert' : 'check', 16), h('span', {}, msg));
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 6000 : 4200);
}

export function modal({ title, body, actions = [], onClose, wide }) {
  const root = $('#modal-root');
  const prev = document.activeElement;
  const back = h('div', { class: 'modal-back' });
  const dlg = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, style: wide ? { width: 'min(640px,100%)' } : null },
    h('h2', {}, title), body, actions.length ? h('div', { class: 'row' }, actions) : null);
  back.append(dlg);
  const close = () => { back.remove(); document.removeEventListener('keydown', key, true); if (prev && prev.focus) prev.focus(); if (onClose) onClose(); };
  const key = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); }
    if (e.key === 'Tab') {                                   // keep focus inside the dialog
      const f = Array.from(dlg.querySelectorAll('button,input,select,textarea,a[href]')).filter((x) => !x.disabled && x.offsetParent !== null);
      if (!f.length) return;
      const a = f[0], z = f[f.length - 1];
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); } else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    }
  };
  back.addEventListener('mousedown', (e) => { if (e.target === back) close(); });
  document.addEventListener('keydown', key, true);
  root.append(back);
  const first = dlg.querySelector('input,select,textarea,button.primary,button');
  if (first) first.focus();
  return close;
}

export const chip = (text, cls = '', ic) => h('span', { class: 'chip ' + cls }, ic ? icon(ic, 12) : null, text);
export function modeChip(mode) { const m = MODE_INFO[mode] || MODE_INFO.watch; return chip(m.short, m.cls === 'warn' ? 'warn' : m.cls === 'bad' ? 'bad' : m.cls === 'info' ? 'info' : ''); }

export function dotClass(s) {
  if (s.state === 'live') return s.activity === 'working' ? 'dot work' : 'dot ok';
  if (s.state === 'starting') return 'dot warn';
  return 'dot bad';
}
export function stateText(s) {
  if (s.state === 'live') return s.activity === 'working' ? 'Working' : 'Idle';
  return { starting: 'Starting', exited: 'Ended', error: 'Failed to start', offline: 'Machine offline' }[s.state] || s.state;
}
export function ctxPct(s) {
  const u = s.usage || {};
  return u.context_window && u.context_used ? Math.min(100, Math.round((u.context_used / u.context_window) * 100)) : null;
}
export const tokensOf = (s) => {
  const u = s.usage || {};
  const t = (u.input_tokens || 0) + (u.output_tokens || 0);
  return u.source && u.source !== 'terminal' && t ? fmtNum(t) + ' tok' : '';
};

// ── minimized live console tile ──
export function createTile(sid) {
  const r = {};
  const open = () => { location.hash = '#/session/' + sid; };
  const el = h('div', { class: 'tile', role: 'button', tabindex: '0', onclick: open, onkeydown: (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === el) { e.preventDefault(); open(); } } },
    h('div', { class: 'tile-h' }, r.pg = h('span', { class: 'pg' }), h('div', { class: 'tt' }, r.title = h('b'), r.sub = h('small')), r.dot = h('span', { class: 'dot' }), r.mode = h('span', { class: 'chip' })),
    r.body = h('div', { class: 'tile-body' }, r.host = h('div', { class: 'term-fit' }), r.empty = h('div', { class: 'empty' }),
      r.ended = h('div', { class: 'ended', hidden: true }, r.endTxt = h('div'), h('div', { class: 'ended-acts' },
        r.restart = h('button', { class: 'btn sm primary', type: 'button', onclick: async (e) => { e.stopPropagation(); try { const x = await hub.call({ t: 'restart', sid }); if (!x.ok) throw new Error(x.err || 'could not restart'); toast(x.applied === false ? 'Request sent to the machine\'s owner' : 'Restarting…'); } catch (err) { toast(err.message, 'bad'); } } }, icon('play', 13), 'Restart'),
        h('button', { class: 'btn sm', type: 'button', onclick: (e) => { e.stopPropagation(); hub.send({ t: 'forget', sid }); } }, icon('trash', 13), 'Remove')))),
    h('div', { class: 'tile-f' }, r.need = h('span', { class: 'chip warn', hidden: true }, icon('hand', 11), 'Needs you'), r.act = h('span', { class: 'act' }), r.ctx = h('span', { class: 'ctxm', hidden: true }), r.tok = h('span', { class: 'tok' })));
  el._r = r; el._sid = sid;
  el._destroy = () => pool.detach(sid, r.host);
  return el;
}
export function updateTile(el, s, node, showNode) {
  const r = el._r, pm = providerMeta(s.provider, s.label), mi = MODE_INFO[s.mode] || MODE_INFO.watch;
  el.setAttribute('aria-label', `Open ${pm.name}${node ? ' on ' + node.name : ''}, ${stateText(s)}, control ${mi.label}`);
  setClass(el, 'tile' + (s.activity === 'working' ? ' working' : ''));
  setText(r.pg, pm.letter); r.pg.style.background = pm.color;
  setText(r.title, pm.name);
  setText(r.sub, showNode && node ? `${node.name} · ${shortPath(s.cwd, 26)}` : shortPath(s.cwd, 40));
  setClass(r.dot, dotClass(s));
  setText(r.mode, mi.short); setClass(r.mode, 'chip ' + (mi.cls === 'warn' ? 'warn' : mi.cls === 'bad' ? 'bad' : mi.cls === 'info' ? 'info' : ''));
  setText(r.act, s.state === 'live' ? (s.activity_text || (s.activity === 'working' ? 'Working…' : 'Waiting for input')) : stateText(s));
  setText(r.tok, tokensOf(s));
  r.need.hidden = !s.needs_input;
  const cp = ctxPct(s);
  r.ctx.hidden = cp == null;
  if (cp != null) { setText(r.ctx, `ctx ${cp}%`); r.ctx.className = 'ctxm' + (cp >= 85 ? ' hot' : cp >= 60 ? ' mid' : ''); r.ctx.title = 'Context window used'; }
  setClass(el, 'tile' + (s.activity === 'working' ? ' working' : '') + (s.needs_input ? ' needs' : ''));
  if (s.profile) r.sub.title = `${s.profile.name} / ${s.profile.model || 'default model'}`;
  const live = s.state === 'live' || s.state === 'exited';
  if (live) {
    pool.ensure(s.sid, s.cols || 100, s.rows || 28);
    const e = pool.entry(s.sid);
    if (!e.host || e.host !== r.host || e.mode !== 'tile') pool.attach(s.sid, r.host, 'tile', false);
  }
  const gone = s.state === 'exited' || s.state === 'error' || s.state === 'offline';
  r.ended.hidden = !gone;
  if (gone) setText(r.endTxt, s.state === 'exited' ? 'Session ended' : s.state === 'error' ? (s.error || 'Could not start') : 'Machine offline');
  const blank = !s.bytes_out && !gone;
  r.empty.style.display = blank ? 'grid' : 'none';
  if (blank) setText(r.empty, s.state === 'error' ? s.error || 'Could not start' : s.state === 'offline' ? 'Machine is offline' : 'Starting…');
  if (blank) r.host.style.height = '96px';
}
