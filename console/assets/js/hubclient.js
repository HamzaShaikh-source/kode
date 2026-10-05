// WebSocket client for /hub/viewer: auto-reconnect, request/response calls, per-session subscriptions.
import { S, applyState, emit } from './store.js';
import * as pool from './termpool.js';
import { toast } from './components.js';

let ws = null, retry = 800, n = 0, lastPong = 0, beat = null;
const refs = new Map();
const subbed = new Set();

export const send = (o) => { if (ws && ws.readyState === 1) { ws.send(JSON.stringify(o)); return true; } return false; };
export function call(o, timeout = 15000) {
  return new Promise((res, rej) => {
    const ref = 'c' + (++n);
    refs.set(ref, res);
    if (!send({ ...o, ref })) { refs.delete(ref); rej(new Error('Not connected to the Brain')); return; }
    setTimeout(() => { if (refs.delete(ref)) rej(new Error('The Brain did not answer in time')); }, timeout);
  });
}

function syncSubs() {
  const want = new Set(S.sessions.filter((s) => s.state !== 'offline' || true).map((s) => s.sid));
  for (const sid of want) if (!subbed.has(sid)) { subbed.add(sid); send({ t: 'sub', sid }); }
  for (const sid of Array.from(subbed)) if (!want.has(sid)) { subbed.delete(sid); pool.dispose(sid); send({ t: 'unsub', sid }); }
}

export function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/hub/viewer?role=${S.role}&key=${encodeURIComponent(S.key)}`);
  ws.onopen = () => { lastPong = Date.now(); clearInterval(beat); beat = setInterval(() => {
      // A sleeping laptop or a dropped Wi-Fi can leave the socket "open" but dead: ping, and recycle the connection if nothing comes back.
      if (!ws || ws.readyState !== 1) return;
      if (Date.now() - lastPong > 40000) { try { ws.close(); } catch { /* already closing */ } return; }
      send({ t: 'ping', n: ++n });
    }, 12000);
    S.connected = true; S.everConnected = true; S.authFailed = false; retry = 800; subbed.clear(); emit(); if (S.role === 'lead') loadProfiles(); };
  ws.onclose = (e) => {
    clearInterval(beat);
    S.connected = false; emit();
    if (e.code === 4401 || e.code === 4403) { S.authFailed = true; emit(); return; }
    setTimeout(connect, retry); retry = Math.min(retry * 1.6, 8000);
  };
  ws.onmessage = (e) => {
    lastPong = Date.now();
    let m; try { m = JSON.parse(e.data); } catch { return; }
    switch (m.t) {
      case 'state': applyState(m); syncSubs(); break;
      case 'out': pool.feed(m.sid, m.d); break;
      case 'replay': pool.replay(m.sid, m.d, !!m.responder); break;
      case 'responder': pool.setResponder(m.sid, !!m.on); break;
      case 'exit': pool.markExit(m.sid); break;
      case 'denied': toast(m.why || 'That action is not allowed right now', 'bad'); break;
      case 'suggestion': S.suggestions.unshift({ ...m, ts: Date.now() }); toast(`Lead suggests for ${m.label}: ${m.text}`); emit(); break;
      case 'pong': lastPong = Date.now(); break;
      case 'orch': S.orch = m; emit(); break;
      case 'projects': S.projects = m.projects || []; emit(); break;
      case 'result': { const r = refs.get(m.ref); if (r) { refs.delete(m.ref); r(m); } break; }
      default: break;
    }
  };
}

export async function loadProfiles() {
  try { const r = await call({ t: 'list_profiles' }); if (r.ok) { S.profiles = r.profiles || []; S.profilesLoaded = true; emit(); } } catch { /* offline */ }
}
