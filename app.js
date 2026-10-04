/**
 * K.O.D.E. — Agent Command Center. No-build, no framework.
 * One `state` object; every mutation goes through `actions`; render functions only read.
 */
(() => {
'use strict';

/* ───────────── helpers ───────────── */
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const lerp = (a, b, t) => a + (b - a) * t;
const rand = (a, b) => a + Math.random() * (b - a);
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const pad = (n, w) => String(n).padStart(w, '0');
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
let reduced = mq.matches;
document.body.classList.toggle('reduced', reduced);

/* ───────────── constants (single sources of truth) ───────────── */
const STORE_KEY = 'kode.v1';
const MAX_LOG = 300;

// Event taxonomy: filters, badges and tallies are all derived from this one table.
const EVENT_TYPES = {
  create:   { label: 'Create',   tone: 'blue'   },
  move:     { label: 'Move',     tone: 'ink'    },
  pause:    { label: 'Pause',    tone: 'yellow' },
  resume:   { label: 'Resume',   tone: 'green'  },
  retry:    { label: 'Retry',    tone: 'orange' },
  error:    { label: 'Error',    tone: 'red'    },
  complete: { label: 'Complete', tone: 'green'  },
  setting:  { label: 'Setting',  tone: 'teal'   },
};
const TYPE_ORDER = Object.keys(EVENT_TYPES);

const COLS = [
  { id: 'backlog',  label: 'Backlog' },
  { id: 'progress', label: 'In progress' },
  { id: 'review',   label: 'In review' },
  { id: 'done',     label: 'Done' },
];
const colIdx = (id) => COLS.findIndex((c) => c.id === id);
const colLabel = (id) => COLS[colIdx(id)]?.label ?? id;

const STATUSES = {
  working: { label: 'Working', glyph: '▶' },
  idle:    { label: 'Idle',    glyph: '○' },
  paused:  { label: 'Paused',  glyph: '■' },
  error:   { label: 'Error',   glyph: '▲' },
};
const STATUS_ORDER = Object.keys(STATUSES);
const PRIORITIES = { low: 'Low', med: 'Med', high: 'High' };

const NAV = [
  ['overview', '01', 'Overview'],
  ['agents',   '02', 'Agents'],
  ['tasks',    '03', 'Tasks'],
  ['activity', '04', 'Activity'],
  ['settings', '05', 'Settings'],
];
const VIEW_IDS = NAV.map((n) => n[0]);

const DEFAULTS = { project: 'Operation Mangrove', assignee: 'ORCA', notify: true, sim: true };

const FAULTS = [
  'Test suite exited 1: 3 failures in api/auth.spec',
  'Upstream API returned 429 — retry budget exhausted',
  'Merge conflict in src/queue/heap.ts could not be auto-resolved',
  'Build runner timed out after 120s',
  'Type check failed: 7 errors in src/ui/timeline.tsx',
  'Sandbox lost connection to the package registry',
];

/* ───────────── seed ───────────── */
function seed(now = Date.now()) {
  const M = 60000;
  const agents = [
    { id: 'orca',   name: 'ORCA',   role: 'Planner',  status: 'working', progress: 62, taskId: 't5', fault: null },
    { id: 'heron',  name: 'HERON',  role: 'Frontend', status: 'working', progress: 35, taskId: 't6', fault: null },
    { id: 'badger', name: 'BADGER', role: 'Backend',  status: 'error',   progress: 48, taskId: 't7', fault: FAULTS[0] },
    { id: 'moth',   name: 'MOTH',   role: 'QA',       status: 'paused',  progress: 80, taskId: 't8', fault: null },
    { id: 'raven',  name: 'RAVEN',  role: 'Reviewer', status: 'idle',    progress: 0,  taskId: null, fault: null },
    { id: 'lynx',   name: 'LYNX',   role: 'DevOps',   status: 'idle',    progress: 0,  taskId: null, fault: null },
  ];
  const T = (id, title, priority, assignee, col) => ({ id, title, priority, assignee, col });
  const tasks = [
    T('t1',  'Add pagination to /v2/runs endpoint',     'med',  'BADGER', 'backlog'),
    T('t2',  'Dark-mode tokens for the dashboard',      'low',  'HERON',  'backlog'),
    T('t3',  'Audit dependency licenses',               'low',  'RAVEN',  'backlog'),
    T('t4',  'Set up canary deploy pipeline',           'high', 'LYNX',   'backlog'),
    T('t13', 'Write migration plan for the runs table', 'med',  'ORCA',   'backlog'),
    T('t5',  'Refactor scheduler queue to a priority heap', 'high', 'ORCA',   'progress'),
    T('t6',  'Build agent timeline component',          'med',  'HERON',  'progress'),
    T('t7',  'Fix OAuth token refresh race',            'high', 'BADGER', 'progress'),
    T('t8',  'Regression suite for billing flows',      'med',  'MOTH',   'progress'),
    T('t9',  'Cache layer for repo index',              'med',  'BADGER', 'review'),
    T('t10', 'Accessibility pass on settings page',     'low',  'HERON',  'review'),
    T('t14', 'Rollback script for failed deploys',      'med',  'LYNX',   'review'),
    T('t11', 'Provision staging environment',           'high', 'LYNX',   'done'),
    T('t12', 'Seed fixtures for e2e suite',             'low',  'MOTH',   'done'),
    T('t15', 'Replace polling with websocket events',   'med',  'RAVEN',  'done'),
  ];
  const title = (id) => tasks.find((t) => t.id === id).title;
  const L = [];
  const e = (type, msg, ago) => L.push({ type, msg, t: now - ago * M });
  e('setting',  `Project set to “${DEFAULTS.project}”`, 1700);
  e('create',   `Task “${title('t11')}” created`, 1560);
  e('create',   `Task “${title('t12')}” created`, 1540);
  e('create',   `Task “${title('t15')}” created`, 1520);
  e('create',   `Task “${title('t9')}” created`, 1200);
  e('create',   `Task “${title('t10')}” created`, 1190);
  e('create',   `Task “${title('t14')}” created`, 1180);
  e('complete', `LYNX finished “${title('t11')}” → Done`, 1100);
  e('complete', `MOTH finished “${title('t12')}” → Done`, 1020);
  e('complete', `RAVEN finished “${title('t15')}” → Done`, 960);
  e('complete', `BADGER finished “${title('t9')}” → In review`, 700);
  e('complete', `HERON finished “${title('t10')}” → In review`, 640);
  e('complete', `LYNX finished “${title('t14')}” → In review`, 600);
  e('create',   `Task “${title('t7')}” created`, 560);
  e('create',   `Task “${title('t5')}” created`, 540);
  e('create',   `Task “${title('t6')}” created`, 530);
  e('create',   `Task “${title('t8')}” created`, 520);
  e('move',     `BADGER picked up “${title('t7')}” (Backlog → In progress)`, 420);
  e('move',     `ORCA picked up “${title('t5')}” (Backlog → In progress)`, 300);
  e('move',     `HERON picked up “${title('t6')}” (Backlog → In progress)`, 200);
  e('pause',    `HERON paused at 18% on “${title('t6')}”`, 150);
  e('resume',   `HERON resumed “${title('t6')}”`, 130);
  e('move',     `MOTH picked up “${title('t8')}” (Backlog → In progress)`, 110);
  e('pause',    `MOTH paused at 80% on “${title('t8')}”`, 60);
  e('error',    `BADGER failed on “${title('t7')}”: ${FAULTS[2]}`, 52);
  e('retry',    `BADGER retried “${title('t7')}”`, 45);
  e('error',    `BADGER failed on “${title('t7')}”: ${FAULTS[0]}`, 34);
  L.sort((a, b) => b.t - a.t);
  const log = L.map((x, i) => ({ id: L.length - i, ...x }));
  return {
    v: 1,
    settings: { ...DEFAULTS },
    agents, tasks, log,
    seq: { task: 100, log: log.length },
  };
}

/* ───────────── state + ui ───────────── */
let state = load();                 // persisted truth
const ui = {                        // ephemeral view state (never persisted)
  view: 'overview',
  agentQ: '', agentStatus: 'all',
  taskQ: '', taskPri: 'all', taskAssignee: 'all',
  actType: 'all',
  ticks: 0, lastTickAt: 0,
  dragging: false,
  frPrompt: 'Add an enterprise Stripe billing tier with usage-based rate limits and frontend usage graphs.',
};

// MODE: ENGINE when the FastAPI engine answers /health with simulation_mode=false; otherwise the original
// timer-driven SIMULATION (standalone file, static server, or SIMULATION_MODE=1). The UI always says which.
const MODE = { engine: false, reason: 'standalone: no engine reachable', minimal: false };
const engine = { approvals: [], contract: null, report: null, status: '', error: '', busy: false, connected: false,
                 seen: new Set(), featureId: null };
const ROLE_AGENT = { backend: 'badger', frontend: 'heron', qa: 'moth' };
const APPR_LABEL = { pending: 'Pending', approved: 'Approved', merged: 'Merged', rejected: 'Rejected' };
const hbLive = () => (MODE.engine ? engine.connected : state.settings.sim);

async function detectMode() {
  try {
    const r = await fetch('/health', { cache: 'no-store' });
    if (!r.ok) throw new Error(String(r.status));
    const h = await r.json();
    MODE.minimal = !!h.minimal;
    if (h.ok && h.simulation_mode === false) { MODE.engine = true; MODE.reason = 'engine connected'; }
    else if (h.ok) MODE.reason = 'SIMULATION_MODE=1 on the server';
  } catch (_) { /* no engine: fall back to simulation */ }
}

function engineState() {
  const roster = [['orca', 'ORCA', 'Planner'], ['heron', 'HERON', 'Frontend'], ['badger', 'BADGER', 'Backend'],
                  ['moth', 'MOTH', 'QA'], ['raven', 'RAVEN', 'Reviewer'], ['lynx', 'LYNX', 'DevOps']];
  let settings = { ...DEFAULTS, project: 'Live engine', sim: false };
  try { settings = { ...settings, ...(JSON.parse(localStorage.getItem(STORE_KEY + '.engine')) || {}).settings, sim: false }; } catch (_) { /* ignore */ }
  return { v: 1, settings, agents: roster.map(([id, name, role]) => ({ id, name, role, status: 'idle', progress: 0, taskId: null, fault: null })),
           tasks: [], log: [], seq: { task: 0, log: 0 } };
}

function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY));
    if (raw && raw.v === 1 && Array.isArray(raw.agents) && Array.isArray(raw.tasks) && Array.isArray(raw.log) && raw.settings) {
      raw.settings = { ...DEFAULTS, ...raw.settings };
      return raw;
    }
  } catch (_) { /* fall through */ }
  return seed();
}
let saveQueued = false;
function persist() {
  if (saveQueued) return;
  saveQueued = true;
  queueMicrotask(() => {
    saveQueued = false;
    try {
      if (MODE.engine) localStorage.setItem(STORE_KEY + '.engine', JSON.stringify({ settings: state.settings }));
      else localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (_) { /* quota */ }
  });
}

/* ───────────── selectors (pure reads) ───────────── */
const agentById = (id) => state.agents.find((a) => a.id === id);
const taskById = (id) => state.tasks.find((t) => t.id === id);
const taskOf = (a) => (a.taskId ? taskById(a.taskId) : null);
const agentOnTask = (taskId) => state.agents.find((a) => a.taskId === taskId);
const countBy = (arr, fn) => arr.reduce((m, x) => { const k = fn(x); m[k] = (m[k] || 0) + 1; return m; }, {});
const statusCounts = () => ({ working: 0, idle: 0, paused: 0, error: 0, ...countBy(state.agents, (a) => a.status) });
const colCounts = () => ({ backlog: 0, progress: 0, review: 0, done: 0, ...countBy(state.tasks, (t) => t.col) });
const nextBacklogFor = (agent) =>
  state.tasks.find((t) => t.col === 'backlog' && t.assignee === agent.name) || state.tasks.find((t) => t.col === 'backlog');

function filteredAgents() {
  const q = ui.agentQ.trim().toLowerCase();
  return state.agents.filter((a) => {
    if (ui.agentStatus !== 'all' && a.status !== ui.agentStatus) return false;
    if (!q) return true;
    const hay = `${a.name} ${a.role} ${STATUSES[a.status].label} ${taskOf(a)?.title ?? ''}`.toLowerCase();
    return q.split(/\s+/).every((w) => hay.includes(w));
  });
}
function taskMatches(t) {
  if (ui.taskPri !== 'all' && t.priority !== ui.taskPri) return false;
  if (ui.taskAssignee !== 'all' && t.assignee !== ui.taskAssignee) return false;
  const q = ui.taskQ.trim().toLowerCase();
  if (!q) return true;
  const hay = `${t.title} ${t.assignee} ${PRIORITIES[t.priority]}`.toLowerCase();
  return q.split(/\s+/).every((w) => hay.includes(w));
}
const tasksFiltered = () => ui.taskQ.trim() !== '' || ui.taskPri !== 'all' || ui.taskAssignee !== 'all';

/* ───────────── logging + confirmations ───────────── */
function log(type, msg) {
  if (!EVENT_TYPES[type]) throw new Error(`Unknown event type: ${type}`);
  state.seq.log += 1;
  state.log.unshift({ id: state.seq.log, type, msg, t: Date.now() });
  if (state.log.length > MAX_LOG) state.log.length = MAX_LOG;
}

const live = $('#live');
let liveTimer = 0;
// The toast IS the live region: one element, so each confirmation is announced exactly once.
function confirmMsg(msg) {
  if (!state.settings.notify) return;       // notifications toggle gates every confirmation
  live.textContent = msg;
  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => { live.textContent = ''; }, 3800);
}

/* ───────────── actions (the only mutators) ───────────── */
function releaseAgentOn(taskId) {
  const a = agentOnTask(taskId);
  if (!a) return null;
  a.status = 'idle'; a.progress = 0; a.taskId = null; a.fault = null;
  return a;
}
function pickUp(agent, via) {
  const t = nextBacklogFor(agent);
  if (!t) return false;
  const reassigned = t.assignee !== agent.name;
  t.col = 'progress'; t.assignee = agent.name;
  agent.status = 'working'; agent.progress = 0; agent.taskId = t.id; agent.fault = null;
  log('move', `${agent.name} ${via === 'auto' ? 'auto-pulled' : 'picked up'} “${t.title}” (Backlog → In progress)${reassigned ? ' · reassigned' : ''}`);
  return t;
}

let renderQueued = false;
function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  setTimeout(() => { renderQueued = false; render(); }, 0);
}

const actions = {
  createTask({ title, assignee, priority, col }) {
    state.seq.task += 1;
    const t = { id: `t${state.seq.task}`, title: title.trim(), priority, assignee, col };
    state.tasks.push(t);
    log('create', `Task “${t.title}” created in ${colLabel(col)} for ${assignee}`);
    commit(`Task created: ${t.title}`);
    return t;
  },
  moveTask(id, col) {
    const t = taskById(id);
    if (!t || colIdx(col) < 0 || t.col === col) return;
    const from = t.col;
    t.col = col;
    let note = '';
    if (col !== 'progress') {
      const freed = releaseAgentOn(id);
      if (freed) note = ` · ${freed.name} released`;
    }
    if (col === 'done') log('complete', `“${t.title}” marked Done (${colLabel(from)} → Done)${note}`);
    else log('move', `“${t.title}” moved ${colLabel(from)} → ${colLabel(col)}${note}`);
    commit(`Moved “${t.title}” to ${colLabel(col)}`);
  },
  pauseAgent(id) {
    const a = agentById(id);
    if (!a || a.status !== 'working') return;
    a.status = 'paused';
    log('pause', `${a.name} paused at ${Math.round(a.progress)}% on “${taskOf(a)?.title ?? 'its task'}”`);
    commit(`${a.name} paused`);
  },
  resumeAgent(id) {
    const a = agentById(id);
    if (!a || a.status !== 'paused') return;
    a.status = 'working';
    log('resume', `${a.name} resumed “${taskOf(a)?.title ?? 'its task'}”`);
    commit(`${a.name} resumed`);
  },
  retryAgent(id) {
    const a = agentById(id);
    if (!a || a.status !== 'error') return;
    a.status = 'working'; a.fault = null;
    log('retry', `${a.name} retried “${taskOf(a)?.title ?? 'its task'}”`);
    commit(`${a.name} retrying`);
  },
  startAgent(id) {
    const a = agentById(id);
    if (!a || a.status !== 'idle') return;
    const t = pickUp(a, 'manual');
    if (!t) { confirmMsg(`${a.name} has nothing to start: Backlog is empty`); return; }
    commit(`${a.name} started “${t.title}”`);
  },
  saveSettings(patch) {
    const s = state.settings;
    const ch = [];
    if (patch.project !== s.project) ch.push(`project → “${patch.project}”`);
    if (patch.assignee !== s.assignee) ch.push(`default assignee → ${patch.assignee}`);
    if (patch.notify !== s.notify) ch.push(`notifications ${patch.notify ? 'on' : 'off'}`);
    if (patch.sim !== s.sim) ch.push(`simulation ${patch.sim ? 'on' : 'off'}`);
    if (!ch.length) { confirmMsg('No changes to save'); return false; }
    Object.assign(s, patch);
    log('setting', `Settings saved: ${ch.join(', ')}`);
    commit('Settings saved');
    return true;
  },
  resetSettings() {
    Object.assign(state.settings, DEFAULTS);
    log('setting', 'Settings reset to defaults');
    commit('Settings reset to defaults');
  },
  resetDemo() {
    state = seed();
    Object.assign(ui, { agentQ: '', agentStatus: 'all', taskQ: '', taskPri: 'all', taskAssignee: 'all', actType: 'all', ticks: 0 });
    log('setting', 'Demo data reset to seed state');
    commit('Demo data reset');
    mountView();
  },
  // ── engine mode: every mutation of engine-derived state goes through these ──
  engineEvent(ev) {
    if (engine.seen.has(ev.seq)) return;
    engine.seen.add(ev.seq);
    ui.ticks += 1;
    const d = ev.data || {};
    const a = agentById(ROLE_AGENT[ev.agent]) || agentById('orca');
    let t = d.task_id ? state.tasks.find((x) => x.engineId === d.task_id) : null;
    if (!t && d.task_id && ev.type === 'THINKING') {
      state.seq.task += 1;
      t = { id: `t${state.seq.task}`, engineId: d.task_id, title: ev.message.split('planning: ')[1] || ev.message,
            priority: 'med', assignee: a.name, col: 'progress', approvalId: null, prUrl: null };
      state.tasks.push(t);
    }
    const byApproval = d.approval_id ? state.tasks.find((x) => x.approvalId === d.approval_id) : null;
    let type = 'move';
    switch (ev.type) {
      case 'THINKING':
        if (ev.agent) { a.status = 'working'; a.progress = 15; a.fault = null; if (t) a.taskId = t.id; }
        else if (d.stage === 'contract_frozen') { a.status = 'idle'; a.progress = 0; type = 'create'; }
        else { a.status = 'working'; a.progress = 30; type = 'create'; }
        break;
      case 'WRITE_CODE': a.status = 'working'; a.progress = Math.max(a.progress, 55); break;
      case 'COMMIT':
        if (d.stage === 'merge') {
          type = 'complete';
          if (byApproval) { byApproval.col = 'done'; const ag = agentOnTask(byApproval.id); if (ag) { ag.status = 'idle'; ag.progress = 0; ag.taskId = null; } }
        } else { a.status = 'working'; a.progress = 80; }
        break;
      case 'PR_CREATED': a.progress = 90; if (t) t.prUrl = d.url; break;
      case 'NEEDS_REVIEW':
        type = 'pause'; a.status = 'paused'; a.progress = 100;
        if (t) { t.col = 'review'; t.approvalId = d.approval_id; }
        break;
      case 'BLOCKED':
        type = 'error';
        if (d.stage === 'rejected') {
          if (byApproval) { byApproval.col = 'backlog'; const ag = agentOnTask(byApproval.id); if (ag) { ag.status = 'idle'; ag.progress = 0; ag.taskId = null; } }
        } else { a.status = 'error'; a.fault = ev.message; }
        break;
      default: break;
    }
    state.log.unshift({ id: ev.seq, type, msg: `[${ev.type}] ${ev.message}`, t: Date.parse(ev.ts) || Date.now() });
    if (state.log.length > MAX_LOG) state.log.length = MAX_LOG;
    if (['NEEDS_REVIEW', 'PR_CREATED'].includes(ev.type) || d.stage === 'merge' || d.stage === 'rejected') this.refreshApprovals();
    queueRender();
  },
  async refreshApprovals() {
    try {
      const r = await fetch('/approvals', { cache: 'no-store' });
      if (r.ok) { engine.approvals = await r.json(); queueRender(); }
    } catch (_) { /* offline: keep last list */ }
  },
  async submitFeature(prompt) {
    if (engine.busy) return;
    engine.busy = true; engine.error = ''; engine.report = null; engine.status = 'Planning with the LLM, drafting and freezing the contract…';
    render();
    try {
      const r = await fetch('/features', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ prompt }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : (r.statusText || 'request failed'));
      engine.contract = j.contract; engine.featureId = j.feature_id;
      engine.status = 'Contract frozen. Three agents are working in parallel…';
      confirmMsg('Contract frozen. Dispatching three agents.');
      render();
      fetch(`/features/${j.feature_id}/dispatch`, { method: 'POST' }).then(async (r2) => {
        const rep = await r2.json().catch(() => ({}));
        if (!r2.ok) throw new Error(typeof rep.detail === 'string' ? rep.detail : 'dispatch failed');
        engine.report = rep;
        engine.status = 'Agents finished. Review and approve the pull requests below.';
      }).catch((e) => { engine.error = `Dispatch failed: ${e.message}`; })
        .finally(() => { engine.busy = false; this.refreshApprovals(); queueRender(); });
    } catch (e) {
      engine.error = e.message; engine.busy = false; engine.status = '';
      queueRender();
    }
  },
  async decideApproval(id, verb) {
    const ap = engine.approvals.find((x) => x.id === id);
    const tag = ap ? `${ap.role} PR #${ap.pr.number}` : 'pull request';
    try {
      const r = await fetch(`/approvals/${id}/${verb}`, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reason: 'Rejected from the K.O.D.E. console' }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'request failed');
      confirmMsg(verb === 'approve' ? `Approved and merged ${tag}` : `Rejected ${tag}; nothing was merged`);
    } catch (e) { engine.error = `${verb} failed: ${e.message}`; }
    await this.refreshApprovals();
    queueRender();
  },
  clearLog() {
    state.log = [];
    commit('Activity log cleared');
  },
};

// Post-mutation pipeline: persist → reschedule heartbeat → re-render → confirm.
function commit(confirmation) {
  persist();
  sim.reschedule();
  render();
  if (confirmation) confirmMsg(confirmation);
}

/* ───────────── simulation heartbeat ───────────── */
const sim = {
  timer: 0,
  reschedule() {
    clearTimeout(this.timer);
    this.timer = 0;
    if (!state.settings.sim || MODE.engine) return;
    const [lo, hi] = reduced ? [6000, 10000] : [1500, 3000];
    this.timer = setTimeout(() => { this.tick(); this.reschedule(); }, rand(lo, hi));
  },
  tick() {
    if (!state.settings.sim || MODE.engine) return;
    ui.ticks += 1;
    ui.lastTickAt = performance.now();
    let structural = false;
    for (const a of state.agents) {
      if (a.status === 'working') {
        a.progress = clamp(a.progress + rand(3, 9), 0, 100);
        if (a.progress >= 100) {
          const t = taskOf(a);
          if (t) { t.col = 'review'; log('complete', `${a.name} finished “${t.title}” → In review`); }
          a.status = 'idle'; a.progress = 0; a.taskId = null;
          structural = true;
        } else if (Math.random() < 0.012 && a.progress > 15) {
          a.status = 'error';
          a.fault = FAULTS[Math.floor(rand(0, FAULTS.length))];
          log('error', `${a.name} failed on “${taskOf(a)?.title ?? 'its task'}”: ${a.fault}`);
          structural = true;
        }
      }
    }
    for (const a of state.agents) {              // idle agents auto-pull the next Backlog task
      if (a.status === 'idle' && Math.random() < 0.5 && pickUp(a, 'auto')) structural = true;
    }
    persist();
    if (structural) render(); else { paintProgress(); renderTick(); }
  },
};

/* ───────────── rendering helpers ───────────── */
let lastFocusFid = null;
document.addEventListener('focusin', (e) => { lastFocusFid = e.target?.dataset?.fid ?? null; });

// Replace innerHTML only when it changed, preserving focus by data-fid.
function setHTML(el, html) {
  if (!el || el._h === html) return;
  const active = document.activeElement;
  const fid = el.contains(active) ? active.dataset.fid : null;
  const alt = el.contains(active) ? active.dataset.alt : null;
  el.innerHTML = html;
  el._h = html;
  if (fid) {
    let target = el.querySelector(`[data-fid="${CSS.escape(fid)}"]:not([disabled])`);
    if (!target && alt) target = el.querySelector(`[data-fid="${CSS.escape(alt)}"]:not([disabled])`);
    if (target) target.focus({ preventScroll: true });
  }
}

const relTime = (t) => {
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
};
const timeEl = (t) => `<time class="rel" data-t="${t}" datetime="${new Date(t).toISOString()}" title="${esc(new Date(t).toLocaleString())}">${relTime(t)}</time>`;

const progressBar = (a, label = 'progress') => {
  const v = Math.round(clamp(a.progress, 0, 100));
  return `<div class="bar" role="progressbar" aria-label="${esc(a.name)} ${label}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${v}" data-prog="${a.id}"><i style="width:${v}%"></i></div>`;
};
const pctText = (a) => `<span class="pct" data-prog-text="${a.id}">${Math.round(a.progress)}%</span>`;

function paintProgress() {
  for (const el of $$('[data-prog]')) {
    const a = agentById(el.dataset.prog);
    if (!a) continue;
    const v = Math.round(clamp(a.progress, 0, 100));
    el.setAttribute('aria-valuenow', v);
    el.firstElementChild.style.width = `${v}%`;
  }
  for (const el of $$('[data-prog-text]')) {
    const a = agentById(el.dataset.progText);
    if (a) el.textContent = `${Math.round(a.progress)}%`;
  }
}

const eventRow = (ev) => {
  const ty = EVENT_TYPES[ev.type];
  return `<li class="ev tone-${ty.tone}"><span class="ty">${ty.label}</span><span class="msg">${esc(ev.msg)}</span>${timeEl(ev.t)}</li>`;
};

/* ───────────── chrome (rail, header) ───────────── */
function renderChrome() {
  const sc = statusCounts();
  const rc = colCounts();
  setHTML($('#nav'), NAV.map(([id, n, label]) => {
    let badge = '';
    if (id === 'agents' && sc.error) badge = `<span class="badge">${sc.error}<span class="sr"> in error</span></span>`;
    if (id === 'tasks' && rc.review) badge = `<span class="badge soft">${rc.review}<span class="sr"> in review</span></span>`;
    return `<a href="#/${id}" data-fid="nav-${id}"${ui.view === id ? ' aria-current="page"' : ''}><span class="n" aria-hidden="true">${n}</span><span class="l">${label}</span>${badge}</a>`;
  }).join(''));

  setHTML($('#roster'), state.agents.map((a) => `
    <li class="s-${a.status}" aria-label="${esc(a.name)}, ${STATUSES[a.status].label}, ${Math.round(a.progress)} percent">
      <span class="r-name">${esc(a.name)}</span>
      <span class="r-st"><span aria-hidden="true">${STATUSES[a.status].glyph}</span> ${STATUSES[a.status].label}</span>
      ${progressBar(a)}
    </li>`).join(''));

  const proj = state.settings.project;
  $('#hdr-project').textContent = proj;
  document.title = `${proj} · K.O.D.E.`;
  const pill = $('#sim-pill');
  if (MODE.engine) {
    pill.innerHTML = `<span class="pre">Engine </span>${engine.connected ? 'live' : 'offline'}`;
    pill.className = `pill ${engine.connected ? 'live' : 'halted'}`;
  } else {
    pill.innerHTML = `<b class="simtag">SIMULATION</b><span class="pre"> · ${state.settings.sim ? 'live' : 'halted'}</span>`;
    pill.className = `pill sim ${state.settings.sim ? 'live' : 'halted'}`;
  }
  const rib = $('#mode-ribbon');
  rib.hidden = MODE.engine;
  rib.textContent = MODE.engine ? '' : `SIMULATION MODE · mock agents on a timer · no engine, LLM or GitHub is in use (${MODE.reason})`;
  $('#btn-reset-rail').hidden = MODE.engine;
  const nb = $('#btn-new');
  nb.firstChild.textContent = MODE.engine ? 'New feature ' : 'New task ';
  renderTick();
  paintStaticHb();
}
function renderTick() { $('#hb-t').textContent = `T+${pad(ui.ticks, 4)}`; }
const HB_CELLS = 12;
function paintStaticHb() {
  if (!reduced) return;
  $('#hb').textContent = (hbLive() ? '▒' : '░').repeat(HB_CELLS);
}

/* ───────────── views: shells (mounted once per navigation) ───────────── */
const head = (title, deck) => `<div class="pg-head"><h1 class="pg-title" id="pg-title" tabindex="-1">${title}</h1><p class="pg-deck">${deck}</p></div>`;

const VIEWS = {
  overview: () => `
    ${head('Overview', MODE.engine ? 'Live engine: one request becomes three parallel agents, one frozen contract, human-approved merges.' : 'The whole fleet at a glance — global truth, unaffected by any filter.')}
    ${MODE.engine ? `
    <section aria-labelledby="ov-h-feature">
      <div class="sec-h"><h2 id="ov-h-feature">Feature request</h2></div>
      <form id="fr-form" class="card toolbar" novalidate>
        <div class="field f-grow"><label for="fr-prompt">Describe the feature</label>
          <input type="text" id="fr-prompt" maxlength="400" autocomplete="off" value="${esc(ui.frPrompt)}" aria-describedby="fr-err"></div>
        <button type="submit" class="btn btn-primary" id="fr-run">Run feature</button>
      </form>
      <p class="count-line" id="fr-err" data-region="ov-engine-status"></p>
      <div data-region="ov-contract"></div>
    </section>
    <section class="sec" aria-labelledby="ov-h-appr">
      <div class="sec-h"><h2 id="ov-h-appr">Awaiting your approval</h2></div>
      <div data-region="ov-approvals"></div>
    </section>` : ''}
    <section${MODE.engine ? ' class="sec"' : ''} aria-labelledby="ov-h-stats"><h2 class="sr" id="ov-h-stats">Fleet statistics</h2>
      <div data-region="ov-tiles"></div>
    </section>
    <section class="sec" aria-labelledby="ov-h-fleet">
      <div class="sec-h"><h2 id="ov-h-fleet">Fleet</h2><a class="more" href="#/agents">Manage agents →</a></div>
      <div data-region="ov-fleet"></div>
    </section>
    <div class="two-col sec">
      <section aria-labelledby="ov-h-board">
        <div class="sec-h"><h2 id="ov-h-board">Board</h2><a class="more" href="#/tasks">Open tasks →</a></div>
        <div data-region="ov-board"></div>
      </section>
      <section aria-labelledby="ov-h-feed">
        <div class="sec-h"><h2 id="ov-h-feed">Latest activity</h2><a class="more" href="#/activity">All activity →</a></div>
        <div data-region="ov-feed"></div>
      </section>
    </div>`,

  agents: () => `
    ${head('Agents', 'Six hands on deck. Pause, resume, retry or put an idle one to work.')}
    <div class="card toolbar" role="search" aria-label="Filter agents">
      <div class="field f-grow"><label for="ag-q">Search agents</label>
        <input type="search" id="ag-q" data-search="agents" autocomplete="off" placeholder="name, role, task…" value="${esc(ui.agentQ)}"></div>
      <div class="field"><span class="lab" id="ag-status-lab">Status</span>
        <div class="chips" role="group" aria-labelledby="ag-status-lab" data-region="ag-chips"></div></div>
      <button type="button" class="btn btn-sm" data-act="clear-agents" data-fid="ag-clear">Clear filters</button>
    </div>
    <p class="count-line" data-region="ag-count"></p>
    <div data-region="ag-list"></div>`,

  tasks: () => `
    ${head('Tasks', MODE.engine ? 'Cards follow the engine: they move as agents work, and when you approve or reject a pull request.' : 'Drag cards between stages, or use the ◀ ▶ buttons on each card.')}
    <div class="card toolbar" role="search" aria-label="Filter tasks">
      <div class="field f-grow"><label for="tk-q">Search tasks</label>
        <input type="search" id="tk-q" data-search="tasks" autocomplete="off" placeholder="title, assignee…" value="${esc(ui.taskQ)}"></div>
      <div class="field f-sel"><label for="tk-pri">Priority</label>
        <select id="tk-pri" data-filter="taskPri">
          <option value="all">All priorities</option>
          ${Object.entries(PRIORITIES).map(([k, v]) => `<option value="${k}"${ui.taskPri === k ? ' selected' : ''}>${v}</option>`).join('')}
        </select></div>
      <div class="field f-sel"><label for="tk-asg">Assignee</label>
        <select id="tk-asg" data-filter="taskAssignee">
          <option value="all">Everyone</option>
          ${state.agents.map((a) => `<option value="${esc(a.name)}"${ui.taskAssignee === a.name ? ' selected' : ''}>${esc(a.name)}</option>`).join('')}
        </select></div>
      <button type="button" class="btn btn-sm" data-act="clear-tasks" data-fid="tk-clear">Clear filters</button>
    </div>
    <p class="count-line" data-region="tk-count"></p>
    <div data-region="tk-board"></div>`,

  activity: () => `
    ${head('Activity', 'Audit trail, newest first. Click a tally to filter by event type.')}
    <div class="card toolbar">
      <div class="field f-grow"><span class="lab" id="ac-type-lab">Event type</span>
        <div class="tally" role="group" aria-labelledby="ac-type-lab" data-region="ac-tally"></div></div>
      <button type="button" class="btn btn-sm btn-danger" data-act="clear-log" data-fid="ac-clear">Clear log</button>
    </div>
    <p class="count-line" data-region="ac-count"></p>
    <div data-region="ac-list"></div>`,

  settings: () => `
    ${head('Settings', 'Every switch here does something you can see. Nothing decorative.')}
    <form id="st-form" class="card form-card" novalidate>
      <div class="field"><label for="st-project">Project name</label>
        <input type="text" id="st-project" maxlength="40" autocomplete="off" aria-describedby="st-project-h st-project-e">
        <span class="hint" id="st-project-h">Shown in the header and the browser tab title.</span>
        <span class="field-err" id="st-project-e"></span></div>
      <div class="field"><label for="st-assignee">Default assignee</label>
        <select id="st-assignee" aria-describedby="st-assignee-h">${state.agents.map((a) => `<option value="${esc(a.name)}">${esc(a.name)} — ${esc(a.role)}</option>`).join('')}</select>
        <span class="hint" id="st-assignee-h">Preselected in the New task dialog.</span></div>
      <div class="toggle"><input type="checkbox" id="st-notify" aria-describedby="st-notify-h"><label for="st-notify">Notifications</label>
        <span class="hint" id="st-notify-h">When off, action confirmations (toast and screen-reader announcement) are suppressed.</span></div>
      ${MODE.engine ? '' : `<div class="toggle"><input type="checkbox" id="st-sim" aria-describedby="st-sim-h"><label for="st-sim">Live simulation</label>
        <span class="hint" id="st-sim-h">When off, the agent heartbeat stops: no progress, no faults, no auto-pull.</span></div>`}
      <div class="form-actions">
        <button type="submit" class="btn btn-primary">Save settings</button>
        <button type="button" class="btn" data-act="settings-reset">Reset to defaults</button>
        <span class="saved" id="st-saved"></span>
      </div>
    </form>
    ${MODE.engine ? '' : `<section class="sec" aria-labelledby="st-h-demo">
      <div class="danger-zone">
        <h2 id="st-h-demo" class="sr">Demo data</h2>
        <strong>Demo data</strong>
        <span class="hint">State and activity log persist in this browser’s localStorage. Reset restores the seeded fleet, board and history.</span>
        <div><button type="button" class="btn btn-danger" data-act="reset-demo" data-fid="st-reset-demo">Reset demo data</button></div>
      </div>
    </section>`}`,
};

let mounted = null;
function mountView() {
  const root = $('#view');
  root.innerHTML = VIEWS[ui.view]();
  for (const r of $$('[data-region]', root)) r._h = null;
  mounted = ui.view;
  if (ui.view === 'settings') syncSettingsForm();
}
function syncSettingsForm() {
  if (ui.view !== 'settings') return;
  const s = state.settings;
  $('#st-project').value = s.project;
  $('#st-assignee').value = s.assignee;
  $('#st-notify').checked = s.notify;
  if ($('#st-sim')) $('#st-sim').checked = s.sim;
  $('#st-project').removeAttribute('aria-invalid');
  $('#st-project-e').textContent = '';
}

/* ───────────── regions ───────────── */
const emptyBlock = (kind, h, b, wide) => `<div class="empty ${kind}${wide ? ' empty-wide' : ''}"><p class="e-h">${h}</p><p class="e-b">${b}</p></div>`;

const REGIONS = {
  'ov-tiles'() {
    const sc = statusCounts();
    const total = state.agents.length;
    const done = state.tasks.filter((t) => t.col === 'done').length;
    const cells = 20;
    const n = Math.round((sc.working / Math.max(total, 1)) * cells);
    const tile = (cls, label, num, sub) => `<div class="tile ${cls}"><div class="lbl">${label}</div><div class="num">${num}</div><div class="sub">${sub}</div></div>`;
    return `<div class="tiles">
      ${tile('s-working', 'Working', sc.working, `of ${total} agents`)}
      ${tile('s-idle', 'Idle', sc.idle, 'ready for work')}
      ${tile('s-paused', 'Paused', sc.paused, 'on hold')}
      ${tile('s-error', 'Errors', sc.error, sc.error ? 'need a retry' : 'all clear')}
      ${tile('', 'Tasks done', `${done}<small>/${state.tasks.length}</small>`, `${state.tasks.length ? Math.round((done / state.tasks.length) * 100) : 0}% of the board`)}
    </div>
    <div class="hud-line"><span>FLEET LOAD</span><pre aria-hidden="true">[${'█'.repeat(n)}${'░'.repeat(cells - n)}]</pre><span>${Math.round((sc.working / Math.max(total, 1)) * 100)}%</span><span class="muted">${sc.working} of ${total} agents working</span></div>`;
  },
  'ov-fleet'() {
    return `<ul class="fleet-strip">${state.agents.map((a) => `
      <li class="s-${a.status}" aria-label="${esc(a.name)}, ${STATUSES[a.status].label}, ${Math.round(a.progress)} percent">
        <div class="f-n">${esc(a.name)}</div>
        <div class="f-s"><span aria-hidden="true">${STATUSES[a.status].glyph}</span> ${STATUSES[a.status].label} · ${esc(a.role)}</div>
        ${progressBar(a)}
      </li>`).join('')}</ul>`;
  },
  'ov-board'() {
    const rc = colCounts();
    const total = Math.max(state.tasks.length, 1);
    return `<div class="card board-sum">
      <div class="stack-bar" role="img" aria-label="Board distribution: ${COLS.map((c) => `${c.label} ${rc[c.id]}`).join(', ')}">
        ${COLS.map((c) => `<i style="width:${(rc[c.id] / total) * 100}%"></i>`).join('')}
      </div>
      <ul class="sum-list">${COLS.map((c) => `<li><span class="sw" aria-hidden="true"></span>${c.label}<b>${rc[c.id]}</b></li>`).join('')}</ul>
    </div>`;
  },
  'ov-engine-status'() {
    if (engine.error) return `<span class="field-err">${esc(engine.error)}</span>`;
    return esc(engine.status || 'Ready. Type a request and press Run feature.');
  },
  'ov-contract'() {
    const c = engine.contract;
    if (!c) return '';
    const rep = engine.report;
    return `<div class="card board-sum">
      <p><strong>Frozen contract</strong> <code>${esc(c.name)}</code> · sha256 <code>${esc(c.sha256.slice(0, 12))}</code> · source ${esc(c.source)}</p>
      <ul class="sum-list">${(c.endpoints || []).map((e) => `<li><span class="sw" aria-hidden="true"></span><code>${esc(e.method)} ${esc(e.path)}</code></li>`).join('')}</ul>
      ${rep && rep.wall_s != null ? `<p class="hint">Parallel run: wall-clock ${Number(rep.wall_s).toFixed(1)}s vs serial-equivalent ${Number(rep.serial_s).toFixed(1)}s (x${Number(rep.speedup).toFixed(2)}).</p>` : ''}
    </div>`;
  },
  'ov-approvals'() {
    const list = engine.approvals;
    if (!list.length) return `<div class="card">${emptyBlock('stage', 'Nothing awaiting approval', 'When agents open pull requests they appear here. Nothing merges without your decision.', true)}</div>`;
    return `<ul class="appr-list">${list.map((a) => {
      const tag = `${a.role} PR #${a.pr.number}`;
      const pend = a.status === 'pending';
      return `<li class="card appr s-${pend ? 'paused' : a.status === 'merged' ? 'working' : 'error'}">
        <div><strong>${esc(a.role)}</strong> · <a href="${esc(a.pr.url)}" target="_blank" rel="noopener">PR #${a.pr.number}<span class="sr"> (opens GitHub)</span></a>
          · <span class="tag">${APPR_LABEL[a.status] || esc(a.status)}</span></div>
        <div class="hint">${esc(a.commit.author_name)} &lt;${esc(a.commit.author_email)}&gt; · branch ${esc(a.pr.branch)} · review: ${esc(a.review_summary || 'n/a')}</div>
        ${pend ? `<div class="actions">
          <button type="button" class="btn btn-primary" data-act="appr-approve" data-id="${esc(a.id)}" data-fid="appr-ok-${esc(a.id)}" aria-label="Approve and merge ${esc(tag)}">Approve &amp; merge</button>
          <button type="button" class="btn btn-danger" data-act="appr-reject" data-id="${esc(a.id)}" data-fid="appr-no-${esc(a.id)}" aria-label="Reject ${esc(tag)}">Reject</button></div>` : ''}
      </li>`;
    }).join('')}</ul>`;
  },
  'ov-feed'() {
    const items = state.log.slice(0, 6);
    if (!items.length) return `<div class="card">${emptyBlock('', 'No activity yet', 'Events appear here as agents and tasks change.', true)}</div>`;
    return `<ul class="card feed mini">${items.map(eventRow).join('')}</ul>`;
  },

  'ag-chips'() {
    const sc = statusCounts();
    const chip = (id, label, n) => `<button type="button" class="chip s-${id}" aria-pressed="${ui.agentStatus === id}" data-act="chip-agent" data-v="${id}" data-fid="agchip-${id}"><span>${label}</span><span class="ct">${n}</span></button>`;
    return chip('all', 'All', state.agents.length) + STATUS_ORDER.map((s) => chip(s, STATUSES[s].label, sc[s])).join('');
  },
  'ag-count'() {
    return `${filteredAgents().length} of ${state.agents.length} agents shown`;
  },
  'ag-list'() {
    const list = filteredAgents();
    if (!list.length) return `<div class="card">${emptyBlock('nomatch', 'No agents match the filter', 'Try a different search, or clear the filters to see the whole fleet.', true)}</div>`;
    return `<div class="agent-grid">${list.map((a) => {
      const t = taskOf(a);
      let btn = '';
      if (MODE.engine) btn = '<span class="hint">Driven by the engine</span>';
      else if (a.status === 'working') btn = `<button type="button" class="btn" data-act="agent-pause" data-id="${a.id}" data-fid="ag-${a.id}-act" aria-label="Pause ${esc(a.name)}">Pause</button>`;
      else if (a.status === 'paused') btn = `<button type="button" class="btn btn-primary" data-act="agent-resume" data-id="${a.id}" data-fid="ag-${a.id}-act" aria-label="Resume ${esc(a.name)}">Resume</button>`;
      else if (a.status === 'error') btn = `<button type="button" class="btn btn-danger" data-act="agent-retry" data-id="${a.id}" data-fid="ag-${a.id}-act" aria-label="Retry ${esc(a.name)}">Retry</button>`;
      else {
        const has = state.tasks.some((x) => x.col === 'backlog');
        btn = `<button type="button" class="btn btn-primary" data-act="agent-start" data-id="${a.id}" data-fid="ag-${a.id}-act" aria-label="Start ${esc(a.name)}${has ? '' : ' (Backlog is empty)'}"${has ? '' : ' disabled'}>Start</button>${has ? '' : '<span class="hint">Backlog is empty</span>'}`;
      }
      return `<article class="card agent s-${a.status}" aria-labelledby="ag-${a.id}-n">
        <div class="agent-h"><h3 id="ag-${a.id}-n">${esc(a.name)}</h3><span class="role">${esc(a.role)}</span>
          <span class="st-badge"><span aria-hidden="true">${STATUSES[a.status].glyph}</span> ${STATUSES[a.status].label}</span></div>
        <div class="cur"><span class="lbl">Current task</span>${t ? `<span class="t">${esc(t.title)}</span>` : '<span class="none">Nothing assigned</span>'}</div>
        ${a.status === 'error' ? `<div class="fault"><strong>Fault</strong>${esc(a.fault || 'Unknown failure')}</div>` : ''}
        <div class="prog-row">${progressBar(a)}${pctText(a)}</div>
        <div class="actions">${btn}</div>
      </article>`;
    }).join('')}</div>`;
  },

  'tk-count'() {
    const n = state.tasks.filter(taskMatches).length;
    return `${n} of ${state.tasks.length} tasks shown`;
  },
  'tk-board'() {
    const filtered = tasksFiltered();
    return `<div class="board">${COLS.map((c, i) => {
      const all = state.tasks.filter((t) => t.col === c.id);
      const shown = all.filter(taskMatches);
      let body;
      if (!all.length) body = emptyBlock('stage', 'Stage is empty', MODE.engine ? `Nothing in ${c.label} yet. The engine moves cards here as work progresses.` : `Nothing in ${c.label} yet. Drag a card here, or use ▶ on a card.`);
      else if (!shown.length) body = emptyBlock('nomatch', 'No tasks match the current filter', `${all.length} hidden in ${c.label}.`);
      else body = shown.map((t) => taskCard(t, i)).join('');
      return `<section class="col" data-col="${c.id}" aria-labelledby="col-h-${c.id}">
        <div class="col-h"><h2 id="col-h-${c.id}">${c.label}</h2><span class="ct" aria-label="${shown.length} of ${all.length} tasks">${filtered ? `${shown.length}/${all.length}` : all.length}</span></div>
        <div class="col-body">${body}</div></section>`;
    }).join('')}</div>`;
  },

  'ac-tally'() {
    const by = countBy(state.log, (e) => e.type);
    const chip = (id, label, n, tone) => `<button type="button" class="chip${tone ? ` tone-${tone}` : ''}" style="${tone ? '--st:var(--tn)' : ''}" aria-pressed="${ui.actType === id}" data-act="chip-type" data-v="${id}" data-fid="acchip-${id}"><span>${label}</span><span class="ct">${n}</span></button>`;
    return chip('all', 'All', state.log.length, '') + TYPE_ORDER.map((k) => chip(k, EVENT_TYPES[k].label, by[k] || 0, EVENT_TYPES[k].tone)).join('');
  },
  'ac-count'() {
    const n = ui.actType === 'all' ? state.log.length : state.log.filter((e) => e.type === ui.actType).length;
    return `${n} of ${state.log.length} events shown`;
  },
  'ac-list'() {
    if (!state.log.length) return `<div class="card">${emptyBlock('stage', 'The log is empty', 'No events recorded. Do something — pause an agent, create a task — and it lands here.', true)}</div>`;
    const items = ui.actType === 'all' ? state.log : state.log.filter((e) => e.type === ui.actType);
    if (!items.length) return `<div class="card">${emptyBlock('nomatch', `No ${EVENT_TYPES[ui.actType].label.toLowerCase()} events`, 'Nothing of this type has been logged. Pick another type or choose All.', true)}</div>`;
    return `<ul class="card feed">${items.map(eventRow).join('')}</ul>`;
  },
};

function taskCard(t, i) {
  const a = agentOnTask(t.id);
  const prev = COLS[i - 1], next = COLS[i + 1];
  const mv = (dir, tgt, glyph) => {
    const nm = esc(t.title);
    const lock = MODE.engine;
    const label = lock ? `Move “${nm}”: locked, the engine moves this card` : tgt ? `Move “${nm}” to ${tgt.label}` : `Move “${nm}” ${dir < 0 ? 'back' : 'forward'}: already in ${dir < 0 ? 'the first' : 'the last'} stage`;
    return `<button type="button" class="btn btn-sm mv" data-act="task-move" data-id="${t.id}" data-dir="${dir}" data-fid="mv-${dir < 0 ? 'prev' : 'next'}-${t.id}" data-alt="mv-${dir < 0 ? 'next' : 'prev'}-${t.id}" aria-label="${label}"${tgt && !lock ? '' : ' disabled'}>${glyph}</button>`;
  };
  return `<article class="task p-${t.priority}" draggable="${MODE.engine ? 'false' : 'true'}" data-task="${t.id}">
    <h3>${esc(t.title)}</h3>
    <div class="task-meta"><span class="tag pri">${PRIORITIES[t.priority]}</span><span class="tag">${esc(t.assignee)}</span>${t.prUrl ? `<a class="tag" href="${esc(t.prUrl)}" target="_blank" rel="noopener">PR<span class="sr"> (opens GitHub)</span></a>` : ''}${a ? `<span class="tag s-${a.status}" style="border-color:var(--st)"><span aria-hidden="true">${STATUSES[a.status].glyph}</span> ${Math.round(a.progress)}%</span>` : ''}</div>
    ${a && a.status !== 'idle' ? progressBar(a, 'task progress') : ''}
    <div class="task-foot">${mv(-1, prev, '◀')}${mv(1, next, '▶')}<span class="spacer"></span></div>
  </article>`;
}

function renderRegions() {
  for (const el of $$('[data-region]', $('#view'))) {
    if (ui.dragging && el.dataset.region === 'tk-board') continue;
    setHTML(el, REGIONS[el.dataset.region]());
  }
}

function render() {
  if (mounted !== ui.view) mountView();
  renderChrome();
  renderRegions();
}

/* ───────────── live relative times + heartbeat painter + rAF master loop ───────────── */
const painters = [];
const watch = (el, paintFn) => {
  let visible = false;
  const io = new IntersectionObserver(([en]) => { visible = en.isIntersecting; }, { threshold: 0.01 });
  io.observe(el);
  painters.push({ paintFn, get visible() { return visible; } });
};

watch($('#hb'), (now) => {
  const el = $('#hb');
  let txt;
  if (!hbLive()) txt = '░'.repeat(HB_CELLS);
  else {
    const pos = Math.floor(now / 110) % HB_CELLS;
    const flash = now - ui.lastTickAt < 450;
    txt = Array.from({ length: HB_CELLS }, (_, i) => (i === pos ? '█' : flash ? '▓' : Math.abs(i - pos) === 1 ? '▓' : '▒')).join('');
  }
  if (el.textContent !== txt) el.textContent = txt;
});

let lastRel = 0;
function tickRelTimes(now) {
  if (now - lastRel < 1000) return;
  lastRel = now;
  for (const el of $$('time.rel')) {
    const txt = relTime(+el.dataset.t);
    if (el.textContent !== txt) el.textContent = txt;
  }
}
function frame(now) {
  requestAnimationFrame(frame);
  tickRelTimes(now);
  if (!reduced) for (const p of painters) if (p.visible) p.paintFn(now);
}
mq.addEventListener('change', (e) => {
  reduced = e.matches;
  document.body.classList.toggle('reduced', reduced);
  paintStaticHb();
  sim.reschedule();
});

/* ───────────── dialog ───────────── */
const dlg = $('#dlg');
let dlgOpener = null;
function openDialog(html, opener) {
  dlgOpener = opener && opener !== document.body ? opener : $('#btn-new');
  dlg.innerHTML = `<div class="dlg-in">${html}</div>`;
  if (!dlg.open) dlg.showModal();
}
dlg.addEventListener('close', () => {
  dlg.innerHTML = '';
  const o = dlgOpener; dlgOpener = null;
  if (o && o.isConnected) o.focus({ preventScroll: true });
  else $('#main').focus({ preventScroll: true });
});
dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
dlg.addEventListener('keydown', (e) => {
  if (e.key !== 'Tab') return;
  const f = $$('button:not([disabled]), input, select, [href]', dlg).filter((x) => x.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

function openTaskModal(opener) {
  if (MODE.engine) { route('overview'); $('#fr-prompt')?.focus(); return; }
  const opt = (v, l, sel) => `<option value="${esc(v)}"${sel ? ' selected' : ''}>${esc(l)}</option>`;
  openDialog(`
    <form id="nt-form" novalidate>
      <h2 id="dlg-title">New task</h2>
      <div class="field"><label for="nt-title">Title</label>
        <input type="text" id="nt-title" maxlength="80" autocomplete="off" aria-describedby="nt-title-e">
        <span class="field-err" id="nt-title-e"></span></div>
      <div class="dlg-row">
        <div class="field"><label for="nt-assignee">Assignee</label>
          <select id="nt-assignee">${state.agents.map((a) => opt(a.name, a.name, a.name === state.settings.assignee)).join('')}</select></div>
        <div class="field"><label for="nt-priority">Priority</label>
          <select id="nt-priority">${Object.entries(PRIORITIES).map(([k, v]) => opt(k, v, k === 'med')).join('')}</select></div>
        <div class="field"><label for="nt-col">Initial column</label>
          <select id="nt-col">${COLS.map((c) => opt(c.id, c.label, c.id === 'backlog')).join('')}</select></div>
      </div>
      <div class="dlg-actions" style="margin-top:16px">
        <button type="button" class="btn" data-act="dlg-cancel">Cancel</button>
        <button type="submit" class="btn btn-primary">Create task</button>
      </div>
    </form>`, opener);
  $('#nt-title').focus();
}
function openConfirm({ title, body, label, onConfirm }, opener) {
  openDialog(`
    <h2 id="dlg-title">${esc(title)}</h2>
    <p>${esc(body)}</p>
    <div class="dlg-actions">
      <button type="button" class="btn" data-act="dlg-cancel">Cancel</button>
      <button type="button" class="btn btn-danger" id="dlg-ok">${esc(label)}</button>
    </div>`, opener);
  $('#dlg-ok').addEventListener('click', () => { dlg.close(); onConfirm(); });
  $('#dlg-ok').focus();
}

dlg.addEventListener('submit', (e) => {
  if (e.target.id !== 'nt-form') return;
  e.preventDefault();
  const title = $('#nt-title').value.trim();
  const err = $('#nt-title-e');
  let msg = '';
  if (!title) msg = 'Give the task a title.';
  else if (title.length < 3) msg = 'Use at least 3 characters.';
  else if (title.length > 80) msg = 'Keep it under 80 characters.';
  if (msg) {
    err.textContent = msg;
    $('#nt-title').setAttribute('aria-invalid', 'true');
    $('#nt-title').focus();
    return;
  }
  const payload = { title, assignee: $('#nt-assignee').value, priority: $('#nt-priority').value, col: $('#nt-col').value };
  dlg.close();
  actions.createTask(payload);
});
dlg.addEventListener('input', (e) => {
  if (e.target.id === 'nt-title') { e.target.removeAttribute('aria-invalid'); $('#nt-title-e').textContent = ''; }
});

/* ───────────── events ───────────── */
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-act]');
  if (!b || b.disabled) return;
  const id = b.dataset.id;
  switch (b.dataset.act) {
    case 'new-task': openTaskModal(b); break;
    case 'dlg-cancel': dlg.close(); break;
    case 'agent-pause': actions.pauseAgent(id); break;
    case 'agent-resume': actions.resumeAgent(id); break;
    case 'agent-retry': actions.retryAgent(id); break;
    case 'agent-start': actions.startAgent(id); break;
    case 'task-move': {
      const t = taskById(id);
      const tgt = t && COLS[colIdx(t.col) + Number(b.dataset.dir)];
      if (tgt) actions.moveTask(id, tgt.id);
      break;
    }
    case 'chip-agent': ui.agentStatus = ui.agentStatus === b.dataset.v ? 'all' : b.dataset.v; renderRegions(); break;
    case 'chip-type': ui.actType = ui.actType === b.dataset.v ? 'all' : b.dataset.v; renderRegions(); break;
    case 'clear-agents': ui.agentQ = ''; ui.agentStatus = 'all'; $('#ag-q').value = ''; renderRegions(); break;
    case 'clear-tasks':
      ui.taskQ = ''; ui.taskPri = 'all'; ui.taskAssignee = 'all';
      $('#tk-q').value = ''; $('#tk-pri').value = 'all'; $('#tk-asg').value = 'all';
      renderRegions(); break;
    case 'clear-log':
      openConfirm({ title: 'Clear the activity log?', body: `This removes all ${state.log.length} recorded events. It cannot be undone.`, label: 'Clear log', onConfirm: () => actions.clearLog() }, b);
      break;
    case 'reset-demo':
      openConfirm({ title: 'Reset demo data?', body: 'Fleet, board, activity log and settings return to the seeded state.', label: 'Reset everything', onConfirm: () => actions.resetDemo() }, b);
      break;
    case 'settings-reset': actions.resetSettings(); break;
    case 'appr-approve': actions.decideApproval(id, 'approve'); break;
    case 'appr-reject': actions.decideApproval(id, 'reject'); break;
  }
});

document.addEventListener('input', (e) => {
  if (e.target.id === 'fr-prompt') { ui.frPrompt = e.target.value; return; }
  const s = e.target.closest('[data-search]');
  if (!s) return;
  if (s.dataset.search === 'agents') ui.agentQ = s.value; else ui.taskQ = s.value;
  renderRegions();                                   // list regions only — the input is never re-rendered
});
document.addEventListener('change', (e) => {
  const f = e.target.closest('[data-filter]');
  if (!f) return;
  ui[f.dataset.filter] = f.value;
  renderRegions();
});

document.addEventListener('submit', (e) => {
  if (e.target.id === 'fr-form') {
    e.preventDefault();
    const p = $('#fr-prompt').value.trim();
    if (p.length < 10) { engine.error = 'Describe the feature in at least 10 characters.'; queueRender(); return; }
    actions.submitFeature(p);
    return;
  }
  if (e.target.id !== 'st-form') return;
  e.preventDefault();
  const project = $('#st-project').value.trim();
  if (!project) {
    $('#st-project').setAttribute('aria-invalid', 'true');
    $('#st-project-e').textContent = 'Project name can’t be empty.';
    $('#st-project').focus();
    return;
  }
  $('#st-project').removeAttribute('aria-invalid');
  $('#st-project-e').textContent = '';
  const changed = actions.saveSettings({ project, assignee: $('#st-assignee').value, notify: $('#st-notify').checked, sim: $('#st-sim') ? $('#st-sim').checked : false });
  $('#st-saved').textContent = changed ? `Saved at ${new Date().toLocaleTimeString()}` : 'Nothing to save';
});

// settings actions that rewrite the form
const _reset = actions.resetSettings, _demo = actions.resetDemo;
actions.resetSettings = function () { _reset(); syncSettingsForm(); const s = $('#st-saved'); if (s) s.textContent = 'Defaults restored'; };
actions.resetDemo = function () { _demo(); syncSettingsForm(); };

// drag and drop
document.addEventListener('dragstart', (e) => {
  const c = e.target.closest?.('[data-task]');
  if (!c) return;
  ui.dragging = true;
  e.dataTransfer.setData('text/plain', c.dataset.task);
  e.dataTransfer.effectAllowed = 'move';
  c.classList.add('dragging');
});
document.addEventListener('dragend', (e) => {
  ui.dragging = false;
  e.target.classList?.remove('dragging');
  $$('.col.over').forEach((c) => c.classList.remove('over'));
  renderRegions();
});
document.addEventListener('dragover', (e) => {
  const col = e.target.closest?.('[data-col]');
  if (!col || !ui.dragging) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  $$('.col.over').forEach((c) => { if (c !== col) c.classList.remove('over'); });
  col.classList.add('over');
});
document.addEventListener('drop', (e) => {
  const col = e.target.closest?.('[data-col]');
  if (!col) return;
  e.preventDefault();
  const id = e.dataTransfer.getData('text/plain');
  ui.dragging = false;
  col.classList.remove('over');
  if (taskById(id)) actions.moveTask(id, col.dataset.col);
  else renderRegions();
});

// keyboard: N new task · / focus search · Esc handled natively by <dialog>
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || dlg.open) return;
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.isContentEditable) return;
  if (e.key === 'n' || e.key === 'N') { e.preventDefault(); openTaskModal(document.activeElement); }
  else if (e.key === '/') {
    e.preventDefault();
    if (!$('[data-search]')) route('tasks');
    $('[data-search]')?.focus();
  }
});

/* ───────────── routing ───────────── */
function route(view, announceFocus) {
  if (!VIEW_IDS.includes(view)) view = 'overview';
  if (location.hash !== `#/${view}`) history.replaceState(null, '', `#/${view}`);
  const changed = ui.view !== view;
  ui.view = view;
  render();
  if (changed && announceFocus) $('#pg-title')?.focus({ preventScroll: true });
  if (changed) window.scrollTo(0, 0);
}
window.addEventListener('hashchange', () => route(location.hash.replace(/^#\//, ''), true));

/* ───────────── boot ───────────── */
function startEngineStream() {
  const es = new EventSource('/events');                 // replays history, then live; reconnects send Last-Event-ID
  es.onopen = () => { engine.connected = true; queueRender(); };
  es.onerror = () => { engine.connected = false; queueRender(); };
  for (const t of ['THINKING', 'WRITE_CODE', 'COMMIT', 'PR_CREATED', 'NEEDS_REVIEW', 'BLOCKED']) {
    es.addEventListener(t, (m) => { try { actions.engineEvent(JSON.parse(m.data)); } catch (_) { /* bad frame */ } });
  }
  return es;
}

(async () => {
  await detectMode();
  if (MODE.engine) state = engineState();
  ui.view = VIEW_IDS.includes(location.hash.replace(/^#\//, '')) ? location.hash.replace(/^#\//, '') : 'overview';
  render();
  if (MODE.engine) { actions.refreshApprovals(); startEngineStream(); } else sim.reschedule();
  requestAnimationFrame(frame);
})();

window.__kode = { getState: () => state, ui, MODE, engine, actions, sim, EVENT_TYPES, isReduced: () => reduced };
})();
