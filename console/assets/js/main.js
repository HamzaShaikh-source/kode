// K.O.D.E. console: boot, routing, sidebar / top bar / banners.
import { S, subscribe, emit, sessionById, isLead } from './store.js';
import { h, $, clear, icon, setText, setClass } from './util.js';
import * as hub from './hubclient.js';
import * as pool from './termpool.js';
import { toast } from './components.js';
import { openAddMachine, openNewSession } from './dialogs.js';
import * as shortcuts from './shortcuts.js';
import * as brain from './views/brain.js';
import * as sessions from './views/sessions.js';
import * as session from './views/session.js';
import * as machines from './views/machines.js';
import * as usage from './views/usage.js';
import * as activity from './views/activity.js';
import * as settings from './views/settings.js';
import * as home from './views/member.js';
import * as models from './views/models.js';
import * as orchestrator from './views/orchestrator.js';
import * as projects from './views/projects.js';

const VIEWS = { brain, sessions, session, machines, team: machines, models, orchestrator, projects, usage, activity, settings, home };
const LEAD_NAV = [['brain', 'Brain', 'brain'], ['projects', 'Projects', 'branch'], ['orchestrator', 'Orchestrator', 'bolt'], ['sessions', 'Sessions', 'grid'], ['team', 'Team', 'user'], ['models', 'Models', 'key'], ['usage', 'Usage', 'chart'], ['activity', 'Activity', 'pulse'], ['settings', 'Settings', 'sliders']];
const MEMBER_NAV = [['home', 'My machine', 'laptop'], ['sessions', 'Sessions', 'grid'], ['activity', 'Activity', 'pulse'], ['settings', 'Privacy', 'shield']];

const params = new URLSearchParams(location.search);
if (params.get('member')) { S.role = 'member'; S.key = params.get('member'); }
else { S.role = 'lead'; S.key = (document.querySelector('meta[name=kode-key]') || {}).content || params.get('key') || sessionStorage.getItem('kode.key') || ''; }
document.body.classList.toggle('rm', matchMedia('(prefers-reduced-motion: reduce)').matches);

// ── terminal <-> hub wiring ──
const canType = (sid) => {
  const s = sessionById(sid);
  if (!s || s.state !== 'live') return false;
  return isLead() ? ['copilot', 'takeover'].includes(s.mode) : s.mode !== 'takeover';
};
let lastBlock = 0;
pool.setIO({
  sendReply: (sid, d) => hub.send({ t: 'reply', sid, d }),
  sendInput: (sid, d) => hub.send({ t: 'in', sid, d }),
  canType,
  blocked: (sid) => {
    if (Date.now() - lastBlock < 2500) return;
    lastBlock = Date.now();
    const s = sessionById(sid);
    toast(!s || s.state !== 'live' ? 'This session is not running' : isLead() ? 'Read-only: ask the owner for co-pilot or takeover to type here' : 'The lead has exclusive control right now', 'bad');
  },
});

// ── shell ──
const sideEl = $('#side'), topEl = $('#top'), bannerEl = $('#banner'), mainEl = $('#main');
let nav = {}, topRefs = {}, cur = null, curName = '';

function buildSide() {
  const items = isLead() ? LEAD_NAV : MEMBER_NAV;
  nav = {};
  const connDot = h('span', { class: 'dot' }), connT = h('b'), connS = h('small', { class: 'faint' });
  const theme = h('button', { class: 'btn ghost icon-btn', type: 'button', 'aria-label': 'Toggle dark mode', onclick: () => {
    const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = t; localStorage.setItem('kode.theme', t); paintTheme();
  } });
  const paintTheme = () => { clear(theme).append(icon(document.documentElement.dataset.theme === 'dark' ? 'sun' : 'moon', 17)); };
  paintTheme();
  sideEl.append(
    h('div', { class: 'logo' }, h('div', { class: 'logo-mark' }, icon('brain', 20, '')), h('div', {}, h('b', {}, 'K.O.D.E.'), h('small', {}, 'Agent command center'))),
    h('div', { class: 'team' }, h('span', { class: 'av' }, isLead() ? 'L' : 'M'), h('div', {}, isLead() ? 'Team lead' : 'Team member', h('small', {}, isLead() ? 'You see every machine' : 'You see only your machine'))),
    h('div', { class: 'nav-label' }, 'Workspace'),
    h('nav', { class: 'nav', 'aria-label': 'Sections' }, items.map(([id, label, ic]) => {
      const cnt = h('span', { class: 'count', hidden: true });
      const a = h('a', { href: '#/' + id }, icon(ic, 18), h('span', { class: 'l' }, label), cnt);
      nav[id] = { a, cnt };
      return a;
    })),
    h('div', { class: 'side-foot' },
      h('a', { class: 'nav-ext', href: '/guide', target: '_blank', rel: 'noopener' }, icon('list', 16), 'How to use', h('span', { class: 'sr' }, ' (opens in a new tab)')),
      h('button', { class: 'nav-ext', type: 'button', onclick: () => shortcuts.showHelp() }, icon('terminal', 16), 'Keyboard shortcuts', h('kbd', {}, '?')),
      h('div', { class: 'conn' }, connDot, h('div', {}, connT, connS)), theme));
  topRefs.conn = { dot: connDot, t: connT, s: connS };
}

// Remote access: the lead key is only auto-served to the machine running the Brain. Anywhere else the lead pastes it once.
function showGate() {
  const input = h('input', { type: 'password', id: 'gate-key', autocomplete: 'off', 'aria-label': 'Lead key', placeholder: 'Paste the lead key' });
  const err = h('div', { class: 'field-err', role: 'alert' });
  mainEl.append(h('div', { class: 'gate' }, h('div', { class: 'card' }, h('div', { class: 'card-b', style: { display: 'grid', gap: '14px' } },
    h('div', { class: 'logo-mark', style: { width: '44px', height: '44px' } }, icon('key', 22)),
    h('h2', { style: { font: '600 24px var(--serif)' } }, 'Enter the lead key'),
    h('p', { class: 'muted' }, 'This console controls every machine on your team, so it asks for the lead key when opened from another computer. You can open it without a key from the computer that runs the Brain, or copy the key from the file '),
    h('pre', { class: 'cmd' }, 'backend/.hub_key'),
    h('form', { style: { display: 'grid', gap: '10px' }, onsubmit: (e) => { e.preventDefault(); const v = input.value.trim(); if (!v) { err.textContent = 'Paste the key first.'; return; } sessionStorage.setItem('kode.key', v); location.reload(); } },
      h('label', { class: 'f' }, 'Lead key', input), err, h('button', { class: 'btn primary', type: 'submit' }, 'Open the console'))))));
  setText($('#top h1') || document.createElement('i'), 'Sign in');
}

function buildTop() {
  topRefs.title = h('h1'); topRefs.sub = h('div', { class: 'sub' }); topRefs.acts = h('div', { class: 'acts' });
  topEl.append(h('div', { class: 'grow' }, topRefs.title, topRefs.sub), topRefs.acts);
}

function parseRoute() {
  const m = location.hash.replace(/^#\/?/, '').split('/');
  let name = m[0] || (isLead() ? 'brain' : 'home');
  if (!VIEWS[name] || (!isLead() && ['brain', 'machines', 'team', 'models', 'orchestrator', 'projects', 'usage'].includes(name)) || (isLead() && name === 'home')) name = isLead() ? 'brain' : 'home';
  return { name, arg: m[1] || '' };
}

function go() {
  const r = parseRoute();
  S.route = r;
  const key = r.name + ':' + r.arg;
  if (key === curName) return;
  if (cur && cur.destroy) cur.destroy();
  curName = key;
  clear(mainEl);
  cur = VIEWS[r.name].mount(mainEl, r.arg);
  mainEl.focus({ preventScroll: true });
  window.scrollTo(0, 0);
  paint();
}

function paint() {
  if (!cur) return;
  const live = S.sessions.filter((s) => s.state === 'live').length;
  const reqs = S.requests.length;
  const asks = S.sessions.filter((s) => s.needs_input).length + ((S.orch && S.orch.pending) ? S.orch.pending.length : 0);
  const counts = { sessions: S.sessions.length, team: S.nodes.filter((n) => n.online).length, orchestrator: (S.orch && S.orch.pending) ? S.orch.pending.length : 0, models: 0, projects: S.projects.filter((p) => ['running', 'planning'].includes(p.status)).length, activity: 0, home: reqs, brain: reqs };
  for (const [id, { a, cnt }] of Object.entries(nav)) {
    const on = S.route.name === id || (id === 'sessions' && S.route.name === 'session');
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    const c = counts[id] || 0;
    cnt.hidden = !c; setText(cnt, c); setClass(cnt, 'count' + (((id === 'brain' || id === 'home') && reqs) || id === 'orchestrator' ? ' hot' : ''));
  }
  const c = topRefs.conn;
  setClass(c.dot, 'dot ' + (S.connected ? 'ok' : S.authFailed ? 'bad' : 'warn'));
  setText(c.t, S.connected ? 'Brain connected' : S.authFailed ? 'Access refused' : 'Connecting…');
  setText(c.s, S.connected ? `${S.nodes.filter((n) => n.online).length} machine(s) · ${live} live` : '');
  const meta = cur.meta ? cur.meta() : { title: '', sub: '', actions: [] };
  setText(topRefs.title, meta.title); setText(topRefs.sub, meta.sub || '');
  const sig = (meta.actions || []).map((a) => a.key).join('|');
  if (topRefs.acts._sig !== sig) { topRefs.acts._sig = sig; clear(topRefs.acts).append(...(meta.actions || []).map((a) => a.el)); }
  const attention = S.requests.length + asks + S.projects.reduce((a, p) => a + (p.cards || []).length, 0);
  document.title = `${attention ? '(' + attention + ') ' : ''}${meta.title || 'Console'} · K.O.D.E.`;
  // loading skeleton: never flash an empty state before the first snapshot arrives
  const loading = !S.loaded && !S.authFailed && !(isLead() && !S.key);
  let sk = mainEl.querySelector('.boot');
  if (loading && !sk) mainEl.prepend(h('div', { class: 'boot', role: 'status', 'aria-label': 'Connecting to the Brain' }, h('div', { class: 'sk-row' }, [1, 2, 3, 4, 5].map(() => h('div', { class: 'sk sk-card' }))), h('div', { class: 'sk sk-big' })));
  if (!loading && sk) sk.remove();
  mainEl.classList.toggle('is-loading', loading);
  // banner
  let b = null;
  if (S.authFailed) b = ['bad', isLead() ? 'The Brain refused this lead key. Open the console from the machine that runs the Brain.' : 'This member link is not valid (the machine may have been removed).'];
  else if (!S.connected && S.everConnected) b = ['warn', 'Connection to the Brain lost. Reconnecting…'];
  const sig2 = b ? b[0] + b[1] : '';
  if (bannerEl._sig !== sig2) { bannerEl._sig = sig2; clear(bannerEl); if (b) bannerEl.append(h('div', { class: 'banner ' + b[0], role: 'status' }, icon('alert', 16), b[1])); }
  if (cur.update) cur.update();
}

buildSide(); buildTop(); shortcuts.init();
window.addEventListener('hashchange', go);
subscribe(() => { go(); paint(); });
window.__kode = { S, hub, pool, openAddMachine, openNewSession };

(async () => {
  try { await document.fonts.load('12px "JetBrains Mono"'); await document.fonts.load('500 14px Inter'); } catch { /* fonts are optional */ }
  if (isLead() && !S.key) { showGate(); return; }
  go(); paint();
  hub.connect();
})();
