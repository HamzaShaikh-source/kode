// Projects: one brief -> a plan with frozen contract -> every teammate's agent builds its share -> merge, verify, fix, done.
import { S, nodeById } from '../store.js';
import { h, icon, setText, clear, rel, fmtNum } from '../util.js';
import { chip, modal, toast } from '../components.js';
import * as hub from '../hubclient.js';

const STATUS = { draft: ['Draft', ''], planning: ['Planning', 'info'], plan_ready: ['Plan ready', 'warn'], running: ['Running', 'accent'], paused: ['Paused', 'warn'], done: ['Done', 'ok'], failed: ['Failed', 'bad'], cancelled: ['Cancelled', ''] };
const TASK = { pending: ['Waiting', ''], starting: ['Starting', 'info'], running: ['Building', 'accent'], review: ['Merging', 'info'], merged: ['Merged', 'ok'], blocked: ['Blocked', 'bad'], cancelled: ['Cancelled', ''] };
const PHASES = [['plan', 'Plan'], ['bootstrap', 'Contract'], ['build', 'Build'], ['verify', 'Verify'], ['polish', 'Polish'], ['done', 'Done']];

export function openNewProject() {
  const nodes = S.nodes.filter((n) => n.online);
  if (!nodes.length) { toast('No teammate is online. Invite one first.', 'bad'); return; }
  const name = h('input', { id: 'np-name', type: 'text', maxlength: 60, placeholder: 'e.g. Campus event planner' });
  const brief = h('textarea', { id: 'np-brief', rows: 5, maxlength: 4000, placeholder: 'What should the team build? Include the must-have features, the stack if you care, and the demo you want to show.' });
  const repo = h('input', { id: 'np-repo', type: 'text', placeholder: 'owner/name (leave empty to create a new repository)', spellcheck: 'false' });
  const rname = h('input', { id: 'np-rname', type: 'text', maxlength: 100, placeholder: 'defaults to the project name', spellcheck: 'false' });
  const vis = h('select', { id: 'np-vis' }, h('option', { value: 'private' }, 'Private (only invited people)'), h('option', { value: 'public' }, 'Public (anyone can see it)'));
  const newRepo = h('div', { class: 'grid2' }, h('label', { class: 'f', for: 'np-rname' }, 'New repository name', rname), h('label', { class: 'f', for: 'np-vis' }, 'Visibility', h('small', {}, 'Every teammate is added as a collaborator and their laptop accepts automatically.'), vis));
  const syncRepo = () => { newRepo.style.display = forge.value === 'github' && !repo.value.trim() ? '' : 'none'; };
  const forge = h('select', { id: 'np-forge' }, h('option', { value: 'github' }, 'GitHub (each laptop uses its own gh login)'), h('option', { value: 'local' }, 'Local git folder (no GitHub, for rehearsal)'));
  const tests = h('input', { id: 'np-tests', type: 'text', placeholder: 'optional, e.g. npm test (otherwise the planner chooses)', spellcheck: 'false' });
  const picker = plannerPicker(nodes);
  forge.addEventListener('change', syncRepo); repo.addEventListener('input', syncRepo); syncRepo();
  const auto = h('input', { type: 'checkbox', id: 'np-auto' });
  const perms = h('select', { id: 'np-perm' }, h('option', { value: 'turbo' }, 'Turbo: agents never ask for permission (fastest)'), h('option', { value: 'auto' }, 'Supervised: agents ask, the supervisor model answers'), h('option', { value: 'ask' }, 'Ask me: every agent question becomes a card'));
  const err = h('div', { class: 'field-err', role: 'alert' });
  const rows = nodes.map((n, idx) => {
    const provs = (n.providers || []).filter((p) => p.available && p.id !== 'shell' && p.id !== 'opencode-free' && p.id !== 'demo-agent');
    const on = h('input', { type: 'checkbox', checked: true, 'aria-label': 'Include ' + (n.member_name || n.name) });
    const sel = h('select', { 'aria-label': 'Agent for ' + (n.member_name || n.name) }, provs.map((p) => h('option', { value: p.id }, p.label + (p.signed_in === false ? ' (sign in needed)' : ''))));
    const order = ['claude', 'agy', 'codex'];                       // a mixed team by default; OpenCode and Hermes stay selectable
    const pref = [order[idx % order.length], ...order, 'opencode', 'hermes'].find((x) => provs.some((p) => p.id === x)); if (pref) sel.value = pref;
    const prof = h('select', { 'aria-label': 'Model source for ' + (n.member_name || n.name) }, h('option', { value: '' }, 'Own login'), S.profiles.map((p) => h('option', { value: p.id }, p.name)));
    return { n, on, sel, prof, el: h('div', { class: 'mrow' }, on, h('div', { class: 'mrow-n' }, h('b', {}, n.member_name || n.name), h('small', { class: 'faint' }, n.access === 'full' ? 'full access' : n.access === 'copilot' ? 'co-pilot access' : 'asks each time')), sel, prof) };
  });
  const go = h('button', { class: 'btn primary', type: 'button' }, icon('play', 14), 'Plan the project');
  const body = h('div', { style: { display: 'grid', gap: '14px' } },
    h('label', { class: 'f', for: 'np-name' }, 'Project name', name), h('label', { class: 'f', for: 'np-brief' }, 'Brief', brief),
    h('div', { class: 'f' }, 'Team and agents', h('small', {}, 'Each teammate builds with the agent you pick on their own laptop.'), h('div', { class: 'mlist' }, rows.map((r) => r.el))),
    h('div', { class: 'grid2' }, h('label', { class: 'f', for: 'np-forge' }, 'Where the code lives', forge), h('label', { class: 'f', for: 'np-repo' }, 'Use an existing repository (optional)', repo)),
    newRepo,
    picker.el,
    h('label', { class: 'f', for: 'np-tests' }, 'Verification command', tests),
    h('label', { class: 'f', for: 'np-perm' }, 'Agent permissions', h('small', {}, 'Turbo lets each agent run any command and edit any file inside the project folder without asking. Terms, logins and destructive prompts still stop and ask you.'), perms),
    h('label', { class: 'opt' }, auto, h('span', {}, h('b', {}, 'Start building as soon as the plan is ready'), h('small', {}, 'Otherwise you review the plan and tasks first.'))), err);
  const close = modal({ title: 'New project', body, wide: true, actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), go] });
  go.addEventListener('click', async () => {
    setText(err, ''); go.disabled = true;
    try {
      if (picker.empty) throw new Error('No planner is available: log in to Claude Code or Codex on a laptop, or add an API profile on the Models page.');
      const members = rows.filter((r) => r.on.checked && r.sel.value).map((r) => ({ node_id: r.n.id, provider: r.sel.value, profile_id: r.prof.value }));
      const r = await hub.call({ t: 'project_create', name: name.value, brief: brief.value, repo: repo.value, repo_name: rname.value.trim(), visibility: vis.value, forge: forge.value, members, test_cmds: tests.value.trim() ? [tests.value.trim()] : [], auto_approve_plan: auto.checked, planner: picker.spec(), permissions: perms.value });
      if (!r.ok) throw new Error(r.err || 'could not create');
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
  const el = h('div', { style: { display: 'grid', gap: '10px' } }, h('label', { class: 'f' }, 'Planner', h('small', {}, 'Writes the plan and diagnoses failures. Pick any of these; nothing is chosen for you behind the scenes.'), sel), mw);
  return { el, spec: () => plannerSpec(sel.value, model.value.trim()), empty: !sel.options.length };
}
const plannerSpec = (v, model) => v.startsWith('cli:') ? { kind: 'cli', provider: v.slice(4) } : v.startsWith('profile:') ? { kind: 'profile', profile_id: v.slice(8), model } : { kind: 'default' };
const act = (t, id, extra = {}) => hub.call({ t, id, ...extra }).then((r) => { if (!r.ok) toast(r.err || 'Not possible right now', 'bad'); return r; }).catch((e) => toast(e.message, 'bad'));
const memberName = (p, nid) => (p.members.find((m) => m.node_id === nid) || { name: '?' }).name;
const progress = (p) => { const t = p.tasks || []; return t.length ? Math.round((t.filter((x) => x.status === 'merged').length / t.length) * 100) : 0; };

export function mount(root, arg) {
  const wrap = h('div', { class: 'page proj' });
  root.append(wrap);
  const newBtn = h('button', { class: 'btn primary', type: 'button', onclick: openNewProject }, icon('plus', 16), 'New project');
  let sig = '';

  function list() {
    const items = S.projects;
    clear(wrap);
    if (!items.length) { wrap.append(h('div', { class: 'card hero-empty empty-state' }, icon('branch', 40), h('h3', {}, 'No projects yet'), h('p', {}, 'Describe what you want built. K.O.D.E. plans it, splits it fairly, and drives every teammate\'s agent, commits and merges.'), h('button', { class: 'btn primary', type: 'button', onclick: openNewProject }, icon('plus', 16), 'Start your first project'))); return; }
    wrap.append(h('div', { class: 'plist' }, items.map((p) => {
      const [st, cls] = STATUS[p.status] || [p.status, ''];
      return h('a', { class: 'card pcard', href: '#/projects/' + p.id },
        h('div', { class: 'prow-t' }, h('h3', {}, p.name), chip(st, cls), p.phase && p.status === 'running' ? chip(p.phase, 'info') : null),
        h('p', { class: 'muted pbrief' }, p.brief),
        h('div', { class: 'pbar' }, h('div', { class: 'bar' }, h('i', { style: { width: Math.max(2, progress(p)) + '%' } })), h('small', { class: 'faint' }, `${(p.tasks || []).filter((x) => x.status === 'merged').length} of ${(p.tasks || []).length} tasks merged  |  ${p.members.length} teammates`)));
    })));
  }

  function detail(p) {
    clear(wrap);
    const [st, cls] = STATUS[p.status] || [p.status, ''];
    const acts = h('div', { class: 'pacts' });
    if (p.status === 'running') acts.append(h('button', { class: 'btn sm', type: 'button', onclick: () => act('project_pause', p.id) }, 'Pause'));
    if (p.status === 'paused' || (p.status === 'failed' && p.phase !== 'plan' && p.plan && p.plan.contract_files.length)) acts.append(h('button', { class: 'btn sm primary', type: 'button', onclick: () => act('project_resume', p.id) }, 'Resume'));
    if (['running', 'paused', 'planning', 'plan_ready'].includes(p.status)) acts.append(h('button', { class: 'btn sm danger', type: 'button', onclick: () => { if (confirm('Cancel this project? Running agent sessions are stopped.')) act('project_cancel', p.id); } }, 'Cancel'));
    else acts.append(h('button', { class: 'btn sm danger', type: 'button', onclick: async () => { if (confirm('Delete this project record? The repository is not touched.')) { await act('project_delete', p.id); location.hash = '#/projects'; } } }, icon('trash', 13), 'Delete'));
    const phaseIdx = Math.max(0, PHASES.findIndex((x) => x[0] === (p.status === 'done' ? 'done' : p.phase)));
    wrap.append(h('div', { class: 'phead' },
      h('div', {}, h('div', { class: 'prow-t' }, h('h2', { class: 'ptitle' }, p.name), chip(st, cls)), h('p', { class: 'muted', style: { maxWidth: '70ch' } }, p.brief),
        p.repo ? h('div', { class: 'faint mono', style: { fontSize: '12.5px' } }, p.forge === 'github' ? 'github.com/' + p.repo : p.repo) : null),
      acts));
    wrap.append(h('ol', { class: 'psteps', 'aria-label': 'Progress' }, PHASES.map(([k, l], i) => h('li', { class: i < phaseIdx || p.status === 'done' ? 'done' : i === phaseIdx ? 'now' : '' }, l))));
    if (p.note && p.status !== 'done') wrap.append(h('div', { class: 'note' }, p.note));
    if (p.error) {
      const rp = p.status === 'failed' && p.phase === 'plan' ? plannerPicker(S.nodes.filter((n) => n.online)) : null;
      wrap.append(h('div', { class: 'note bad', role: 'alert' }, h('b', {}, 'Stopped: '), p.error, rp ? h('div', { style: { marginTop: '10px', display: 'grid', gap: '10px' } }, rp.el, h('div', {}, h('button', { class: 'btn sm primary', type: 'button', onclick: () => act('project_replan', p.id, { planner: rp.spec() }) }, 'Plan again with this planner'))) : null));
    }
    for (const c of p.cards || []) {
      const b = h('div', { class: 'acard', role: 'alert' }, h('div', { class: 'acard-t' }, icon('hand', 15), h('b', {}, c.kind === 'prompt' ? 'An agent is waiting for you' : c.kind === 'stalled' ? 'A task looks stuck' : 'A task needs you')), h('pre', { class: 'acard-s' }, c.text),
        h('div', { class: 'acard-b' }, ...(c.sid ? [h('a', { class: 'btn sm', href: '#/session/' + c.sid }, 'Open console')] : []),
          ...(c.kind === 'prompt' ? [['no', 'Send n'], ['yes', 'Send y'], ['enter', 'Press Enter']].map(([k, l]) => h('button', { class: 'btn sm' + (k === 'enter' ? ' primary' : ''), type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: k }) }, l))
            : [h('button', { class: 'btn sm', type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: 'dismiss' }) }, 'Dismiss'), h('button', { class: 'btn sm', type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: 'skip' }) }, 'Skip task'), h('button', { class: 'btn sm primary', type: 'button', onclick: () => act('project_card', p.id, { card: c.id, action: 'retry' }) }, 'Retry')])));
      wrap.append(b);
    }
    if (p.plan && p.status === 'plan_ready') wrap.append(planReview(p));
    if (p.tasks && p.tasks.length && p.status !== 'plan_ready') wrap.append(lanes(p));
    if (p.tasks && p.tasks.length) wrap.append(balance(p));
    if (p.verify) wrap.append(verifyCard(p));
    const q = (p.final && p.final.quality) || p.quality;
    if (q) wrap.append(qualityCard(p, q));
    if (p.final && p.final.passed) wrap.append(h('div', { class: 'note ok' }, h('b', {}, 'Finished. '), `All ${p.tasks.length} tasks merged and the verification commands pass${p.round ? ` after ${p.round} fix round(s)` : ''}.`));
    wrap.append(h('div', { class: 'sec-h' }, h('h2', {}, 'Activity')), h('div', { class: 'card plog' }, (p.log || []).slice().reverse().slice(0, 80).map((l) => h('div', { class: 'plog-r ' + l.kind }, h('time', {}, rel(l.ts)), h('span', {}, l.text)))));
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
        h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['Task', 'Owner', 'Files it owns', 'Needs'].map((x) => h('th', {}, x)))),
          h('tbody', {}, p.tasks.map((t) => h('tr', {}, h('td', {}, h('b', {}, t.id + ' '), t.title, h('div', { class: 'faint', style: { fontSize: '12px' } }, `weight ${t.weight}`)), h('td', {}, own(t)), h('td', { class: 'mono', style: { fontSize: '12px' } }, t.files_owned.join(', ')), h('td', {}, t.depends_on.join(', ') || '-'))))),
        rpk.el, h('div', { class: 'pplan-a' }, fb, h('button', { class: 'btn', type: 'button', onclick: () => act('project_replan', p.id, { feedback: fb.value, planner: rpk.spec() }) }, 'Re-plan'), h('button', { class: 'btn primary', type: 'button', onclick: () => act('project_approve', p.id) }, icon('play', 14), 'Approve and start building'))));
  }

  function lanes(p) {
    return h('div', {}, h('div', { class: 'sec-h' }, h('h2', {}, 'Team')), h('div', { class: 'lanes' }, p.members.map((m) => {
      const ts = p.tasks.filter((t) => t.owner === m.node_id);
      const node = nodeById(m.node_id);
      return h('section', { class: 'card lane' }, h('div', { class: 'card-h' }, h('div', {}, h('b', {}, m.name), h('small', { class: 'faint', style: { display: 'block' } }, m.provider + (node && node.online ? '' : ' (offline)')))),
        h('div', { class: 'lane-b' }, ts.map((t) => {
          const [ts_, tc] = TASK[t.status] || [t.status, ''];
          return h('div', { class: 'tcard ' + t.status }, h('div', { class: 'prow-t' }, h('b', {}, t.id), chip(ts_, tc), t.round ? chip('fix', 'warn') : null), h('div', {}, t.title),
            h('div', { class: 'faint', style: { fontSize: '12px' } }, `${t.commits || 0} commit${t.commits === 1 ? '' : 's'}${t.violations ? ', ' + t.violations + ' scope fix' : ''}`),
            t.wait ? h('div', { class: 'twait' }, icon('wifi', 12), t.wait + ' - retrying automatically') : null,
            h('div', { class: 'tcard-a' }, t.sid && ['running', 'starting', 'review'].includes(t.status) ? h('a', { class: 'btn sm', href: '#/session/' + t.sid }, icon('terminal', 12), 'Console') : null,
              t.pr_url && t.pr_url.startsWith('http') ? h('a', { class: 'btn sm', href: t.pr_url, target: '_blank', rel: 'noopener' }, 'PR') : null));
        })));
    })));
  }

  function balance(p) {
    const rows = p.members.map((m) => {
      const fin = p.final && p.final.balance.find((b) => b.member === m.name);
      const commits = fin ? fin.commits : p.tasks.filter((t) => t.owner === m.node_id).reduce((a, t) => a + (t.commits || 0), 0);
      return { name: m.name, commits, lines: fin ? fin.added : p.tasks.filter((t) => t.owner === m.node_id).reduce((a, t) => a + (t.added || 0), 0) };
    });
    const max = Math.max(1, ...rows.map((r) => r.commits));
    return h('div', {}, h('div', { class: 'sec-h' }, h('h2', {}, 'Contributions'), h('p', {}, p.final ? 'Final, from git history' : 'Live, from each laptop\'s branch')),
      h('div', { class: 'card card-b', style: { display: 'grid', gap: '12px' } }, rows.map((r) => h('div', {}, h('div', { class: 'orow' }, h('b', {}, r.name), h('span', { class: 'mono' }, `${r.commits} commits, ${fmtNum(r.lines)} lines added`)), h('div', { class: 'bar' }, h('i', { style: { width: Math.max(2, (r.commits / max) * 100) + '%' } }))))));
  }

  function qualityCard(p, q) {
    const open = q.issues || [];
    const facts = [['README', q.readme ? q.readme + ' chars' : 'missing'], ['Tests', q.tests ? 'present' : 'none found'], ['Stray files', String(q.stray || 0)], ['Secrets', String(q.secrets || 0)], ['Files', String(q.files || 0)]];
    return h('div', {}, h('div', { class: 'sec-h' }, h('h2', {}, 'Quality review'), h('p', {}, (open.length ? open.length + ' open note' + (open.length === 1 ? '' : 's') : 'Clean') + (q.round ? `, after ${q.round} polish round${q.round === 1 ? '' : 's'}` : ''))),
      h('div', { class: 'card card-b', style: { display: 'grid', gap: '10px' } },
        h('div', { class: 'prow-t' }, facts.map(([k, v]) => chip(k + ': ' + v, 'ok'))),
        open.length ? h('ul', { class: 'muted' }, open.map((i) => h('li', {}, h('b', {}, i.severity + ': '), i.title))) : h('p', { class: 'muted' }, 'The README, tests and repository hygiene were checked and a reviewer model found nothing to fix.')));
  }

  function verifyCard(p) {
    const v = p.verify;
    return h('div', {}, h('div', { class: 'sec-h' }, h('h2', {}, 'Verification'), h('p', {}, (v.passed ? 'Passed' : 'Failed') + (v.round ? `, after fix round ${v.round}` : ''))),
      h('div', { class: 'card card-b', style: { display: 'grid', gap: '10px' } }, v.results.map((r) => h('div', {}, h('div', { class: 'prow-t' }, chip(r.code === 0 ? 'exit 0' : 'exit ' + r.code, r.code === 0 ? 'ok' : 'bad'), h('code', {}, r.cmd)), h('pre', { class: 'cmd', style: { maxHeight: '180px', overflow: 'auto' } }, (r.out || '').slice(-1600))))));
  }

  function update() {
    const p = arg ? S.projects.find((x) => x.id === arg) : null;
    const s = JSON.stringify(arg ? p : S.projects.map((x) => [x.id, x.status, x.phase, (x.tasks || []).map((t) => t.status)])) + S.nodes.length;
    if (s === sig) return;
    sig = s;
    if (!arg) { list(); return; }
    if (!p) { clear(wrap).append(h('div', { class: 'empty-state' }, h('h3', {}, S.loaded ? 'Project not found' : 'Loading...'), h('a', { class: 'btn', href: '#/projects' }, 'All projects'))); return; }
    const y = window.scrollY; detail(p); window.scrollTo(0, y);
  }
  return {
    meta: () => { const p = arg ? S.projects.find((x) => x.id === arg) : null; return { title: 'Projects', sub: p ? p.name : 'One brief, the whole team building', actions: arg ? [{ key: 'back', el: h('a', { class: 'btn', href: '#/projects' }, 'All projects') }] : [{ key: 'new', el: newBtn }] }; },
    update, destroy() {},
  };
}
