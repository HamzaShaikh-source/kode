// Sessions: every provider session across machines, filterable, as big live consoles.
import { S, nodeById, isLead } from '../store.js';
import { h, icon, setText, reconcile, providerMeta } from '../util.js';
import { createTile, updateTile } from '../components.js';
import { openNewSession } from '../dialogs.js';

export function mount(root) {
  const F = { q: '', provider: 'all', node: 'all', state: 'all' };
  const q = h('input', { type: 'search', placeholder: 'Filter by name or folder', 'aria-label': 'Filter sessions', oninput: (e) => { F.q = e.target.value.toLowerCase(); update(); } });
  const pSel = h('select', { 'aria-label': 'Provider', onchange: (e) => { F.provider = e.target.value; update(); } });
  const nSel = h('select', { 'aria-label': 'Machine', onchange: (e) => { F.node = e.target.value; update(); } });
  const stateBtns = [['all', 'All'], ['working', 'Working'], ['idle', 'Idle'], ['ended', 'Ended']].map(([id, l]) =>
    h('button', { class: 'pillbtn', type: 'button', 'aria-pressed': id === 'all' ? 'true' : 'false', onclick: () => { F.state = id; stateBtns.forEach((b) => b.setAttribute('aria-pressed', b._id === id ? 'true' : 'false')); update(); } }, l));
  stateBtns.forEach((b, i) => { b._id = ['all', 'working', 'idle', 'ended'][i]; });
  const count = h('span', { class: 'muted' });
  const grid = h('div', { class: 'tiles big' });
  const empty = h('div', { class: 'card empty-state hero-empty' }, icon('grid', 40), h('h3', {}), h('p', {}));
  const newBtn = h('button', { class: 'btn primary', type: 'button', onclick: () => openNewSession() }, icon('play', 14), 'New session');
  root.append(h('div', { class: 'page' }, h('div', { class: 'filters' }, h('div', { style: { width: '240px' } }, q), pSel, nSel, h('div', { style: { display: 'flex', gap: '6px' } }, stateBtns), h('span', { class: 'grow' }), count), grid, empty));

  function fill(sel, vals, label) {
    const cur = sel.value || 'all';
    const sig = vals.map((v) => v[0]).join('|');
    if (sel._sig !== sig) { sel._sig = sig; sel.replaceChildren(h('option', { value: 'all' }, label), ...vals.map(([v, l]) => h('option', { value: v }, l))); sel.value = vals.some((v) => v[0] === cur) ? cur : 'all'; }
  }
  function update() {
    const provs = [...new Map(S.sessions.map((s) => [s.provider, providerMeta(s.provider, s.label).name]))];
    fill(pSel, provs, 'All providers');
    fill(nSel, S.nodes.map((n) => [n.id, n.name]), 'All machines');
    const list = S.sessions.filter((s) => {
      const n = nodeById(s.node_id);
      if (F.provider !== 'all' && s.provider !== F.provider) return false;
      if (F.node !== 'all' && s.node_id !== F.node) return false;
      if (F.state === 'working' && s.activity !== 'working') return false;
      if (F.state === 'idle' && !(s.state === 'live' && s.activity === 'idle')) return false;
      if (F.state === 'ended' && !['exited', 'error', 'offline'].includes(s.state)) return false;
      const hay = `${s.label} ${s.cwd} ${n ? n.name : ''}`.toLowerCase();
      return !F.q || hay.includes(F.q);
    }).sort((a, b) => (b.state === 'live') - (a.state === 'live') || b.created - a.created);
    setText(count, `${list.length} of ${S.sessions.length} sessions`);
    reconcile(grid, list, (s) => s.sid, (s) => createTile(s.sid), (t, s) => updateTile(t, s, nodeById(s.node_id), true));
    empty.style.display = list.length ? 'none' : '';
    if (!list.length) {
      setText(empty.querySelector('h3'), S.sessions.length ? 'No sessions match' : 'No sessions yet');
      setText(empty.querySelector('p'), S.sessions.length ? 'Try a different filter.' : 'Start a provider session on any connected machine and it appears here, live.');
    }
  }
  return {
    meta: () => ({ title: 'Sessions', sub: 'Every provider session, live', actions: [{ key: 'n', el: newBtn }] }),
    update,
    destroy() { grid.querySelectorAll('.tile').forEach((t) => t._destroy && t._destroy()); },
  };
}
