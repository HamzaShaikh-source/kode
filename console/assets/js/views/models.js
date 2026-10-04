// Models: custom API profiles (base URL + key + protocol) and which agents can use which.
import { S } from '../store.js';
import { h, icon, setText, reconcile, clear, fmtNum } from '../util.js';
import { chip, modal, toast } from '../components.js';
import { openProfile, PROTOCOLS, profileFit } from '../dialogs.js';
import * as hub from '../hubclient.js';

const NOTES = { claude: 'Needs an endpoint that speaks the Anthropic Messages API', codex: 'Needs an endpoint that implements the OpenAI Responses API', opencode: 'Works with any OpenAI-compatible endpoint' };
const AGENTS = [['claude', 'Claude Code'], ['codex', 'Codex'], ['opencode', 'OpenCode'], ['hermes', 'Hermes'], ['agy', 'Antigravity'], ['shell', 'Shell']];

export function mount(root) {
  const list = h('div', { class: 'plist' });
  const addBtn = h('button', { class: 'btn primary', type: 'button', onclick: () => openProfile() }, icon('plus', 16), 'New profile');
  const empty = h('div', { class: 'card hero-empty empty-state' }, icon('key', 40), h('h3', {}, 'No API profiles yet'),
    h('p', {}, 'Point a session at your own gateway or provider account: give it a base URL, a key and the protocol it speaks.'),
    h('button', { class: 'btn primary', type: 'button', onclick: () => openProfile() }, icon('plus', 16), 'Add your first profile'));
  const matrix = h('div', { class: 'card', style: { overflow: 'auto' } });
  root.append(h('div', { class: 'page' },
    h('div', { class: 'note' }, h('b', {}, 'How it works. '), 'A profile applies to one session at a time through that agent\'s own launch settings. Your global agent configuration is never edited, and the key is injected only into that session and redacted from its output.'),
    h('div', { class: 'sec-h' }, h('h2', {}, 'API profiles'), h('p', {}, 'Also used by the Orchestrator')), empty, list,
    h('div', { class: 'sec-h' }, h('h2', {}, 'Which agent can use which protocol')), matrix));

  function create(p) {
    const r = {};
    const el = h('article', { class: 'card prow' },
      h('div', { class: 'prow-main' }, h('div', { class: 'prow-t' }, r.name = h('h3'), r.proto = h('span', { class: 'chip info' }), r.key = h('span', { class: 'chip' })),
        r.url = h('div', { class: 'mono faint prow-u' }), r.models = h('div', { class: 'prow-m' }), r.meta = h('div', { class: 'faint', style: { fontSize: '12.5px' } }), r.res = h('div', { 'aria-live': 'polite' })),
      h('div', { class: 'prow-a' },
        r.test = h('button', { class: 'btn sm', type: 'button' }, icon('bolt', 13), 'Test connection'),
        r.edit = h('button', { class: 'btn sm', type: 'button' }, icon('sliders', 13), 'Edit'),
        r.del = h('button', { class: 'btn sm danger', type: 'button', 'aria-label': 'Delete profile' }, icon('trash', 14))));
    r.test.addEventListener('click', async () => {
      r.test.disabled = true; clear(r.res).append(h('span', { class: 'faint' }, 'Testing…'));
      try {
        const x = await hub.call({ t: 'test_profile', id: el._id }, 25000);
        clear(r.res).append(chip(x.ok ? `Reachable (${x.ms} ms)` : 'Failed', x.ok ? 'ok' : 'bad', x.ok ? 'check' : 'alert'), h('span', { class: 'muted', style: { marginLeft: '8px', fontSize: '12.5px' } }, x.detail || x.err || ''));
      } catch (e) { clear(r.res).append(chip('Failed', 'bad', 'alert'), h('span', { class: 'muted', style: { marginLeft: '8px' } }, e.message)); }
      r.test.disabled = false;
    });
    r.edit.addEventListener('click', () => openProfile(S.profiles.find((x) => x.id === el._id)));
    r.del.addEventListener('click', () => {
      const close = modal({ title: 'Delete this profile?', body: h('p', { class: 'muted' }, `${el._name} and its saved key will be removed. Running sessions keep working; new ones cannot use it.`),
        actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn danger solid', type: 'button', onclick: async () => { const x = await hub.call({ t: 'delete_profile', id: el._id }); S.profiles = x.profiles || []; close(); toast('Profile deleted'); update(); } }, 'Delete')] });
    });
    el._r = r; return el;
  }
  function upd(el, p) {
    const r = el._r; el._id = p.id; el._name = p.name;
    setText(r.name, p.name); setText(r.proto, PROTOCOLS[p.protocol] || p.protocol);
    setText(r.key, p.has_key ? `key saved, ends in ${p.key_hint}` : 'no key'); r.key.className = 'chip ' + (p.has_key ? 'ok' : 'warn');
    setText(r.url, p.base_url);
    const k = JSON.stringify([p.models, p.default_model]);
    if (r.models._k !== k) { r.models._k = k; r.models.replaceChildren(...(p.models.length ? p.models : ['no models listed']).map((m) => h('span', { class: 'mchip' + (m === p.default_model ? ' on' : '') }, m))); }
    setText(r.meta, `Context ${p.context_window ? fmtNum(p.context_window) + ' tokens' : 'unknown'}  |  ${p.price_in || p.price_out ? `$${p.price_in || 0} in / $${p.price_out || 0} out per 1M (your prices)` : 'no prices set, cost shows unknown'}`);
  }
  function drawMatrix() {
    const protos = Object.keys(PROTOCOLS);
    clear(matrix).append(h('table', { class: 'table' }, h('thead', {}, h('tr', {}, h('th', {}, 'Agent'), ...protos.map((p) => h('th', {}, PROTOCOLS[p])), h('th', {}, 'Note'))),
      h('tbody', {}, AGENTS.map(([id, label]) => {
        const cells = protos.map((p) => { const [ok] = profileFit(id, { protocol: p }); return h('td', {}, ok ? chip('Yes', 'ok', 'check') : h('span', { class: 'faint' }, '-')); });
        const [any, why] = profileFit(id, { protocol: 'none' });
        return h('tr', {}, h('td', {}, label), ...cells, h('td', { class: 'muted', style: { fontSize: '12.5px' } }, NOTES[id] || why));
      }))));
  }
  drawMatrix();
  function update() {
    empty.style.display = S.profiles.length ? 'none' : '';
    reconcile(list, S.profiles, (p) => p.id, create, upd);
  }
  hub.loadProfiles();
  return { meta: () => ({ title: 'Models', sub: 'Custom endpoints and who can use them', actions: [{ key: 'm', el: addBtn }] }), update, destroy() {} };
}
