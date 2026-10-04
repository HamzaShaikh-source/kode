// Settings (lead) / Privacy (member): explains exactly what is shared and who can do what.
import { S, isLead, nodeById } from '../store.js';
import { h, icon, setText, clear } from '../util.js';
import { chip } from '../components.js';
import { openAddMachine, teamUrlPicker } from '../dialogs.js';
import * as hub from '../hubclient.js';

const row = (ic, title, text, extra) => h('div', { style: { display: 'flex', gap: '14px', padding: '14px 18px', borderBottom: '1px solid var(--line)', alignItems: 'flex-start' } },
  h('span', { class: 'mach-ic' }, icon(ic, 18)), h('div', { style: { flex: 1 } }, h('b', { style: { fontWeight: 600 } }, title), h('p', { class: 'muted', style: { fontSize: '13px', marginTop: '2px' } }, text)), extra || null);

export function mount(root) {
  const lead = isLead();
  const allow = h('input', { type: 'checkbox', id: 'allow-start', onchange: (e) => hub.send({ t: 'allow_remote_start', on: e.target.checked }) });
  const body = h('div', { class: 'page', style: { display: 'grid', gap: '18px', maxWidth: '860px' } },
    h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('shield', 17), h('h3', {}, lead ? 'How control works' : 'What the lead can and cannot do')),
      row('eye', 'Watch is the default', lead ? 'Every session is read-only for you until its owner says otherwise.' : 'The lead can watch your provider sessions. Only sessions started through K.O.D.E. are visible, never your whole desktop.'),
      row('hand', 'Control is granted, not taken', 'Suggest, co-pilot and takeover each need the machine\'s owner to approve. Co-pilot and takeover expire automatically (takeover after 10 minutes at most).'),
      row('lock', 'Enforced twice', 'The Brain and the member\'s own agent both check the control mode. A compromised Brain still cannot type into a session the owner kept read-only.'),
      row('shield', 'Kill switch', 'The owner can drop every session on their machine back to watch-only instantly, and discard pending requests.'),
      row('key', 'Secrets are scrubbed at the source', 'API keys, tokens and private keys are redacted by the agent on the machine before output leaves it. This is best-effort pattern matching, not a guarantee.'),
      row('list', 'Everything is audited', 'Pairing, starts, control grants and the lead\'s input (summarised and redacted) are written to an audit log.')),
    lead ? h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('laptop', 17), h('h3', {}, 'Machines'), h('span', { style: { flex: 1 } }), h('button', { class: 'btn primary sm', type: 'button', onclick: openAddMachine }, icon('plus', 14), 'Invite teammate')),
      row('link', 'Pairing codes', 'Codes are single-use and expire after 10 minutes. Machines authenticate afterwards with a long random token that is stored hashed.'),
      row('wifi', 'Network', 'Machines connect outward to the Brain, so no inbound ports are opened on teammates\' laptops. The Brain listens on localhost unless started with --lan.'))
      : h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('hand', 17), h('h3', {}, 'Your choices')),
        row('play', 'Let the lead start sessions without asking', 'Off by default. When off, you approve every start request.', h('label', { class: 'sr', for: 'allow-start' }, 'Allow lead to start sessions'), allow)),
    h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('sun', 17), h('h3', {}, 'About this build')),
      row('cpu', 'Honest limits', 'Terminal streaming, consent, audit, git status and token counts for Claude Code and Codex are real. Other providers show a live terminal only. Cost is never estimated.'),
      row('terminal', 'Coverage', 'Tested here with Claude Code, Codex, Hermes, OpenCode, Antigravity and a shell. See COMPAT.md in the repository for the measured matrix.')));
  if (lead) {
    const netCard = h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('wifi', 17), h('h3', {}, 'How teammates reach this Brain')), h('div', { class: 'card-b' }, h('p', { class: 'faint' }, 'Loading...')));
    body.insertBefore(netCard, body.children[1]);
    hub.call({ t: 'net_info' }).then((net) => { const b = netCard.querySelector('.card-b'); const paint = (n) => clear(b).append(teamUrlPicker(n, paint)); paint(net); }).catch(() => {});
  }
  root.append(body);
  return {
    meta: () => ({ title: lead ? 'Settings' : 'Privacy', sub: lead ? 'Security model and machines' : 'What is shared and what you control', actions: [] }),
    update() { const n = nodeById(S.nodeId); if (!lead && n) allow.checked = !!n.allow_remote_start; },
    destroy() {},
  };
}
