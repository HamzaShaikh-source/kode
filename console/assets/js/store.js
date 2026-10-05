// Single source of truth for the console. The hub pushes full snapshots; views read S and patch their DOM.
export const S = {
  role: 'lead', key: '', hubHost: '', nodeId: '',
  connected: false, everConnected: false, authFailed: false, loaded: false,
  nodes: [], sessions: [], requests: [], events: [], modes: ['watch', 'suggest', 'copilot', 'takeover'],
  suggestions: [], orch: null, projects: [], presets: [], memory: { hackathons: [], agents: {} }, profiles: [], profilesLoaded: false, route: { name: 'brain', arg: '' }, usage: null,
};
const subs = new Set();
let queued = false;
export const subscribe = (fn) => { subs.add(fn); return () => subs.delete(fn); };
export function emit() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => { queued = false; subs.forEach((f) => { try { f(S); } catch (e) { console.error(e); } }); });
}
export function applyState(m) {
  S.nodes = m.nodes || []; S.sessions = m.sessions || []; S.requests = m.requests || []; S.events = m.events || [];
  S.loaded = true;
  S.hubHost = m.hub_host || ''; S.nodeId = m.node_id || ''; S.role = m.role || S.role; S.modes = m.modes || S.modes;
  emit();
}
export const sessionById = (sid) => S.sessions.find((s) => s.sid === sid);
export const nodeById = (id) => S.nodes.find((n) => n.id === id);
export const sessionsOf = (nodeId) => S.sessions.filter((s) => s.node_id === nodeId);
export const isLead = () => S.role === 'lead';
export const liveSessions = () => S.sessions.filter((s) => s.state === 'live');
export const workingSessions = () => S.sessions.filter((s) => s.activity === 'working');
