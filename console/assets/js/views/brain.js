// Brain view: the lead's cockpit in the middle, every machine around it, every provider session as a live minimized console.
import { S, sessionsOf, isLead } from '../store.js';
import { h, icon, setText, setClass, reconcile, fmtNum } from '../util.js';
import { chip, createTile, updateTile, tokensOf } from '../components.js';
import { openAddMachine, openNewSession } from '../dialogs.js';
import { showHelp } from '../shortcuts.js';

const NS = 'http://www.w3.org/2000/svg';

export function capabilityCards() {
  const items = [
    ['laptop', 'Connect any machine', 'A one-time code pairs a laptop. The agent only connects outward, so it works behind a router.'],
    ['terminal', 'Run any provider', 'Claude Code, Codex, Hermes, OpenCode, Antigravity or a plain shell: anything that runs in a terminal.'],
    ['eye', 'Watch it live', 'Every session is a live console. Click a tile to see the full terminal, its plan, files changed and usage.'],
    ['hand', 'Step in, with consent', 'Ask for co-pilot or takeover. The machine\'s owner approves, sees a banner, and can pull the kill switch.'],
    ['chart', 'Track real usage', 'Token counts come from the provider\'s own transcripts. Where a provider does not report them, we say unknown.'],
    ['shield', 'Audited and redacted', 'Secrets are scrubbed on the machine before output leaves it, and every control action is written to an audit log.'],
  ];
  return h('div', { class: 'cap-grid' }, items.map(([ic, t, p]) => h('div', { class: 'card cap' }, h('div', { class: 'ci' }, icon(ic, 19)), h('h3', {}, t), h('p', {}, p))));
}

export function mount(root) {
  const R = {};
  const addBtn = h('button', { class: 'btn', type: 'button', onclick: openAddMachine }, icon('plus', 16), 'Invite teammate');
  const newBtn = h('button', { class: 'btn primary', type: 'button', onclick: () => openNewSession() }, icon('play', 14), 'New session');
  const stat = (key, ic, label) => {
    const v = h('div', { class: 'v' }), s = h('div', { class: 's' });
    R[key] = { v, s };
    return h('div', { class: 'card stat' }, h('div', { class: 'k' }, icon(ic, 15), label), v, s);
  };
  const stats = h('section', { class: 'stats', 'aria-label': 'Summary' },
    stat('mach', 'laptop', 'Machines online'), stat('live', 'terminal', 'Sessions live'), stat('work', 'bolt', 'Working now'), stat('attn', 'alert', 'Needs attention'), stat('tok', 'chart', 'Tokens measured'));

  const edges = document.createElementNS(NS, 'svg');
  edges.setAttribute('class', 'edges'); edges.setAttribute('aria-hidden', 'true');
  const brainEl = h('div', { class: 'brain', role: 'img', 'aria-label': 'The Brain: the team lead\'s console' }, brainSvg());
  R.bsub = h('span');
  const brainCol = h('div', { class: 'brain-col' }, brainEl, h('div', { class: 'brain-label' }, h('b', {}, 'The Brain'), R.bsub));
  const mcol = h('div', { class: 'mcol' });
  const stage = h('section', { class: 'stage', 'aria-label': 'Machines and their live sessions' }, edges, brainCol, mcol);
  const empty = h('div', { class: 'card hero-empty empty-state' }, icon('laptop', 40), h('h3', {}, 'Connect your first machine'),
    h('p', {}, 'Machines show up here with a live console for every provider they run. Pair one with a one-time code; it takes under a minute.'),
    h('div', { style: { display: 'flex', gap: '10px' } }, h('button', { class: 'btn primary', type: 'button', onclick: openAddMachine }, icon('plus', 16), 'Invite a teammate')));
  const onboard = h('section', { class: 'card onboard', 'aria-label': 'Get started' });
  const page = h('div', { class: 'page' }, onboard, stats,
    h('div', { class: 'sec-h' }, h('h2', {}, 'Live map'), h('p', {}, 'Click any console to open it'), h('span', { class: 'grow' })),
    stage, empty,
    h('div', { class: 'sec-h' }, h('h2', {}, 'What you can do here')), capabilityCards());
  root.append(page);

  function createMachine(n) {
    const r = {};
    const el = h('article', { class: 'card mach' },
      h('header', { class: 'mach-h' }, h('span', { class: 'mach-ic' }, icon('laptop', 19)), r.titleBox = h('div', { class: 'grow' }, r.name = h('b'), r.sub = h('small')),
        r.badges = h('span', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'flex-end' } }),
        r.start = h('button', { class: 'btn sm', type: 'button' }, icon('plus', 14), 'Session')),
      h('div', { class: 'mach-b' }, r.tiles = h('div', { class: 'tiles' }), r.none = h('div', { class: 'empty-state', style: { padding: '14px' } }, h('p', {}, 'No sessions yet. Start one to see it live here.'))));
    r.start.addEventListener('click', () => openNewSession(el._id));
    el._r = r; el._id = n.id;
    return el;
  }
  function updateMachine(el, n) {
    const r = el._r, ss = sessionsOf(n.id);
    el._id = n.id; el._working = ss.some((s) => s.activity === 'working'); el._online = n.online;
    setText(r.name, n.name); setText(r.sub, `${n.hostname} · ${n.os}`);
    const key = [n.online, n.same_host, ss.length].join('|');
    if (r.badges._k !== key) {
      r.badges._k = key;
      r.badges.replaceChildren(chip(n.online ? 'Online' : 'Offline', n.online ? 'ok' : 'bad'), n.same_host ? chip('same PC as the Brain', 'info', 'laptop') : null);
    }
    r.start.disabled = !n.online;
    r.none.style.display = ss.length ? 'none' : 'grid';
    reconcile(r.tiles, ss, (s) => s.sid, (s) => createTile(s.sid), (t, s) => updateTile(t, s, n, false));
  }

  function drawEdges() {
    const sr = stage.getBoundingClientRect();
    if (!sr.width || window.innerWidth <= 860) return;
    edges.setAttribute('viewBox', `0 0 ${sr.width} ${sr.height}`);
    const b = brainEl.getBoundingClientRect();
    const sx = b.right - sr.left, sy = b.top + b.height / 2 - sr.top;
    const frag = [];
    for (const el of mcol.children) {
      const hd = el.querySelector('.mach-h').getBoundingClientRect();
      const ex = hd.left - sr.left, ey = hd.top + hd.height / 2 - sr.top, dx = Math.max(30, (ex - sx) / 2);
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('d', `M${sx.toFixed(1)} ${sy.toFixed(1)} C${(sx + dx).toFixed(1)} ${sy.toFixed(1)}, ${(ex - dx).toFixed(1)} ${ey.toFixed(1)}, ${ex.toFixed(1)} ${ey.toFixed(1)}`);
      p.setAttribute('class', 'edge ' + (!el._online ? 'off' : el._working ? 'work' : 'live'));
      const c = document.createElementNS(NS, 'circle');
      c.setAttribute('cx', ex); c.setAttribute('cy', ey); c.setAttribute('r', 4.5); c.setAttribute('fill', !el._online ? 'var(--line-2)' : el._working ? 'var(--accent)' : 'var(--ok)');
      frag.push(p, c);
    }
    edges.replaceChildren(...frag);
  }
  const ro = new ResizeObserver(() => requestAnimationFrame(drawEdges));
  ro.observe(stage);

  function renderOnboard() {
    if (localStorage.getItem('kode.onboard.hide')) { onboard.hidden = true; return; }
    const first = S.sessions.find((s) => s.state === 'live');
    const steps = [
      ['Invite your team', 'Send each teammate a link; they run one command on their laptop.', S.nodes.length > 0, h('button', { class: 'btn sm primary', type: 'button', onclick: openAddMachine }, 'Invite teammate')],
      ['Start a provider session', 'Run Claude Code, Codex, Hermes, OpenCode or a shell on that machine.', S.sessions.length > 0, h('button', { class: 'btn sm primary', type: 'button', onclick: () => openNewSession() }, 'New session')],
      ['Open a live console', 'Click any tile to see the full terminal, plan, files and usage.', !!localStorage.getItem('kode.seen.session'), first ? h('a', { class: 'btn sm', href: '#/session/' + first.sid }, 'Open one') : null],
      ['Try control, with consent', 'Ask for co-pilot. The machine\'s owner approves; you can then type into the session.', !!localStorage.getItem('kode.tried.control') || S.events.some((e) => e.kind === 'answer'), first ? h('a', { class: 'btn sm', href: '#/session/' + first.sid }, 'Try it') : null],
    ];
    const done = steps.filter((s) => s[2]).length;
    const sig = JSON.stringify([steps.map((s) => s[2]), first && first.sid]);
    onboard.hidden = false;
    if (onboard._sig === sig) return;
    onboard._sig = sig;
    onboard.replaceChildren(
      h('div', { class: 'card-h' }, icon('bolt', 17), h('h3', {}, done === steps.length ? 'You are all set' : 'Get started'), chip(`${done} of ${steps.length}`, done === steps.length ? 'ok' : 'accent'), h('span', { style: { flex: 1 } }),
        h('a', { class: 'btn sm ghost', href: '/guide', target: '_blank', rel: 'noopener' }, 'Read the guide'), h('button', { class: 'btn sm ghost', type: 'button', onclick: showHelp }, 'Shortcuts'),
        h('button', { class: 'btn sm ghost', type: 'button', 'aria-label': 'Hide the checklist', onclick: () => { localStorage.setItem('kode.onboard.hide', '1'); onboard.hidden = true; } }, icon('x', 14))),
      h('ol', { class: 'steps' }, steps.map(([t, d, ok, act], i) => h('li', { class: ok ? 'done' : '' }, h('span', { class: 'n' }, ok ? icon('check', 14) : String(i + 1)), h('div', { class: 'grow' }, h('b', {}, t), h('p', {}, d)), ok ? null : act))));
  }

  function update() {
    renderOnboard();
    const online = S.nodes.filter((n) => n.online), live = S.sessions.filter((s) => s.state === 'live'), work = live.filter((s) => s.activity === 'working');
    const attn = S.requests.length + S.sessions.filter((s) => s.state === 'error').length;
    const tok = S.sessions.reduce((a, s) => a + (s.usage && s.usage.source && s.usage.source !== 'terminal' ? (s.usage.input_tokens || 0) + (s.usage.output_tokens || 0) : 0), 0);
    const measured = S.sessions.filter((s) => s.usage && s.usage.source && s.usage.source !== 'terminal').length;
    setText(R.mach.v, online.length); setText(R.mach.s, `${S.nodes.length} paired in total`);
    setText(R.live.v, live.length); setText(R.live.s, `${S.sessions.length} sessions known`);
    setText(R.work.v, work.length); setText(R.work.s, work.length ? 'producing output right now' : 'all quiet');
    setText(R.attn.v, attn); setText(R.attn.s, S.requests.length ? `${S.requests.length} awaiting a member` : 'nothing waiting');
    setText(R.tok.v, fmtNum(tok)); setText(R.tok.s, measured ? `from ${measured} instrumented session(s)` : 'no instrumented sessions yet');
    setText(R.bsub, `${online.length} machine(s) · ${live.length} live`);
    const nodes = [...S.nodes].sort((a, b) => (b.online - a.online) || a.name.localeCompare(b.name));
    stage.style.display = nodes.length ? '' : 'none';
    empty.style.display = nodes.length ? 'none' : '';
    reconcile(mcol, nodes, (n) => n.id, createMachine, updateMachine);
    requestAnimationFrame(drawEdges);
  }
  return {
    meta: () => ({ title: 'The Brain', sub: S.connected ? `${S.nodes.filter((n) => n.online).length} machine(s) connected` : 'Connecting to the Brain…', actions: [{ key: 'a', el: addBtn }, { key: 'n', el: newBtn }] }),
    update,
    destroy() { ro.disconnect(); mcol.querySelectorAll('.tile').forEach((t) => t._destroy && t._destroy()); },
  };
}

function brainSvg() {
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('viewBox', '0 0 100 100'); s.setAttribute('aria-hidden', 'true');
  s.innerHTML = `
    <g stroke="var(--accent)" stroke-width="2.4" stroke-linecap="round" fill="none" opacity=".55">
      <path d="M50 50 22 24M50 50 80 22M50 50 16 62M50 50 84 64M50 50 50 86M22 24 16 62M80 22 84 64M16 62 50 86M84 64 50 86"/></g>
    <g fill="var(--card)" stroke="var(--accent)" stroke-width="3">
      <circle cx="22" cy="24" r="7"/><circle cx="80" cy="22" r="7"/><circle cx="16" cy="62" r="7"/><circle cx="84" cy="64" r="7"/><circle cx="50" cy="86" r="7"/></g>
    <circle cx="50" cy="50" r="15" fill="var(--accent)"/><circle cx="50" cy="50" r="6.5" fill="var(--card)"/>`;
  return s;
}
