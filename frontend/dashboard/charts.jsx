/* global React */
/* Pure-SVG charts. Minimal, single-accent, hairline aesthetic.
   Every chart returns a self-contained SVG sized via viewBox so it
   scales with its container. */

const { useState: useStateChart } = React;

/* ─── helpers ──────────────────────────────────────────────────────── */
const fmtChart = (n) => n == null ? "—" : new Intl.NumberFormat("en-US").format(n);

/* ─── Sparkline (multi-series area + line) ─────────────────────────── */
function Sparkline({ series, labels, height = 80, accent }) {
  // series: [{ label, color, values: number[] }]
  const W = 600, H = height, P = 4;
  const n = series[0].values.length;
  const max = Math.max(...series.flatMap((s) => s.values), 1);
  const xStep = (W - P * 2) / (n - 1);
  const y = (v) => H - P - (v / max) * (H - P * 2);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="chart-svg">
      {/* grid */}
      <line x1={P} y1={H/2} x2={W-P} y2={H/2} stroke="var(--line)" strokeDasharray="2 3" strokeWidth="0.5" />
      {series.map((s, si) => {
        const pts = s.values.map((v, i) => `${P + i * xStep},${y(v)}`).join(" ");
        const area = `M ${P},${H-P} L ${pts.split(" ").join(" L ")} L ${W-P},${H-P} Z`;
        const color = s.color || accent || "var(--accent)";
        return (
          <g key={si}>
            <path d={area} fill={color} opacity="0.08" />
            <polyline points={pts} fill="none" stroke={color} strokeWidth="1.4" />
          </g>
        );
      })}
    </svg>
  );
}

/* ─── Vertical bar histogram ───────────────────────────────────────── */
function Histogram({ data, height = 140, accentBin }) {
  const W = 600, H = height;
  const padL = 28, padB = 18, padT = 8, padR = 8;
  const max = Math.max(...data.map((d) => d.count), 1);
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const bw = innerW / data.length;
  const niceMax = Math.ceil(max / 100) * 100;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart-svg" preserveAspectRatio="none">
      {/* y axis ticks */}
      {[0, 0.5, 1].map((t) => (
        <g key={t}>
          <line x1={padL} y1={padT + (1 - t) * innerH} x2={W - padR} y2={padT + (1 - t) * innerH} stroke="var(--line)" strokeWidth="0.5" />
          <text x={padL - 4} y={padT + (1 - t) * innerH + 3} textAnchor="end" className="chart-tick">{fmtChart(Math.round(niceMax * t))}</text>
        </g>
      ))}
      {/* bars */}
      {data.map((d, i) => {
        const h = (d.count / niceMax) * innerH;
        const x = padL + i * bw + 2;
        const y = padT + innerH - h;
        const isAccent = accentBin && accentBin(d, i);
        return (
          <g key={d.bin}>
            <rect
              x={x} y={y}
              width={bw - 4} height={h}
              fill={isAccent ? "var(--accent)" : "var(--ink-50)"}
              opacity={isAccent ? 1 : 0.55}
            >
              <title>{`${d.bin}: ${fmtChart(d.count)}`}</title>
            </rect>
            <text x={x + (bw - 4) / 2} y={H - 4} textAnchor="middle" className="chart-tick">{d.bin}</text>
          </g>
        );
      })}
    </svg>
  );
}

/* ─── Donut chart ──────────────────────────────────────────────────── */
function Donut({ data, size = 180, thickness = 22, accentIndex }) {
  // data: [{label, count, color}]
  const total = data.reduce((s, d) => s + d.count, 0);
  const cx = size / 2, cy = size / 2;
  const r = (size - thickness) / 2;
  let angle = -Math.PI / 2;
  const polar = (a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];

  const palette = [
    "var(--accent)",
    "oklch(0.55 0.13 245)",
    "oklch(0.55 0.10 270)",
    "oklch(0.72 0.14 85)",
    "oklch(0.55 0.04 260)",
    "oklch(0.55 0.13 25)",
    "oklch(0.6 0.10 320)",
    "oklch(0.65 0.10 180)",
  ];

  return (
    <div className="donut-wrap">
      <svg viewBox={`0 0 ${size} ${size}`} className="chart-svg donut-svg">
        {data.map((d, i) => {
          const frac = d.count / total;
          const a2 = angle + frac * Math.PI * 2;
          const large = frac > 0.5 ? 1 : 0;
          const [x1, y1] = polar(angle);
          const [x2, y2] = polar(a2);
          const path = `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
          const color = d.color || palette[i % palette.length];
          const seg = (
            <path
              key={d.label}
              d={path}
              fill="none"
              stroke={color}
              strokeWidth={thickness}
              opacity={accentIndex == null || accentIndex === i ? 1 : 0.35}
            >
              <title>{`${d.label}: ${d.count} (${(frac * 100).toFixed(1)}%)`}</title>
            </path>
          );
          angle = a2;
          return seg;
        })}
        {/* center label */}
        <text x={cx} y={cy - 3} textAnchor="middle" className="donut-center-num">{fmtChart(total)}</text>
        <text x={cx} y={cy + 14} textAnchor="middle" className="donut-center-lbl">total</text>
      </svg>
      <ul className="donut-legend">
        {data.map((d, i) => {
          const color = d.color || palette[i % palette.length];
          const pct = ((d.count / total) * 100).toFixed(1);
          return (
            <li key={d.label} className="donut-legend-row">
              <span className="donut-legend-swatch" style={{ background: color }} />
              <span className="donut-legend-label">{d.label.replace(/_/g, " ")}</span>
              <span className="donut-legend-val mono">{d.count}</span>
              <span className="donut-legend-pct mono">{pct}%</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ─── Funnel chart (horizontal, descending stages) ─────────────────── */
function Funnel({ stages }) {
  // stages: [{label, value, color?}]
  const max = stages[0].value;
  return (
    <div className="funnel">
      {stages.map((s, i) => {
        const w = (s.value / max) * 100;
        const drop = i > 0 ? (1 - s.value / stages[i - 1].value) * 100 : null;
        const color = s.color || "var(--accent)";
        return (
          <div key={s.label} className="funnel-row">
            <div className="funnel-label">{s.label}</div>
            <div className="funnel-bar-wrap">
              <div className="funnel-bar" style={{ width: w + "%", background: color }}>
                <span className="funnel-bar-num mono">{fmtChart(s.value)}</span>
              </div>
            </div>
            <div className="funnel-meta mono">
              {drop != null && (
                <span className={"funnel-drop" + (drop > 50 ? " funnel-drop-big" : "")}>
                  −{drop.toFixed(1)}%
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ─── Heatmap (7 days × 24 hours) ──────────────────────────────────── */
function Heatmap({ grid, days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"] }) {
  const max = Math.max(...grid.flat(), 1);
  return (
    <div className="heatmap">
      <div className="heatmap-corner" />
      {Array.from({length: 24}, (_, h) => (
        <div key={"h"+h} className="heatmap-hcol-label">{h % 6 === 0 ? String(h).padStart(2,"0") : ""}</div>
      ))}
      {grid.map((row, di) => (
        <React.Fragment key={di}>
          <div className="heatmap-row-label">{days[di]}</div>
          {row.map((v, hi) => {
            const t = v / max;
            return (
              <div
                key={hi}
                className="heatmap-cell"
                style={{ background: `color-mix(in oklch, var(--accent) ${Math.round(t*100)}%, var(--bg-sunk))` }}
                title={`${days[di]} ${String(hi).padStart(2,"0")}:00 · ${v} cycles`}
              />
            );
          })}
        </React.Fragment>
      ))}
    </div>
  );
}

/* ─── Radial gauge ─────────────────────────────────────────────────── */
function Gauge({ value, max, label, sub, ok = true }) {
  const size = 90;
  const r = 36;
  const cx = size / 2, cy = size / 2;
  const start = -Math.PI * 0.75;
  const end   =  Math.PI * 0.75;
  const span  = end - start;
  const t = Math.min(1, value / max);
  const arc = (a1, a2) => {
    const [x1, y1] = [cx + r*Math.cos(a1), cy + r*Math.sin(a1)];
    const [x2, y2] = [cx + r*Math.cos(a2), cy + r*Math.sin(a2)];
    const large = (a2 - a1) > Math.PI ? 1 : 0;
    return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
  };
  const color = ok ? "var(--accent)" : "oklch(0.62 0.14 25)";
  return (
    <div className="gauge">
      <svg viewBox={`0 0 ${size} ${size}`} className="gauge-svg">
        <path d={arc(start, end)} fill="none" stroke="var(--bg-sunk)" strokeWidth="6" strokeLinecap="round" />
        <path d={arc(start, start + t * span)} fill="none" stroke={color} strokeWidth="6" strokeLinecap="round" />
        <text x={cx} y={cy + 2} textAnchor="middle" className="gauge-num">{value}</text>
        <text x={cx} y={cy + 14} textAnchor="middle" className="gauge-sub">{sub}</text>
      </svg>
      <div className="gauge-label">{label}</div>
    </div>
  );
}

/* ─── Stacked horizontal bar (per-chain source split) ──────────────── */
function StackedBar({ groups, keys, palette }) {
  // groups: [{label, values: {[key]: count}}]
  const totals = groups.map((g) => keys.reduce((s, k) => s + (g.values[k] || 0), 0));
  const maxTotal = Math.max(...totals, 1);
  const fmtK = (n) => n >= 1000 ? (n/1000).toFixed(1) + "k" : n;
  return (
    <div className="stacked">
      {groups.map((g, gi) => {
        const total = totals[gi];
        const w = (total / maxTotal) * 100;
        return (
          <div key={g.label} className="stacked-row">
            <div className="stacked-label">{g.label}</div>
            <div className="stacked-track" style={{ width: w + "%" }}>
              {keys.map((k, ki) => {
                const v = g.values[k] || 0;
                const pct = (v / total) * 100;
                return (
                  <div
                    key={k}
                    className="stacked-seg"
                    style={{ width: pct + "%", background: palette[ki] }}
                    title={`${k}: ${fmtChart(v)} (${pct.toFixed(1)}%)`}
                  />
                );
              })}
            </div>
            <div className="stacked-total mono">{fmtK(total)}</div>
          </div>
        );
      })}
      <div className="stacked-legend">
        {keys.map((k, ki) => (
          <span key={k} className="stacked-legend-item">
            <span className="stacked-legend-dot" style={{ background: palette[ki] }} />
            <span className="stacked-legend-lbl">{k.replace(/_/g, " ")}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/* ─── Line chart (multi-series) for hourly trends ──────────────────── */
function LineChart({ series, labels, height = 200, showLegend = true }) {
  // series: [{label, color, values: number[]}]
  const W = 800, H = height;
  const padL = 38, padB = 26, padT = 12, padR = 12;
  const n = series[0].values.length;
  const max = Math.max(...series.flatMap((s) => s.values), 1);
  const niceMax = Math.ceil(max / 5) * 5;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const x = (i) => padL + (i / (n - 1)) * innerW;
  const y = (v) => padT + innerH - (v / niceMax) * innerH;
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="chart-svg" preserveAspectRatio="none">
        {/* y grid */}
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line x1={padL} y1={padT + (1 - t) * innerH} x2={W - padR} y2={padT + (1 - t) * innerH} stroke="var(--line)" strokeWidth="0.5" />
            <text x={padL - 4} y={padT + (1 - t) * innerH + 3} textAnchor="end" className="chart-tick">{Math.round(niceMax * t)}</text>
          </g>
        ))}
        {/* x labels */}
        {labels.map((l, i) => (
          (i % Math.ceil(n / 12) === 0) && (
            <text key={i} x={x(i)} y={H - 6} textAnchor="middle" className="chart-tick">{l}</text>
          )
        ))}
        {/* series */}
        {series.map((s, si) => {
          const pts = s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
          const area = `M ${x(0)},${y(0)} L ${pts.split(" ").join(" L ")} L ${x(n-1)},${y(0)} Z`;
          return (
            <g key={si}>
              <path d={area} fill={s.color} opacity="0.06" />
              <polyline points={pts} fill="none" stroke={s.color} strokeWidth="1.6" />
              {s.values.map((v, i) => (
                <circle key={i} cx={x(i)} cy={y(v)} r="2" fill={s.color}>
                  <title>{`${labels[i]} · ${s.label}: ${v}`}</title>
                </circle>
              ))}
            </g>
          );
        })}
      </svg>
      {showLegend && (
        <div className="chart-legend">
          {series.map((s) => (
            <span key={s.label} className="chart-legend-item">
              <span className="chart-legend-swatch" style={{ background: s.color }} />
              <span>{s.label}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { Sparkline, Histogram, Donut, Funnel, Heatmap, Gauge, StackedBar, LineChart });
