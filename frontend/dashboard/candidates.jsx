/* global React, fmt, sentenceCase, tierDot, ChainPill, AddressCell, ChainFilter,
   useT, localCategory, localTier, localReview */
const { useState: useStateCand, useMemo: useMemoCand, useRef: useRefCand, useEffect: useEffectCand } = React;

/* ─── header cell with sort + optional filter popover ──────────────── */
function HeaderCell({
  label,
  sortKey,
  sort,
  setSort,
  filterValues,        // [string,...] available values
  filterValueLabel,    // optional fn(v) -> ReactNode for option label
  selectedFilters,     // string[] currently active
  onFilterChange,      // (next: string[]) => void
  align,
}) {
  const { t } = useT();
  const [open, setOpen] = useStateCand(false);
  const ref = useRefCand(null);
  useEffectCand(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const active = sort.key === sortKey;
  const onSort = (e) => {
    e.stopPropagation();
    if (!sortKey) return;
    if (!active) setSort({ key: sortKey, dir: "desc" });
    else if (sort.dir === "desc") setSort({ key: sortKey, dir: "asc" });
    else setSort({ key: "score", dir: "desc" });
  };
  const sortIcon = active ? (sort.dir === "desc" ? "▼" : "▲") : "↕";
  const hasFilter = filterValues && filterValues.length > 0;
  const filterActive = selectedFilters && selectedFilters.length > 0 && selectedFilters.length < filterValues.length;

  return (
    <th className={"th " + (align === "right" ? "th-right" : "")}>
      <div className="th-inner">
        <button
          className={"th-btn" + (active ? " th-btn-active" : "") + (sortKey ? "" : " th-btn-nosort")}
          onClick={onSort}
          disabled={!sortKey}
        >
          <span>{label}</span>
          {sortKey && <span className={"th-sort" + (active ? " th-sort-active" : "")}>{sortIcon}</span>}
        </button>
        {hasFilter && (
          <div className="th-filter-wrap" ref={ref}>
            <button
              className={"th-filter-btn" + (filterActive ? " th-filter-active" : "")}
              onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
              title="Filter"
            >
              ⌄
            </button>
            {open && (
              <div className="th-popover">
                <div className="th-popover-head">
                  <span>{t("cand.filterBy", { col: label.toLowerCase() })}</span>
                  <button
                    className="th-popover-clear"
                    onClick={() => onFilterChange(filterValues.slice())}
                  >{t("common.clear")}</button>
                </div>
                <div className="th-popover-list">
                  {filterValues.map((v) => {
                    const checked = selectedFilters.includes(v);
                    return (
                      <label key={v} className="th-popover-row">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => {
                            if (checked) onFilterChange(selectedFilters.filter((x) => x !== v));
                            else onFilterChange([...selectedFilters, v]);
                          }}
                        />
                        <span>{filterValueLabel ? filterValueLabel(v) : sentenceCase(v)}</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </th>
  );
}

/* ─── tier sort ordering helper ───────────────────────────────────── */
const tierRank = { high: 0, medium: 1, watch: 2, low: 3 };
const reviewRank = { confirmed: 0, watchlist: 1, needs_review: 2, reject: 3, unreviewed: 4 };

/* ─── candidates pane ──────────────────────────────────────────────── */
function CandidatesPane({ projects, chains, onReview, defaultChain }) {
  const { t } = useT();
  const [chainFilter, setChainFilter] = useStateCand(defaultChain || "all");
  const [q, setQ] = useStateCand("");
  const [sort, setSort] = useStateCand({ key: "score", dir: "desc" });
  const [selected, setSelected] = useStateCand(null);

  // available filter values
  const allTiers = useMemoCand(() => ["high", "medium", "watch", "low"], []);
  const allChains = useMemoCand(() => Object.keys(chains), [chains]);
  const allCategories = useMemoCand(
    () => Array.from(new Set(projects.map((p) => p.category))).sort(),
    [projects]
  );
  const allReviews = useMemoCand(() => ["confirmed", "watchlist", "needs_review", "reject", "unreviewed"], []);

  // selected filters per column (default = all selected)
  const [tierSel, setTierSel]       = useStateCand(allTiers);
  const [chainSel, setChainSel]     = useStateCand(allChains);
  const [catSel, setCatSel]         = useStateCand(allCategories);
  const [reviewSel, setReviewSel]   = useStateCand(allReviews);

  const chainCounts = useMemoCand(() => {
    const c = { all: projects.length };
    Object.keys(chains).forEach((k) => { c[k] = projects.filter((p) => p.chain === k).length; });
    return c;
  }, [projects, chains]);

  const filtered = useMemoCand(() => {
    let arr = projects;
    if (chainFilter !== "all") arr = arr.filter((p) => p.chain === chainFilter);
    if (tierSel.length < allTiers.length)       arr = arr.filter((p) => tierSel.includes(p.tier));
    if (chainSel.length < allChains.length)     arr = arr.filter((p) => chainSel.includes(p.chain));
    if (catSel.length < allCategories.length)   arr = arr.filter((p) => catSel.includes(p.category));
    if (reviewSel.length < allReviews.length) {
      arr = arr.filter((p) => {
        const r = p.review ? p.review.decision : "unreviewed";
        return reviewSel.includes(r);
      });
    }
    if (q) {
      const qq = q.toLowerCase();
      arr = arr.filter(
        (p) =>
          p.label.toLowerCase().includes(qq) ||
          (p.symbol || "").toLowerCase().includes(qq) ||
          (p.twitter || "").toLowerCase().includes(qq) ||
          p.primary.includes(qq)
      );
    }
    // sort
    const { key, dir } = sort;
    const m = dir === "desc" ? -1 : 1;
    arr = [...arr].sort((a, b) => {
      let av, bv;
      switch (key) {
        case "score":    av = a.score; bv = b.score; break;
        case "tier":     av = tierRank[a.tier]; bv = tierRank[b.tier]; break;
        case "chain":    av = a.chain; bv = b.chain; break;
        case "label":    av = a.label.toLowerCase(); bv = b.label.toLowerCase(); break;
        case "category": av = a.category; bv = b.category; break;
        case "twitter":  av = a.twitter || "~"; bv = b.twitter || "~"; break;
        case "website":  av = a.website || "~"; bv = b.website || "~"; break;
        case "primary":  av = a.primary; bv = b.primary; break;
        case "review":   av = reviewRank[a.review ? a.review.decision : "unreviewed"]; bv = reviewRank[b.review ? b.review.decision : "unreviewed"]; break;
        default:         av = 0; bv = 0;
      }
      if (av < bv) return -1 * m;
      if (av > bv) return  1 * m;
      // tiebreak by score desc
      return b.score - a.score;
    });
    return arr;
  }, [projects, chainFilter, tierSel, chainSel, catSel, reviewSel, q, sort, allTiers, allChains, allCategories, allReviews]);

  const clearAll = () => {
    setTierSel(allTiers);
    setChainSel(allChains);
    setCatSel(allCategories);
    setReviewSel(allReviews);
    setQ("");
    setChainFilter("all");
    setSort({ key: "score", dir: "desc" });
  };
  const hasActiveFilters =
    chainFilter !== "all" ||
    tierSel.length < allTiers.length ||
    chainSel.length < allChains.length ||
    catSel.length < allCategories.length ||
    reviewSel.length < allReviews.length ||
    q.length > 0;

  return (
    <div className="pane">
      <div className="pane-header">
        <div>
          <div className="pane-eyebrow">{t("cand.eyebrow")}</div>
          <h1 className="pane-title">{t("cand.title")}</h1>
          <p className="pane-sub">{t("cand.sub")}</p>
        </div>
        <div className="pane-actions">
          <button className="btn btn-ghost">{t("common.reviewPack")}</button>
          <button className="btn btn-primary">{t("common.exportCsv")}</button>
        </div>
      </div>

      <ChainFilter value={chainFilter} onChange={setChainFilter} chains={chains} counts={chainCounts} />

      <div className="filterbar">
        <div className="search">
          <input
            placeholder={t("common.search")}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div className="filter-spacer" />
        {hasActiveFilters && (
          <button className="btn btn-ghost btn-sm" onClick={clearAll}>{t("common.clearAll")}</button>
        )}
        <div className="result-count">{t("common.groupsCount", { n: filtered.length, total: projects.length })}</div>
      </div>

      <table className="data-table candidates-table">
        <thead>
          <tr>
            <HeaderCell label={t("cand.col.score")}    sortKey="score"    sort={sort} setSort={setSort} align="right" />
            <HeaderCell label={t("cand.col.tier")}     sortKey="tier"     sort={sort} setSort={setSort}
              filterValues={allTiers}     selectedFilters={tierSel}   onFilterChange={setTierSel}
              filterValueLabel={(v) => <span>{tierDot(v)}{localTier(t, v)}</span>}
            />
            <HeaderCell label={t("cand.col.chain")}    sortKey="chain"    sort={sort} setSort={setSort}
              filterValues={allChains}    selectedFilters={chainSel}  onFilterChange={setChainSel}
              filterValueLabel={(v) => <span><ChainPill chain={v} size="sm" /></span>}
            />
            <HeaderCell label={t("cand.col.project")}  sortKey="label"    sort={sort} setSort={setSort} />
            <HeaderCell label={t("cand.col.category")} sortKey="category" sort={sort} setSort={setSort}
              filterValues={allCategories} selectedFilters={catSel}   onFilterChange={setCatSel}
              filterValueLabel={(v) => localCategory(t, v)}
            />
            <HeaderCell label={t("cand.col.twitter")}  sortKey="twitter"  sort={sort} setSort={setSort} />
            <HeaderCell label={t("cand.col.website")}  sortKey="website"  sort={sort} setSort={setSort} />
            <HeaderCell label={t("cand.col.address")}  sortKey="primary"  sort={sort} setSort={setSort} />
            <HeaderCell label={t("cand.col.review")}   sortKey="review"   sort={sort} setSort={setSort}
              filterValues={allReviews}   selectedFilters={reviewSel} onFilterChange={setReviewSel}
              filterValueLabel={(v) => localReview(t, v)}
            />
          </tr>
        </thead>
        <tbody>
          {filtered.map((p) => (
            <tr
              key={p.primary}
              className={"clickable" + (selected && selected.primary === p.primary ? " row-selected" : "")}
              onClick={() => setSelected(p)}
            >
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
                  {p.addresses > 1 && <span className="addr-cluster">+{p.addresses - 1}</span>}
                </div>
              </td>
              <td className="muted">{localCategory(t, p.category)}</td>
              <td className="small">
                {p.twitter
                  ? <a className="ext-link mono" href={"https://x.com/" + p.twitter} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>@{p.twitter}<span className="ext-icon">↗</span></a>
                  : <span className="muted">—</span>}
              </td>
              <td className="small">
                {p.website
                  ? <a className="ext-link" href={p.website} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>{new URL(p.website).hostname}<span className="ext-icon">↗</span></a>
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
          {filtered.length === 0 && (
            <tr><td colSpan="9" className="empty-row">{t("common.noResults")} <button className="link-btn" onClick={clearAll}>{t("common.clearFilters")}</button></td></tr>
          )}
        </tbody>
      </table>

      {selected && (
        <Drawer project={selected} onClose={() => setSelected(null)} onReview={onReview} />
      )}
    </div>
  );
}

/* ─── detail drawer ────────────────────────────────────────────────── */
function Drawer({ project, onClose, onReview }) {
  const { t } = useT();
  const p = project;
  const breakdownEntries = Object.entries(p.breakdown);
  const maxBd = Math.max(...breakdownEntries.map(([, v]) => Math.abs(v)), 1);

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <div className="drawer-eyebrow">
              {tierDot(p.tier)} <span>{localTier(t, p.tier)} · {t("dr.score")} {p.score}</span>
              <span className="drawer-eyebrow-sep">·</span>
              <ChainPill chain={p.chain} size="sm" />
            </div>
            <h2 className="drawer-title">{p.label}</h2>
            <div className="drawer-sub">
              <span className="proj-sym large">{p.symbol}</span>
              <span className="muted small">{localCategory(t, p.category)} · {p.positioning}</span>
            </div>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="drawer-body">
          <p className="drawer-desc">{p.description}</p>

          <div className="dl">
            <div className="dl-row">
              <dt>{t("dr.primary")}</dt>
              <dd><AddressCell addr={p.primary} chain={p.chain} short={false} /></dd>
            </div>
            <div className="dl-row"><dt>{t("dr.addresses")}</dt><dd>{p.addresses}</dd></div>
            <div className="dl-row"><dt>{t("dr.observations")}</dt><dd>{p.observations}</dd></div>
            <div className="dl-row"><dt>{t("dr.firstLast")}</dt><dd className="mono">{fmt(p.firstBlock)} → {fmt(p.lastBlock)}</dd></div>
            <div className="dl-row">
              <dt>{t("dr.twitter")}</dt>
              <dd>{p.twitter
                ? <a className="ext-link" href={"https://x.com/" + p.twitter} target="_blank" rel="noreferrer">@{p.twitter}<span className="ext-icon">↗</span></a>
                : <span className="muted">—</span>}</dd>
            </div>
            <div className="dl-row">
              <dt>{t("dr.website")}</dt>
              <dd>{p.website
                ? <a className="ext-link" href={p.website} target="_blank" rel="noreferrer">{p.website}<span className="ext-icon">↗</span></a>
                : <span className="muted">—</span>}</dd>
            </div>
          </div>

          <h4 className="drawer-section">{t("dr.scoreBreakdown")}</h4>
          <div className="breakdown">
            {breakdownEntries.map(([k, v]) => (
              <div key={k} className="breakdown-row">
                <div className="breakdown-key">{sentenceCase(k)}</div>
                <div className="breakdown-track">
                  <div
                    className={"breakdown-fill" + (v < 0 ? " breakdown-neg" : "")}
                    style={{ width: (Math.abs(v) / maxBd * 100) + "%" }}
                  />
                </div>
                <div className={"breakdown-val" + (v < 0 ? " breakdown-neg-val" : "")}>
                  {v > 0 ? "+" + v : v}
                </div>
              </div>
            ))}
          </div>

          <h4 className="drawer-section">{t("dr.obsSources")}</h4>
          <div className="chips">
            {p.sources.map((s) => (
              <span key={s} className="chip-static">{sentenceCase(s)}</span>
            ))}
          </div>

          {p.review && (
            <>
              <h4 className="drawer-section">{t("dr.opReview")}</h4>
              <div className="review-block">
                <span className={"review-pill review-" + p.review.decision}>{localReview(t, p.review.decision)}</span>
                <p className="review-note">{p.review.note}</p>
              </div>
            </>
          )}
        </div>

        <div className="drawer-foot">
          <div className="drawer-foot-label">{t("dr.dispatch")}</div>
          <div className="drawer-actions">
            {["confirmed", "watchlist", "needs_review", "reject"].map((d) => (
              <button
                key={d}
                className={"btn btn-decision btn-decision-" + d}
                onClick={() => onReview && onReview(p, d)}
              >
                {localReview(t, d)}
              </button>
            ))}
          </div>
        </div>
      </aside>
    </div>
  );
}

window.CandidatesPane = CandidatesPane;
window.HeaderCell = HeaderCell;
