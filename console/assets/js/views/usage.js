// Usage: real token numbers where a provider exposes them (Claude Code, Codex transcripts), "unknown" everywhere else.
import { S, nodeById } from '../store.js';
import { h, icon, setText, fmtNum, fmtBytes, mmss, providerMeta, clear } from '../util.js';
import { chip } from '../components.js';

const INSTR = new Set(['claude', 'codex']);        // providers whose transcripts we can read for real token counts

export function mount(root) {
  const R = {};
  const stat = (k, ic, label) => { const v = h('div', { class: 'v' }), s = h('div', { class: 's' }); R[k] = { v, s }; return h('div', { class: 'card stat' }, h('div', { class: 'k' }, icon(ic, 15), label), v, s); };
  const stats = h('section', { class: 'stats', style: { gridTemplateColumns: 'repeat(5,minmax(0,1fr))' } }, stat('in', 'arrow', 'Input tokens'), stat('out', 'send', 'Output tokens'), stat('cov', 'eye', 'Sessions measured'), stat('act', 'clock', 'Active terminal time'), stat('cost', 'chart', 'Estimated cost'));
  const provBody = h('tbody'), nodeBody = h('div', { style: { display: 'grid', gap: '14px' } }), chartBox = h('div');
  root.append(h('div', { class: 'page' }, stats,
    h('div', { class: 'note', style: { marginTop: '16px' } }, h('b', {}, 'How honest these numbers are. '), 'Token counts are read from each provider\'s own local transcript (Claude Code, Codex). Providers that do not expose usage show "unknown" instead of an estimate. Cost is only an estimate: tokens times the per-million prices you set on a Models profile. Without prices it stays unknown.'),
    h('div', { class: 'sec-h' }, h('h2', {}, 'By provider')),
    h('div', { class: 'card', style: { overflow: 'hidden' } }, h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['Provider', 'Sessions', 'Input', 'Output', 'Cache read', 'Est. cost', 'Source'].map((t, i) => h('th', { class: i && i < 5 ? 'num' : '' }, t)))), provBody)),
    h('div', { class: 'sec-h' }, h('h2', {}, 'By machine')), h('div', { class: 'card card-b' }, nodeBody),
    h('div', { class: 'sec-h' }, h('h2', {}, 'Recent history'), h('p', {}, 'Peak tokens per day, from sampled sessions')), h('div', { class: 'card card-b' }, chartBox)));

  async function loadHistory() {
    try {
      const r = await fetch('/hub/usage', { headers: { 'x-kode-key': S.key } });
      if (!r.ok) return;
      const u = await r.json();
      const byDay = {};
      for (const row of u.history || []) byDay[row.day] = (byDay[row.day] || 0) + (row.inp || 0) + (row.out || 0);
      const days = Object.keys(byDay).sort().slice(-14), max = Math.max(1, ...days.map((d) => byDay[d]));
      clear(chartBox);
      chartBox.append(days.length ? h('div', { class: 'chart' }, days.map((d) => h('div', { class: 'col', title: `${d}: ${byDay[d]} tokens` }, h('i', { style: { height: Math.max(3, (byDay[d] / max) * 110) + 'px' } }), h('span', {}, d.slice(5))))) : h('div', { class: 'empty-state' }, icon('chart', 34), h('p', {}, 'History builds up while instrumented sessions (Claude Code, Codex) are running.')));
    } catch { /* offline */ }
  }
  loadHistory();
  const iv = setInterval(loadHistory, 30000);

  function update() {
    let tin = 0, tout = 0, meas = 0, act = 0;
    const byProv = {}, byNode = {};
    let cost = 0, anyCost = false;
    for (const s of S.sessions) {
      const u = s.usage || {}, known = u.source && u.source !== 'terminal';
      const p = byProv[s.provider] || (byProv[s.provider] = { label: s.label, n: 0, i: 0, o: 0, c: 0, known: 0, src: new Set() });
      const nd = byNode[s.node_id] || (byNode[s.node_id] = { i: 0, o: 0, n: 0, act: 0 });
      p.n++; nd.n++; nd.act += u.active_seconds || 0; act += u.active_seconds || 0;
      if (s.cost_estimate != null) { p.cost = (p.cost || 0) + s.cost_estimate; cost += s.cost_estimate; anyCost = true; }
      if (known) { p.known++; p.i += u.input_tokens || 0; p.o += u.output_tokens || 0; p.c += u.cache_read || 0; p.src.add(u.source); nd.i += u.input_tokens || 0; nd.o += u.output_tokens || 0; tin += u.input_tokens || 0; tout += u.output_tokens || 0; meas++; }
    }
    setText(R.in.v, fmtNum(tin)); setText(R.in.s, 'across instrumented sessions');
    setText(R.out.v, fmtNum(tout)); setText(R.out.s, 'as reported by providers');
    setText(R.cov.v, `${meas}/${S.sessions.length}`); setText(R.cov.s, 'sessions with provider-reported tokens');
    setText(R.act.v, mmss(act)); setText(R.act.s, 'time producing output');
    setText(R.cost.v, anyCost ? '$' + cost.toFixed(2) : 'unknown'); setText(R.cost.s, anyCost ? 'estimate from your own prices' : 'set prices on a Models profile');
    clear(provBody).append(...(Object.keys(byProv).length ? Object.entries(byProv).map(([id, p]) => {
      const pm = providerMeta(id, p.label);
      return h('tr', {}, h('td', {}, h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '9px' } }, h('span', { class: 'pg', style: { background: pm.color } }, pm.letter), pm.name)),
        h('td', { class: 'num' }, p.n), ...(p.known ? [h('td', { class: 'num' }, fmtNum(p.i)), h('td', { class: 'num' }, fmtNum(p.o)), h('td', { class: 'num' }, fmtNum(p.c)), h('td', { class: 'num' }, p.cost != null ? '$' + p.cost.toFixed(4) : h('span', { class: 'faint' }, 'unknown')), h('td', {}, chip([...p.src].join(', '), 'ok', 'check'))]
          : INSTR.has(id) ? [h('td', { class: 'num faint' }, '-'), h('td', { class: 'num faint' }, '-'), h('td', { class: 'num faint' }, '-'), h('td', { class: 'num faint' }, '-'), h('td', {}, chip('instrumented: no messages yet', 'info'))]
          : [h('td', { class: 'num faint' }, 'unknown'), h('td', { class: 'num faint' }, 'unknown'), h('td', { class: 'num faint' }, 'unknown'), h('td', { class: 'num faint' }, 'unknown'), h('td', {}, chip('terminal only', ''))]));
    }) : [h('tr', {}, h('td', { colspan: 7 }, h('div', { class: 'empty-state' }, h('p', {}, 'No sessions yet.'))))]));
    const maxT = Math.max(1, ...Object.values(byNode).map((n) => n.i + n.o));
    clear(nodeBody).append(...(Object.keys(byNode).length ? Object.entries(byNode).map(([id, n]) => h('div', {}, h('div', { style: { display: 'flex', justifyContent: 'space-between', marginBottom: '6px', fontWeight: 500 } }, h('span', {}, (nodeById(id) || { name: id }).name), h('span', { class: 'muted' }, n.i + n.o ? fmtNum(n.i + n.o) + ' tokens' : `${n.n} session(s), tokens unknown`)),
      n.i + n.o ? h('div', { class: 'bar' }, h('i', { style: { width: Math.max(2, ((n.i + n.o) / maxT) * 100) + '%' } })) : null)) : [h('p', { class: 'muted' }, 'No machines yet.')]));
  }
  return { meta: () => ({ title: 'Usage', sub: 'What each provider actually reports', actions: [] }), update, destroy() { clearInterval(iv); } };
}
