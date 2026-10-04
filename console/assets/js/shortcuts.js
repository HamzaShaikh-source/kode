// Keyboard shortcuts + the "?" help overlay. Ignored while typing in a field or inside a terminal.
import { h } from './util.js';
import { modal } from './components.js';
import { openNewSession, openAddMachine } from './dialogs.js';
import { isLead } from './store.js';

const LEAD_G = { b: 'brain', s: 'sessions', m: 'machines', u: 'usage', a: 'activity', ',': 'settings' };
const MEMBER_G = { h: 'home', s: 'sessions', a: 'activity', p: 'settings' };

export function showHelp() {
  const lead = isLead();
  const rows = [['?', 'Show this help'], ['n', 'New session'], lead ? ['a', 'Add a machine'] : null, ['/', 'Focus the filter box'],
    ['g then ' + (lead ? 'b' : 'h'), lead ? 'Go to the Brain' : 'Go to My machine'], ['g then s', 'Go to Sessions'], lead ? ['g then m', 'Go to Machines'] : null, lead ? ['g then u', 'Go to Usage'] : null,
    ['g then a', 'Go to Activity'], [lead ? 'g then ,' : 'g then p', lead ? 'Go to Settings' : 'Go to Privacy'], ['Esc', 'Close a dialog']].filter(Boolean);
  const close = modal({ title: 'Keyboard shortcuts', body: h('table', { class: 'table', style: { fontSize: '14px' } }, h('tbody', {}, rows.map(([k, d]) => h('tr', {}, h('td', { style: { width: '150px' } }, h('kbd', {}, k)), h('td', {}, d))))),
    actions: [h('button', { class: 'btn primary', type: 'button', onclick: () => close() }, 'Got it')] });
}

export function init() {
  let gAt = 0;
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
    const t = e.target;
    if (t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable || (t.closest && t.closest('.xterm')))) return;
    if (document.querySelector('.modal-back')) return;
    const lead = isLead(), k = e.key;
    if (gAt && Date.now() - gAt < 1200) {
      gAt = 0;
      const dest = (lead ? LEAD_G : MEMBER_G)[k];
      if (dest) { e.preventDefault(); location.hash = '#/' + dest; }
      return;
    }
    if (k === 'g') { gAt = Date.now(); return; }
    if (k === '?') { e.preventDefault(); showHelp(); }
    else if (k === 'n') { e.preventDefault(); openNewSession(); }
    else if (k === 'a' && lead) { e.preventDefault(); openAddMachine(); }
    else if (k === '/') { const i = document.querySelector('.filters input'); if (i) { e.preventDefault(); i.focus(); } }
  });
}
