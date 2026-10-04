// Member home: "My machine". Consent requests, kill switch, my sessions (full control for me), suggestions from the lead.
import { S, nodeById, sessionsOf } from '../store.js';
import { h, icon, setText, reconcile, clear } from '../util.js';
import { chip, createTile, updateTile, toast } from '../components.js';
import { openNewSession } from '../dialogs.js';
import * as hub from '../hubclient.js';

export function mount(root) {
  const reqBox = h('div', { class: 'reqs' });
  const kill = h('div', { class: 'killswitch' }, icon('shield', 26), h('div', { class: 'grow' }, h('b', {}, 'Kill switch'), h('span', { class: 'muted', style: { fontSize: '13px' } }, 'Instantly returns every session on this machine to watch-only and discards pending requests.')),
    h('button', { class: 'btn danger solid', type: 'button', onclick: async () => { const r = await hub.call({ t: 'revoke' }); toast(r.n ? `Revoked control on ${r.n} session(s)` : 'Nothing was granted. All sessions are already watch-only.'); } }, 'Pull kill switch'));
  const status = h('div', { class: 'note' });
  const tiles = h('div', { class: 'tiles big' });
  const none = h('div', { class: 'card empty-state hero-empty' }, icon('terminal', 40), h('h3', {}, 'No sessions on this machine'), h('p', {}, 'Start one to work with a provider here. The lead can watch it, and only gets control if you approve.'),
    h('button', { class: 'btn primary', type: 'button', onclick: () => openNewSession() }, icon('play', 14), 'Start a session'));
  const sugBox = h('div');
  const newBtn = h('button', { class: 'btn primary', type: 'button', onclick: () => openNewSession() }, icon('play', 14), 'New session');
  root.append(h('div', { class: 'page', style: { display: 'grid', gap: '18px' } }, status, reqBox, kill, h('div', { class: 'sec-h', style: { margin: '10px 0 0' } }, h('h2', {}, 'Your sessions')), tiles, none, sugBox));

  function answer(r, allow, minutes) { hub.call({ t: 'answer', rid: r.rid, allow, minutes }).then(() => toast(allow ? 'Allowed' : 'Denied')); }
  function reqCard(r) {
    const mins = h('select', { 'aria-label': 'Duration', style: { width: 'auto', height: '32px' } }, [['5', '5 min'], ['10', '10 min'], ['30', '30 min'], ['60', '1 hour']].map(([v, l]) => h('option', { value: v }, l)));
    const isStart = r.kind === 'start';
    return h('div', { class: 'req' }, icon('hand', 22), h('div', { class: 'grow' }, h('b', {}, isStart ? `The lead wants to start ${r.label} on your machine` : `The lead is asking for ${r.mode === 'takeover' ? 'exclusive takeover' : r.mode} of ${r.label}`),
      h('div', { class: 'muted', style: { fontSize: '13px' } }, isStart ? 'It runs inside your workspace folder only.' : r.mode === 'takeover' ? 'You will not be able to type while it lasts.' : 'You can both type. You can revoke it any time.')),
      isStart ? null : mins, h('button', { class: 'btn', type: 'button', onclick: () => answer(r, false) }, 'Deny'), h('button', { class: 'btn primary', type: 'button', onclick: () => answer(r, true, Number(mins.value)) }, 'Allow'));
  }
  function update() {
    const n = nodeById(S.nodeId), ss = sessionsOf(S.nodeId);
    clear(status).append(n ? h('span', {}, h('b', {}, n.name), `  ·  ${n.hostname} · `, chip(n.online ? 'connected to the Brain' : 'offline', n.online ? 'ok' : 'bad')) : 'Waiting for your machine…');
    const sig = JSON.stringify(S.requests.map((r) => r.rid));
    if (reqBox._sig !== sig) { reqBox._sig = sig; clear(reqBox).append(...S.requests.map(reqCard)); }
    reconcile(tiles, ss, (s) => s.sid, (s) => createTile(s.sid), (t, s) => updateTile(t, s, n, false));
    none.style.display = ss.length ? 'none' : '';
    const sg = S.suggestions.slice(0, 5);
    const sig2 = JSON.stringify(sg.map((x) => x.ts));
    if (sugBox._sig !== sig2) { sugBox._sig = sig2; clear(sugBox).append(...(sg.length ? [h('div', { class: 'sec-h' }, h('h2', {}, 'Suggestions from the lead')), h('div', { class: 'card' }, h('ul', { class: 'timeline', style: { padding: '0 18px' } }, sg.map((x) => h('li', {}, h('span', {}, `${x.label}: ${x.text}`)))))] : [])); }
  }
  return { meta: () => ({ title: 'My machine', sub: 'You are in control of what the lead can do', actions: [{ key: 'n', el: newBtn }] }), update, destroy() { tiles.querySelectorAll('.tile').forEach((t) => t._destroy && t._destroy()); } };
}
