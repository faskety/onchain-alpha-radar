/* global React, ChainPill, useT */
const { useEffect: useEffectSet, useState: useStateSet } = React;

function cloneSettings(value) {
  return JSON.parse(JSON.stringify(value || {}));
}

/* ─── form primitives ──────────────────────────────────────────────── */
function Field({ label, hint, children }) {
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <div className="field-control">{children}</div>
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  );
}

function NumberInput({ value, onChange, min, max, step = 1, suffix }) {
  return (
    <div className="num-input">
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {suffix && <span className="num-suffix">{suffix}</span>}
    </div>
  );
}

function Slider({ value, onChange, min, max, step = 1 }) {
  return (
    <div className="slider-wrap">
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <div className="slider-val mono">{value}</div>
    </div>
  );
}

function Toggle({ value, onChange }) {
  return (
    <button
      className={"toggle" + (value ? " toggle-on" : "")}
      onClick={() => onChange(!value)}
      aria-pressed={value}
    >
      <span className="toggle-knob" />
    </button>
  );
}

function Segmented({ value, onChange, options }) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button
          key={o.value}
          className={"seg" + (value === o.value ? " seg-active" : "")}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function SecretInput({ value, onChange, placeholder, onReveal }) {
  const [visible, setVisible] = useStateSet(false);
  const [busy, setBusy] = useStateSet(false);
  const toggle = async () => {
    if (!visible && onReveal && String(value || "").startsWith("****")) {
      setBusy(true);
      try {
        const next = await onReveal();
        if (typeof next === "string") onChange(next);
      } finally {
        setBusy(false);
      }
    }
    setVisible((v) => !v);
  };
  return (
    <div className="secret-input">
      <input
        type={visible ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
      <button
        type="button"
        className="secret-btn"
        onClick={toggle}
        disabled={busy}
        aria-label={visible ? "Hide" : "Show"}
      >
        {busy ? "..." : visible ? "hide" : "show"}
      </button>
    </div>
  );
}

/* ─── per-chain settings panel ─────────────────────────────────────── */
function PerChainSettings({ chainId, settings, onChange }) {
  const { t } = useT();
  const s = settings;
  const set = (k) => (v) => onChange({ ...s, [k]: v });
  return (
    <>
      <Field label={t("set.scannerEnabled")} hint={t("set.scannerEnabledHint")}>
        <Toggle value={s.enabled} onChange={set("enabled")} />
      </Field>
      <Field label={t("set.cycleInterval")} hint={t("set.cycleIntervalHint")}>
        <Slider value={s.intervalSeconds} onChange={set("intervalSeconds")} min={60} max={3600} step={60} />
        <div className="field-meta mono">{(s.intervalSeconds / 60).toFixed(0)} min</div>
      </Field>
      <Field label={t("set.confirmations")} hint={t("set.confirmationsHint")}>
        <NumberInput value={s.confirmations} onChange={set("confirmations")} min={1} max={128} suffix="blk" />
      </Field>
      <Field label={t("set.maxBlocks")} hint={t("set.maxBlocksHint")}>
        <Slider value={s.maxBlocksPerCycle} onChange={set("maxBlocksPerCycle")} min={50} max={10000} step={50} />
      </Field>
      <Field label={t("set.lookback")} hint={t("set.lookbackHint")}>
        <NumberInput value={s.lookbackBlocks} onChange={set("lookbackBlocks")} min={50} max={10000} step={50} suffix="blk" />
      </Field>
      <Field label={t("set.discMode")} hint={t("set.discModeHint")}>
        <Segmented
          value={s.discoveryMode}
          onChange={set("discoveryMode")}
          options={[
            { value: "activity", label: "Activity" },
            { value: "block", label: "Block" },
          ]}
        />
      </Field>
      <Field label={t("set.actThresh")} hint={t("set.actThreshHint")}>
        <Slider value={s.activityMinObservations} onChange={set("activityMinObservations")} min={10} max={1000} step={10} />
      </Field>
      <Field label={t("set.allTransfers")} hint={t("set.allTransfersHint")}>
        <Toggle value={s.includeAllTransfers} onChange={set("includeAllTransfers")} />
      </Field>
      <Field label={t("set.enrichLimit")} hint={t("set.enrichLimitHint")}>
        <Slider value={s.enrichLimitPerCycle} onChange={set("enrichLimitPerCycle")} min={0} max={100} step={5} />
      </Field>
      <Field label={t("set.maxAge")} hint={t("set.maxAgeHint")}>
        <NumberInput value={s.newContractMaxAgeBlocks} onChange={set("newContractMaxAgeBlocks")} min={100} max={50000} step={100} suffix="blk" />
      </Field>
      <Field label={t("set.customTopics")} hint={t("set.customTopicsHint")}>
        <input
          type="text"
          className="text-input"
          value={s.customEventTopics}
          onChange={(e) => set("customEventTopics")(e.target.value)}
          placeholder="0x… , 0x…"
        />
      </Field>
    </>
  );
}

/* ─── settings pane ────────────────────────────────────────────────── */
function SettingsPane({ defaults, chains }) {
  const { t } = useT();
  const [s, setS] = useStateSet(() => cloneSettings(defaults));
  const [baseline, setBaseline] = useStateSet(() => cloneSettings(defaults));
  const [activeChain, setActiveChain] = useStateSet("ethereum");
  const [status, setStatus] = useStateSet("");
  const [testStatus, setTestStatus] = useStateSet("");
  const [saving, setSaving] = useStateSet(false);
  const canUseApi = window.location.protocol === "http:" || window.location.protocol === "https:";
  const reset = () => setS(cloneSettings(baseline));
  const dirty = JSON.stringify(s) !== JSON.stringify(baseline);

  const loadSettings = async (reveal = false) => {
    if (!canUseApi) return null;
    const res = await fetch(`/api/settings${reveal ? "?reveal=1" : ""}`, { cache: "no-store" });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  };

  useEffectSet(() => {
    let alive = true;
    loadSettings(false)
      .then((next) => {
        if (!alive || !next) return;
        setS(cloneSettings(next));
        setBaseline(cloneSettings(next));
        setStatus("");
      })
      .catch(() => {
        if (alive && canUseApi) setStatus("Settings API unavailable");
      });
    return () => { alive = false; };
  }, []);

  const revealSecret = (key) => async () => {
    try {
      const next = await loadSettings(true);
      const value = next && next.apiKeys ? next.apiKeys[key] : "";
      if (typeof value === "string") {
        setBaseline((p) => ({ ...p, apiKeys: { ...p.apiKeys, [key]: value } }));
        return value;
      }
      return "";
    } catch (err) {
      setStatus("Unable to reveal saved key");
      return s.apiKeys[key] || "";
    }
  };

  const save = async () => {
    if (!dirty || saving || !canUseApi) return;
    setSaving(true);
    setStatus("Saving...");
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(s),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || "Save failed");
      const next = body.settings || s;
      setS(cloneSettings(next));
      setBaseline(cloneSettings(next));
      setStatus(`Saved ${body.savedKeys ? body.savedKeys.length : 0} settings to .env`);
    } catch (err) {
      setStatus(err && err.message ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const testConnection = (service) => async () => {
    if (!canUseApi) return;
    setTestStatus(`Testing ${service}...`);
    try {
      const res = await fetch(`/api/settings/test?service=${encodeURIComponent(service)}`, { cache: "no-store" });
      const body = await res.json();
      if (!res.ok || body.ok === false) throw new Error(body.error || "Connection failed");
      setTestStatus(body.message || `${service} connected`);
    } catch (err) {
      setTestStatus(err && err.message ? err.message : `${service} connection failed`);
    }
  };

  const setApi = (k, v) => setS((p) => ({ ...p, apiKeys: { ...p.apiKeys, [k]: v } }));
  const setGlobal = (k) => (v) => setS((p) => ({ ...p, global: { ...p.global, [k]: v } }));
  const setChain = (chainId, next) =>
    setS((p) => {
      const current = p.perChain[chainId] || {};
      const perChain = { ...p.perChain, [chainId]: next };
      if (next.enrichLimitPerCycle !== current.enrichLimitPerCycle) {
        Object.keys(perChain).forEach((id) => {
          perChain[id] = { ...perChain[id], enrichLimitPerCycle: next.enrichLimitPerCycle };
        });
      }
      return { ...p, perChain };
    });

  return (
    <div className="pane">
      <div className="pane-header">
        <div>
          <div className="pane-eyebrow">{t("set.eyebrow")}</div>
          <h1 className="pane-title">{t("set.title")}</h1>
          <p className="pane-sub">{t("set.sub", { env: ".env" })}</p>
        </div>
        <div className="pane-actions">
          {status && <span className="save-status">{status}</span>}
          <button className="btn btn-ghost" disabled={!dirty} onClick={reset}>{t("common.discard")}</button>
          <button className="btn btn-primary" disabled={!dirty || saving || !canUseApi} onClick={save}>
            {saving ? "Saving" : dirty ? t("common.save") : t("common.noChanges")}
          </button>
        </div>
      </div>

      {/* API keys */}
      <section className="settings-section">
        <div className="settings-section-head">
          <h3>{t("set.api")}</h3>
          <p>{t("set.apiSub", { env: ".env" })}</p>
        </div>
        <div className="settings-grid">
          <Field label={t("set.etherscanKey")} hint={t("set.etherscanKeyHint")}>
            <SecretInput
              value={s.apiKeys.etherscan}
              onChange={(v) => setApi("etherscan", v)}
              placeholder="YourEtherscanApiKey"
              onReveal={revealSecret("etherscan")}
            />
          </Field>
          <Field label={t("set.etherscanBase")} hint={t("set.etherscanBaseHint")}>
            <input
              type="text"
              className="text-input"
              value={s.apiKeys.etherscanBase || "https://api.etherscan.io/v2/api"}
              onChange={(e) => setApi("etherscanBase", e.target.value)}
            />
          </Field>
          <Field label={t("set.twitterKey")} hint={t("set.twitterKeyHint")}>
            <SecretInput
              value={s.apiKeys.opentwitter}
              onChange={(v) => setApi("opentwitter", v)}
              placeholder="opentwitter token"
              onReveal={revealSecret("opentwitter")}
            />
          </Field>
          <Field label={t("set.testConn")} hint={t("set.testConnHint")}>
            <div className="btn-row">
              <button className="btn btn-ghost btn-sm" onClick={testConnection("etherscan")}>{t("set.testEs")}</button>
              <button className="btn btn-ghost btn-sm" onClick={testConnection("opentwitter")}>{t("set.testOt")}</button>
            </div>
            {testStatus && <div className="field-meta">{testStatus}</div>}
          </Field>
        </div>
      </section>

      {/* Per-chain */}
      <section className="settings-section settings-section-stack">
        <div className="settings-section-head">
          <h3>{t("set.perChain")}</h3>
          <p>{t("set.perChainSub", { data: "data/" })}</p>
        </div>
        <div className="settings-stack-body">
          <div className="chain-tabs">
            {Object.values(chains).map((c) => {
              const enabled = s.perChain[c.id].enabled;
              return (
                <button
                  key={c.id}
                  className={"chain-tab" + (activeChain === c.id ? " chain-tab-active" : "")}
                  onClick={() => setActiveChain(c.id)}
                >
                  <span className={"chain-pill-dot chain-pill-" + c.id}><span className="chain-pill-dot" /></span>
                  <span>{c.label}</span>
                  <span className={"chain-tab-state " + (enabled ? "chain-tab-on" : "chain-tab-off")}>
                    {enabled ? t("set.activeChain") : t("set.inactiveChain")}
                  </span>
                </button>
              );
            })}
          </div>
          <div className="chain-tab-panel">
            <div className="settings-grid">
              <PerChainSettings
                chainId={activeChain}
                settings={s.perChain[activeChain]}
                onChange={(next) => setChain(activeChain, next)}
              />
            </div>
          </div>
        </div>
      </section>

      {/* Shared / global */}
      <section className="settings-section">
        <div className="settings-section-head">
          <h3>{t("set.shared")}</h3>
          <p>{t("set.sharedSub")}</p>
        </div>
        <div className="settings-grid">
          <Field label={t("set.rps")} hint={t("set.rpsHint")}>
            <Slider value={s.global.etherscanRequestsPerSecond} onChange={setGlobal("etherscanRequestsPerSecond")} min={1} max={10} step={1} />
          </Field>
          <Field label={t("set.twitterMax")} hint={t("set.twitterMaxHint")}>
            <NumberInput value={s.global.twitterMaxResults} onChange={setGlobal("twitterMaxResults")} min={5} max={100} step={5} />
          </Field>
          <Field label={t("set.twitterEnabled")} hint={t("set.twitterEnabledHint")}>
            <Toggle value={s.global.enableTwitter} onChange={setGlobal("enableTwitter")} />
          </Field>
          <Field label={t("set.dailySnap")} hint={t("set.dailySnapHint")}>
            <Toggle value={s.global.dailySnapshotEnabled} onChange={setGlobal("dailySnapshotEnabled")} />
          </Field>
          <Field label={t("set.staleMin")} hint={t("set.staleMinHint")}>
            <NumberInput value={s.global.runtimeStaleMinutes} onChange={setGlobal("runtimeStaleMinutes")} min={5} max={240} step={5} suffix="min" />
          </Field>
          <Field label={t("set.coverage")} hint={t("set.coverageHint")}>
            <NumberInput value={s.global.coverageWindowBlocks} onChange={setGlobal("coverageWindowBlocks")} min={60} max={5000} step={60} suffix="blk" />
          </Field>
        </div>
      </section>

      {/* Maintenance */}
      <section className="settings-section settings-section-stack">
        <div className="settings-section-head">
          <h3>{t("set.maint")}</h3>
          <p>{t("set.maintSub")}</p>
        </div>
        <div className="cmd-grid">
          {[
            ["status",                "Read SQLite counters and by-source / by-tier totals"],
            ["queue",                 "Inspect priority buckets in pending_enrichment"],
            ["health",                "Compare watermark against live Etherscan head"],
            ["coverage --repair",     "Rewind watermark to fill recent block gaps"],
            ["audit",                 "Operational JSON: workers, fingerprints, lag"],
            ["classify-backlog",      "Drain classification_deferred with Etherscan only"],
            ["skip-low-surface",      "Mark trivial creation-only rows as low without OpenTwitter"],
            ["verify-websites",       "Live-check high/medium official websites"],
            ["backfill-website-twitter", "Promote X handles found on verified websites"],
            ["report --snapshot",     "Write timestamped Markdown + JSON to data/reports/"],
            ["rescore --tier high",   "Recompute scores from stored evidence"],
            ["recover-processing",    "Release reservations older than 30 min"],
          ].map(([cmd, desc]) => (
            <button key={cmd} className="cmd-card">
              <div className="cmd-head">
                <span className="cmd-name mono">{cmd}</span>
                <span className="cmd-arrow">→</span>
              </div>
              <div className="cmd-desc">{desc}</div>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

window.SettingsPane = SettingsPane;
