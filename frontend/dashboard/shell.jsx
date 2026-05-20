/* global React, useT */
const { useState, useMemo, useEffect, useRef } = React;

/* ─── tier & color helpers ──────────────────────────────────────────── */
const TIER_DOT = {
  high:   "var(--color-tier-high)",
  medium: "var(--color-tier-medium)",
  watch:  "var(--color-tier-watch)",
  low:    "var(--color-tier-low)",
};
const tierDot = (tier) => (
  <span className="tier-dot" style={{ background: TIER_DOT[tier] || "var(--ink-30)" }} />
);

const fmt = (n) => n == null ? "—" : new Intl.NumberFormat("en-US").format(n);

const shortAddr = (a) => a ? a.slice(0, 6) + "…" + a.slice(-4) : "—";

const sentenceCase = (s) =>
  s.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());

// Localized category label
const localCategory = (t, cat) => {
  const k = "cat." + cat;
  const v = t(k);
  return v === k ? sentenceCase(cat) : v;
};

// Localized tier label
const localTier = (t, tier) => {
  const k = "tier." + tier;
  const v = t(k);
  return v === k ? tier : v;
};

// Localized review label
const localReview = (t, decision) => {
  const k = "review." + decision;
  const v = t(k);
  return v === k ? decision.replace("_", " ") : v;
};

const localStatus = (t, status) => {
  const value = status || "unknown";
  const k = "status." + value;
  const v = t(k);
  return v === k ? value : v;
};

/* ─── chain pill (tone per chain) ──────────────────────────────────── */
function ChainPill({ chain, size = "md" }) {
  if (!chain) return null;
  const c = window.DASHBOARD_CHAINS[chain] || { short: chain.toUpperCase(), label: chain };
  return (
    <span className={"chain-pill chain-pill-" + chain + " chain-pill-" + size}>
      <span className="chain-pill-dot" />
      <span>{c.short}</span>
    </span>
  );
}

/* ─── copyable mono address ────────────────────────────────────────── */
function AddressCell({ addr, chain, short = true }) {
  const [copied, setCopied] = useState(false);
  if (!addr) return <span className="muted">—</span>;
  const explorer = chain && window.DASHBOARD_CHAINS[chain]
    ? window.DASHBOARD_CHAINS[chain].explorer + "/address/" + addr
    : null;
  const copy = (e) => {
    e.stopPropagation();
    navigator.clipboard?.writeText(addr).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return (
    <span className="addr-cell" onClick={(e) => e.stopPropagation()}>
      {explorer ? (
        <a className="mono addr-link" href={explorer} target="_blank" rel="noreferrer">
          {short ? shortAddr(addr) : addr}
        </a>
      ) : (
        <span className="mono">{short ? shortAddr(addr) : addr}</span>
      )}
      <button
        className={"copy-btn" + (copied ? " copy-btn-done" : "")}
        onClick={copy}
        aria-label="Copy address"
        title={copied ? "Copied" : "Copy address"}
      >
        {copied ? "✓" : (
          <svg width="11" height="11" viewBox="0 0 14 14" fill="none">
            <rect x="3" y="3" width="8" height="8" stroke="currentColor" strokeWidth="1.2" />
            <path d="M1 1 V9 H9" stroke="currentColor" strokeWidth="1.2" fill="none" />
          </svg>
        )}
      </button>
    </span>
  );
}

/* ─── language toggle ─────────────────────────────────────────────── */
function LangToggle() {
  const { lang, setLang } = useT();
  return (
    <div className="lang-toggle">
      <button
        className={"lang-opt" + (lang === "en" ? " lang-opt-active" : "")}
        onClick={() => setLang("en")}
      >EN</button>
      <button
        className={"lang-opt" + (lang === "zh" ? " lang-opt-active" : "")}
        onClick={() => setLang("zh")}
      >中</button>
    </div>
  );
}

/* ─── topbar ────────────────────────────────────────────────────────── */
function TopBar({ chains }) {
  const { t } = useT();
  const arr = Object.values(chains);
  return (
    <header className="topbar">
      <div className="topbar-left">
        <div className="brand">
          <span className="brand-mark" />
          <span className="brand-name">{t("topbar.brand")}</span>
          <span className="brand-sep">/</span>
          <span className="brand-sub">{t("topbar.sub")}</span>
        </div>
      </div>
      <div className="topbar-right">
        <div className="chain-stats">
          {arr.map((c) => (
            <a key={c.id} className="chain-stat" href={"#"+c.id}>
              <span className={"chain-stat-dot chain-pill-" + c.id}><span className="chain-pill-dot" /></span>
              <span className="chain-stat-label">{c.short}</span>
              <span className="chain-stat-meta mono">{fmt(c.lastScannedBlock)}</span>
              <span className="chain-stat-lag">+{c.lagBlocks}</span>
            </a>
          ))}
        </div>
        <LangToggle />
        <div className="live-badge">
          <span className="live-pulse" />
          <span>{t("topbar.live")}</span>
        </div>
      </div>
    </header>
  );
}

/* ─── sidebar ───────────────────────────────────────────────────────── */
function Sidebar({ tab, setTab, counts }) {
  const { t } = useT();
  const items = [
    { id: "overview",   labelKey: "nav.discoveries", count: counts.candidates },
    { id: "candidates", labelKey: "nav.candidates",  count: counts.candidates },
    { id: "workers",    labelKey: "nav.workers",     count: counts.workers },
    { id: "events",     labelKey: "nav.events",      count: counts.events },
    { id: "discovery",  labelKey: "nav.discovery",   count: null },
    { id: "settings",   labelKey: "nav.settings",    count: null },
  ];
  return (
    <nav className="sidebar">
      <div className="sb-label">{t("nav.workspace")}</div>
      <ul className="sb-list">
        {items.map((it) => (
          <li key={it.id}>
            <button
              className={"sb-item" + (tab === it.id ? " sb-item-active" : "")}
              onClick={() => setTab(it.id)}
            >
              <span>{t(it.labelKey)}</span>
              {it.count != null && <span className="sb-count">{it.count}</span>}
            </button>
          </li>
        ))}
      </ul>
      <div className="sb-foot">
        <div className="sb-foot-row"><span>{t("topbar.env")}</span><b>{t("topbar.production")}</b></div>
        <div className="sb-foot-row"><span>{t("topbar.build")}</span><b>2026.05.17</b></div>
      </div>
    </nav>
  );
}

/* ─── chain filter row ──────────────────────────────────────────────── */
function ChainFilter({ value, onChange, chains, counts }) {
  const { t } = useT();
  const opts = [{ id: "all", labelKey: "common.allChains" }, ...Object.values(chains)];
  return (
    <div className="chain-filter">
      {opts.map((c) => (
        <button
          key={c.id}
          className={"chain-tab" + (value === c.id ? " chain-tab-active" : "")}
          onClick={() => onChange(c.id)}
        >
          {c.id !== "all" && <span className={"chain-pill-dot chain-pill-" + c.id}><span className="chain-pill-dot" /></span>}
          <span>{c.labelKey ? t(c.labelKey) : (c.label || c.short || c.id)}</span>
          {counts && counts[c.id] != null && (
            <span className="chain-tab-count">{counts[c.id]}</span>
          )}
        </button>
      ))}
    </div>
  );
}

Object.assign(window, {
  TopBar, Sidebar, tierDot, fmt, shortAddr, sentenceCase,
  TIER_DOT, ChainPill, AddressCell, ChainFilter, LangToggle,
  localCategory, localTier, localReview, localStatus,
});
