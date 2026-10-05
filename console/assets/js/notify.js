// Notifications: a toast in the console, a desktop notification when the tab is in the background, a short sound, and the tab title badge.
// Everything here is derived from the project snapshots the Brain already pushes, plus the Brain's own "notice" messages (agent switches, failovers).
import { S, subscribe } from './store.js';
import { toast } from './components.js';

const KEY = 'kode.notify';
export const prefs = () => { try { return { desktop: true, sound: true, ...(JSON.parse(localStorage.getItem(KEY) || '{}')) }; } catch { return { desktop: true, sound: true }; } };
export const setPrefs = (p) => localStorage.setItem(KEY, JSON.stringify({ ...prefs(), ...p }));
export const permission = () => (typeof Notification === 'undefined' ? 'unsupported' : Notification.permission);
export async function askPermission() { if (typeof Notification === 'undefined') return 'unsupported'; try { return await Notification.requestPermission(); } catch { return Notification.permission; } }

let seeded = false, unseen = 0, audio = null;
const prev = new Map();
const seenNotices = new Set();
const baseTitle = document.title;

function beep() {
  if (!prefs().sound) return;
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    const o = audio.createOscillator(), g = audio.createGain();
    o.type = 'sine'; o.frequency.value = 660; g.gain.value = 0.0001;
    o.connect(g); g.connect(audio.destination);
    const t = audio.currentTime;
    g.gain.exponentialRampToValueAtTime(0.12, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);
    o.frequency.setValueAtTime(660, t); o.frequency.setValueAtTime(880, t + 0.12);
    o.start(t); o.stop(t + 0.3);
  } catch { /* no audio available */ }
}

export function fire(title, body, kind = '', url = '') {
  toast(`${title}: ${body}`, kind === 'bad' ? 'bad' : '');
  beep();
  if (document.hidden) {
    unseen++; document.title = `(${unseen}) ${baseTitle}`;
    if (prefs().desktop && permission() === 'granted') {
      try { const n = new Notification(title, { body, tag: title + body, silent: true }); n.onclick = () => { window.focus(); if (url) location.hash = url; n.close(); }; } catch { /* blocked */ }
    }
  }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) { unseen = 0; document.title = baseTitle; } });

const STATUS_MSG = {
  questions: ['The planner has questions for you', 'warn'], plan_ready: ['The plan is ready for your approval', 'warn'], review: ['Verify, polish and judge are done: your review is needed', 'warn'],
  done: ['The project is finished', 'ok'], failed: ['The project stopped with an error', 'bad'], paused: ['The project is paused', ''],
};

function onState() {
  if (!S.loaded) return;
  for (const p of S.projects || []) {
    const old = prev.get(p.id);
    const cards = new Set((p.cards || []).map((c) => c.id));
    if (seeded && old) {
      if (old.status !== p.status && STATUS_MSG[p.status]) fire(p.name, STATUS_MSG[p.status][0], STATUS_MSG[p.status][1], '#/projects/' + p.id);
      for (const c of p.cards || []) if (!old.cards.has(c.id)) fire(p.name, c.kind === 'prompt' ? 'An agent is waiting for you' : c.kind === 'stalled' ? 'A task looks stuck: ' + c.text.slice(0, 90) : c.text.slice(0, 120), c.kind === 'blocked' ? 'bad' : '', '#/projects/' + p.id);
    }
    prev.set(p.id, { status: p.status, cards });
  }
  seeded = true;
}

export function onNotices(items) {
  for (const n of items || []) {
    if (seenNotices.has(n.id)) continue;
    seenNotices.add(n.id);
    if (seeded && Date.now() - n.ts < 60000) fire('K.O.D.E.', n.text, n.level === 'warn' || n.level === 'bad' ? 'bad' : '', n.project ? '#/projects/' + n.project : '');
  }
}

export function init() { subscribe(onState); }
