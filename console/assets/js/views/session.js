// Focus view: one session in full. Live terminal + consent-gated control + plan / files / usage / timeline / instruct.
import { S, sessionById, nodeById, isLead } from '../store.js';
import { h, icon, setText, setClass, clear, fmtNum, fmtBytes, rel, mmss, shortPath, providerMeta, MODE_INFO } from '../util.js';
import { chip, modeChip, dotClass, stateText, toast, modal, ctxPct } from '../components.js';
import * as hub from '../hubclient.js';
import * as pool from '../termpool.js';

const MODE_ORDER = ['watch', 'suggest', 'copilot', 'takeover'];
const MODE_TEXT = {
  watch: 'Read-only. You can see everything but cannot type.',
  suggest: 'You can send written suggestions that appear on their screen. Nothing is typed for them.',
  copilot: 'Both of you can type into this session.',
  takeover: 'Exclusive control: only the lead can type. It expires automatically.',
};

export function mount(root, sid) {
  localStorage.setItem('kode.seen.session', '1');
  const R = {};
  const missing = h('div', { class: 'card empty-state hero-empty', hidden: true }, icon('terminal', 40), h('h3', {}, 'Session not found'), h('p', {}, 'It may have been removed, or you may not have access to it.'),
    h('a', { class: 'btn', href: '#/sessions' }, 'Back to sessions'));

  // ── terminal card ──
  R.stage = h('div', { class: 'term-stage' }, R.fit = h('div', { class: 'term-fit', onclick: () => pool.focusTerm(sid) }));
  R.lock = h('div', { class: 'lock', hidden: true }, R.lockIc = h('span'), R.lockTxt = h('div', { class: 'grow' }), R.lockBtn = h('button', { class: 'btn sm primary', type: 'button' }));
  R.copyBtn = h('button', { class: 'btn sm ghost term-btn', type: 'button', onclick: () => { const t = pool.visibleText(sid); navigator.clipboard.writeText(t).then(() => toast('Screen copied'), () => toast('Could not copy', 'bad')); } }, icon('copy', 13), 'Copy screen');
  R.restartBtn = h('button', { class: 'btn sm primary', type: 'button', hidden: true, onclick: async () => { try { const x = await hub.call({ t: 'restart', sid }); if (!x.ok) throw new Error(x.err || 'could not restart'); toast(x.applied === false ? 'Request sent to the machine\'s owner' : 'Restarting…'); if (x.sid) location.hash = '#/session/' + x.sid; } catch (e) { toast(e.message, 'bad'); } } }, icon('play', 13), 'Restart');
  R.removeBtn = h('button', { class: 'btn sm', type: 'button', hidden: true, onclick: () => { hub.send({ t: 'forget', sid }); location.hash = isLead() ? '#/sessions' : '#/home'; } }, icon('trash', 13), 'Remove');
  R.stopBtn = h('button', { class: 'btn sm danger', type: 'button', onclick: () => confirmStop() }, icon('stop', 13), 'Stop session');
  const termCard = h('div', { class: 'term-card' },
    h('div', { class: 'term-bar' }, h('span', { class: 'lights' }, h('i'), h('i'), h('i')), R.path = h('span', { class: 'grow' }), R.copyBtn, R.restartBtn, R.removeBtn, R.stopBtn), R.stage, R.lock);

  // ── header ──
  R.pg = h('span', { class: 'pg', style: { width: '34px', height: '34px', fontSize: '15px', borderRadius: '10px' } });
  R.title = h('h2', { style: { font: '600 20px var(--serif)' } });
  R.chips = h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' } });
  R.now = h('div', { class: 'muted', style: { fontSize: '13px', marginTop: '2px' } });
  const header = h('div', { style: { display: 'flex', gap: '14px', alignItems: 'center', margin: '0 0 14px' } }, R.pg, h('div', { style: { minWidth: 0 } }, R.title, R.now), h('span', { style: { flex: 1 } }), R.chips);

  // ── control ladder ──
  R.seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Control mode' }, MODE_ORDER.map((m) => {
    const b = h('button', { type: 'button', 'data-mode': m, onclick: () => chooseMode(m) }, MODE_INFO[m].label, h('small', {}, MODE_INFO[m].hint));
    b.setAttribute('aria-pressed', 'false');
    return b;
  }));
  R.modeText = h('p', { class: 'muted', style: { fontSize: '13px' } });
  R.pending = h('div', { class: 'note warn', hidden: true });
  R.minutes = h('select', { 'aria-label': 'Grant duration', hidden: true, style: { width: 'auto', height: '30px' } },
    [['5', '5 minutes'], ['10', '10 minutes'], ['30', '30 minutes'], ['60', '1 hour']].map(([v, l]) => h('option', { value: v }, l)));
  const control = h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('hand', 17), h('h3', {}, 'Control'), h('span', { style: { flex: 1 } }), R.minutes),
    h('div', { class: 'card-b', style: { display: 'grid', gap: '12px' } }, R.seg, R.modeText, R.pending));

  // ── tabs ──
  const TABS = [['plan', 'Plan'], ['files', 'Files'], ['usage', 'Usage'], ['timeline', 'Timeline'], ['instruct', 'Instruct']];
  R.tab = 'plan'; R.panes = {}; R.tabBtns = {};
  const tablist = h('div', { class: 'tabs', role: 'tablist' }, TABS.map(([id, l]) => {
    const cnt = h('span', { class: 'chip', hidden: true });
    const b = h('button', { type: 'button', role: 'tab', id: 'tab-' + id, 'aria-selected': id === 'plan' ? 'true' : 'false', 'aria-controls': 'pane-' + id, onclick: () => selectTab(id) }, l, cnt);
    b._cnt = cnt; R.tabBtns[id] = b; return b;
  }));
  const paneWrap = h('div', { class: 'tabpane' }, TABS.map(([id]) => (R.panes[id] = h('div', { role: 'tabpanel', id: 'pane-' + id, 'aria-labelledby': 'tab-' + id, hidden: id !== 'plan' }))));
  const panel = h('div', { class: 'card panel' }, tablist, paneWrap);
  function selectTab(id) { R.tab = id; for (const [k, b] of Object.entries(R.tabBtns)) { b.setAttribute('aria-selected', k === id ? 'true' : 'false'); R.panes[k].hidden = k !== id; } }

  // instruct pane is stateful (textarea): build once
  R.text = h('textarea', { 'aria-label': 'Instruction', placeholder: 'e.g. Add unit tests for the billing module, then run them.' });
  R.sugBtn = h('button', { class: 'btn', type: 'button', onclick: sendSuggestion }, icon('send', 14), 'Send as suggestion');
  R.typeBtn = h('button', { class: 'btn primary', type: 'button', onclick: typeIntoSession }, icon('terminal', 14), 'Type into session');
  R.inst = h('div', { style: { display: 'grid', gap: '12px' } }, R.instNote = h('div', { class: 'note' }), R.text, h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } }, R.sugBtn, R.typeBtn), R.sugList = h('div'));
  R.panes.instruct.append(R.inst);

  root.append(h('div', { class: 'page' }, missing, h('div', { class: 'focus', id: 'focus-root' }, h('div', {}, header, termCard), h('div', { style: { display: 'grid', gap: '16px' } }, control, panel))));

  const cur = () => sessionById(sid);
  const node = () => (cur() ? nodeById(cur().node_id) : null);

  async function chooseMode(m) {
    const s = cur(); if (!s) return;
    localStorage.setItem('kode.tried.control', '1');
    try {
      if (isLead()) {
        const r = await hub.call({ t: 'request_mode', sid, mode: m, reason: '' });
        if (!r.ok) throw new Error(r.err || 'not allowed');
        if (r.applied === false) toast(`Asked ${node() ? node().name : 'the owner'} for ${MODE_INFO[m].label}. Waiting for approval.`);
      } else {
        hub.send({ t: 'set_mode', sid, mode: m, minutes: Number(R.minutes.value) });
      }
    } catch (e) { toast(e.message, 'bad'); }
  }
  function confirmStop() {
    const close = modal({ title: 'Stop this session?', body: h('p', { class: 'muted' }, 'The provider process is terminated. Unsaved work in that terminal is lost.'),
      actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn danger solid', type: 'button', onclick: () => { hub.send({ t: 'stop', sid }); close(); } }, 'Stop session')] });
  }
  function sendSuggestion() {
    const t = R.text.value.trim(); if (!t) return;
    hub.send({ t: 'suggest', sid, text: t }); toast('Suggestion sent'); R.text.value = '';
  }
  function typeIntoSession() {
    const t = R.text.value.trim(); if (!t) return;
    hub.send({ t: 'in', sid, d: t + '\r' }); toast('Typed into the session'); R.text.value = '';
  }

  let sigs = {};
  const once = (k, sig, fn) => { if (sigs[k] !== sig) { sigs[k] = sig; fn(); } };

  function update() {
    const s = cur();
    const ready = !!s;
    missing.hidden = ready || !S.everConnected || !S.nodes.length && !S.sessions.length && !S.connected;
    document.getElementById('focus-root').hidden = !ready;
    if (!ready) return;
    const n = node(), pm = providerMeta(s.provider, s.label), lead = isLead();
    // attach the persistent terminal
    if (s.state === 'live' || s.state === 'exited') {
      pool.ensure(sid, s.cols || 100, s.rows || 28);
      const e = pool.entry(sid);
      if (e.host !== R.fit || e.mode !== 'focus') pool.attach(sid, R.fit, 'focus', true);
    }
    setText(R.pg, pm.letter); R.pg.style.background = pm.color;
    setText(R.title, pm.name);
    setText(R.now, s.state === 'live' ? (s.activity_text || (s.activity === 'working' ? 'Working…' : 'Waiting for input')) : stateText(s));
    setText(R.path, s.cwd ? shortPath(s.cwd, 90) : (n ? n.name : ''));
    R.stopBtn.hidden = !(s.state === 'live' && (!lead || s.mode === 'takeover'));
    const gone = ['exited', 'error', 'offline'].includes(s.state);
    R.restartBtn.hidden = !gone || s.state === 'offline'; R.removeBtn.hidden = !gone;
    once('chips', [s.state, s.activity, s.mode, n && n.name, n && n.same_host].join('|'), () => {
      clear(R.chips).append(h('span', { class: dotClass(s) }), chip(stateText(s), s.state === 'live' ? (s.activity === 'working' ? 'accent' : 'ok') : 'bad'),
        chip(n ? n.name : 'unknown machine', '', 'laptop'), n && n.same_host ? chip('same PC as the Brain', 'info') : null, modeChip(s.mode));
    });
    // control ladder
    const reqFor = S.requests.find((r) => r.sid === sid && r.kind === 'mode');
    for (const b of R.seg.children) {
      const m = b.dataset.mode;
      b.setAttribute('aria-pressed', String(s.mode === m));
      b.disabled = s.state !== 'live' || (lead && !!reqFor && MODE_ORDER.indexOf(m) > MODE_ORDER.indexOf(s.mode) && reqFor.mode !== m ? true : false) || (lead && reqFor && reqFor.mode === m);
    }
    R.minutes.hidden = lead;
    const left = s.mode_expires ? Math.max(0, s.mode_expires - Date.now() / 1000) : 0;
    setText(R.modeText, MODE_TEXT[s.mode] + (left ? `  Expires in ${mmss(left)}.` : ''));
    R.pending.hidden = !(lead && reqFor);
    if (lead && reqFor) setText(R.pending, `Waiting for ${n ? n.name : 'the owner'} to approve ${MODE_INFO[reqFor.mode].label}…`);
    // lock overlay
    let lock = null;
    if (s.state !== 'live') lock = ['', s.state === 'offline' ? 'The machine is offline. The session will reappear if it reconnects.' : 'This session has ended.', null];
    else if (lead && ['watch', 'suggest'].includes(s.mode)) lock = ['lock', `Read-only. ${n ? n.name : 'The owner'} has not given you control of this session.`, ['Ask for co-pilot', () => chooseMode('copilot')]];
    else if (lead && s.mode === 'takeover') lock = ['live', `Takeover active. Only you can type. ${left ? mmss(left) + ' left.' : ''}`, null];
    else if (lead && s.mode === 'copilot') lock = ['live', 'Co-pilot: you and the owner can both type.', null];
    else if (!lead && s.mode === 'takeover') lock = ['lock', 'The lead has exclusive control right now. You cannot type until it ends or you pull the kill switch.', ['Pull kill switch', () => hub.call({ t: 'revoke' }).then(() => toast('All control revoked'))]];
    once('lock', JSON.stringify(lock && [lock[0], lock[1], lock[2] && lock[2][0]]) + Math.floor(left / 5), () => {
      R.lock.hidden = !lock;
      if (lock) {
        setClass(R.lock, 'lock ' + (lock[2] === null && lock[0] === 'live' ? 'live' : ''));
        clear(R.lockIc).append(icon(lock[0] === 'live' ? 'hand' : 'lock', 16));
        setText(R.lockTxt, lock[1]);
        R.lockBtn.hidden = !lock[2];
        if (lock[2]) { setText(R.lockBtn, lock[2][0]); R.lockBtn.onclick = lock[2][1]; }
      }
    });
    // ── tabs ──
    const todos = s.todos || [];
    R.tabBtns.plan._cnt.hidden = !todos.length; setText(R.tabBtns.plan._cnt, `${todos.filter((t) => t.status === 'completed').length}/${todos.length}`);
    once('plan', JSON.stringify([todos, s.activity_text, s.provider]), () => {
      const p = R.panes.plan; clear(p);
      if (s.activity_text) p.append(h('div', { class: 'note', style: { marginBottom: '12px' } }, h('b', {}, 'Now: '), s.activity_text));
      if (todos.length) p.append(h('ul', { class: 'todo' }, todos.map((t) => h('li', { class: t.status }, h('span', { class: 'ck' }, t.status === 'completed' ? icon('check', 12) : null), h('span', {}, t.text)))));
      else p.append(h('div', { class: 'empty-state' }, icon('list', 34), h('h3', {}, 'No plan to show'),
        h('p', {}, ['claude', 'codex'].includes(s.provider) ? `${pm.name} has not written a to-do list in this session yet. It appears here as soon as it does.` : `${pm.name} does not publish a plan we can read, so this stays empty. The terminal is always live. Plans are read from Claude Code and Codex.`)));
    });
    const f = s.files || {};
    R.tabBtns.files._cnt.hidden = !(f.repo && f.changed); setText(R.tabBtns.files._cnt, f.changed || 0);
    once('files', JSON.stringify(f), () => {
      const p = R.panes.files; clear(p);
      if (!f.repo) { p.append(h('div', { class: 'empty-state' }, icon('branch', 34), h('h3', {}, 'Not a git repository'), h('p', {}, 'Changed files are shown for sessions that run inside a git repository.'))); return; }
      p.append(h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '10px', flexWrap: 'wrap' } }, chip(f.branch || 'detached', 'accent', 'branch'), chip(`${f.changed} changed`, f.changed ? 'warn' : 'ok'), f.stat ? h('span', { class: 'faint', style: { fontSize: '12.5px' } }, f.stat) : null));
      p.append((f.files || []).length ? h('ul', { class: 'files' }, f.files.map((x) => h('li', {}, h('span', { class: 'st ' + x.s[0] }, x.s === '??' ? 'A' : x.s[0]), h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis' } }, x.path)))) : h('p', { class: 'muted' }, 'Working tree is clean.'));
    });
    once('usage', JSON.stringify(s.usage), () => {
      const u = s.usage || {}, p = R.panes.usage; clear(p);
      const known = u.source && u.source !== 'terminal';
      p.append(known ? h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '12px' } }, chip('measured', 'ok', 'check'), h('span', { class: 'faint', style: { fontSize: '12.5px' } }, 'source: ' + u.source))
        : h('div', { class: 'note', style: { marginBottom: '12px' } }, h('b', {}, 'Token usage: unknown. '), `${pm.name} does not expose token counts we can read, so we do not guess. Terminal activity below is exact.`));
      const cp = ctxPct(s);
      if (known || cp != null) p.append(h('div', { class: 'gauge', style: { marginBottom: '14px' } },
        h('div', { class: 'gauge-h' }, h('b', {}, 'Context window'), h('span', { class: 'mono' }, cp != null ? `${fmtNum(u.context_used)} / ${fmtNum(u.context_window)}  (${cp}%)` : 'unknown')),
        cp != null ? h('div', { class: 'bar ' + (cp >= 85 ? 'hot' : cp >= 60 ? 'mid' : ''), role: 'progressbar', 'aria-valuenow': cp, 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-label': 'Context window used' }, h('i', { style: { width: Math.max(2, cp) + '%' } })) : null,
        h('small', { class: 'faint' }, cp != null ? (u.window_source === 'reported' ? 'Window size reported by the tool.' : u.window_source === 'profile' ? 'Window size from your API profile.' : 'Window size is an assumption for this model; set it on the profile to be exact.') : 'This tool does not expose how full its context is.')));
      if (s.profile) p.append(h('dl', { class: 'kv', style: { marginBottom: '14px' } }, kv('API profile', s.profile.name), kv('Model', s.profile.model || 'profile default')));
      else if (u.model) p.append(h('dl', { class: 'kv', style: { marginBottom: '14px' } }, kv('Model', u.model)));
      if (known) p.append(h('dl', { class: 'kv', style: { marginBottom: '14px' } }, kv('Input tokens', fmtNum(u.input_tokens)), kv('Output tokens', fmtNum(u.output_tokens)), kv('Cache read', fmtNum(u.cache_read || 0)), kv('Cache written', fmtNum(u.cache_write || 0)),
        u.messages != null ? kv('Model messages', u.messages) : null, u.reasoning_tokens ? kv('Reasoning tokens', fmtNum(u.reasoning_tokens)) : null, kv('Cost (estimate)', s.cost_estimate != null ? '$' + s.cost_estimate.toFixed(4) + ' at your prices' : 'unknown (no prices set)')));
      p.append(h('div', { class: 'mlabel' }, 'Terminal activity'),
        h('dl', { class: 'kv' }, kv('Output volume', fmtBytes(u.bytes_out || s.bytes_out || 0)), kv('Active time', mmss(u.active_seconds || 0))));
    });
    const evs = S.events.filter((e) => e.sid === sid).slice(-30).reverse();
    once('timeline', JSON.stringify(evs.map((e) => e.ts + e.text)) + Math.floor(Date.now() / 20000), () => {
      const p = R.panes.timeline; clear(p);
      p.append(evs.length ? h('ul', { class: 'timeline' }, evs.map((e) => h('li', {}, h('span', {}, e.text), h('time', {}, rel(e.ts))))) : h('div', { class: 'empty-state' }, icon('clock', 34), h('h3', {}, 'Nothing yet'), h('p', {}, 'Control changes and start/stop events for this session appear here.')));
    });
    // instruct
    const canSuggest = lead && ['suggest', 'copilot', 'takeover'].includes(s.mode), canType = lead && ['copilot', 'takeover'].includes(s.mode) && s.state === 'live';
    R.sugBtn.hidden = !lead; R.typeBtn.hidden = !lead; R.text.hidden = !lead; R.sugBtn.disabled = !canSuggest; R.typeBtn.disabled = !canType;
    once('inst', [lead, s.mode, JSON.stringify(S.suggestions.filter((x) => x.sid === sid).map((x) => x.ts))].join('|'), () => {
      clear(R.instNote);
      if (lead) R.instNote.append(canType ? 'Co-pilot or takeover is active: you can type a task straight into the session.' : canSuggest ? 'Suggest mode: your text appears as a note on their screen. They decide whether to use it.' : 'Ask for suggest, co-pilot or takeover above to send instructions.');
      else R.instNote.append('Suggestions from the lead for this session appear below. Nothing is typed unless you choose to.');
      clear(R.sugList);
      const mine = S.suggestions.filter((x) => x.sid === sid);
      if (!lead) R.sugList.append(mine.length ? h('ul', { class: 'timeline' }, mine.map((x) => h('li', {}, h('span', {}, x.text), h('button', { class: 'btn sm', type: 'button', onclick: () => { hub.send({ t: 'in', sid, d: x.text }); toast('Pasted into the terminal (press Enter to run)'); } }, 'Paste')))) : h('p', { class: 'muted' }, 'No suggestions yet.'));
      R.text.hidden = !lead;
    });
  }
  const kv = (k, v) => [h('dt', {}, k), h('dd', {}, String(v))];
  const tick = setInterval(update, 1000);
  return {
    meta() {
      const s = cur(), pm = s ? providerMeta(s.provider, s.label) : null;
      const back = h('a', { class: 'btn', href: isLead() ? '#/sessions' : '#/home' }, icon('arrow', 14, ''), 'Back');
      back.firstChild.style.transform = 'rotate(180deg)';
      return { title: 'Session', sub: s ? `${pm.name} on ${(nodeById(s.node_id) || {}).name || 'unknown machine'}` : '', actions: [{ key: 'back', el: back }] };
    },
    update,
    destroy() { clearInterval(tick); pool.detach(sid, R.fit); },
  };
}
