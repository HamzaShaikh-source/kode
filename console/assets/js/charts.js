// Small SVG charts for the project pages. No libraries: every chart is a few <rect>s, themed with the console's CSS variables.
import { h } from './util.js';

const NS = 'http://www.w3.org/2000/svg';
export function svg(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v);
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}

const MEMBER_COLORS = ['var(--accent)', 'var(--info)', 'var(--ok)', '#a855f7', '#0ea5a4', '#d946ef', '#eab308', '#64748b'];
export const colorOf = (i) => MEMBER_COLORS[i % MEMBER_COLORS.length];

export const dur = (sec) => {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ' + String(sec % 60).padStart(2, '0') + 's';
  return Math.floor(sec / 3600) + 'h ' + String(Math.floor((sec % 3600) / 60)).padStart(2, '0') + 'm';
};

/** Commit heatmap, one row per person, one column per time bucket (GitHub-style). members = [{member, times:[epoch s]}]. */
export function heatmap(members, start, end, buckets = 48) {
  if (!members.length) return null;
  const span = Math.max(60, end - start);
  const step = span / buckets;
  const rows = members.map((m) => {
    const c = new Array(buckets).fill(0);
    for (const t of m.times || []) { const i = Math.min(buckets - 1, Math.max(0, Math.floor((t - start) / step))); c[i]++; }
    return c;
  });
  const max = Math.max(1, ...rows.flat());
  const cw = 14, gap = 3, lw = 104, rh = 18;
  const W = lw + buckets * (cw + gap), H = members.length * (rh + gap) + 22;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'hm', role: 'img', 'aria-label': 'Commits over time per person' });
  members.forEach((m, r) => {
    const y = r * (rh + gap);
    root.append(svg('text', { x: 0, y: y + 13, class: 'hm-l' }, m.member.length > 14 ? m.member.slice(0, 13) + '…' : m.member));
    rows[r].forEach((n, i) => {
      const rect = svg('rect', { x: lw + i * (cw + gap), y, width: cw, height: rh, rx: 3, class: 'hm-c', 'data-l': n ? String(Math.min(4, Math.ceil((n / max) * 4))) : '0' },
        svg('title', {}, `${m.member}: ${n} commit${n === 1 ? '' : 's'}, ${new Date((start + i * step) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`));
      root.append(rect);
    });
  });
  const fmt = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  root.append(svg('text', { x: lw, y: H - 4, class: 'hm-a' }, fmt(start)), svg('text', { x: W, y: H - 4, class: 'hm-a', 'text-anchor': 'end' }, fmt(end)));
  return root;
}

/** Horizontal bars on a shared time axis: rows = [{label, sub, start, end, color, running}] (epoch seconds). */
export function gantt(rows, start, end) {
  if (!rows.length) return null;
  const span = Math.max(60, end - start);
  const lw = 190, bw = 560, rh = 20, gap = 6, W = lw + bw + 70, H = rows.length * (rh + gap) + 24;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'gantt', role: 'img', 'aria-label': 'When each task ran' });
  for (let i = 0; i <= 4; i++) {
    const x = lw + (bw * i) / 4;
    root.append(svg('line', { x1: x, x2: x, y1: 0, y2: H - 18, class: 'g-grid' }), svg('text', { x, y: H - 4, class: 'hm-a', 'text-anchor': 'middle' }, dur((span * i) / 4)));
  }
  rows.forEach((r, i) => {
    const y = i * (rh + gap);
    const x1 = lw + ((Math.max(start, r.start) - start) / span) * bw;
    const x2 = lw + ((Math.min(end, r.end || end) - start) / span) * bw;
    root.append(svg('text', { x: 0, y: y + 14, class: 'g-l' }, r.label.length > 30 ? r.label.slice(0, 29) + '…' : r.label),
      svg('rect', { x: x1, y, width: Math.max(4, x2 - x1), height: rh, rx: 5, fill: r.color, class: 'g-b' + (r.running ? ' run' : ''), opacity: r.skipped ? 0.35 : 1 }, svg('title', {}, `${r.label}  ${dur((r.end || end) - r.start)}${r.sub ? '  ' + r.sub : ''}`)),
      svg('text', { x: x2 + 6, y: y + 14, class: 'g-t' }, dur((r.end || end) - r.start)));
  });
  return root;
}

/** A thin stacked bar, e.g. time per phase. parts = [{label, value, color}] */
export function stack(parts, total) {
  const sum = total || parts.reduce((a, p) => a + p.value, 0) || 1;
  return h('div', { class: 'stack' }, parts.filter((p) => p.value > 0).map((p) => h('i', { style: { width: Math.max(1.5, (p.value / sum) * 100) + '%', background: p.color }, title: `${p.label}: ${dur(p.value)}` })));
}

/** Tiny line of numbers, for score history. */
export function spark(values, w = 120, hgt = 28) {
  if (!values || values.length < 2) return null;
  const mx = Math.max(...values), mn = Math.min(...values), r = Math.max(1, mx - mn);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${hgt - 3 - ((v - mn) / r) * (hgt - 6)}`).join(' ');
  return svg('svg', { viewBox: `0 0 ${w} ${hgt}`, width: w, height: hgt, class: 'spark' }, svg('polyline', { points: pts, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
}

/** Radial gauge 0-100 for a score. */
export function gauge(value, label) {
  const r = 38, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, value));
  return h('div', { class: 'gauge' },
    svg('svg', { viewBox: '0 0 100 100', width: 112, height: 112 },
      svg('circle', { cx: 50, cy: 50, r, class: 'gauge-bg' }),
      svg('circle', { cx: 50, cy: 50, r, class: 'gauge-fg', 'stroke-dasharray': `${(v / 100) * c} ${c}`, transform: 'rotate(-90 50 50)' }),
      svg('text', { x: 50, y: 54, 'text-anchor': 'middle', class: 'gauge-n' }, String(Math.round(v)))),
    h('small', { class: 'faint' }, label || ''));
}
