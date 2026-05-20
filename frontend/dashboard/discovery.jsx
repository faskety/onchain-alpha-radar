/* global React, fmt, sentenceCase, KPI, ChainPill, ChainFilter, useT, localStatus,
   Sparkline, Histogram, Donut, LineChart, Heatmap, Gauge, StackedBar, Funnel */
const { useState: useStateDisc } = React;

/* ─── Discovery pane — rich diagnostics view ───────────────────────── */
function DiscoveryPane({ meta, chains }) {
  const { t } = useT();
  const ts = window.DASHBOARD_TIMESERIES;
  const cycleDurations = window.DASHBOARD_CYCLE_DURATIONS;
  const sourceByChain = window.DASHBOARD_SOURCE_BY_CHAIN;
  const sourceTrendData = window.DASHBOARD_SOURCE_TRENDS || {};
  const poolEvents = window.DASHBOARD_POOL_EVENTS;
  const heatmap = window.DASHBOARD_HEATMAP;

  const obs = Object.values(sourceByChain).reduce((acc, values) => {
    Object.entries(values).forEach(([key, count]) => {
      acc[key] = (acc[key] || 0) + (count || 0);
    });
    return acc;
  }, {});
  const totalObs = Object.values(obs).reduce((s, n) => s + n, 0);
  const totalDisc = Object.values(chains).reduce((s, c) => s + c.contracts, 0);

  const sourceKeys = ["mint_transfer", "contract_creation", "uniswap_v2_pair_created", "uniswap_v3_pool_created", "uniswap_v4_initialize", "internal_create2"];
  const palette = ["var(--accent)", "oklch(0.55 0.13 245)", "oklch(0.55 0.10 270)", "oklch(0.72 0.14 85)", "oklch(0.55 0.04 260)", "oklch(0.55 0.13 25)"];
  const sourceGroups = Object.keys(sourceByChain).map((k) => ({
    label: chains[k].short,
    values: sourceByChain[k],
  }));
  sourceGroups.forEach((g) => { sourceKeys.forEach((k) => { if (g.values[k] == null) g.values[k] = 0; }); });

  const sourceTrends = sourceKeys.map((k, i) => ({
    key: k,
    label: sentenceCase(k),
    values: sourceTrendData[k] || Array.from({length: 24}, () => 0),
    color: palette[i],
    total: sourceByChain.ethereum[k] + sourceByChain.base[k] + sourceByChain.bsc[k],
  })).sort((a, b) => b.total - a.total);

  // chain detection-mode comparison data
  const chainEffData = Object.values(chains).map((c) => ({
    label: c.short,
    color: "var(--chain-" + c.id + ")",
    contracts: c.contracts,
    surfacing: c.mediumHigh,
    pct: c.contracts ? ((c.mediumHigh / c.contracts) * 100).toFixed(2) : "0.00",
  }));

  return (
    <div className="pane pane-wide">
      <div className="pane-header">
        <div>
          <div className="pane-eyebrow">{t("disc.eyebrow")}</div>
          <h1 className="pane-title">{t("disc.title")}</h1>
          <p className="pane-sub">{t("disc.sub")}</p>
        </div>
      </div>

      <section className="kpi-row">
        <KPI label={t("disc.totalSurfaces")}     value={fmt(totalDisc)}   sub={t("disc.acrossChains")} />
        <KPI label={t("disc.totalObs")}          value={fmt(totalObs)}    sub={t("disc.logEvents")} />
        <KPI label={t("disc.poolInits")}         value={fmt(poolEvents.reduce((s,p)=>s+p.count,0))} sub={t("disc.last7d")} />
        <KPI label={t("disc.rps")}               value="2.0"              sub={t("disc.sharedThrottle")} />
      </section>

      {/* chain lag gauges */}
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("disc.chainLag")}</h3>
          <span className="card-meta">{t("disc.chainLagMeta")}</span>
        </div>
        <div className="gauges">
          {Object.values(chains).map((c) => (
            <Gauge
              key={c.id}
              value={c.lagBlocks}
              max={Math.max(c.confirmations * 5, 100)}
              label={c.label}
              sub={"≤" + c.confirmations + " ok"}
              ok={c.lagBlocks <= c.confirmations * 3}
            />
          ))}
        </div>
      </section>

      {/* 24h hourly LineChart */}
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("ov.trendsTitle")}</h3>
          <span className="card-meta">{t("ov.trendsMeta")}</span>
        </div>
        <LineChart
          series={[
            { label: "Ethereum", color: "var(--chain-ethereum)", values: ts.ethereum },
            { label: "Base",     color: "var(--chain-base)",     values: ts.base },
            { label: "BSC",      color: "var(--chain-bsc)",      values: ts.bsc },
          ]}
          labels={ts.hours}
          height={220}
        />
      </section>

      {/* per-source sparkline grid */}
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("disc.sourceBreakdown")}</h3>
          <span className="card-meta">{t("disc.sourceBreakdownMeta", { n: sourceTrends.length })}</span>
        </div>
        <div className="source-grid">
          {sourceTrends.map((s) => (
            <div key={s.key} className="source-tile">
              <div className="source-tile-head">
                <span className="source-tile-dot" style={{ background: s.color }} />
                <span className="source-tile-label">{s.label}</span>
                <span className="source-tile-num mono">{fmt(s.total)}</span>
              </div>
              <Sparkline series={[{ label: s.label, color: s.color, values: s.values }]} height={48} />
            </div>
          ))}
        </div>
      </section>

      <section className="charts-grid charts-grid-2">
        <div className="card">
          <div className="card-head">
            <h3 className="card-title">{t("ov.pools")}</h3>
            <span className="card-meta">{t("ov.poolsMeta")} · {t("disc.last7d")}</span>
          </div>
          <Donut data={poolEvents.map((p) => ({ label: p.protocol, count: p.count, color: p.color }))} size={180} thickness={22} />
        </div>

        <div className="card">
          <div className="card-head">
            <h3 className="card-title">{t("disc.cycleDuration")}</h3>
            <span className="card-meta">{t("disc.cycleDurationMeta")}</span>
          </div>
          <Histogram data={cycleDurations} height={220} accentBin={(d) => d.bin === ">300"} />
          <div className="chart-note">{t("disc.cycleNote")}</div>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("ov.heatmap")}</h3>
          <span className="card-meta">{t("ov.heatmapMeta")}</span>
        </div>
        <Heatmap grid={heatmap} />
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("ov.sourceMix")}</h3>
          <span className="card-meta">{t("ov.sourceMixMeta")}</span>
        </div>
        <StackedBar groups={sourceGroups} keys={sourceKeys} palette={palette} />
      </section>

      {/* detection efficiency table */}
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("disc.efficiency")}</h3>
          <span className="card-meta">{t("disc.efficiencyMeta")}</span>
        </div>
        <table className="data-table compact">
          <thead>
            <tr>
              <th>{t("cand.col.chain")}</th>
              <th className="num">{t("disc.eff.contracts")}</th>
              <th className="num">{t("disc.eff.medhigh")}</th>
              <th className="num">{t("disc.eff.yield")}</th>
              <th>{t("disc.eff.mode")}</th>
              <th>{t("disc.eff.threshold")}</th>
            </tr>
          </thead>
          <tbody>
            {Object.values(chains).map((c) => (
              <tr key={c.id}>
                <td><ChainPill chain={c.id} /></td>
                <td className="num">{fmt(c.contracts)}</td>
                <td className="num">{fmt(c.mediumHigh)}</td>
                <td className="num mono">{c.contracts ? ((c.mediumHigh / c.contracts) * 100).toFixed(3) : "0.000"}%</td>
                <td className="muted">{c.discoveryMode}</td>
                <td className="mono">≥{c.activityMinObservations} obs</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("disc.boundary")}</h3>
          <span className="card-meta">{t("disc.boundaryMeta")}</span>
        </div>
        <div className="boundary">
          {[
            [t("disc.b.mints"),       t("disc.b.mintsDesc"), true],
            [t("disc.b.transfers"),   t("disc.b.transfersDesc"), true],
            [t("disc.b.erc1155"),     t("disc.b.erc1155Desc"), true],
            [t("disc.b.v2v3"),        t("disc.b.v2v3Desc"), true],
            [t("disc.b.v4"),          t("disc.b.v4Desc"), true],
            [t("disc.b.balancer"),    t("disc.b.balancerDesc"), true],
            [t("disc.b.custom"),      t("disc.b.customDesc"), true],
            [t("disc.b.methodOnly"),  t("disc.b.methodOnlyDesc"), false],
          ].map(([title, desc, on]) => (
            <div key={title} className={"boundary-row" + (on ? "" : " boundary-off")}>
              <div className="boundary-status">{on ? "●" : "○"}</div>
              <div>
                <div className="boundary-title">{title}</div>
                <div className="boundary-desc">{desc}</div>
              </div>
              <div className="boundary-tag">{on ? t("disc.b.active") : t("disc.b.gap")}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

/* ─── Workers pane ─────────────────────────────────────────────────── */
function WorkersPane({ meta }) {
  const { t } = useT();
  return (
    <div className="pane">
      <div className="pane-header">
        <div>
          <div className="pane-eyebrow">{t("work.eyebrow")}</div>
          <h1 className="pane-title">{t("work.title")}</h1>
          <p className="pane-sub">{t("work.sub")}</p>
        </div>
        <div className="pane-actions">
          <button className="btn btn-ghost">{t("work.recover")}</button>
          <button className="btn btn-ghost">{t("work.stopAll")}</button>
          <button className="btn btn-primary">{t("work.startAll")}</button>
        </div>
      </div>

      <div className="workers">
        {meta.workers.map((w) => (
          <div key={w.id} className={"worker worker-" + w.status}>
            <div className="worker-head">
              <div className="worker-title">
                <span className={"worker-dot worker-dot-" + w.status} />
                <h3>{w.name}</h3>
                <span className="worker-role">{w.role}</span>
                {w.chain && w.chain !== "multi" && <ChainPill chain={w.chain} size="sm" />}
              </div>
              <div className="worker-actions">
                {w.status === "running" ? (
                  <>
                    <button className="btn btn-ghost btn-sm">{t("work.restart")}</button>
                    <button className="btn btn-ghost btn-sm">{t("work.stop")}</button>
                  </>
                ) : (
                  <button className="btn btn-primary btn-sm">{t("work.start")}</button>
                )}
              </div>
            </div>
            <div className="worker-grid">
              <div><span className="worker-label">{t("work.pid")}</span><span className="worker-val mono">{w.pid ?? "—"}</span></div>
              <div><span className="worker-label">{t("work.status")}</span><span className="worker-val">{localStatus(t, w.status)}</span></div>
              <div><span className="worker-label">{t("work.lastCycle")}</span><span className="worker-val mono">{w.lastCycle.replace("T", " ").replace("Z", "")}</span></div>
              <div><span className="worker-label">{t("work.notes")}</span><span className="worker-val">{w.notes}</span></div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─── Events pane ──────────────────────────────────────────────────── */
function EventsPane({ meta, chains }) {
  const { t } = useT();
  const [chainFilter, setChainFilter] = useStateDisc("all");
  const counts = { all: meta.events.length };
  Object.keys(chains).forEach((k) => { counts[k] = meta.events.filter((e) => e.chain === k).length; });

  const events = chainFilter === "all" ? meta.events : meta.events.filter((e) => e.chain === chainFilter);

  return (
    <div className="pane">
      <div className="pane-header">
        <div>
          <div className="pane-eyebrow">{t("ev.eyebrow")}</div>
          <h1 className="pane-title">{t("ev.title")}</h1>
          <p className="pane-sub">
            {t("ev.sub")}
            <span className="mono"> data/events.jsonl</span>.
          </p>
        </div>
        <div className="pane-actions">
          <button className="btn btn-ghost">{t("ev.pause")}</button>
          <button className="btn btn-ghost">{t("ev.download")}</button>
        </div>
      </div>

      <ChainFilter value={chainFilter} onChange={setChainFilter} chains={chains} counts={counts} />

      <div className="card no-pad">
        <table className="data-table compact events-table">
          <thead>
            <tr>
              <th>{t("ev.col.time")}</th><th>{t("ev.col.chain")}</th><th>{t("ev.col.level")}</th><th>{t("ev.col.kind")}</th><th>{t("ev.col.detail")}</th>
            </tr>
          </thead>
          <tbody>
            {events.map((ev, i) => (
              <tr key={i}>
                <td className="mono small">{ev.t}</td>
                <td><ChainPill chain={ev.chain} size="sm" /></td>
                <td><span className={"ev-level ev-level-" + ev.level}>{ev.level}</span></td>
                <td className="mono small">{ev.kind}</td>
                <td className="small">{ev.text}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

window.DiscoveryPane = DiscoveryPane;
window.WorkersPane = WorkersPane;
window.EventsPane = EventsPane;
