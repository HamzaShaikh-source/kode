// The pieces of a project page. Each function takes the project snapshot and returns DOM; none of them keeps state except the draft fields,
// so a re-render (the Brain pushes a new snapshot every few seconds) never loses what the lead was typing.
import { S, nodeById } from '../store.js';
import { h, icon, setText, rel, fmtNum, providerMeta } from '../util.js';
import { chip, toast } from '../components.js';
import * as hub from '../hubclient.js';
import { heatmap, gantt, stack, spark, gauge, dur, colorOf } from '../charts.js';

export const STATUS = { draft: ['Draft', ''], planning: ['Planning', 'info'], questions: ['Needs your answers', 'warn'], plan_ready: ['Plan ready', 'warn'], running: ['Running', 'accent'], paused: ['Paused', 'warn'],
  review: ['Needs your review', 'warn'], done: ['Done', 'ok'], failed: ['Stopped', 'bad'], cancelled: ['Cancelled', ''] };
const PHASES = [['plan', 'Plan'], ['bootstrap', 'Contract'], ['build', 'Build'], ['verify', 'Verify'], ['polish', 'Polish'], ['judge', 'Judge'], ['review', 'Review'], ['done', 'Done']];
export const phasesOf = (p) => (p.hackathon ? [['ideate', 'Ideas'], ...PHASES] : PHASES);
const NEXT = {
  plan: 'Next: you approve the plan, then the Brain creates the repository, freezes the contract and every laptop starts building.',
  bootstrap: 'Next: every teammate gets their first task and their agent starts.',
  build: 'Next: when every task is merged the Brain runs the tests, then a quality review and the judges, then asks for your review.',
  verify: 'Next: if the tests pass, the quality review starts. If they fail, the planner diagnoses it and fix tasks are given to whoever is best placed.',
  polish: 'Next: independent judges score the project.',
  judge: 'Next: a submission write-up is generated and then it is your turn to review.',
  review: 'Next: approve to finish, or describe what should change and the team builds it.',
};
export const drafts = {};
export const dkey = (p, k) => `${p.id}:${k}`;
export const memberName = (p, nid) => (p.members.find((m) => m.node_id === nid) || { name: '?' }).name;
const memberIdx = (p, nid) => Math.max(0, p.members.findIndex((m) => m.node_id === nid));
const nowS = () => Date.now() / 1000;

// ── live timers: any element with data-since counts up without a re-render ──
export function timer(sinceSec, until) { return h('span', { class: 'tm', 'data-since': Math.round(sinceSec), 'data-until': until ? Math.round(until) : null }, dur((until || nowS()) - sinceSec)); }
setInterval(() => document.querySelectorAll('[data-since]').forEach((el) => { if (el.dataset.until) return; setText(el, dur(nowS() - Number(el.dataset.since))); }), 1000);

export const agentChip = (id) => { const m = providerMeta(id); return h('span', { class: 'chip', style: { gap: '5px' } }, h('i', { style: { width: '8px', height: '8px', borderRadius: '50%', background: m.color, display: 'inline-block' } }), m.name); };
const field = (p, k, el) => { const v = drafts[dkey(p, k)]; if (v != null) el.value = v; el.addEventListener('input', () => { drafts[dkey(p, k)] = el.value; }); return el; };
const fileB64 = (f) => new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(',')[1] || ''); r.onerror = () => rej(new Error('could not read ' + f.name)); r.readAsDataURL(f); });

// ───────── what is happening right now ─────────
export function nowInfo(p) {
  const ts = p.tasks || [];
  const merged = ts.filter((t) => t.status === 'merged').length;
  const working = ts.filter((t) => ['running', 'starting', 'review'].includes(t.status));
  const since = (p.now && p.now.since) || ((p.timeline || []).slice(-1)[0] || {}).start || p.created;
  const nxt = NEXT[p.phase] || '';
  switch (p.status) {
    case 'planning': {
      const s = (p.steps || []).find((x) => x.state === 'working');
      return { tone: 'live', spin: true, title: s ? s.title : 'Preparing the plan', sub: (s && s.detail) || p.note || '', then: nxt, since: (s && s.started) || since };
    }
    case 'questions': return { tone: 'wait', title: `The planner has ${(p.questions || []).length} question${(p.questions || []).length === 1 ? '' : 's'} for you`, sub: 'Answer them below, or let the planner decide. Planning continues the moment you reply.', since };
    case 'plan_ready': return { tone: 'wait', title: 'The plan is ready: review it and press Approve', sub: 'Nothing is built until you approve. You can change owners, or ask for a new plan.', then: NEXT.plan, since };
    case 'paused': return { tone: 'wait', title: 'Paused', sub: p.note || 'Agents keep their sessions. Press Resume to continue.', since };
    case 'review': return { tone: 'wait', title: 'Your turn: read the report and approve or ask for changes', sub: 'Verify, polish and judge are finished. Nothing is final until you say so.', since };
    case 'done': return { tone: 'good', title: 'Finished', sub: `${merged} task${merged === 1 ? '' : 's'} merged, tests passing` + (p.judge && p.judge.overall != null ? `, judged ${p.judge.overall}/100 (${p.judge.grade})` : '') + '.', since: since, until: ((p.final || {}).finished) || null };
    case 'failed': return { tone: 'bad', title: 'Stopped', sub: p.error || 'Something went wrong.', since };
    case 'cancelled': return { tone: '', title: 'Cancelled', sub: '', since };
    default: break;
  }
  if (p.phase === 'build' && !p.now) {
    const names = working.map((t) => t.id).join(', ');
    const stuck = ts.filter((t) => t.activity && ['limit', 'api_error', 'ended'].includes(t.activity.state)).length;
    return { tone: 'live', spin: true, title: working.length ? `Building: ${working.length} agent${working.length === 1 ? '' : 's'} working on ${names}` : 'Building: waiting for the next task to be ready',
      sub: `${merged} of ${ts.length} tasks merged.` + (stuck ? ` ${stuck} agent${stuck === 1 ? ' needs' : 's need'} attention and the Brain is handling it.` : ''), then: nxt, since };
  }
  return { tone: 'live', spin: true, title: (p.now && p.now.text) || 'Working', sub: (p.now && p.now.sub) || '', then: nxt, since };
}

export function nowBanner(p) {
  const n = nowInfo(p);
  const orb = n.tone === 'good' ? icon('check', 20) : n.tone === 'bad' ? icon('alert', 20) : n.tone === 'wait' ? icon('hand', 20) : n.spin ? icon('bolt', 20, 'spin') : icon('clock', 20);
  return h('section', { class: 'now ' + (n.tone || ''), 'aria-live': 'polite' }, h('div', { class: 'now-orb' }, orb),
    h('div', {}, h('div', { class: 'now-t' }, n.title), n.sub ? h('div', { class: 'now-s' }, n.sub) : null, n.then ? h('div', { class: 'now-s faint' }, n.then) : null),
    n.since ? h('div', { class: 'now-timer' }, timer(n.since, n.until), h('small', {}, n.tone === 'good' ? 'total' : 'in this step')) : null);
}

export function stepper(p) {
  const PH = phasesOf(p);
  const key = p.status === 'done' ? 'done' : p.status === 'review' ? 'review' : p.phase;
  const idx = Math.max(0, PH.findIndex((x) => x[0] === key));
  return h('ol', { class: 'psteps', 'aria-label': 'Progress' }, PH.map(([k, l], i) => h('li', { class: p.status === 'done' || i < idx ? 'done' : i === idx ? 'now' : '' }, l)));
}

// ───────── planning checklist and the live ideas room ─────────
export function stepsCard(p) {
  if (!(p.steps || []).length || !['planning', 'questions'].includes(p.status)) return null;
  const ic = { pending: '·', working: '●', done: '✓', failed: '!', skipped: '–' };
  const agents = Object.entries(p.live_agents || {});
  const ide = p.ideation;
  return h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('list', 17), h('h3', {}, 'What the Brain is doing')),
    h('div', { class: 'card-b' }, h('div', { class: 'steps' }, p.steps.map((s) => h('div', { class: 'step ' + s.state },
      h('div', { class: 'step-i' }, ic[s.state] || '·'),
      h('div', {}, h('div', { class: 'step-t' }, s.title), s.detail ? h('div', { class: 'step-d' }, s.detail) : null,
        s.id === 'ideate' && s.state === 'working' ? h('div', {}, h('div', { class: 'rounds' }, [1, 2, 3, 4].map((i) => h('i', { class: i < ((ide || {}).round_now || 1) ? 'on' : i === ((ide || {}).round_now || 1) ? 'now' : '' }))),
          h('div', { class: 'agents' }, agents.map(([name, a]) => h('div', { class: 'agent ' + a.state }, h('span', { class: 'dot' }), h('b', {}, name), h('small', {}, `${a.agent} · round ${a.round} · ` + (a.state === 'working' ? '' : a.state === 'failed' ? 'no answer ' : 'done in '), a.state === 'working' ? timer(a.since) : dur(a.took || 0)))))) : null),
      h('div', { class: 'step-time' }, s.started ? timer(s.started, s.ended) : ''))))));
}

// ───────── things that need the lead ─────────
export function questionsPanel(p, act, rerender) {
  if (p.status !== 'questions' || !(p.questions || []).length) return null;
  const pick = (q, v) => { drafts[dkey(p, 'ans-' + q.id)] = v; rerender(); };
  const qs = p.questions.map((q) => {
    const cur = drafts[dkey(p, 'ans-' + q.id)] != null ? drafts[dkey(p, 'ans-' + q.id)] : q.default || '';
    const inp = h('input', { type: 'text', placeholder: 'Or type your own answer', 'aria-label': q.q });
    inp.value = q.options && q.options.includes(cur) ? '' : cur;
    inp.addEventListener('input', () => { drafts[dkey(p, 'ans-' + q.id)] = inp.value; });
    return h('div', { class: 'q' }, h('b', {}, q.q), q.why ? h('small', {}, q.why) : null,
      q.options && q.options.length ? h('div', { class: 'opts' }, q.options.map((o) => h('button', { type: 'button', 'aria-pressed': String(cur === o), onclick: () => pick(q, o) }, o))) : null, inp);
  });
  const send = (skip) => { const answers = {}; p.questions.forEach((q) => { const v = drafts[dkey(p, 'ans-' + q.id)]; answers[q.id] = v != null ? v : q.default || ''; }); act('project_answers', p.id, { answers, skip }); };
  return h('section', { class: 'ask', role: 'region', 'aria-label': 'Questions from the planner' }, h('h3', {}, 'Before it plans: a few questions'), ...qs,
    h('div', { class: 'rbtn' }, h('button', { class: 'btn primary', type: 'button', onclick: () => send(false) }, icon('send', 14), 'Send answers'), h('button', { class: 'btn', type: 'button', onclick: () => send(true) }, 'Let the planner decide')));
}

export function reviewPanel(p, act) {
  if (p.status !== 'review' || !p.report) return null;
  const r = p.report;
  const picked = new Set();
  const fb = field(p, 'feedback', h('textarea', { rows: 4, placeholder: 'What should change? For example: "the ranking page should show why a candidate scored low", or "add a CSV export".', 'aria-label': 'Requested changes' }));
  const box = h('div', { class: 'review', role: 'region', 'aria-label': 'Final review' },
    h('h3', {}, 'Report: what was built and how it was checked'),
    h('div', { class: 'kpis' }, h('div', { class: 'kpi' }, h('div', { class: 'v' }, r.tests_passed ? 'Pass' : 'Fail'), h('div', { class: 'l' }, 'Tests on the merged code')),
      r.judge ? h('div', { class: 'kpi' }, h('div', { class: 'v' }, `${r.judge.overall} (${r.judge.grade})`), h('div', { class: 'l' }, 'Judge score out of 100')) : null,
      h('div', { class: 'kpi' }, h('div', { class: 'v' }, String(r.tasks.filter((t) => !t.skipped).length)), h('div', { class: 'l' }, 'Tasks merged')),
      h('div', { class: 'kpi' }, h('div', { class: 'v' }, String(r.fix_rounds + (r.rework_rounds || 0))), h('div', { class: 'l' }, 'Fix and rework rounds'))),
    h('table', { class: 'rtab' }, h('thead', {}, h('tr', {}, ['', 'Task', 'By', 'Minutes', 'PR'].map((x) => h('th', {}, x)))),
      h('tbody', {}, r.tasks.map((t) => h('tr', {}, h('td', {}, h('input', { type: 'checkbox', 'aria-label': 'Ask for changes to ' + t.id, onchange: (e) => { if (e.target.checked) picked.add(t.id); else picked.delete(t.id); } })),
        h('td', {}, h('b', {}, t.id + ' '), t.title, t.skipped ? chip('skipped', 'warn') : null), h('td', {}, t.owner + (t.agent ? ' · ' + t.agent : '')), h('td', { class: 'mono' }, t.minutes || ''),
        h('td', {}, t.pr && t.pr.startsWith('http') ? h('a', { href: t.pr, target: '_blank', rel: 'noopener' }, 'open') : ''))))),
    (r.quality_open || []).length ? h('div', { class: 'note warn' }, h('b', {}, 'Open review notes: '), r.quality_open.map((q) => q.title).join('; ')) : null,
    h('div', { class: 'note' }, h('b', {}, 'How it was checked: '), r.tests.map((t) => `${t.cmd} (exit ${t.code})`).join('; '), '. ', (r.limits || []).join(' ')),
    h('div', { class: 'faint' }, 'Ticking a task row tells the planner where the problem is. Repository: ', r.repo ? h('code', {}, r.repo) : '', ' (', r.visibility || 'private', ').'),
    fb,
    h('div', { class: 'rbtn' }, h('button', { class: 'btn primary', type: 'button', onclick: () => act('project_feedback', p.id, { approve: true }) }, icon('check', 14), 'Looks good: finish the project'),
      h('button', { class: 'btn', type: 'button', onclick: () => { if (!fb.value.trim()) { toast('Write what should change first', 'bad'); return; } act('project_feedback', p.id, { text: fb.value, tasks: [...picked] }).then((r2) => { if (r2 && r2.ok) delete drafts[dkey(p, 'feedback')]; }); } }, 'Request these changes'),
      h('small', { class: 'faint' }, 'The planner turns your request into tasks, the team builds them, then verify, polish and judge run again.')));
  return box;
}

export function stuckCards(p, act) {
  return (p.cards || []).map((c) => {
    const bits = c.kind === 'prompt' ? [['no', 'Send n'], ['yes', 'Send y'], ['enter', 'Press Enter']].map(([k, l]) => h('button', { class: 'btn sm' + (k === 'enter' ? ' primary' : ''), type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: k }) }, l))
      : [h('button', { class: 'btn sm', type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: 'dismiss' }) }, 'Dismiss'), h('button', { class: 'btn sm', type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: 'skip' }) }, 'Skip task'),
        ...(c.task ? [h('button', { class: 'btn sm', type: 'button', title: 'Start another agent on this laptop, or hand the task to a teammate', onclick: () => act('project_card', p.id, { card: c.id, action: 'switch' }) }, 'Switch agent'),
          h('button', { class: 'btn sm', type: 'button', title: 'The planner looks at the screen and the history and decides', onclick: () => act('project_card', p.id, { card: c.id, action: 'ask' }) }, 'Ask the planner')] : []),
        h('button', { class: 'btn sm primary', type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: 'retry' }) }, 'Retry')];
    return h('div', { class: 'acard', role: 'alert' }, h('div', { class: 'acard-t' }, icon('hand', 15), h('b', {}, c.kind === 'prompt' ? 'An agent is waiting for you' : c.kind === 'stalled' ? 'A task looks stuck' : 'A task needs you')), h('pre', { class: 'acard-s' }, c.text),
      h('div', { class: 'acard-b' }, ...(c.sid ? [h('a', { class: 'btn sm', href: '#/session/' + c.sid }, 'Open console')] : []), ...bits));
  });
}

// ───────── Build tab ─────────
const stateOf = (p, t) => {
  if (t.status === 'merged') return t.skipped ? ['merged', 'Skipped by you'] : ['merged', 'Merged'];
  if (t.status === 'blocked') return ['ended', 'Blocked'];
  if (t.status === 'pending') {
    const dep = (t.depends_on || []).find((d) => !(p.tasks.find((x) => x.id === d) || {}).status || (p.tasks.find((x) => x.id === d) || {}).status !== 'merged');
    return ['pending', dep ? `Waiting for ${dep}` : 'Waiting for its agent'];
  }
  if (t.status === 'review') return ['working', 'Merging the pull request'];
  const a = t.activity;
  if (!a) return ['working', t.status === 'starting' ? 'Starting its agent' : 'Working'];
  if (p.status === 'paused') return ['idle', 'Paused'];
  if (a.state === 'limit') return ['limit', 'Hit a usage limit: switching agent'];
  if (a.state === 'api_error') return ['api_error', 'API errors: switching agent'];
  if (a.state === 'ended') return ['ended', 'Agent session ended: restarting'];
  if (a.state === 'idle') return ['idle', `Idle for ${dur(a.idle)}`];
  return ['working', 'Working'];
};

export function taskCard(p, t, act) {
  const [st, label] = stateOf(p, t);
  const live = ['running', 'starting', 'review'].includes(t.status);
  const a = t.activity || {};
  const stuck = ['limit', 'api_error', 'ended'].includes(st) || (st === 'idle' && a.idle > 300);
  return h('div', { class: 'tc ' + t.status + (stuck ? ' stuck' : '') },
    h('div', { class: 'tc-top' }, h('span', { class: 'tc-id' }, t.id), h('span', { class: 'tc-st ' + st }, h('i'), label), t.round ? chip(String(t.id).startsWith('R') ? 'rework' : String(t.id).startsWith('Q') || String(t.id).startsWith('J') ? 'polish' : 'fix', 'warn') : null, t.agent ? agentChip(t.agent) : null),
    h('div', { class: 'tc-t' }, t.title),
    live && a.line ? h('div', { class: 'tc-line', title: a.line }, a.line) : null,
    t.wait ? h('div', { class: 'twait' }, icon('wifi', 12), t.wait + ' - retrying automatically') : null,
    h('div', { class: 'tc-meta' }, h('span', {}, h('b', {}, String(t.commits || 0)), ` commit${t.commits === 1 ? '' : 's'}`), t.started && live ? h('span', {}, 'running ', h('b', {}, timer(t.started))) : null,
      t.done_at && t.started ? h('span', {}, 'took ', h('b', {}, dur(t.done_at - t.started))) : null, t.violations ? h('span', {}, h('b', {}, String(t.violations)), ' scope fix') : null, t.restarts ? h('span', {}, h('b', {}, String(t.restarts)), ' restart') : null),
    h('div', { class: 'tc-a' }, t.sid && live ? h('a', { class: 'btn sm', href: '#/session/' + t.sid }, icon('terminal', 12), 'Console') : null,
      t.pr_url && t.pr_url.startsWith('http') ? h('a', { class: 'btn sm', href: t.pr_url, target: '_blank', rel: 'noopener' }, 'Pull request') : null,
      ['running', 'starting'].includes(t.status) ? [h('button', { class: 'btn sm', type: 'button', title: 'Stop this agent and start a fresh one on the same branch', onclick: () => act('project_task', p.id, { task: t.id, action: 'retry' }) }, 'Retry'),
        h('button', { class: 'btn sm', type: 'button', title: 'Another agent on this laptop, or a teammate, takes over', onclick: () => act('project_task', p.id, { task: t.id, action: 'switch' }) }, 'Switch agent'),
        h('button', { class: 'btn sm', type: 'button', title: 'The planner reads the screen and decides what to do', onclick: () => act('project_task', p.id, { task: t.id, action: 'ask' }) }, 'Ask planner'),
        h('button', { class: 'btn sm', type: 'button', title: 'Accept the gap and move on', onclick: () => act('project_task', p.id, { task: t.id, action: 'skip' }) }, 'Skip')] : null));
}

export function buildTab(p, act) {
  const ts = p.tasks || [];
  if (!ts.length) return h('div', { class: 'card card-b muted' }, 'Tasks appear here once the plan is ready.');
  const merged = ts.filter((t) => t.status === 'merged').length;
  const working = ts.filter((t) => ['running', 'starting', 'review'].includes(t.status)).length;
  const commits = ts.reduce((a, t) => a + (t.commits || 0), 0);
  const start = ((p.timeline || []).find((x) => x.phase === 'build') || {}).start || p.created;
  return h('div', {}, h('div', { class: 'kpis' }, h('div', { class: 'kpi' }, h('div', { class: 'v' }, `${merged}/${ts.length}`), h('div', { class: 'l' }, 'Tasks merged')), h('div', { class: 'kpi' }, h('div', { class: 'v' }, String(working)), h('div', { class: 'l' }, 'Agents working now')),
    h('div', { class: 'kpi' }, h('div', { class: 'v' }, String(commits)), h('div', { class: 'l' }, 'Commits so far')), h('div', { class: 'kpi' }, h('div', { class: 'v' }, timer(start, p.status === 'done' ? (p.final || {}).finished : 0)), h('div', { class: 'l' }, 'Since the build started'))),
    h('div', { class: 'bar', style: { marginBottom: '16px' } }, h('i', { style: { width: Math.max(2, (merged / ts.length) * 100) + '%' } })),
    h('div', { class: 'lanes' }, p.members.map((m, i) => {
      const mine = ts.filter((t) => t.owner === m.node_id);
      const node = nodeById(m.node_id);
      const pm = providerMeta(m.provider);
      return h('section', { class: 'card lane' }, h('div', { class: 'lane-h' }, h('div', { class: 'av', style: { background: colorOf(i) } }, (m.name[0] || '?').toUpperCase()),
        h('div', {}, h('b', {}, m.name), h('small', {}, `${pm.name}${(m.pool || []).length > 1 ? ' + ' + (m.pool.length - 1) + ' backup' : ''}` + (node && node.online ? '' : ' · laptop offline'))), h('div', { style: { marginLeft: 'auto' }, class: 'faint' }, `${mine.filter((t) => t.status === 'merged').length}/${mine.length}`)),
        h('div', { class: 'lane-b' }, mine.length ? mine.map((t) => taskCard(p, t, act)) : h('div', { class: 'faint' }, 'No tasks yet')));
    })));
}

// ───────── Results tab ─────────
export function judgeCard(p) {
  const j = p.judge;
  if (!j) return null;
  if (!j.criteria) return h('div', { class: 'note warn' }, 'The code judge could not score the project: ' + (j.error || 'no judge replied') + '. Press "Judge again" to retry.');
  return h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('shield', 17), h('h3', {}, 'Code judge'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, `${j.judges.length} independent judge${j.judges.length === 1 ? '' : 's'}, blind to who wrote what`)),
    h('div', { class: 'card-b', style: { display: 'grid', gap: '16px' } },
      h('div', { class: 'score-row' }, gauge(j.overall, 'grade ' + j.grade), h('div', {}, j.criteria.map((c) => h('div', { style: { marginBottom: '9px' } }, h('div', { class: 'orow' }, h('span', {}, `${c.label} `, h('small', { class: 'faint' }, `(${c.weight})`)), h('span', { class: 'mono' }, c.score.toFixed(1) + (c.scores.length > 1 ? '  [' + c.scores.map((x) => x.toFixed(1)).join(' / ') + ']' : ''))),
        h('div', { class: 'bar' }, h('i', { style: { width: Math.max(2, c.score * 10) + '%' } })), c.cap ? h('small', { class: 'faint' }, 'Capped: ' + c.cap) : null)))),
      (j.history || []).length > 1 ? h('div', { class: 'faint' }, 'Score history ', spark(j.history), '  ', j.history.join(' → ')) : null,
      j.disagreement && j.disagreement.length ? h('div', { class: 'note warn' }, h('b', {}, 'Judges disagree: '), j.disagreement.join('; ')) : null,
      h('div', { class: 'grid2' }, j.strengths && j.strengths.length ? h('div', {}, h('b', {}, 'Strengths'), h('ul', { class: 'muted' }, j.strengths.map((x) => h('li', {}, x)))) : null,
        j.improvements && j.improvements.length ? h('div', {}, h('b', {}, 'What would raise the score'), h('ul', { class: 'muted' }, j.improvements.map((x) => h('li', {}, h('b', {}, x.severity + ': '), x.title)))) : null),
      h('small', { class: 'faint' }, 'Judges: ' + j.judges.map((x) => `${x.source} ${x.overall}`).join(', ') + '. Models have preferences; the spread shows how much they differ.')));
}

export function qualityCard(p) {
  const q = (p.final && p.final.quality) || p.quality;
  if (!q) return null;
  const open = q.issues || [];
  const facts = [['README', q.readme ? q.readme + ' chars' : 'missing'], ['Tests', q.tests ? 'present' : 'none found'], ['Stray files', String(q.stray || 0)], ['Secrets', String(q.secrets || 0)], ['Files', String(q.files || 0)]];
  return h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('list', 17), h('h3', {}, 'Quality review'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, (open.length ? open.length + ' open' : 'Clean') + (q.round ? `, after ${q.round} polish round${q.round === 1 ? '' : 's'}` : ''))),
    h('div', { class: 'card-b', style: { display: 'grid', gap: '10px' } }, h('div', { class: 'prow-t' }, facts.map(([k, v]) => chip(k + ': ' + v, 'ok'))),
      open.length ? h('ul', { class: 'muted' }, open.map((i) => h('li', {}, h('b', {}, i.severity + ': '), i.title))) : h('p', { class: 'muted' }, 'README, tests and repository hygiene were checked and the reviewer model found nothing to fix.')));
}

export function verifyCard(p) {
  const v = p.verify;
  if (!v) return null;
  return h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('terminal', 17), h('h3', {}, 'Verification'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, (v.passed ? 'Passed' : 'Failed') + (v.round ? `, after fix round ${v.round}` : '') + ' · ' + rel(v.ts * 1000))),
    h('div', { class: 'card-b', style: { display: 'grid', gap: '10px' } }, v.results.map((r) => h('details', { open: r.code !== 0 }, h('summary', {}, chip(r.code === 0 ? 'exit 0' : 'exit ' + r.code, r.code === 0 ? 'ok' : 'bad'), ' ', h('code', {}, r.cmd)), h('pre', { class: 'cmd', style: { maxHeight: '220px', overflow: 'auto' } }, (r.out || '').slice(-2400))))));
}

export function ideasCard(p) {
  const d = p.ideation;
  if (!d || !(d.ideas || []).length) return null;
  const rank = d.ranking || [];
  const byId = Object.fromEntries(d.ideas.map((i) => [i.id, i]));
  return h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('bolt', 17), h('h3', {}, 'Ideas and the vote'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, d.winner ? 'Decided by the agents' : 'In progress')),
    h('div', { class: 'card-b', style: { display: 'grid', gap: '12px' } }, h('div', { class: 'prow-t' }, (d.rounds || []).map((r) => chip(`${r.n}. ${r.title}: ${r.text}`, 'ok'))),
      ...(rank.length ? rank : d.ideas.map((i) => ({ id: i.id, title: i.title, votes: 0, score: 0 }))).map((r) => {
        const i = byId[r.id] || {};
        const win = d.winner === r.id;
        return h('div', { class: win ? 'note ok' : '' }, h('div', { class: 'orow' }, h('b', {}, `${r.id}  ${r.title}${win ? '  (winner)' : ''}`), h('span', { class: 'mono' }, rank.length ? `${r.votes} vote${r.votes === 1 ? '' : 's'}, mean ${r.score}` : '')),
          h('div', { class: 'muted', style: { fontSize: '13px' } }, i.pitch || ''), i.author && d.winner ? h('small', { class: 'faint' }, 'Proposed by ' + i.author + (i.stack ? '  ·  ' + i.stack : '')) : null,
          win && (i.research || []).length ? h('ul', { class: 'muted', style: { fontSize: '12.5px' } }, i.research.map((x) => h('li', {}, x))) : null);
      }),
      (d.votes || []).length ? h('div', {}, h('b', {}, 'Ballots'), h('ul', { class: 'muted' }, d.votes.map((v) => h('li', {}, `${v.voter} voted ${v.vote}: ${v.reason || ''}`)))) : null));
}

export function resultsTab(p) {
  const parts = [ideasCard(p), verifyCard(p), qualityCard(p), judgeCard(p)].filter(Boolean);
  return parts.length ? h('div', { style: { display: 'grid', gap: '16px' } }, parts) : h('div', { class: 'card card-b muted' }, 'Verification, the quality review and the judge scores appear here once the build is merged.');
}

// ───────── Team tab ─────────
const statsCache = {};
export async function loadStats(p, rerender) {
  const c = statsCache[p.id];
  if (c && Date.now() - c.at < 40000) return;
  statsCache[p.id] = { at: Date.now(), members: c ? c.members : null };
  try { const r = await hub.call({ t: 'project_stats', id: p.id }); if (r.ok) { statsCache[p.id] = { at: Date.now(), members: r.members }; rerender(); } } catch { /* offline */ }
}

export function teamTab(p, rerender) {
  loadStats(p, rerender);
  const st = (statsCache[p.id] || {}).members;
  const fin = (p.final && p.final.balance) || null;
  const members = (st || fin || []).map((m) => ({ member: m.member, times: m.times || [], commits: m.commits, added: m.added }));
  const start = ((p.timeline || []).find((x) => x.phase === 'build') || {}).start || p.created;
  const end = p.status === 'done' && p.final ? p.final.finished : nowS();
  const rows = p.members.map((m, i) => {
    const mine = (p.tasks || []).filter((t) => t.owner === m.node_id);
    const done = mine.filter((t) => t.status === 'merged' && !t.skipped);
    const secs = done.filter((t) => t.started && t.done_at).map((t) => t.done_at - t.started);
    const clean = done.filter((t) => !(t.restarts || t.attempts || t.violations)).length;
    const s = members.find((x) => x.member === m.name) || {};
    return { i, m, tasks: mine.length, done: done.length, avg: secs.length ? secs.reduce((a, b) => a + b, 0) / secs.length : 0, clean: done.length ? Math.round((clean / done.length) * 100) : null, commits: s.commits != null ? s.commits : mine.reduce((a, t) => a + (t.commits || 0), 0), added: s.added || 0 };
  });
  const max = Math.max(1, ...rows.map((r) => r.commits));
  const usage = Object.entries(p.usage || {});
  const totalTok = usage.reduce((a, [, u]) => a + u.in + u.out, 0);
  return h('div', { style: { display: 'grid', gap: '16px' } },
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('chart', 17), h('h3', {}, 'Contributions'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, 'Meaningful work, not equal commit counts')),
      h('div', { class: 'card-b', style: { display: 'grid', gap: '14px' } }, rows.map((r) => h('div', {}, h('div', { class: 'orow' }, h('span', {}, h('b', {}, r.m.name), h('small', { class: 'faint' }, `  ${providerMeta(r.m.provider).name}`)), h('span', { class: 'mono' }, `${r.commits} commits · ${fmtNum(r.added)} lines · ${r.done}/${r.tasks} tasks` + (r.avg ? ` · ${dur(r.avg)} per task` : '') + (r.clean != null ? ` · ${r.clean}% first time right` : ''))),
        h('div', { class: 'bar' }, h('i', { style: { width: Math.max(2, (r.commits / max) * 100) + '%', background: colorOf(r.i) } })))))),
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('pulse', 17), h('h3', {}, 'Commit activity')), h('div', { class: 'card-b' }, members.some((m) => m.times.length) ? heatmap(members, start, end) : h('p', { class: 'muted' }, st ? 'No commits have reached the main branch yet.' : 'Loading from git...'))),
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('brain', 17), h('h3', {}, 'What the Brain spent'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, `${usage.reduce((a, [, u]) => a + u.calls, 0)} calls, about ${fmtNum(totalTok)} tokens`)),
      h('div', { class: 'card-b' }, usage.length ? [stack(usage.map(([k, u], i) => ({ label: k, value: u.in + u.out, color: colorOf(i) }))),
        h('div', { class: 'legend' }, usage.map(([k, u], i) => h('span', {}, h('i', { style: { background: colorOf(i) } }), `${k}: ${u.calls} call${u.calls === 1 ? '' : 's'}, ${fmtNum(u.in + u.out)} tok, ${dur(u.secs)}`))),
        h('p', { class: 'faint', style: { marginTop: '8px' } }, 'Planner, supervisor, judge and decision calls made by the Brain. Tokens are estimated from text length when the provider reports none. Agent sessions on the laptops are on the Usage page.')] : h('p', { class: 'muted' }, 'No Brain calls yet.'))));
}

// ───────── Timeline tab ─────────
const PHASE_COLOR = { ideate: '#a855f7', plan: 'var(--info)', bootstrap: '#64748b', build: 'var(--accent)', verify: 'var(--ok)', polish: '#eab308', judge: '#d946ef', review: 'var(--warn)', done: 'var(--ok)' };
export function timelineTab(p) {
  const t0 = p.created, now = p.status === 'done' && p.final ? p.final.finished : nowS();
  const per = {};
  for (const e of p.timeline || []) { if (e.status === 'paused') continue; per[e.phase] = (per[e.phase] || 0) + Math.max(0, (e.end || now) - e.start); }
  const parts = Object.entries(per).filter(([k]) => k !== 'done').map(([k, v]) => ({ label: k, value: v, color: PHASE_COLOR[k] || 'var(--ink-3)' }));
  const rows = (p.tasks || []).filter((t) => t.started).map((t) => ({ label: `${t.id}  ${t.title}`, start: t.started, end: t.done_at || 0, color: colorOf(memberIdx(p, t.owner)), running: !t.done_at && ['running', 'starting', 'review'].includes(t.status), skipped: t.skipped, sub: memberName(p, t.owner) }));
  return h('div', { style: { display: 'grid', gap: '16px' } },
    h('div', { class: 'kpis' }, h('div', { class: 'kpi' }, h('div', { class: 'v' }, dur(now - t0)), h('div', { class: 'l' }, 'Total elapsed')), ...parts.slice(0, 5).map((x) => h('div', { class: 'kpi' }, h('div', { class: 'v' }, dur(x.value)), h('div', { class: 'l' }, x.label)))),
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('clock', 17), h('h3', {}, 'Where the time went')), h('div', { class: 'card-b' }, stack(parts), h('div', { class: 'legend' }, parts.map((x) => h('span', {}, h('i', { style: { background: x.color } }), `${x.label} ${dur(x.value)}`))))),
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('branch', 17), h('h3', {}, 'Every task on one time axis')), h('div', { class: 'card-b' }, rows.length ? [gantt(rows, Math.min(...rows.map((r) => r.start)), now), h('div', { class: 'legend' }, p.members.map((m, i) => h('span', {}, h('i', { style: { background: colorOf(i) } }), m.name)))] : h('p', { class: 'muted' }, 'Tasks appear here as they start.'))),
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', {}, 'Steps')), h('div', { class: 'card-b' }, h('table', { class: 'rtab' }, h('thead', {}, h('tr', {}, ['Step', 'Started', 'Lasted'].map((x) => h('th', {}, x)))),
      h('tbody', {}, (p.timeline || []).slice().reverse().slice(0, 30).map((e) => h('tr', {}, h('td', {}, `${e.phase}${e.status === 'paused' ? ' (paused)' : ''}`), h('td', { class: 'mono' }, new Date(e.start * 1000).toLocaleTimeString()), h('td', { class: 'mono' }, dur((e.end || now) - e.start)))))))));
}

// ───────── Context tab ─────────
export function contextTab(p, act, rerender) {
  const items = p.context || [];
  const icn = { file: 'copy', image: 'expand', link: 'link', note: 'list' };
  const add = async (payload) => { const r = await hub.call({ t: 'project_context_add', id: p.id, ...payload }, 60000).catch((e) => ({ ok: false, err: e.message })); if (!r.ok) toast(r.err || 'Could not add that', 'bad'); else toast('Added: the planner and every agent will see it'); return r; };
  const picker = h('input', { type: 'file', multiple: true, style: { display: 'none' }, onchange: async () => { for (const f of picker.files) { try { await add({ name: f.name, mime: f.type, b64: await fileB64(f) }); } catch (e) { toast(e.message, 'bad'); } } picker.value = ''; } });
  const url = h('input', { type: 'text', placeholder: 'Link to the competition page, rules, API docs...', 'aria-label': 'Link' });
  const note = h('textarea', { rows: 3, placeholder: 'Or paste text: rules, judging criteria, prizes, sponsor requirements...', 'aria-label': 'Note' });
  const drop = h('div', { class: 'dropz', tabindex: '0', role: 'button', onclick: () => picker.click(), onkeydown: (e) => { if (e.key === 'Enter') picker.click(); } }, icon('plus', 16), ' Drop files here or click to choose (PDF, images, text, up to 6 MB each)');
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); }); drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', async (e) => { e.preventDefault(); drop.classList.remove('over'); for (const f of e.dataTransfer.files) { try { await add({ name: f.name, mime: f.type, b64: await fileB64(f) }); } catch (er) { toast(er.message, 'bad'); } } });
  const sess = p.sessions || [];
  return h('div', { style: { display: 'grid', gap: '16px' } },
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('link', 17), h('h3', {}, 'Competition context'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, 'Used by the planner and copied to every laptop')),
      h('div', { class: 'card-b', style: { display: 'grid', gap: '12px' } }, items.length ? h('div', { class: 'ctxlist' }, items.map((it) => h('div', { class: 'ctxi' }, h('div', { class: 'ic' }, icon(icn[it.kind] || 'copy', 15)),
        h('div', {}, h('b', {}, it.name), h('small', {}, `${it.kind}${it.url ? ' · ' + it.url : ''} · ${it.chars ? fmtNum(it.chars) + ' characters read' : it.kind === 'image' ? 'image: agents can open it' : 'no text extracted'}`)),
        h('button', { class: 'btn sm', type: 'button', onclick: async () => { await hub.call({ t: 'project_context_remove', id: p.id, item: it.id }); } }, 'Remove')))) : h('p', { class: 'muted' }, 'Nothing attached yet. Add the event page, rules or judging criteria and the plan and every agent will build to them.'),
        drop, picker,
        h('div', { class: 'grid2' }, h('div', { style: { display: 'grid', gap: '8px' } }, url, h('button', { class: 'btn', type: 'button', onclick: async () => { if (!url.value.trim()) return; const r = await add({ url: url.value.trim() }); if (r.ok) url.value = ''; } }, icon('link', 14), 'Add link')),
          h('div', { style: { display: 'grid', gap: '8px' } }, note, h('button', { class: 'btn', type: 'button', onclick: async () => { if (!note.value.trim()) return; const r = await add({ text: note.value }); if (r.ok) note.value = ''; } }, 'Add note'))),
        ['running', 'paused', 'plan_ready', 'planning'].includes(p.status) ? h('small', { class: 'faint' }, 'New material reaches each laptop with that laptop\'s next task. Tasks already running keep what they have.') : null)),
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('copy', 17), h('h3', {}, 'Hand-off file'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, 'One file with everything another AI needs')),
      h('div', { class: 'card-b', style: { display: 'grid', gap: '10px' } }, h('p', { class: 'muted' }, 'The brief, the decision, the frozen contract, every task and its pull request, what is done and what is not, the session list and how to continue. Give it to any AI assistant together with the repository link.'),
        h('div', { class: 'rbtn' }, h('button', { class: 'btn primary', type: 'button', onclick: async () => { const r = await hub.call({ t: 'project_handoff', id: p.id }); if (!r.ok) return toast(r.err || 'Not available', 'bad'); const a = h('a', { href: URL.createObjectURL(new Blob([r.text], { type: 'text/markdown' })), download: r.name }); document.body.append(a); a.click(); a.remove(); } }, 'Download context file'),
          h('button', { class: 'btn', type: 'button', onclick: async () => { const r = await hub.call({ t: 'project_handoff', id: p.id }); if (r.ok) { await navigator.clipboard.writeText(r.text); toast('Copied the whole context file'); } } }, icon('copy', 14), 'Copy to clipboard')))),
    h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('terminal', 17), h('h3', {}, 'Agent sessions in this project'), h('span', { class: 'faint', style: { marginLeft: 'auto' } }, 'Kept so you can revisit them')),
      h('div', { class: 'card-b' }, sess.length ? h('table', { class: 'rtab' }, h('thead', {}, h('tr', {}, ['Teammate', 'Agent', 'Session', 'First task', 'Started', ''].map((x) => h('th', {}, x)))),
        h('tbody', {}, sess.map((s) => h('tr', {}, h('td', {}, s.member), h('td', {}, agentChip(s.agent)), h('td', { class: 'mono' }, s.sid), h('td', {}, s.first_task), h('td', { class: 'mono' }, new Date(s.started * 1000).toLocaleTimeString()),
          h('td', {}, S.sessions.some((x) => x.sid === s.sid) ? h('a', { class: 'btn sm', href: '#/session/' + s.sid }, 'Open') : h('small', { class: 'faint' }, 'ended')))))) : h('p', { class: 'muted' }, 'Sessions appear here as agents start.'))));
}

// ───────── Log tab ─────────
const KIND = { decision: 'Decisions', failover: 'Agent switches', nudge: 'Reminders', error: 'Problems', warn: 'Problems', merge: 'Merges', pr: 'Merges', git: 'Git', task: 'Tasks', judge: 'Judge', quality: 'Judge', plan: 'Planning', ideate: 'Planning', guard: 'Scope guard', supervisor: 'Prompts', prompt: 'Prompts', phase: 'Phases', net: 'Network', triage: 'Fixes' };
let logFilter = 'All';
export function logTab(p, rerender) {
  const groups = ['All', ...new Set((p.log || []).map((l) => KIND[l.kind] || 'Other'))];
  const rows = (p.log || []).slice().reverse().filter((l) => logFilter === 'All' || (KIND[l.kind] || 'Other') === logFilter).slice(0, 200);
  return h('div', {}, h('div', { class: 'fchips' }, groups.map((g) => h('button', { type: 'button', 'aria-pressed': String(g === logFilter), onclick: () => { logFilter = g; rerender(); } }, g))),
    h('div', { class: 'card log' }, rows.map((l) => h('div', { class: 'log-r ' + l.kind }, h('time', {}, rel(l.ts)), h('span', { class: 'k' }, ({ error: '!', warn: '!', failover: '↻', decision: '◆', merge: '✓', pr: '↗', judge: '★', quality: '★' })[l.kind] || '·'), h('span', {}, l.text)))));
}
