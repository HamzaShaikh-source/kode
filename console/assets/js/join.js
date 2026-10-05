// Join page: shows the one-line installer for this invite and waits until the laptop has paired. No secrets ever reach this page.
const code = decodeURIComponent(location.pathname.split('/')[2] || '');
const $ = (s) => document.querySelector(s);
const enc = encodeURIComponent(code);
let os = /Windows/i.test(navigator.userAgent) ? 'win' : /Mac/i.test(navigator.userAgent) ? 'mac' : 'linux';
let brain = location.origin;

const WIN_FILE = 'powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\\Downloads\\kode-join.ps1"';
function command() {
  if (os === 'win') return WIN_FILE;
  return `curl -fsSL ${brain}/join/${enc}/install.sh | sh`;
}
function paint() {
  for (const b of document.querySelectorAll('[data-os]')) b.setAttribute('aria-selected', String(b.dataset.os === os));
  $('#cmd').textContent = command();
  $('#cmd-hint').innerHTML = '';
  $('#cmd-hint').append(...(os === 'win' ? ['First press ', bold('Download installer'), ', then open ', bold('PowerShell'), ' and paste:'] : ['Open ', bold('Terminal'), ' and paste:']));
  $('#view').href = `/join/${enc}/install.${os === 'win' ? 'ps1' : 'sh'}`;
  $('#dl').hidden = os !== 'win';
  $('#dl').href = `/join/${enc}/install.ps1?dl=1`;
  $('#win-note').hidden = os !== 'win';
}
const bold = (t) => { const b = document.createElement('b'); b.textContent = t; return b; };

async function check(first) {
  let r;
  try { r = await fetch(`/join/api/${enc}`, { cache: 'no-store' }); } catch { return; }
  if (r.status === 429) { $('#join-sub').textContent = 'Too many attempts from this network. Wait a few minutes and reload.'; return; }
  if (!r.ok) { $('#join-title').textContent = 'This invite link is not valid'; $('#join-sub').textContent = 'Check that you copied the whole link, or ask your team lead for a new one.'; return; }
  const j = await r.json();
  brain = j.brain || location.origin;
  if (first) {
    $('#join-title').textContent = j.member ? `Welcome, ${j.member}` : 'Join your team';
    $('#join-body').hidden = false;
    paint();
  }
  const st = $('#status');
  if (j.status === 'valid') {
    $('#join-sub').textContent = `Your lead invited you to their K.O.D.E. team. This invite works once and expires in ${Math.max(1, Math.round(j.expires_in / 60))} minutes.`;
    st.className = 'note'; st.textContent = 'Waiting for your laptop to connect...';
  } else if (j.status === 'used') {
    $('#join-sub').textContent = 'Your laptop is connected to the team.';
    st.className = 'note ok'; st.textContent = 'Connected. Your lead can now see your machine. Your member console link is printed in the agent window. Next time just run start.ps1 (Windows) or ~/kode-node/start.sh.';
    for (const el of document.querySelectorAll('.jsteps, .jrow')) el.style.opacity = '.55';
    return 'done';
  } else {
    $('#join-sub').textContent = 'This invite has expired. Ask your team lead for a new link.';
    st.className = 'note bad'; st.textContent = 'Expired invite.';
    return 'done';
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  for (const b of document.querySelectorAll('[data-os]')) b.addEventListener('click', () => { os = b.dataset.os; paint(); });
  $('#copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(command()); $('#copy').textContent = 'Copied'; setTimeout(() => { $('#copy').textContent = 'Copy command'; }, 1600); }
    catch { $('#copy').textContent = 'Select the text and copy it'; }
  });
  if ((await check(true)) === 'done') return;
  const iv = setInterval(async () => { if ((await check(false)) === 'done') clearInterval(iv); }, 3000);
});
