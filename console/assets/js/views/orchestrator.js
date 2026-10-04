// Orchestrator: one dedicated agent that watches and coordinates every session, always inside the consent ladder.
import { S } from '../store.js';
import { h, icon, setText, clear, rel, fmtNum } from '../util.js';
import { chip } from '../components.js';
import * as hub from '../hubclient.js';

const AUTONOMY = [
  ['observe', 'Observe', 'Reads and reports. Never types or starts anything.'],
  ['ask', 'Ask', 'Every write needs your click.'],
  ['auto', 'Auto', 'Writes run without asking, only where consent already exists.'],
];
const KIND = { lead: 'You', tool: 'Action', result: 'Result', final: 'Answer', system: 'System' };

export function mount(root) {
  const log = h('div', { class: 'olog', role: 'log', 'aria-live': 'polite', 'aria-label': 'Orchestrator conversation' });
  const pending = h('div', { class: 'opend' });
  const input = h('textarea', { id: 'orch-in', rows: 2, placeholder: 'Give the orchestrator a goal, e.g. "Check what everyone is working on and tell me who is blocked."', 'aria-label': 'Goal for the orchestrator', maxlength: 2000 });
  const send = h('button', { class: 'btn primary', type: 'button' }, icon('send', 15), 'Send');
  const stop = h('button', { class: 'btn danger', type: 'button', hidden: true, onclick: () => hub.send({ t: 'orch_cancel' }) }, icon('stop', 14), 'Stop');
  const clearBtn = h('button', { class: 'btn ghost sm', type: 'button', onclick: () => hub.send({ t: 'orch_clear' }) }, icon('trash', 13), 'Clear');
  const autoSeg = h('div', { class: 'seg3', role: 'group', 'aria-label': 'Autonomy' }, AUTONOMY.map(([k, t, d]) =>
    h('button', { type: 'button', 'data-k': k, 'aria-pressed': 'false', title: d, onclick: () => hub.send({ t: 'orch_config', autonomy: k }) }, t)));
  const autoHelp = h('p', { class: 'faint', style: { fontSize: '12.5px' } });
  const prof = h('select', { id: 'orch-prof', 'aria-label': 'Model source', onchange: () => { model.value = ''; hub.send({ t: 'orch_config', profile_id: prof.value, model: '' }); } });
  const model = h('input', { id: 'orch-model', list: 'orch-ml', type: 'text', 'aria-label': 'Model', placeholder: 'Profile default', spellcheck: 'false', onchange: () => hub.send({ t: 'orch_config', model: model.value.trim() }) });
  const ml = h('datalist', { id: 'orch-ml' });
  const stat = h('div', { class: 'ostat' });
  const status = h('div', { class: 'ostatus' });

  const submit = async () => {
    const text = input.value.trim(); if (!text) return;
    send.disabled = true;
    try { const r = await hub.call({ t: 'orch_say', text }); if (r.ok) input.value = ''; } finally { send.disabled = false; input.focus(); }
  };
  send.addEventListener('click', submit);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } });

  root.append(h('div', { class: 'page orch' },
    h('section', { class: 'card ochat' },
      h('div', { class: 'card-h' }, icon('brain', 17), h('h3', {}, 'Orchestrator'), status, h('span', { style: { flex: 1 } }), clearBtn),
      pending, log,
      h('div', { class: 'oin' }, h('label', { class: 'sr', for: 'orch-in' }, 'Goal'), input, h('div', { class: 'oin-r' }, h('small', { class: 'faint' }, 'Ctrl+Enter to send'), stop, send))),
    h('aside', { class: 'oside' },
      h('div', { class: 'card card-b', style: { display: 'grid', gap: '12px' } }, h('h3', { class: 'oh' }, 'Autonomy'), autoSeg, autoHelp),
      h('div', { class: 'card card-b', style: { display: 'grid', gap: '12px' } }, h('h3', { class: 'oh' }, 'Brain model'),
        h('label', { class: 'f', for: 'orch-prof' }, 'Source', prof), h('label', { class: 'f', for: 'orch-model' }, 'Model', model, ml)),
      h('div', { class: 'card card-b', style: { display: 'grid', gap: '10px' } }, h('h3', { class: 'oh' }, 'This run'), stat),
      h('div', { class: 'note' }, h('b', {}, 'Guardrails. '), 'It can only type into a session when its owner has granted co-pilot or takeover. Terminal output it reads is treated as untrusted text, never as instructions. Each run stops at 12 steps, 15 minutes or 150k tokens.'))));

  function msgEl(m) {
    const el = h('div', { class: 'om ' + m.kind },
      h('div', { class: 'om-k' }, KIND[m.kind] || m.kind, h('span', { class: 'faint' }, ' ' + rel(m.ts))),
      h('div', { class: 'om-t' }, m.text), m.thought ? h('div', { class: 'om-th' }, m.thought) : null);
    return el;
  }
  let shown = '';
  function update() {
    const o = S.orch;
    if (!o) { setText(status, 'Connecting…'); return; }
    const busy = o.status !== 'idle';
    setText(status, busy ? 'Working' : 'Idle'); status.className = 'ostatus ' + (busy ? 'busy' : '');
    stop.hidden = !busy; send.hidden = busy ? false : false; send.disabled = busy; input.disabled = false;
    for (const b of autoSeg.children) b.setAttribute('aria-pressed', String(b.dataset.k === o.autonomy));
    setText(autoHelp, (AUTONOMY.find((a) => a[0] === o.autonomy) || [])[2] || '');
    const sig = o.profile_id + '|' + S.profiles.map((p) => p.id).join(',');
    if (prof._sig !== sig) {
      prof._sig = sig;
      clear(prof).append(h('option', { value: '' }, 'Hub default' + (o.default_label ? ` (${o.default_label})` : '')), ...S.profiles.map((p) => h('option', { value: p.id }, p.name)));
      prof.value = o.profile_id || '';
    }
    const cur = S.profiles.find((p) => p.id === prof.value);
    if (document.activeElement !== model) model.value = o.model || '';
    clear(ml).append(...((cur && cur.models) || []).map((m) => h('option', { value: m })));
    clear(stat).append(...[['Steps', `${o.steps} of ${o.step_limit}`], ['Tokens', `${fmtNum(o.tokens)} of ${fmtNum(o.token_limit)}`]].map(([k, v]) => h('div', { class: 'orow' }, h('span', { class: 'faint' }, k), h('b', { class: 'mono' }, v))));
    // approvals
    const psig = (o.pending || []).map((p) => p.aid).join(',');
    if (pending._sig !== psig) {
      pending._sig = psig;
      clear(pending).append(...(o.pending || []).map((p) => h('div', { class: 'acard', role: 'alert' },
        h('div', { class: 'acard-t' }, icon('hand', 15), h('b', {}, 'Approval needed')), h('div', { class: 'acard-s' }, p.summary),
        h('div', { class: 'acard-b' }, h('button', { class: 'btn sm', type: 'button', onclick: () => hub.send({ t: 'orch_decide', aid: p.aid, allow: false }) }, 'Deny'),
          h('button', { class: 'btn sm primary', type: 'button', onclick: () => hub.send({ t: 'orch_decide', aid: p.aid, allow: true }) }, 'Approve')))));
    }
    const msig = (o.messages || []).map((m) => m.id).join(',');
    if (shown !== msig) {
      const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
      shown = msig;
      clear(log);
      if (!(o.messages || []).length) log.append(h('div', { class: 'empty-state' }, icon('brain', 34), h('h3', {}, 'Nothing yet'), h('p', {}, 'Describe a goal. The orchestrator lists your team, reads screens and plans, and asks before it acts.')));
      else log.append(...o.messages.map(msgEl));
      if (stick) log.scrollTop = log.scrollHeight;
    }
  }
  return { meta: () => ({ title: 'Orchestrator', sub: 'One agent coordinating every session', actions: [] }), update, destroy() {} };
}
