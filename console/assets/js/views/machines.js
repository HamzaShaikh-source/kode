import { S, sessionsOf } from '../store.js';
import { h, icon, setText, reconcile, rel, clear } from '../util.js';
import { chip, modal, toast } from '../components.js';
import { openAddMachine, openNewSession } from '../dialogs.js';
import * as hub from '../hubclient.js';

export function mount(root) {
  const list = h('div', { style: { display: 'grid', gap: '16px' } });
  const empty = h('div', { class: 'card hero-empty empty-state' }, icon('user', 40), h('h3', {}, 'No teammates yet'), h('p', {}, 'Invite a teammate: name them, choose your access level, and send the one-time message.'),
    h('button', { class: 'btn primary', type: 'button', onclick: openAddMachine }, icon('plus', 16), 'Invite a teammate'));
  const addBtn = h('button', { class: 'btn primary', type: 'button', onclick: openAddMachine }, icon('plus', 16), 'Invite teammate');
  root.append(h('div', { class: 'page' }, list, empty));

  function create(n) {
    const r = {};
    const el = h('article', { class: 'card' },
      h('div', { class: 'card-h' }, h('span', { class: 'mach-ic' }, icon('laptop', 19)), h('div', { style: { flex: 1, minWidth: 0 } }, r.name = h('h3'), r.sub = h('small', { class: 'faint' })), r.badges = h('div', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap' } }),
        r.newBtn = h('button', { class: 'btn sm', type: 'button' }, icon('play', 13), 'New session'), r.rm = h('button', { class: 'btn sm danger', type: 'button', 'aria-label': 'Remove machine' }, icon('trash', 14))),
      h('div', { class: 'card-b', style: { display: 'grid', gap: '14px' } },
        h('div', {}, h('div', { class: 'faint', style: { fontSize: '12px', fontWeight: 600, letterSpacing: '.04em', textTransform: 'uppercase', marginBottom: '8px' } }, 'Providers on this machine'), r.provs = h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } })),
        h('div', {}, h('div', { class: 'faint', style: { fontSize: '12px', fontWeight: 600, letterSpacing: '.04em', textTransform: 'uppercase', marginBottom: '8px' } }, 'Ready for projects'), r.ready = h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } })),
        h('div', {}, h('div', { class: 'faint', style: { fontSize: '12px', fontWeight: 600, letterSpacing: '.04em', textTransform: 'uppercase', marginBottom: '8px' } }, 'Workspace roots (sessions cannot leave these)'), r.roots = h('div', { class: 'mono', style: { fontSize: '12.5px' } })),
        r.meta = h('div', { class: 'faint', style: { fontSize: '12.5px' } })));
    r.newBtn.addEventListener('click', () => openNewSession(el._id));
    r.rm.addEventListener('click', () => {
      const close = modal({ title: 'Remove this teammate?', body: h('p', { class: 'muted' }, `${el._name} will be disconnected and must be paired again with a new code. Its sessions disappear from the Brain.`),
        actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn danger solid', type: 'button', onclick: () => { hub.send({ t: 'remove_node', node: el._id }); close(); toast('Machine removed'); } }, 'Remove')] });
    });
    el._r = r; return el;
  }
  function upd(el, n) {
    const r = el._r; el._id = n.id; el._name = n.name;
    setText(r.name, n.member_name && n.member_name !== n.name ? `${n.member_name} (${n.name})` : n.name); setText(r.sub, `${n.hostname} · ${n.os}${n.version ? ' · agent v' + n.version : ''}`);
    r.newBtn.disabled = !n.online;
    const k = JSON.stringify([n.online, n.same_host, n.allow_remote_start, n.providers, n.roots, n.access, n.info]);
    if (r.badges._k !== k) {
      r.badges._k = k;
      r.badges.replaceChildren(...[chip(n.online ? 'Online' : 'Offline', n.online ? 'ok' : 'bad'), n.same_host ? chip('same PC as the Brain', 'info') : null, chip({ ask: 'Ask each time', copilot: 'Co-pilot allowed', full: 'Full access' }[n.access || 'ask'], n.access === 'full' ? 'accent' : n.access === 'copilot' ? 'warn' : '', 'hand'), n.access !== 'full' ? h('button', { class: 'btn sm', type: 'button', onclick: async () => { const x = await hub.call({ t: 'set_access', node: n.id, access: 'full' }); toast(x.ok ? 'Full access set (applies if the machine allows it)' : 'Could not change access', x.ok ? '' : 'bad'); } }, 'Give full access') : null, chip(n.allow_remote_start ? 'Lead may start sessions' : 'Starts need approval', n.allow_remote_start ? 'warn' : '', 'lock')].filter(Boolean));
      r.provs.replaceChildren(...(n.providers || []).map((p) => chip(p.label, p.available ? 'ok' : '', p.available ? 'check' : 'x')));
      const inf = n.info || {};
      r.ready.replaceChildren(...(inf.checked ? [chip(inf.git ? 'git installed' : 'git missing', inf.git ? 'ok' : 'bad', inf.git ? 'check' : 'x'), chip(inf.gh ? `GitHub: ${inf.gh_user || 'logged in'}` : 'GitHub CLI not logged in (gh auth login)', inf.gh ? 'ok' : 'bad', inf.gh ? 'check' : 'x'), chip(inf.git_email ? `commits as ${inf.git_name || ''} <${inf.git_email}>` : 'no git email set', inf.git_email ? 'ok' : 'warn')] : [h('span', { class: 'faint' }, n.online ? 'Checking...' : 'Offline')]));
      r.roots.replaceChildren(...(n.roots || []).map((x) => h('div', {}, x)));
    }
    setText(r.meta, `${sessionsOf(n.id).length} session(s) · ${n.online ? 'connected now' : 'last seen ' + rel(n.last_seen * 1000)}`);
  }
  function update() {
    empty.style.display = S.nodes.length ? 'none' : '';
    reconcile(list, [...S.nodes].sort((a, b) => (b.online - a.online) || a.name.localeCompare(b.name)), (n) => n.id, create, upd);
  }
  return { meta: () => ({ title: 'Team', sub: `${S.nodes.filter((n) => n.online).length} online of ${S.nodes.length} teammates`, actions: [{ key: 'a', el: addBtn }] }), update, destroy() {} };
}
