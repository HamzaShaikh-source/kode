// Command palette (Ctrl/Cmd+K): jump to any page, session, project or teammate, or run an action, from the keyboard.
import { S, isLead } from './store.js';
import { h, icon, clear } from './util.js';
import { openNewSession, openInvite } from './dialogs.js';
import { openNewProject } from './views/projects.js';
import { providerMeta } from './util.js';

let open = null;

function items() {
  const lead = isLead();
  const go = (hash) => () => { location.hash = hash; };
  const out = [];
  const nav = lead
    ? [['Brain', 'brain', 'brain'], ['Projects', 'projects', 'branch'], ['Orchestrator', 'orchestrator', 'bolt'], ['Sessions', 'sessions', 'grid'], ['Team', 'team', 'user'],
       ['Models', 'models', 'key'], ['Usage', 'usage', 'chart'], ['Activity', 'activity', 'pulse'], ['Settings', 'settings', 'sliders']]
    : [['My machine', 'home', 'laptop'], ['Sessions', 'sessions', 'grid'], ['Activity', 'activity', 'pulse'], ['Privacy', 'settings', 'shield']];
  for (const [label, r, ic] of nav) out.push({ group: 'Go to', label, icon: ic, run: go('#/' + r) });
  const acts = [['New session', 'play', () => openNewSession()]];
  if (lead) acts.push(['New project', 'branch', () => openNewProject()], ['Invite a teammate', 'plus', () => openInvite()]);
  acts.push(['Toggle dark mode', 'sun', () => {
    const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = t; try { localStorage.setItem('kode.theme', t); } catch { /* storage blocked */ }
  }], ['Open the guide', 'list', () => window.open('/guide', '_blank', 'noopener')]);
  for (const [label, ic, run] of acts) out.push({ group: 'Actions', label, icon: ic, run });
  for (const p of S.projects || []) out.push({ group: 'Projects', label: p.name, hint: p.status.replace('_', ' '), icon: 'branch', run: go('#/projects/' + p.id) });
  for (const s of S.sessions) {
    const n = S.nodes.find((x) => x.id === s.node_id);
    out.push({ group: 'Sessions', label: providerMeta(s.provider, s.label).name, hint: (n ? n.member_name || n.name : '') + (s.state === 'live' ? '' : ' (' + s.state + ')'), icon: 'terminal', run: go('#/session/' + s.sid) });
  }
  if (lead) for (const n of S.nodes) out.push({ group: 'Team', label: n.member_name || n.name, hint: n.online ? 'online' : 'offline', icon: 'laptop', run: go('#/team') });
  return out;
}

// Ranked fuzzy match: contiguous substring beats scattered letters; earlier beats later.
function score(q, text) {
  if (!q) return 1;
  const t = text.toLowerCase();
  const i = t.indexOf(q);
  if (i >= 0) return 100 - i;
  let at = 0;
  for (const c of q) { at = t.indexOf(c, at); if (at < 0) return 0; at++; }
  return 10;
}

export function openPalette() {
  if (open) { open.input.focus(); return; }
  const prev = document.activeElement;
  const all = items();
  const input = h('input', { type: 'text', 'aria-label': 'Search pages, sessions, projects and actions', placeholder: 'Search pages, sessions, projects, actions...', autocomplete: 'off', spellcheck: 'false', role: 'combobox', 'aria-expanded': 'true', 'aria-controls': 'cmdk-list' });
  const list = h('div', { class: 'cmdk-list', id: 'cmdk-list', role: 'listbox' });
  const back = h('div', { class: 'cmdk-back' }, h('div', { class: 'cmdk', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Command palette' },
    h('div', { class: 'cmdk-in' }, icon('search', 16), input), list,
    h('div', { class: 'cmdk-f' }, h('span', {}, h('kbd', {}, 'Enter'), ' open'), h('span', {}, h('kbd', {}, 'Up'), ' ', h('kbd', {}, 'Down'), ' move'), h('span', {}, h('kbd', {}, 'Esc'), ' close'))));
  let shown = [], sel = 0;
  const close = () => { back.remove(); open = null; document.removeEventListener('keydown', key, true); if (prev && prev.focus) prev.focus(); };
  const run = (it) => { close(); it.run(); };
  function paint() {
    const q = input.value.trim().toLowerCase();
    shown = all.map((it) => ({ it, s: Math.max(score(q, it.label), q ? score(q, it.hint || '') * 0.5 : 0) })).filter((x) => x.s > 0)
      .sort((a, b) => (q ? b.s - a.s : 0)).map((x) => x.it).slice(0, 60);
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    clear(list);
    if (!shown.length) { list.append(h('div', { class: 'cmdk-empty' }, 'Nothing matches "' + input.value + '"')); return; }
    let g = '';
    shown.forEach((it, i) => {
      if (!q && it.group !== g) { g = it.group; list.append(h('div', { class: 'cmdk-g' }, g)); }
      const row = h('div', { class: 'cmdk-i', role: 'option', id: 'cmdk-' + i, 'aria-selected': String(i === sel), onmousemove: () => { if (sel !== i) { sel = i; mark(); } }, onclick: () => run(it) },
        icon(it.icon, 16), h('span', {}, it.label), it.hint ? h('small', {}, it.hint) : null);
      list.append(row);
    });
    mark();
  }
  function mark() {
    list.querySelectorAll('.cmdk-i').forEach((el, i) => el.setAttribute('aria-selected', String(i === sel)));
    const cur = list.querySelector('#cmdk-' + sel);
    if (cur) { cur.scrollIntoView({ block: 'nearest' }); input.setAttribute('aria-activedescendant', cur.id); }
  }
  function key(e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); mark(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); mark(); }
    else if (e.key === 'Enter' && shown[sel]) { e.preventDefault(); run(shown[sel]); }
  }
  input.addEventListener('input', () => { sel = 0; paint(); });
  back.addEventListener('mousedown', (e) => { if (e.target === back) close(); });
  document.addEventListener('keydown', key, true);
  document.body.append(back);
  open = { input };
  paint();
  input.focus();
}

export function initPalette() {
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); openPalette(); }
  });
}
