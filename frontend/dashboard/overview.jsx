/* global React, fmt, sentenceCase, tierDot, ChainPill, AddressCell, ChainFilter,
   HeaderCell, Sparkline, Histogram, Donut, Funnel, Heatmap, Gauge, StackedBar, LineChart,
   useT, localCategory, localTier, localReview */
const { useMemo: useMemoOv, useState: useStateOv } = React;

const tierRankOv = { high: 0, medium: 1, watch: 2, low: 3 };
const reviewRankOv = { confirmed: 0, watchlist: 1, needs_review: 2, reject: 3, unreviewed: 4 };

/* ─── KPI block ─────────────────────────────────────────────────────── */
function KPI({ label, value, sub, accent, trend }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" style={accent ? { color: accent } : null}>{value}</div>
      <div className="kpi-foot">
        {sub && <div className="kpi-sub">{sub}</div>}
        {trend && (
          <div className="kpi-trend">
            <Sparkline series={[{ label: "trend", color: accent || "var(--accent)", values: trend }]} height={28} />
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── chain card ───────────────────────────────────────────────────── */
function ChainCard({ chain, sparkValues }) {
  const { t } = useT();
  const c = chain;
  const total = Object.values(c.tiers).reduce((s, n) => s + n, 0);
  const order = ["high", "medium", "watch", "low"];
  return (
    <a className="chain-card" id={c.id} href="#" onClick={(e) => e.preventDefault()}>
      <div className="chain-card-head">
        <ChainPill chain={c.id} />
        <span className="chain-card-name">{c.label}</span>
        <span className="chain-card-meta mono">id {c.chainId}</span>
      </div>
      <div className="chain-card-grid">
        <div>
          <div className="chain-card-num mono">{fmt(c.mediumHigh)}</div>
          <div className="chain-card-lbl">{t("ov.cardMediumHigh")}</div>
        </div>
        <div>
          <div className="chain-card-num mono">{fmt(c.contracts)}</div>
          <div className="chain-card-lbl">{t("ov.cardContracts")}</div>
        </div>
        <div>
          <div className="chain-card-num mono">{fmt(c.pending)}</div>
          <div className="chain-card-lbl">{t("ov.cardPending")}</div>
        </div>
        <div>
          <div className="chain-card-num mono">+{c.lagBlocks}</div>
          <div className="chain-card-lbl">{t("ov.cardLag")}</div>
        </div>
      </div>
      <div className="chain-card-spark">
        <Sparkline
          series={[{ label: c.id, color: "var(--chain-" + c.id + ")", values: sparkValues }]}
          height={40}
        />
        <div className="chain-card-spark-meta mono">{t("ov.cardSpark")}</div>
      </div>
      <div className="chain-card-bar">
        {order.map((tt) => (
          <div
            key={tt}
            className={"chain-card-bar-seg tierbar-" + tt}
            style={{ width: (c.tiers[tt] / total) * 100 + "%" }}
            title={tt + ": " + fmt(c.tiers[tt])}
          />
        ))}
      </div>
      <div className="chain-card-foot mono">
        <span>{t("chain.block")} {fmt(c.lastScannedBlock)}</span>
        <span className="chain-card-foot-sep">·</span>
        <span>{c.discoveryMode}</span>
        <span className="chain-card-foot-sep">·</span>
        <span>≥{c.activityMinObservations} {t("ov.observations")}</span>
      </div>
    </a>
  );
}

/* ─── activity feed ─────────────────────────────────────────────────── */
function ActivityFeed({ events, limit = 10 }) {
  return (
    <div className="feed">
      {events.slice(0, limit).map((ev, i) => (
        <div key={i} className="feed-row">
          <div className="feed-time mono">{ev.t}</div>
          <div className="feed-body">
            <div className="feed-meta">
              <ChainPill chain={ev.chain} size="sm" />
              <span className={"ev-level ev-level-" + ev.level}>{ev.level}</span>
              <span className="feed-kind mono">{ev.kind}</span>
            </div>
            <div className="feed-text">{ev.text}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

/* ─── overview pane (charts first, table last) ─────────────────────── */
function OverviewPane({ meta, chains, projects, onJumpToCandidates }) {
  const { t } = useT();
  const [chainFilter, setChainFilter] = useStateOv("all");
  const [sort, setSort] = useStateOv({ key: "score", dir: "desc" });
  const ts = window.DASHBOARD_TIMESERIES;
  const scoreDist = window.DASHBOARD_SCORE_DIST;
  const categoryMix = window.DASHBOARD_CATEGORY_MIX;
  const heatmap = window.DASHBOARD_HEATMAP;
  const sourceByChain = window.DASHBOARD_SOURCE_BY_CHAIN;
  const poolEvents = window.DASHBOARD_POOL_EVENTS;

  const chainCounts = useMemoOv(() => {
    const c = { all: projects.length };
    Object.keys(chains).forEach((k) => { c[k] = projects.filter((p) => p.chain === k).length; });
    return c;
  }, [projects, chains]);

  const featured = useMemoOv(() => {
    let arr = projects;
    if (chainFilter !== "all") arr = arr.filter((p) => p.chain === chainFilter);
    const { key, dir } = sort;
    const m = dir === "desc" ? -1 : 1;
    arr = [...arr].sort((a, b) => {
      let av, bv;
      switch (key) {
        case "score":    av = a.score; bv = b.score; break;
        case "tier":     av = tierRankOv[a.tier]; bv = tierRankOv[b.tier]; break;
        case "chain":    av = a.chain; bv = b.chain; break;
        case "label":    av = a.label.toLowerCase(); bv = b.label.toLowerCase(); break;
        case "category": av = a.category; bv = b.category; break;
        case "twitter":  av = a.twitter || "~"; bv = b.twitter || "~"; break;
        case "website":  av = a.website || "~"; bv = b.website || "~"; break;
        case "primary":  av = a.primary; bv = b.primary; break;
        case "review":   av = reviewRankOv[a.review ? a.review.decision : "unreviewed"]; bv = reviewRankOv[b.review ? b.review.decision : "unreviewed"]; break;
        default:         av = 0; bv = 0;
      }
      if (av < bv) return -1 * m;
      if (av > bv) return 1 * m;
      return b.score - a.score;
    });
    if (key === "score" && dir === "desc") {
      arr.sort((a, b) => {
        const r = tierRankOv[a.tier] - tierRankOv[b.tier];
        return r !== 0 ? r : b.score - a.score;
      });
    }
    return arr.slice(0, 10);
  }, [projects, chainFilter, sort]);

  const totals = useMemoOv(() => {
    const arr = Object.values(chains);
    return {
      contracts: arr.reduce((s, c) => s + c.contracts, 0),
      enriched: arr.reduce((s, c) => s + c.enriched, 0),
      pending: arr.reduce((s, c) => s + c.pending, 0),
      mediumHigh: arr.reduce((s, c) => s + c.mediumHigh, 0),
      observations: arr.reduce((s, c) => s + c.observations, 0),
    };
  }, [chains]);

  const confirmed = projects.filter((p) => p.review && p.review.decision === "confirmed").length;
  const funnelStages = [
    { label: t("ov.funnel.contracts"), value: totals.contracts },
    { label: t("ov.funnel.enriched"),  value: totals.enriched },
    { label: t("ov.funnel.medhigh"),   value: totals.mediumHigh },
    { label: t("ov.funnel.confirmed"), value: confirmed },
  ];

  const trendSeries = [
    { label: "Ethereum", color: "var(--chain-ethereum)", values: ts.ethereum },
    { label: "Base",     color: "var(--chain-base)",     values: ts.base },
    { label: "BSC",      color: "var(--chain-bsc)",      values: ts.bsc },
  ];

  const sourceKeys = ["mint_transfer", "contract_creation", "uniswap_v2_pair_created", "uniswap_v3_pool_created", "uniswap_v4_initialize", "internal_create2"];
  const palette = ["var(--accent)", "oklch(0.55 0.13 245)", "oklch(0.55 0.10 270)", "oklch(0.72 0.14 85)", "oklch(0.55 0.04 260)", "oklch(0.55 0.13 25)"];
  const sourceGroups = Object.keys(sourceByChain).map((k) => ({
    label: chains[k].short,
    values: sourceByChain[k],
  }));
  sourceGroups.forEach((g) => { sourceKeys.forEach((k) => { if (g.values[k] == null) g.values[k] = 0; }); });

  return (
    <div className="pane pane-wide">
      <div className="pane-header">
        <div>
          <div className="pane-eyebrow">{t("ov.eyebrow")}</div>
          <h1 className="pane-title">{t("ov.title")}</h1>
          <p className="pane-sub">{t("ov.sub")}</p>
        </div>
        <div className="pane-actions">
          <button className="btn btn-ghost">{t("common.snapshot")}</button>
          <button className="btn btn-ghost">{t("common.exportCsv")}</button>
          <button className="btn btn-primary">{t("common.runCycle")}</button>
        </div>
      </div>

      {/* ─── 1. KPIs ─────────────────────────────────────────── */}
      <section className="kpi-row kpi-row-5">
        <KPI label={t("ov.mediumHigh")} value={fmt(totals.mediumHigh)} sub={t("ov.readyReview")} accent="var(--accent)" trend={ts.mediumHigh} />
        <KPI label={t("ov.contracts")}  value={fmt(totals.contracts)}  sub={fmt(totals.observations) + " " + t("ov.observations")} trend={ts.ethereum.map((v,i)=>v+ts.base[i]+ts.bsc[i])} />
        <KPI label={t("ov.enriched")}   value={fmt(totals.enriched)}   sub={fmt(totals.pending) + " " + t("ov.pending")} />
        <KPI label={t("ov.chainsLive")} value={Object.keys(chains).length + " / 3"} sub="ethereum · base · bsc" />
        <KPI label={t("ov.workers")}    value={meta.workers.filter(w => w.status === "running").length + " / " + meta.workers.length} sub={t("ov.backgroundProcesses")} />
      </section>

      {/* ─── 2. Chain cards ──────────────────────────────────── */}
      <section className="chain-cards">
        {Object.values(chains).map((c) => (
          <ChainCard key={c.id} chain={c} sparkValues={ts[c.id]} />
        ))}
      </section>

      {/* ─── 3. 24h trends + score histogram ─────────────────── */}
      <section className="charts-grid charts-grid-2">
        <div className="card">
          <div className="card-head">
            <h3 className="card-title">{t("ov.trendsTitle")}</h3>
            <span className="card-meta">{t("ov.trendsMeta")}</span>
          </div>
          <LineChart series={trendSeries} labels={ts.hours} height={220} />
        </div>

        <div className="card">
          <div className="card-head">
            <h3 className="card-title">{t("ov.scoreDist")}</h3>
            <span className="card-meta">{t("ov.scoreMeta", { n: fmt(scoreDist.reduce((s,d)=>s+d.count,0)) })}</span>
          </div>
          <Histogram data={scoreDist} height={220} accentBin={(d) => d.bin === "100" || d.bin === "90-99" || d.bin === "80-89"} />
          <div className="chart-note">{t("ov.scoreNote")}</div>
        </div>
      </section>

      {/* ─── 4. Funnel + 2 donuts ───────────────────────────── */}
      <section className="charts-grid charts-grid-3">
        <div className="card">
          <div className="card-head">
            <h3 className="card-title">{t("ov.funnel")}</h3>
            <span className="card-meta">{t("ov.funnelMeta")}</span>
          </div>
          <Funnel stages={funnelStages} />
        </div>

        <div className="card">
          <div className="card-head">
            <h3 className="card-title">{t("ov.catMix")}</h3>
            <span className="card-meta">{t("ov.catMixMeta")}</span>
          </div>
          <Donut data={categoryMix.map((c) => ({ label: localCategory(t, c.category), count: c.count }))} size={170} thickness={20} />
        </div>

        <div className="card">
          <div className="card-head">
            <h3 className="card-title">{t("ov.pools")}</h3>
            <span className="card-meta">{t("ov.poolsMeta")}</span>
          </div>
          <Donut data={poolEvents.map((p) => ({ label: p.protocol, count: p.count, color: p.color }))} size={170} thickness={20} />
        </div>
      </section>

      {/* ─── 5. Heatmap ─────────────────────────────────────── */}
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("ov.heatmap")}</h3>
          <span className="card-meta">{t("ov.heatmapMeta")}</span>
        </div>
        <Heatmap grid={heatmap} />
      </section>

      {/* ─── 6. Source mix stacked bar ──────────────────────── */}
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">{t("ov.sourceMix")}</h3>
          <span className="card-meta">{t("ov.sourceMixMeta")}</span>
        </div>
        <StackedBar groups={sourceGroups} keys={sourceKeys} palette={palette} />
      </section>

      {/* ─── 7. Top discoveries table + live activity feed ──── */}
      <section className="ov-split">
        <div className="card no-pad ov-split-main">
          <div className="card-head card-head-padded">
            <div>
              <h3 className="card-title">{t("ov.topTitle")}</h3>
              <div className="card-meta">{t("ov.topMeta")}</div>
            </div>
            <button className="btn btn-ghost btn-sm" onClick={onJumpToCandidates}>
              {t("common.seeAll", { n: projects.length })}
            </button>
          </div>
          <div className="card-head-padded card-head-padded-thin">
            <ChainFilter value={chainFilter} onChange={setChainFilter} chains={chains} counts={chainCounts} />
          </div>
          <table className="data-table candidates-table">
            <thead>
              <tr>
                <HeaderCell label={t("cand.col.score")}    sortKey="score"    sort={sort} setSort={setSort} align="right" />
                <HeaderCell label={t("cand.col.tier")}     sortKey="tier"     sort={sort} setSort={setSort} />
                <HeaderCell label={t("cand.col.chain")}    sortKey="chain"    sort={sort} setSort={setSort} />
                <HeaderCell label={t("cand.col.project")}  sortKey="label"    sort={sort} setSort={setSort} />
                <HeaderCell label={t("cand.col.category")} sortKey="category" sort={sort} setSort={setSort} />
                <HeaderCell label={t("cand.col.twitter")}  sortKey="twitter"  sort={sort} setSort={setSort} />
                <HeaderCell label={t("cand.col.address")}  sortKey="primary"  sort={sort} setSort={setSort} />
                <HeaderCell label={t("cand.col.review")}   sortKey="review"   sort={sort} setSort={setSort} />
              </tr>
            </thead>
            <tbody>
              {featured.map((p) => (
                <tr key={p.primary} className="clickable" onClick={onJumpToCandidates}>
                  <td className="num"><span className="score">{p.score}</span></td>
                  <td>
                    <div className="tier-cell">
                      {tierDot(p.tier)}
                      <span>{localTier(t, p.tier)}</span>
                    </div>
                  </td>
                  <td><ChainPill chain={p.chain} /></td>
                  <td>
                    <div className="proj-cell">
                      <span className="proj-label">{p.label}</span>
                      <span className="proj-sym">{p.symbol}</span>
                    </div>
                  </td>
                  <td className="muted">{localCategory(t, p.category)}</td>
                  <td className="small">
                    {p.twitter
                      ? <a className="ext-link mono" href={"https://x.com/" + p.twitter} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>@{p.twitter}<span className="ext-icon">↗</span></a>
                      : <span className="muted">—</span>}
                  </td>
                  <td><AddressCell addr={p.primary} chain={p.chain} /></td>
                  <td>
                    {p.review
                      ? <span className={"review-pill review-" + p.review.decision}>{localReview(t, p.review.decision)}</span>
                      : <span className="muted small">{t("common.unreviewed")}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="ov-split-rail">
          <div className="card">
            <div className="card-head">
              <h3 className="card-title">{t("ov.liveActivity")}</h3>
              <span className="card-meta">{t("ov.liveActivityMeta")}</span>
            </div>
            <ActivityFeed events={meta.events} limit={12} />
          </div>
        </div>
      </section>
    </div>
  );
}

window.OverviewPane = OverviewPane;
window.KPI = KPI;
window.ChainCard = ChainCard;
