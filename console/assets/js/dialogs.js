// Add-machine (pairing) and new-session dialogs.
import { S, nodeById, isLead, emit } from './store.js';
import { h, icon, setText, clear } from './util.js';
import { modal, toast, chip } from './components.js';
import * as hub from './hubclient.js';

function copyBtn(getText) {
  return h('button', { class: 'btn sm', type: 'button', onclick: async (e) => {
    try { await navigator.clipboard.writeText(getText()); toast('Copied to clipboard'); } catch { toast('Select the text and copy it manually', 'bad'); }
  } }, icon('copy', 14), 'Copy');
}

export const ACCESS = {
  ask: ['Ask each time', 'Every control request needs the teammate to approve. Safest.'],
  copilot: ['Co-pilot allowed', 'You can type into their sessions without asking. They can still switch you back to watch-only with one click.'],
  full: ['Full access', 'Co-pilot, and you may start sessions on their machine. This is the default for the team lead. Takeover still expires after 10 minutes and the kill switch always works.'],
};

// Where teammates reach this Brain. Without a reachable URL an invite link is useless, so the dialog says how to get one.
export function teamUrlPicker(net, onSaved) {
  const opts = [...new Set([net.team_url, net.public_url, ...(net.lan_urls || [])].filter(Boolean))];
  const input = h('input', { type: 'text', id: 'team-url', value: net.team_url || '', placeholder: 'http://192.168.1.20:8000 or https://name.trycloudflare.com', spellcheck: 'false', list: 'team-url-list' });
  const list = h('datalist', { id: 'team-url-list' }, opts.map((u) => h('option', { value: u })));
  const msg = h('div', { class: 'field-err', role: 'alert' });
  const save = h('button', { class: 'btn sm', type: 'button', onclick: async () => {
    const r = await hub.call({ t: 'set_team_url', url: input.value.trim() });
    if (!r.ok) { setText(msg, r.err || 'Could not save'); return; }
    setText(msg, ''); toast('Team URL saved'); if (onSaved) onSaved(r);
  } }, 'Save');
  const local = net.bind === '127.0.0.1' || net.bind === 'localhost';
  const help = local && !net.public_url
    ? h('div', { class: 'note warn' }, h('b', {}, 'Only this PC can reach the Brain right now. '), 'For teammates on the same Wi-Fi, restart it with ', h('code', {}, 'python run_kode.py --lan'),
      '. For teammates anywhere, run a tunnel such as ', h('code', {}, 'cloudflared tunnel --url http://localhost:' + net.port), ' and paste its https address here.')
    : h('p', { class: 'faint', style: { fontSize: '12.5px' } }, local ? 'Using your tunnel address.' : 'The Brain is listening on your network. Same Wi-Fi works directly; use a tunnel address for teammates elsewhere.');
  return h('div', { style: { display: 'grid', gap: '8px' } }, h('label', { class: 'f', for: 'team-url' }, 'Teammates reach this Brain at', h('div', { style: { display: 'flex', gap: '8px' } }, input, save), list), msg, help);
}

// Invite a teammate: name + access policy + lifetime -> a link they open on their own laptop.
export async function openInvite() {
  let net = { team_url: '', lan_urls: [], bind: '127.0.0.1', port: '8000' };
  try { net = await hub.call({ t: 'net_info' }); } catch { /* offline */ }
  const name = h('input', { type: 'text', id: 'inv-name', placeholder: 'e.g. Nishitha', maxlength: 60, autocomplete: 'off' });
  const ttl = h('select', { id: 'inv-ttl' }, [['1h', '1 hour'], ['24h', '24 hours'], ['10m', '10 minutes']].map(([v, l]) => h('option', { value: v }, l)));
  const err = h('div', { class: 'field-err', role: 'alert' });
  let access = 'full';
  const opts = Object.entries(ACCESS).map(([k, [t, d]]) => {
    const r = h('input', { type: 'radio', name: 'inv-access', value: k, checked: k === 'full', onchange: () => { access = k; } });
    return h('label', { class: 'opt' }, r, h('span', {}, h('b', {}, t), h('small', {}, d)));
  });
  const urlBox = h('div');
  const paintUrl = () => clear(urlBox).append(teamUrlPicker(net, (r) => { net = r; paintUrl(); }));
  paintUrl();
  const out = h('div', { style: { display: 'none' } });
  const create = h('button', { class: 'btn primary', type: 'button' }, icon('plus', 14), 'Create invite link');
  const body = h('div', { style: { display: 'grid', gap: '16px' } },
    h('p', { class: 'muted' }, 'Your teammate opens the link on their own laptop and runs one command. Their machine only ever connects outward to this Brain.'),
    h('div', { class: 'grid2' }, h('label', { class: 'f', for: 'inv-name' }, 'Teammate name', name, err), h('label', { class: 'f', for: 'inv-ttl' }, 'Link works for', ttl)),
    h('fieldset', { class: 'opts' }, h('legend', {}, 'Your access to their machine'), ...opts),
    urlBox, out);
  const close = modal({ title: 'Invite a teammate', body, wide: true, actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Close'), create] });
  create.addEventListener('click', async () => {
    const member = name.value.trim();
    if (!member) { setText(err, 'Give the teammate a name first.'); name.focus(); return; }
    const base = (net.team_url || '').replace(/\/$/, '');
    if (!base) { setText(err, 'Set the address teammates use to reach this Brain first (below).'); return; }
    setText(err, ''); create.disabled = true;
    try {
      const r = await hub.call({ t: 'create_invite', member, access, ttl: ttl.value });
      const link = `${base}/join/${r.code}`;
      const life = ttl.options[ttl.selectedIndex].text;
      const msg = `Hi ${member}, join our K.O.D.E. team: open ${link} on your laptop and run the one command it shows. The link works once, for ${life}.`;
      out.style.display = 'grid'; out.style.gap = '10px';
      clear(out).append(
        h('div', { class: 'faint', style: { fontSize: '12.5px' } }, 'Invite link'),
        h('pre', { class: 'cmd' }, link),
        h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' } }, copyBtn(() => link), copyBtn(() => msg), chip(`works for ${life}`, 'warn', 'clock'), chip('single use', 'info', 'lock')),
        h('p', { class: 'faint', style: { fontSize: '12.5px' } }, 'The second Copy button copies a ready-to-send message with the link.'),
        h('div', { class: 'note', 'aria-live': 'polite' }, `Waiting for ${member}'s laptop to connect. It appears on the Team page when it does.`));
      create.hidden = true;
      const before = S.nodes.length;
      const iv = setInterval(() => {
        if (!document.body.contains(body)) { clearInterval(iv); return; }
        if (S.nodes.length > before) { clearInterval(iv); toast(`${member} joined the team`); close(); location.hash = '#/team'; }
      }, 700);
    } catch (e) { setText(err, 'Could not create the invite: ' + e.message); create.disabled = false; }
  });
}

export const openAddMachine = openInvite;

// Protocol / provider compatibility (mirrors backend hub/apiprofiles.py; the hub re-checks everything).
export const PROTOCOLS = { anthropic: 'Anthropic Messages', 'openai-chat': 'OpenAI Chat Completions', 'openai-responses': 'OpenAI Responses' };
const SUPPORT = { claude: ['anthropic'], codex: ['openai-responses'], opencode: ['openai-chat', 'openai-responses'] };
const NO_PROFILE = { hermes: 'Hermes reads its endpoint from its own config on that machine', agy: 'Antigravity has no custom endpoint setting', shell: 'A plain shell does not call a model', 'opencode-free': "This variant is pinned to OpenCode's free models" };
export function profileFit(provider, prof) {
  if (NO_PROFILE[provider]) return [false, NO_PROFILE[provider]];
  const ok = SUPPORT[provider];
  if (!ok) return [false, 'This provider cannot take a custom endpoint'];
  if (!ok.includes(prof.protocol)) return [false, `${provider} needs ${ok.map((p) => PROTOCOLS[p]).join(' or ')}; this profile speaks ${PROTOCOLS[prof.protocol]}`];
  return [true, ''];
}

export function openProfile(existing) {
  const p = existing || { name: '', base_url: '', protocol: 'openai-chat', models: [], default_model: '', price_in: 0, price_out: 0, context_window: 0 };
  const f = (id, label, el, help) => h('label', { class: 'f', for: id }, label, help ? h('small', {}, help) : null, el);
  const name = h('input', { id: 'pf-name', type: 'text', value: p.name, placeholder: 'e.g. Company gateway', maxlength: 60 });
  const base = h('input', { id: 'pf-base', type: 'url', value: p.base_url, placeholder: 'https://gateway.example.com/v1', autocomplete: 'off', spellcheck: 'false' });
  const proto = h('select', { id: 'pf-proto' }, Object.entries(PROTOCOLS).map(([k, v]) => h('option', { value: k, selected: k === p.protocol }, v)));
  const key = h('input', { id: 'pf-key', type: 'password', autocomplete: 'new-password', placeholder: existing && p.has_key ? `Saved (ends in ${p.key_hint}). Leave empty to keep it.` : 'Paste the API key', spellcheck: 'false' });
  const models = h('input', { id: 'pf-models', type: 'text', value: (p.models || []).join(', '), placeholder: 'model-a, model-b', spellcheck: 'false' });
  const dm = h('input', { id: 'pf-dm', type: 'text', value: p.default_model || '', placeholder: 'defaults to the first model', spellcheck: 'false' });
  const pin = h('input', { id: 'pf-pin', type: 'number', min: 0, step: 'any', value: p.price_in || '', placeholder: '0' });
  const pout = h('input', { id: 'pf-pout', type: 'number', min: 0, step: 'any', value: p.price_out || '', placeholder: '0' });
  const ctx = h('input', { id: 'pf-ctx', type: 'number', min: 0, step: 1000, value: p.context_window || '', placeholder: 'e.g. 200000' });
  const err = h('div', { class: 'field-err', role: 'alert' });
  const save = h('button', { class: 'btn primary', type: 'button' }, 'Save profile');
  const body = h('div', { style: { display: 'grid', gap: '14px' } },
    h('div', { class: 'grid2' }, f('pf-name', 'Name', name), f('pf-proto', 'Protocol', proto)),
    f('pf-base', 'Base URL', base, 'The endpoint root. The key is encrypted at rest and never shown again.'),
    f('pf-key', 'API key', key),
    h('div', { class: 'grid2' }, f('pf-models', 'Models', models, 'Comma separated'), f('pf-dm', 'Default model', dm)),
    h('div', { class: 'grid3' }, f('pf-pin', 'Input $ per 1M tokens', pin), f('pf-pout', 'Output $ per 1M tokens', pout), f('pf-ctx', 'Context window', ctx)),
    h('p', { class: 'faint', style: { fontSize: '12.5px' } }, 'Prices and context window are optional. Cost is an estimate from the prices you enter here; empty means unknown.'), err);
  const close = modal({ title: existing ? 'Edit API profile' : 'New API profile', body, wide: true, actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), save] });
  save.addEventListener('click', async () => {
    setText(err, ''); save.disabled = true;
    const list = models.value.split(',').map((x) => x.trim()).filter(Boolean);
    try {
      const r = await hub.call({ t: 'save_profile', id: existing ? existing.id : '', profile: { name: name.value, base_url: base.value, protocol: proto.value, api_key: key.value, models: list, default_model: dm.value.trim() || list[0] || '', price_in: Number(pin.value) || 0, price_out: Number(pout.value) || 0, context_window: Number(ctx.value) || 0 } });
      if (!r.ok) throw new Error(r.err || 'could not save');
      S.profiles = r.profiles || S.profiles; emit(); close(); toast('Profile saved');
    } catch (e) { setText(err, e.message); save.disabled = false; }
  });
}

export function openNewSession(preNodeId = '') {
  const nodes = isLead() ? S.nodes.filter((n) => n.online) : S.nodes.filter((n) => n.online && n.id === S.nodeId);
  if (!nodes.length) { toast('No machine is online. Add one first.', 'bad'); return; }
  const mSel = h('select', { id: 'ns-m', 'aria-label': 'Machine' }, nodes.map((n) => h('option', { value: n.id }, n.name)));
  const pSel = h('select', { id: 'ns-p', 'aria-label': 'Provider' });
  const cwd = h('input', { type: 'text', id: 'ns-c', placeholder: 'Leave empty for the machine\'s workspace root', 'aria-label': 'Working folder' });
  const note = h('div', { class: 'note' });
  const prSel = h('select', { id: 'ns-pr', 'aria-label': 'API profile' });
  const mdSel = h('input', { type: 'text', id: 'ns-md', list: 'ns-md-list', placeholder: 'Profile default', spellcheck: 'false' });
  const mdList = h('datalist', { id: 'ns-md-list' });
  const prNote = h('div', { class: 'faint', style: { fontSize: '12.5px' } });
  const mdWrap = h('label', { class: 'f' }, 'Model', mdSel, mdList);
  const profBox = h('div', { style: { display: 'grid', gap: '10px' } },
    h('label', { class: 'f' }, 'Model source', h('small', {}, "Use the provider's own login, or route this session through one of your API profiles."), prSel), mdWrap, prNote);
  if (preNodeId && nodes.some((n) => n.id === preNodeId)) mSel.value = preNodeId;
  const refresh = () => {
    const n = nodeById(mSel.value);
    clear(pSel).append(...(n.providers || []).map((p) => h('option', { value: p.id, disabled: !p.available }, p.label + (p.available ? (p.signed_in === false ? ' (sign in needed)' : '') : ' (not installed)'))));
    const firstOk = (n.providers || []).find((p) => p.available);
    if (firstOk) pSel.value = firstOk.id;
    paintProfiles();
    clear(note);
    if (isLead() && !n.allow_remote_start) { note.className = 'note warn'; note.append(h('b', {}, 'Needs approval. '), `${n.name}'s owner will be asked before this starts.`); }
    else { note.className = 'note'; note.append(isLead() ? `${n.name} allows the lead to start sessions.` : 'The session runs on this machine inside its workspace folder.'); }
  };
  const paintProfiles = () => {
    const keep = prSel.value;
    clear(prSel).append(h('option', { value: '' }, "Provider's own login (default)"),
      ...S.profiles.map((pr) => h('option', { value: pr.id, disabled: !profileFit(pSel.value, pr)[0] }, pr.name + (profileFit(pSel.value, pr)[0] ? '' : ' (not compatible)'))));
    const kept = Array.from(prSel.options).find((o) => o.value === keep && !o.disabled);
    prSel.value = kept ? keep : '';
    const pr = S.profiles.find((x) => x.id === prSel.value);
    mdWrap.style.display = pr ? '' : 'none';
    clear(mdList).append(...((pr && pr.models) || []).map((m) => h('option', { value: m })));
    clear(prNote);
    if (pr) prNote.append(`Context window ${pr.context_window ? pr.context_window.toLocaleString() + ' tokens' : 'unknown'}. Endpoint: ${pr.base_url}`);
    else if (!S.profiles.length) prNote.append('No API profiles yet. Add one on the Models page to use a custom endpoint.');
    else { const bad = S.profiles.find((x) => !profileFit(pSel.value, x)[0]); if (bad) prNote.append(profileFit(pSel.value, bad)[1] + '.'); }
  };
  pSel.addEventListener('change', paintProfiles); prSel.addEventListener('change', paintProfiles);
  mSel.addEventListener('change', refresh); refresh();
  const go = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
    go.disabled = true;
    try {
      const r = await hub.call({ t: 'start', node: mSel.value, provider: pSel.value, cwd: cwd.value.trim(), profile_id: prSel.value, model: prSel.value ? mdSel.value.trim() : '' });
      if (!r.ok) throw new Error(r.err || 'could not start');
      close();
      if (r.applied === false) toast('Request sent. Waiting for the machine\'s owner to approve.');
      else { toast('Session starting…'); if (r.sid) location.hash = '#/session/' + r.sid; }
    } catch (e) { toast(e.message, 'bad'); go.disabled = false; }
  } }, icon('play', 14), 'Start session');
  const body = h('div', { style: { display: 'grid', gap: '14px' } },
    h('label', { class: 'f' }, 'Machine', mSel), h('label', { class: 'f' }, 'Provider', pSel), isLead() ? profBox : null,
    h('label', { class: 'f' }, 'Working folder', h('small', {}, 'Relative to the machine\'s workspace. It cannot leave it.'), cwd), note);
  const close = modal({ title: 'New session', body, actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), go] });
}
