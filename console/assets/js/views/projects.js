// Projects: brief or problem statement -> (ideas) -> plan with a frozen contract -> every teammate's agent builds -> merge, verify, polish, judge -> your review -> done.
import { S, nodeById } from '../store.js';
import { h, icon, setText, clear, rel, fmtNum, providerMeta } from '../util.js';
import { chip, modal, toast } from '../components.js';
import * as hub from '../hubclient.js';
import { askPermission, permission } from '../notify.js';
import * as P from './proj-parts.js';

const act = (t, id, extra = {}) => hub.call({ t, id, ...extra }).then((r) => { if (!r.ok) toast(r.err || 'Not possible right now', 'bad'); return r; }).catch((e) => { toast(e.message, 'bad'); return { ok: false }; });
const progress = (p) => { const t = p.tasks || []; return t.length ? Math.round((t.filter((x) => x.status === 'merged').length / t.length) * 100) : 0; };
const AGENT_ORDER = ['claude', 'codex', 'agy', 'commandcode', 'opencode', 'hermes'];
const HIDDEN = new Set(['shell', 'opencode-free', 'demo-agent']);
const fileB64 = (f) => new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(',')[1] || ''); r.onerror = () => rej(new Error('could not read ' + f.name)); r.readAsDataURL(f); });

// ───────────────────────────── the new-project dialog ─────────────────────────────
export async function openNewProject() {
  const nodes = S.nodes.filter((n) => n.online).sort((a, b) => Number(!!b.same_host) - Number(!!a.same_host));
  let defaults = {};
  try { const r = await hub.call({ t: 'settings_get' }, 3000); if (r.ok) defaults = r.values || {}; } catch { /* use built-in defaults */ }
  const name = h('input', { id: 'np-name', type: 'text', maxlength: 60, placeholder: 'e.g. Campus event planner' });
  const hack = h('input', { type: 'checkbox', id: 'np-hack' });
  const briefLabel = h('span', {}, 'What should the team build?');
  const brief = h('textarea', { id: 'np-brief', rows: 5, maxlength: 4000, placeholder: 'The must-have features, the stack if you care, and the demo you want to show.' });
  hack.addEventListener('change', () => { setText(briefLabel, hack.checked ? 'The problem statement' : 'What should the team build?'); brief.placeholder = hack.checked ? 'Paste the hackathon problem statement and any constraints (time, judging criteria, required tech). Your agents will research it, propose ideas, debate and vote.' : 'The must-have features, the stack if you care, and the demo you want to show.'; });
  // context staging
  const staged = [];
  const stagedEl = h('div', { class: 'ctxlist' });
  const drawStaged = () => { clear(stagedEl); staged.forEach((s, i) => stagedEl.append(h('div', { class: 'ctxi' }, h('div', { class: 'ic' }, icon(s.url ? 'link' : s.text ? 'list' : 'copy', 15)), h('div', {}, h('b', {}, s.name || s.url), h('small', {}, s.url ? 'link, fetched when planning starts' : s.text ? 'note' : (s.size / 1024).toFixed(0) + ' KB')), h('button', { class: 'btn sm', type: 'button', onclick: () => { staged.splice(i, 1); drawStaged(); } }, 'Remove')))); };
  const picker = h('input', { type: 'file', multiple: true, style: { display: 'none' }, onchange: async () => { for (const f of picker.files) { if (f.size > 6e6) { toast(f.name + ' is larger than 6 MB', 'bad'); continue; } staged.push({ name: f.name, mime: f.type, b64: await fileB64(f), size: f.size }); } picker.value = ''; drawStaged(); } });
  const urlIn = h('input', { type: 'text', placeholder: 'https://... the competition page, rules, API docs', 'aria-label': 'Link to add' });
  const ctxAdd = h('div', { class: 'pool' }, h('button', { class: 'btn sm', type: 'button', onclick: () => picker.click() }, icon('plus', 13), 'Add files'), picker, urlIn,
    h('button', { class: 'btn sm', type: 'button', onclick: () => { const u = urlIn.value.trim(); if (!/^https?:\/\//i.test(u)) { toast('Enter a full link starting with https://', 'bad'); return; } staged.push({ url: u, name: u }); urlIn.value = ''; drawStaged(); } }, 'Add link'),
    h('button', { class: 'btn sm', type: 'button', onclick: () => { const t = prompt('Paste text to attach (rules, criteria, prizes...)'); if (t && t.trim()) { staged.push({ text: t.trim(), name: 'Note: ' + t.trim().slice(0, 30) }); drawStaged(); } } }, 'Paste text'));
  // presets
  const presetSel = h('select', { 'aria-label': 'Load a saved preset' }, h('option', { value: '' }, 'Start from scratch'), (S.presets || []).map((p, i) => h('option', { value: String(i) }, p.name)));
  // team
  const rows = nodes.map((n) => {
    const provs = (n.providers || []).filter((p) => p.available && !HIDDEN.has(p.id)).sort((a, b) => AGENT_ORDER.indexOf(a.id) - AGENT_ORDER.indexOf(b.id));
    const on = h('input', { type: 'checkbox', checked: provs.length > 0, disabled: !provs.length, 'aria-label': 'Include ' + (n.member_name || n.name) });
    const sel = h('select', { 'aria-label': 'Main agent for ' + (n.member_name || n.name) }, provs.map((p) => h('option', { value: p.id }, p.label + (p.signed_in === false ? ' (sign in needed)' : ''))));
    const order = ['claude', 'agy', 'codex'];
    const pref = [order[nodes.indexOf(n) % order.length], ...order, 'commandcode', 'opencode', 'hermes'].find((x) => provs.some((p) => p.id === x)); if (pref) sel.value = pref;
    const prof = h('select', { 'aria-label': 'Model source for ' + (n.member_name || n.name) }, h('option', { value: '' }, 'Own login'), S.profiles.map((p) => h('option', { value: p.id }, p.name)));
    const pool = new Set();
    const poolEl = h('div', { class: 'pool' });
    const draw = () => { clear(poolEl); poolEl.append(h('small', {}, 'Also allow here:')); provs.filter((p) => p.id !== sel.value).forEach((p) => poolEl.append(h('button', { type: 'button', class: 'pchip', 'aria-pressed': String(pool.has(p.id)), title: p.signed_in === false ? 'Not signed in on that laptop' : 'Backup or extra agent', onclick: () => { if (pool.has(p.id)) pool.delete(p.id); else pool.add(p.id); draw(); } }, p.label))); if (provs.length < 2) poolEl.append(h('small', { class: 'faint' }, 'only one agent installed here')); };
    sel.addEventListener('change', () => { pool.delete(sel.value); draw(); }); draw();
    const el = h('div', { class: 'lap' }, h('div', { class: 'lap-h' }, on, h('div', {}, h('b', {}, n.member_name || n.name), h('small', { class: 'faint', style: { display: 'block' } }, (n.same_host ? 'Lead PC · ' : '') + (n.access === 'full' ? 'full access' : n.access === 'copilot' ? 'co-pilot access' : 'asks each time'))), sel, prof), poolEl);
    on.addEventListener('change', () => el.classList.toggle('off', !on.checked));
    return { n, on, sel, prof, pool, draw, el };
  });
  // helpers
  const planner = plannerPicker(nodes);
  const sw = (id, label, help, checked) => { const c = h('input', { type: 'checkbox', id, checked }); return { c, el: h('label', { class: 'sw', for: id }, c, h('span', {}, h('b', {}, label), h('small', {}, help))) }; };
  const askQ = sw('np-q', 'Let the planner ask me questions first', 'It asks only what would change what gets built: users, must-haves, tech, demo. You can skip them.', defaults['proj.default_questions'] !== '0');
  const humanRev = sw('np-rev', 'Ask me to review before it is final', 'When verify, polish and judge are done you see a report and can approve or request changes.', (defaults['proj.default_review'] || 'human') !== 'auto');
  const auto = sw('np-auto', 'Start building as soon as the plan is ready', 'Otherwise you review the plan and the task list first.', false);
  const repo = h('input', { id: 'np-repo', type: 'text', placeholder: 'owner/name (leave empty to create a new repository)', spellcheck: 'false' });
  const rname = h('input', { id: 'np-rname', type: 'text', maxlength: 100, placeholder: 'defaults to the project name', spellcheck: 'false' });
  const vis = h('select', { id: 'np-vis' }, h('option', { value: 'private' }, 'Private (only invited people)'), h('option', { value: 'public' }, 'Public (anyone can see it)'));
  const forge = h('select', { id: 'np-forge' }, h('option', { value: 'github' }, 'GitHub (each laptop uses its own gh login)'), h('option', { value: 'local' }, 'Local git folder (rehearsal, no GitHub)'));
  const newRepo = h('div', { class: 'grid2' }, h('label', { class: 'f', for: 'np-rname' }, 'New repository name', rname), h('label', { class: 'f', for: 'np-vis' }, 'Visibility', h('small', {}, 'Every teammate is added as a collaborator and their laptop accepts automatically.'), vis));
  const syncRepo = () => { newRepo.style.display = forge.value === 'github' && !repo.value.trim() ? '' : 'none'; };
  forge.addEventListener('change', syncRepo); repo.addEventListener('input', syncRepo); syncRepo();
  const tests = h('input', { id: 'np-tests', type: 'text', placeholder: 'optional, e.g. npm test (otherwise the planner chooses)', spellcheck: 'false' });
  const perms = h('select', { id: 'np-perm' }, h('option', { value: 'turbo' }, 'Turbo: agents never ask for permission (fastest)'), h('option', { value: 'auto' }, 'Supervised: agents ask, the supervisor model answers'), h('option', { value: 'ask' }, 'Ask me: every agent question becomes a card'));
  if (defaults['proj.default_permissions']) perms.value = defaults['proj.default_permissions'];
  const backup = []; const backupEl = h('div', { class: 'pool' });
  const allAgents = [...new Set(nodes.flatMap((n) => (n.providers || []).filter((p) => p.available && !HIDDEN.has(p.id)).map((p) => p.id)))].sort((a, b) => AGENT_ORDER.indexOf(a) - AGENT_ORDER.indexOf(b));
  const drawBackup = () => { clear(backupEl); backupEl.append(h('small', {}, 'If an agent hits its limit or breaks, try:')); allAgents.forEach((id) => { const i = backup.indexOf(id); backupEl.append(h('button', { type: 'button', class: 'pchip', 'aria-pressed': String(i >= 0), onclick: () => { if (i >= 0) backup.splice(i, 1); else backup.push(id); drawBackup(); } }, (i >= 0 ? (i + 1) + '. ' : '') + providerMeta(id).name)); }); backupEl.append(h('small', { class: 'faint' }, backup.length ? 'in this order, then the laptop\'s other agents, then a teammate.' : 'none chosen: the laptop\'s other agents, then a teammate.')); };
  const dflt = String(defaults['proj.default_backup'] || '').split(',').filter((x) => allAgents.includes(x)); backup.push(...dflt); drawBackup();
  const presetName = h('input', { type: 'text', placeholder: 'Remember this setup as...', 'aria-label': 'Preset name', maxlength: 50 });
  const err = h('div', { class: 'field-err', role: 'alert' });
  const go = h('button', { class: 'btn primary', type: 'button' }, icon('play', 14), 'Start');
  const apply = (pr) => {
    if (!pr) return;
    hack.checked = !!pr.hackathon; hack.dispatchEvent(new Event('change'));
    askQ.c.checked = !!pr.ask_questions; humanRev.c.checked = pr.review !== 'auto'; if (pr.permissions) perms.value = pr.permissions;
    backup.length = 0; backup.push(...(pr.backup || []).filter((x) => allAgents.includes(x))); drawBackup();
    rows.forEach((r) => { const m = (pr.members || []).find((x) => x.member === (r.n.member_name || r.n.name)); r.on.checked = !!m && r.sel.options.length > 0; r.el.classList.toggle('off', !r.on.checked);
      if (m) { if ([...r.sel.options].some((o) => o.value === m.provider)) r.sel.value = m.provider; r.pool.clear(); (m.pool || []).forEach((a) => { if (a !== r.sel.value) r.pool.add(a); }); if (m.profile_id) r.prof.value = m.profile_id; r.draw(); } });
  };
  presetSel.addEventListener('change', () => apply((S.presets || [])[Number(presetSel.value)]));
  const sec = (title, ...kids) => h('div', { class: 'dsec' }, h('h4', {}, title), ...kids);
  const body = h('div', {},
    (S.presets || []).length ? sec('Start from a saved setup', h('div', { class: 'presetbar' }, presetSel, h('small', { class: 'faint' }, 'Remembers the team, each laptop\'s agents, the planner and your options. Matched by teammate name.'))) : null,
    sec('The project', h('label', { class: 'f', for: 'np-name' }, 'Name', name),
      h('label', { class: 'sw', for: 'np-hack' }, hack, h('span', {}, h('b', {}, 'Hackathon mode: let the agents decide what to build'), h('small', {}, 'Paste the problem statement. Each agent researches it and proposes ideas; they critique each other, rebut, then vote. The idea with the most votes is built.'))),
      h('label', { class: 'f', for: 'np-brief' }, briefLabel, brief),
      h('div', { class: 'f' }, 'Context (optional)', h('small', {}, 'The event website, rules, judging criteria, PDFs, screenshots, API docs. The planner and every agent use them.'), stagedEl, ctxAdd)),
    sec('Team and agents', h('small', { class: 'faint' }, 'Pick the main agent on each laptop and which others it may use as extras or backups. The lead\'s own PC takes tasks and makes commits like everyone else.'),
      nodes.some((n) => n.same_host) ? null : h('div', { class: 'note warn' }, 'This PC (the Brain) is not on the team, so you would have no tasks or commits. Restart with run_kode.py (it joins this PC automatically) or add it from the Team page.'),
      rows.map((r) => r.el), h('div', { class: 'f' }, 'Backup agents', backupEl)),
    sec('How it should run', planner.el, askQ.el, humanRev.el, auto.el),
    h('details', { class: 'adv' }, h('summary', {}, 'Advanced: repository, permissions, verification'), h('div', { style: { display: 'grid', gap: '12px' } },
      h('div', { class: 'grid2' }, h('label', { class: 'f', for: 'np-forge' }, 'Where the code lives', forge), h('label', { class: 'f', for: 'np-repo' }, 'Use an existing repository (optional)', repo)), newRepo,
      h('label', { class: 'f', for: 'np-tests' }, 'Verification command', tests),
      h('label', { class: 'f', for: 'np-perm' }, 'Agent permissions', h('small', {}, 'Turbo lets each agent run any command and edit any file inside the project folder without asking. Terms, logins and destructive prompts still stop and ask you.'), perms))),
    h('div', { class: 'dsec' }, h('div', { class: 'presetbar' }, presetName, h('small', { class: 'faint' }, 'Optional: save this setup for next time.')), err));
  const close = modal({ title: 'New project', body, wide: true, actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), go] });
  go.addEventListener('click', async () => {
    setText(err, ''); go.disabled = true;
    try {
      if (planner.empty) throw new Error('No planner is available: log in to Claude Code or Codex on a laptop, or add an API profile on the Models page.');
      const on = rows.filter((r) => r.on.checked && r.sel.value);
      if (!on.length) throw new Error('Pick at least one laptop for the team.');
      const members = on.map((r) => ({ node_id: r.n.id, provider: r.sel.value, profile_id: r.prof.value, pool: [...r.pool] }));
      const cfg = { planner: planner.spec(), permissions: perms.value, backup: [...backup], review: humanRev.c.checked ? 'human' : 'auto', ask_questions: askQ.c.checked, hackathon: hack.checked };
      if (permission() === 'default') askPermission();                     // this click is the user gesture the browser needs to offer desktop notifications
      const r = await hub.call({ t: 'project_create', name: name.value, brief: brief.value, repo: repo.value, repo_name: rname.value.trim(), visibility: vis.value, forge: forge.value, members, test_cmds: tests.value.trim() ? [tests.value.trim()] : [],
        auto_approve_plan: auto.c.checked, context: staged.map((s) => (s.url ? { url: s.url } : s.text ? { text: s.text, name: s.name } : { name: s.name, mime: s.mime, b64: s.b64 })), ...cfg }, 120000);
      if (!r.ok) throw new Error(r.err || 'could not create');
      if (presetName.value.trim()) hub.call({ t: 'preset_save', name: presetName.value.trim(), members: on.map((r2) => ({ member: r2.n.member_name || r2.n.name, provider: r2.sel.value, pool: [...r2.pool], profile_id: r2.prof.value })), ...cfg });
      close(); location.hash = '#/projects/' + r.id;
    } catch (e) { setText(err, e.message); go.disabled = false; }
  });
}

// "Which model plans this project?" - every available source is listed; nothing is picked silently.
function plannerPicker(nodes) {
  const clis = ['claude', 'codex'].filter((id) => nodes.some((n) => (n.providers || []).some((p) => p.id === id && p.available)));
  const sel = h('select', { id: 'np-pl-' + Math.random().toString(36).slice(2, 6), 'aria-label': 'Planner model' },
    clis.map((id) => h('option', { value: 'cli:' + id }, (id === 'claude' ? 'Claude Code' : 'Codex') + ' (own login on that laptop, no API key)')),
    S.profiles.map((p) => h('option', { value: 'profile:' + p.id }, 'API profile: ' + p.name)),
    S.orch && S.orch.default_label ? h('option', { value: 'default' }, 'Brain default from .env: ' + S.orch.default_label) : null);
  const model = h('input', { type: 'text', placeholder: 'model name (profile default)', spellcheck: 'false', 'aria-label': 'Planner model name' });
  const mw = h('label', { class: 'f', style: { display: 'none' } }, 'Model', model);
  sel.addEventListener('change', () => { mw.style.display = sel.value.startsWith('profile:') ? '' : 'none'; });
  const el = h('div', { style: { display: 'grid', gap: '10px' } }, h('label', { class: 'f' }, 'Planner (the manager)', h('small', {}, 'Writes the plan, answers when a task is stuck and diagnoses failures. It keeps one conversation for the whole project, so it remembers every decision.'), sel), mw);
  return { el, spec: () => (sel.value.startsWith('cli:') ? { kind: 'cli', provider: sel.value.slice(4) } : sel.value.startsWith('profile:') ? { kind: 'profile', profile_id: sel.value.slice(8), model: model.value.trim() } : { kind: 'default' }), empty: !sel.options.length };
}

// ───────────────────────────── list + detail ─────────────────────────────
export function mount(root, arg) {
  const wrap = h('div', { class: 'page proj' });
  root.append(wrap);
  const newBtn = h('button', { class: 'btn primary', type: 'button', onclick: openNewProject }, icon('plus', 16), 'New project');
  let sig = '', tab = 'overview', deferred = false;
  const TABS = [['overview', 'Overview'], ['build', 'Build'], ['results', 'Results'], ['team', 'Team'], ['timeline', 'Timeline'], ['context', 'Context'], ['log', 'Log']];

  function list() {
    clear(wrap);
    const items = S.projects;
    if (!items.length) { wrap.append(h('div', { class: 'card hero-empty empty-state' }, icon('branch', 40), h('h3', {}, 'No projects yet'), h('p', {}, 'Describe what you want built, or paste a hackathon problem statement and let the agents pick the idea. K.O.D.E. plans it, gives everyone real work, and drives every laptop\'s agent.'), h('button', { class: 'btn primary', type: 'button', onclick: openNewProject }, 'New project'))); }
    else wrap.append(h('div', { class: 'plist' }, items.map((p) => {
      const [st, cls] = P.STATUS[p.status] || [p.status, ''];
      const n = P.nowInfo(p);
      return h('a', { class: 'card pcard', href: '#/projects/' + p.id },
        h('div', { class: 'prow-t' }, h('h3', {}, p.name), chip(st, cls), p.hackathon ? chip('hackathon', 'info') : null, p.completed ? chip('completed', 'ok') : null, p.judge && p.judge.overall != null ? chip(`${p.judge.overall}/100 ${p.judge.grade}`, 'ok') : null),
        h('p', { class: 'muted pbrief' }, p.problem || p.brief),
        ['running', 'planning', 'questions', 'review'].includes(p.status) ? h('div', { class: 'faint', style: { fontSize: '12.5px' } }, n.title) : null,
        h('div', { class: 'pbar' }, h('div', { class: 'bar' }, h('i', { style: { width: Math.max(2, progress(p)) + '%' } })), h('small', { class: 'faint' }, `${(p.tasks || []).filter((x) => x.status === 'merged').length} of ${(p.tasks || []).length} tasks merged  |  ${p.members.length} teammates  |  ${rel(p.created * 1000)}`)));
    })));
    const mem = S.memory || {};
    if ((mem.hackathons || []).length || Object.keys(mem.agents || {}).length) {
      wrap.append(h('div', { class: 'sec-h' }, h('h2', {}, 'Remembered from past hackathons'), h('p', {}, 'Used as the starting point when choosing who gets which task')),
        h('div', { class: 'card card-b', style: { display: 'grid', gap: '10px' } },
          Object.entries(mem.agents || {}).map(([id, a]) => h('div', { class: 'orow' }, h('span', {}, providerMeta(id).name), h('span', { class: 'mono' }, `${a.tasks} tasks · ${a.tasks ? Math.round((a.clean / a.tasks) * 100) : 0}% first time right · ${a.tasks ? Math.round(a.secs / a.tasks / 60) : 0} min per task`))),
          (mem.hackathons || []).slice().reverse().map((x) => h('div', { class: 'orow' }, h('span', {}, h('b', {}, x.name), h('small', { class: 'faint' }, '  ' + (x.idea || ''))), h('span', { class: 'mono' }, `${x.team.map((t) => t.name).join(', ')}${x.score != null ? ' · ' + x.score + '/100' : ''}`)))));
    }
  }

  function planReview(p) {
    const own = (t) => h('select', { 'aria-label': 'Owner of ' + t.id, onchange: (e) => act('project_assign', p.id, { task: t.id, node_id: e.target.value }) }, p.members.map((m) => h('option', { value: m.node_id, selected: m.node_id === t.owner }, m.name)));
    const rpk = plannerPicker(S.nodes.filter((n) => n.online));
    const fb = h('input', { type: 'text', placeholder: 'Optional: what should change in the plan?', 'aria-label': 'Feedback for re-planning' });
    return h('section', { class: 'card' }, h('div', { class: 'card-h' }, icon('list', 17), h('h3', {}, 'Review the plan')),
      h('div', { class: 'card-b', style: { display: 'grid', gap: '14px' } },
        h('p', {}, p.plan.summary), h('div', { class: 'faint' }, 'Stack: ' + p.plan.stack),
        h('div', {}, h('b', {}, 'Frozen contract'), ' (nobody may edit these): ', p.plan.contract_files.map((f) => h('code', { style: { marginRight: '6px' } }, f))),
        h('div', {}, h('b', {}, 'Verified by: '), p.plan.test_cmds.map((c) => h('code', { style: { marginRight: '6px' } }, c))),
        h('table', { class: 'rtab' }, h('thead', {}, h('tr', {}, ['Task', 'Owner', 'Files it owns', 'Needs'].map((x) => h('th', {}, x)))),
          h('tbody', {}, p.tasks.map((t) => h('tr', {}, h('td', {}, h('b', {}, t.id + ' '), t.title, (t.must || []).length ? h('div', { class: 'faint', style: { fontSize: '12px' } }, 'Must: ' + t.must.join('; ')) : null, (t.must_not || []).length ? h('div', { class: 'faint', style: { fontSize: '12px' } }, 'Must not: ' + t.must_not.join('; ')) : null),
            h('td', {}, own(t)), h('td', { class: 'mono', style: { fontSize: '12px' } }, t.files_owned.join(', ')), h('td', {}, t.depends_on.join(', ') || '-'))))),
        rpk.el, h('div', { class: 'pplan-a' }, fb, h('button', { class: 'btn', type: 'button', onclick: () => act('project_replan', p.id, { feedback: fb.value, planner: rpk.spec() }) }, 'Re-plan'), h('button', { class: 'btn primary', type: 'button', onclick: () => act('project_approve', p.id) }, icon('play', 14), 'Approve and start building'))));
  }

  const rerender = () => { sig = ''; update(); };

  function detail(p) {
    clear(wrap);
    const [st, cls] = P.STATUS[p.status] || [p.status, ''];
    const acts = h('div', { class: 'pacts' });
    if (p.status === 'running') acts.append(h('button', { class: 'btn sm', type: 'button', onclick: () => act('project_pause', p.id) }, 'Pause'));
    if (p.status === 'paused' || (p.status === 'failed' && p.phase !== 'plan' && p.plan && p.plan.contract_files.length)) acts.append(h('button', { class: 'btn sm primary', type: 'button', onclick: () => act('project_resume', p.id) }, 'Resume'));
    if (['done', 'failed', 'paused'].includes(p.status) && p.repo && p.coordinator) acts.append(h('button', { class: 'btn sm', type: 'button', title: 'Score the repository again with the independent judges', onclick: () => act('project_judge', p.id) }, 'Judge again'));
    if (p.status === 'done' && !p.completed) acts.append(h('button', { class: 'btn sm primary', type: 'button', title: 'Close the event: remembers the team, the agents and the results for next time', onclick: () => { if (confirm('Mark this hackathon complete? The team and agent results are remembered for next time.')) act('project_complete', p.id); } }, icon('check', 14), 'Hackathon complete'));
    if (['running', 'paused', 'planning', 'plan_ready', 'questions', 'review'].includes(p.status)) acts.append(h('button', { class: 'btn sm danger', type: 'button', onclick: () => { if (confirm('Cancel this project? Running agent sessions are stopped.')) act('project_cancel', p.id); } }, 'Cancel'));
    else acts.append(h('button', { class: 'btn sm danger', type: 'button', onclick: async () => { if (confirm('Delete this project record? The repository is not touched.')) { await act('project_delete', p.id); location.hash = '#/projects'; } } }, icon('trash', 14), 'Delete'));
    wrap.append(h('div', { class: 'phead' }, h('div', {}, h('div', { class: 'prow-t' }, h('h2', { class: 'ptitle' }, p.name), chip(st, cls), p.hackathon ? chip('hackathon', 'info') : null, p.completed ? chip('completed', 'ok') : null),
      h('p', { class: 'muted', style: { maxWidth: '70ch' } }, p.problem || p.brief), p.repo ? h('div', { class: 'faint mono', style: { fontSize: '12.5px' } }, p.forge === 'github' ? h('a', { href: 'https://github.com/' + p.repo, target: '_blank', rel: 'noopener' }, 'github.com/' + p.repo) : p.repo, ` · ${p.visibility || 'private'}`) : null), acts));
    wrap.append(P.nowBanner(p), P.stepper(p));
    if (p.error && p.status !== 'failed') wrap.append(h('div', { class: 'note bad', role: 'alert' }, h('b', {}, 'Problem: '), p.error));
    if (p.status === 'failed' && p.phase === 'plan') {
      const rp = plannerPicker(S.nodes.filter((n) => n.online));
      wrap.append(h('div', { class: 'note bad', role: 'alert' }, h('b', {}, 'Stopped: '), p.error || '', h('div', { style: { marginTop: '10px', display: 'grid', gap: '10px' } }, rp.el, h('div', {}, h('button', { class: 'btn sm primary', type: 'button', onclick: () => act('project_replan', p.id, { planner: rp.spec() }) }, 'Plan again with this planner')))));
    }
    const q = P.questionsPanel(p, act, rerender), rv = P.reviewPanel(p, act);
    if (q) wrap.append(h('div', { style: { marginTop: '16px' } }, q));
    if (rv) wrap.append(h('div', { style: { marginTop: '16px' } }, rv));
    const stuck = P.stuckCards(p, act);
    if (stuck.length) wrap.append(h('div', { style: { marginTop: '16px', display: 'grid', gap: '10px' } }, stuck));
    const working = (p.tasks || []).filter((t) => ['running', 'starting', 'review'].includes(t.status)).length;
    wrap.append(h('div', { class: 'tabbar', role: 'tablist' }, TABS.map(([k, l]) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(tab === k), onclick: () => { tab = k; rerender(); } }, l,
      k === 'build' && working ? h('span', { class: 'n hot' }, String(working)) : k === 'context' && (p.context || []).length ? h('span', { class: 'n' }, String(p.context.length)) : k === 'log' ? h('span', { class: 'n' }, String((p.log || []).length)) : null))));
    const body = h('div', { role: 'tabpanel' });
    if (tab === 'overview') {
      const parts = [P.stepsCard(p), P.plannerCard(p), p.status === 'plan_ready' ? planReview(p) : null, ['planning', 'questions'].includes(p.status) ? P.ideasCard(p) : null];
      if (['running', 'paused', 'review', 'done'].includes(p.status) && (p.tasks || []).length) {
        const act_ = p.tasks.filter((t) => ['running', 'starting', 'review'].includes(t.status));
        parts.push(h('div', {}, h('div', { class: 'sec-h' }, h('h2', {}, act_.length ? 'Being worked on right now' : 'Progress'), h('p', {}, `${p.tasks.filter((t) => t.status === 'merged').length} of ${p.tasks.length} tasks merged`)),
          h('div', { class: 'bar', style: { marginBottom: '14px' } }, h('i', { style: { width: Math.max(2, progress(p)) + '%' } })), act_.length ? h('div', { class: 'lanes' }, act_.slice(0, 6).map((t) => P.taskCard(p, t, act))) : null));
      }
      parts.push(h('div', {}, h('div', { class: 'sec-h' }, h('h2', {}, 'Latest')), h('div', { class: 'card log' }, (p.log || []).slice().reverse().slice(0, 8).map((l) => h('div', { class: 'log-r ' + l.kind }, h('time', {}, rel(l.ts)), h('span', { class: 'k' }, '·'), h('span', {}, l.text))))));
      body.append(h('div', { style: { display: 'grid', gap: '16px' } }, parts.filter(Boolean)));
    } else if (tab === 'build') body.append(P.buildTab(p, act));
    else if (tab === 'results') body.append(P.resultsTab(p));
    else if (tab === 'team') body.append(P.teamTab(p, rerender));
    else if (tab === 'timeline') body.append(P.timelineTab(p));
    else if (tab === 'context') body.append(P.contextTab(p, act, rerender));
    else body.append(P.logTab(p, rerender));
    wrap.append(body);
  }

  function update() {
    const p = arg ? S.projects.find((x) => x.id === arg) : null;
    const s = JSON.stringify(arg ? p : [S.projects.map((x) => [x.id, x.status, x.phase, (x.tasks || []).map((t) => t.status)]), S.memory]) + S.nodes.length + tab + (arg ? '' : '');
    if (s === sig) return;
    const ae = document.activeElement;
    if (arg && wrap.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName) && ae.type !== 'checkbox') {   // never rebuild the page under someone who is typing
      if (!deferred) { deferred = true; ae.addEventListener('blur', () => { deferred = false; setTimeout(update, 50); }, { once: true }); }
      return;
    }
    sig = s;
    if (!arg) { list(); return; }
    if (!p) { clear(wrap).append(h('div', { class: 'empty-state' }, h('h3', {}, S.loaded ? 'Project not found' : 'Loading...'), h('a', { class: 'btn', href: '#/projects' }, 'All projects'))); return; }
    const y = window.scrollY; detail(p); window.scrollTo(0, y);
  }
  return {
    meta: () => { const p = arg ? S.projects.find((x) => x.id === arg) : null; return { title: 'Projects', sub: p ? p.name : 'One brief or problem statement, the whole team building', actions: arg ? [{ key: 'back', el: h('a', { class: 'btn', href: '#/projects' }, 'All projects') }] : [{ key: 'new', el: newBtn }] }; },
    update, destroy() {},
  };
}
