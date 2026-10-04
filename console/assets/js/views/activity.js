import { S, isLead } from '../store.js';
import { h, icon, rel, clear, setText } from '../util.js';
import { chip } from '../components.js';

const KIND = { pair: ['link', 'ok'], online: ['wifi', 'ok'], offline: ['wifi', 'bad'], start: ['play', 'accent'], stop: ['stop', ''], session: ['terminal', ''], mode: ['hand', 'warn'], request: ['hand', 'warn'],
  answer: ['check', 'ok'], revoke: ['shield', 'bad'], suggestion: ['send', 'info'], invite: ['key', ''], remove: ['trash', 'bad'], config: ['sliders', ''], input: ['terminal', 'warn'] };

export function mount(root) {
  let kind = 'all', full = null;
  const chips = h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } });
  const list = h('ul', { class: 'timeline card', style: { padding: '4px 18px' } });
  const loadBtn = h('button', { class: 'btn', type: 'button', onclick: async () => {
    const r = await fetch('/hub/audit?limit=300', { headers: { 'x-kode-key': S.key } });
    if (r.ok) { full = (await r.json()).map((a) => ({ ts: a.ts * 1000, kind: a.action, text: (a.detail || a.action) + (a.actor ? ` — ${a.actor}` : ''), node: a.node, sid: a.sid })); update(); }
  } }, icon('shield', 15), 'Load full audit log');
  root.append(h('div', { class: 'page' }, h('div', { class: 'filters' }, chips, h('span', { class: 'grow' }), isLead() ? loadBtn : null), list));
  function update() {
    const evs = (full || [...S.events].reverse()).filter((e) => kind === 'all' || e.kind === kind);
    const kinds = ['all', ...new Set((full || S.events).map((e) => e.kind))];
    const sig = kinds.join('|') + kind;
    if (chips._sig !== sig) { chips._sig = sig; chips.replaceChildren(...kinds.map((k) => h('button', { class: 'pillbtn', type: 'button', 'aria-pressed': String(k === kind), onclick: () => { kind = k; update(); } }, k))); }
    clear(list).append(...(evs.length ? evs.map((e) => { const [ic, cls] = KIND[e.kind] || ['pulse', '']; return h('li', {}, chip('', cls, ic), h('span', {}, e.text), h('time', {}, rel(e.ts))); })
      : [h('li', {}, h('div', { class: 'empty-state', style: { width: '100%' } }, icon('pulse', 34), h('h3', {}, 'No activity yet'), h('p', {}, 'Pairing, session starts, control requests and kill-switch use all appear here.')))]));
  }
  const iv = setInterval(update, 15000);
  return { meta: () => ({ title: 'Activity', sub: full ? 'Full audit log' : 'Latest events', actions: [] }), update, destroy() { clearInterval(iv); } };
}
