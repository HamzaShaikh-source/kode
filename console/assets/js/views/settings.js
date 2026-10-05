// Settings (lead) / Privacy (member): explains exactly what is shared and who can do what.
import { S, isLead, nodeById } from '../store.js';
import { h, icon, setText, clear } from '../util.js';
import { chip } from '../components.js';
import { openAddMachine, teamUrlPicker } from '../dialogs.js';
import * as hub from '../hubclient.js';
import { toast } from '../components.js';
import { prefs, setPrefs, permission, askPermission, fire } from '../notify.js';

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
    const fieldRow = (label, help, el) => h('div', { style: { display: 'grid', gridTemplateColumns: '1fr minmax(120px, 220px)', gap: '14px', padding: '12px 18px', borderBottom: '1px solid var(--line)', alignItems: 'center' } }, h('div', {}, h('b', { style: { fontWeight: 600 } }, label), h('p', { class: 'muted', style: { fontSize: '12.5px', marginTop: '2px' } }, help)), el);
    const save = (key, v) => hub.call({ t: 'settings_set', key, value: v }).then((r) => { if (!r.ok) toast(r.err || 'Not saved', 'bad'); else toast('Saved'); }).catch((e) => toast(e.message, 'bad'));
    const num = (key, min, max, ph) => { const i = h('input', { type: 'number', min, max, placeholder: ph, 'aria-label': key, style: { width: '100%' } }); i.addEventListener('change', () => save(key, i.value)); return i; };
    const sel = (key, opts) => { const s = h('select', { 'aria-label': key, style: { width: '100%' } }, opts.map(([v, l]) => h('option', { value: v }, l))); s.addEventListener('change', () => save(key, s.value)); return s; };
    const ctl = { 'proj.max_fix_rounds': num('proj.max_fix_rounds', 0, 8, '3'), 'proj.polish_rounds': num('proj.polish_rounds', 0, 4, '2'), 'proj.judge_pass': num('proj.judge_pass', 0, 100, '70'), 'proj.reuse': num('proj.reuse', 1, 12, '4'), 'proj.stall_min': num('proj.stall_min', 3, 60, '8'),
      'proj.default_permissions': sel('proj.default_permissions', [['', 'Turbo (default)'], ['turbo', 'Turbo'], ['auto', 'Supervised'], ['ask', 'Ask me']]), 'proj.default_review': sel('proj.default_review', [['', 'I review (default)'], ['human', 'I review'], ['auto', 'Finish automatically']]),
      'proj.default_questions': sel('proj.default_questions', [['', 'Ask me first (default)'], ['1', 'Ask me first'], ['0', 'Do not ask']]), 'proj.default_backup': h('input', { type: 'text', placeholder: 'commandcode,claude', 'aria-label': 'backup agents', style: { width: '100%' } }), 'notify.webhook': h('input', { type: 'text', placeholder: 'https://hooks.slack.com/... or a Discord webhook', 'aria-label': 'webhook', style: { width: '100%' } }) };
    ctl['proj.default_backup'].addEventListener('change', () => save('proj.default_backup', ctl['proj.default_backup'].value.replace(/\s/g, '')));
    ctl['notify.webhook'].addEventListener('change', () => save('notify.webhook', ctl['notify.webhook'].value));
    hub.call({ t: 'settings_get' }).then((r) => { if (r.ok) for (const [k, el] of Object.entries(ctl)) el.value = r.values[k] || ''; }).catch(() => {});
    const desk = h('select', { 'aria-label': 'desktop notifications', style: { width: '100%' } }, h('option', { value: '1' }, 'On'), h('option', { value: '0' }, 'Off')); desk.value = prefs().desktop ? '1' : '0';
    desk.addEventListener('change', async () => { setPrefs({ desktop: desk.value === '1' }); if (desk.value === '1' && permission() === 'default') { const p = await askPermission(); if (p !== 'granted') toast('The browser did not allow notifications. Allow them in the site settings.', 'bad'); } });
    const snd = h('select', { 'aria-label': 'sound', style: { width: '100%' } }, h('option', { value: '1' }, 'On'), h('option', { value: '0' }, 'Off')); snd.value = prefs().sound ? '1' : '0';
    snd.addEventListener('change', () => setPrefs({ sound: snd.value === '1' }));
    const projCard = h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('sliders', 17), h('h3', {}, 'Projects')),
      fieldRow('Fix rounds', 'How many times the planner may create fix tasks when the tests fail. 0 means stop at the first failure.', ctl['proj.max_fix_rounds']),
      fieldRow('Polish rounds', 'How many times the quality review may send the team back to fix README, tests or hygiene.', ctl['proj.polish_rounds']),
      fieldRow('Judge pass mark', 'Below this score out of 100 the team gets one improvement round before the final judgement.', ctl['proj.judge_pass']),
      fieldRow('Tasks per agent session', 'A teammate agent session is reused for this many tasks, then replaced by a fresh one so its context stays small.', ctl['proj.reuse']),
      fieldRow('Call a task stuck after (minutes)', 'After half of this the planner decides what to do; after all of it you get a card.', ctl['proj.stall_min']),
      fieldRow('Default agent permissions', 'Used by the New project dialog.', ctl['proj.default_permissions']),
      fieldRow('Default review', 'Whether projects wait for your approval after verify, polish and judge.', ctl['proj.default_review']),
      fieldRow('Default: ask questions first', 'Whether the planner asks you questions before it plans.', ctl['proj.default_questions']),
      fieldRow('Default backup agents', 'Comma separated, in order, for example commandcode,claude. Used when an agent hits its limit or breaks.', ctl['proj.default_backup']));
    const notCard = h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('bolt', 17), h('h3', {}, 'Notifications')),
      fieldRow('Desktop notifications', 'When the tab is in the background: questions, plan ready, review needed, stuck tasks, agent switches, finished or failed.', desk),
      fieldRow('Sound', 'A short tone with each notification.', snd),
      fieldRow('Webhook (Slack, Discord or any https URL)', 'The Brain posts agent switches and failovers here, so you hear about them even with the console closed.', ctl['notify.webhook']),
      h('div', { style: { padding: '12px 18px' } }, h('button', { class: 'btn sm', type: 'button', onclick: () => fire('K.O.D.E.', 'This is what a notification looks like.') }, 'Send a test notification')));
    body.insertBefore(projCard, body.children[1]); body.insertBefore(notCard, body.children[2]);
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
