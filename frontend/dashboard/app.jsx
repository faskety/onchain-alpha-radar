/* global React, ReactDOM,
   TopBar, Sidebar, OverviewPane, CandidatesPane, DiscoveryPane, WorkersPane,
   EventsPane, SettingsPane, LangProvider, useT,
   useTweaks, TweaksPanel, TweakSection, TweakRadio, TweakColor */

const { useState, useEffect } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "paper",
  "accent": "#3a8a4a",
  "density": "comfortable"
}/*EDITMODE-END*/;

const ACCENT_OPTIONS = [
  "#3a8a4a", // signal green
  "#1f6feb", // azure
  "#a85b00", // burnt orange
  "#6b4ed8", // indigo
];

function App() {
  const [tab, setTab] = useState("overview");
  const meta = window.DASHBOARD_META;
  const chains = window.DASHBOARD_CHAINS;
  const projects = window.DASHBOARD_PROJECTS;
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const { t: tt } = useT();

  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", t.theme);
    root.setAttribute("data-density", t.density);
    root.style.setProperty("--accent", t.accent);
    root.style.setProperty("--accent-soft", t.accent + "1a");
  }, [t.theme, t.density, t.accent]);

  const counts = {
    candidates: projects.length,
    workers: meta.workers.length,
    events: meta.events.length,
  };

  const onReview = (p, decision) => {
    console.log("review", p.primary, "→", decision);
  };

  return (
    <div className="app">
      <TopBar chains={chains} />
      <div className="app-body">
        <Sidebar tab={tab} setTab={setTab} counts={counts} />
        <main className="main">
          {tab === "overview"   && <OverviewPane meta={meta} chains={chains} projects={projects} onJumpToCandidates={() => setTab("candidates")} />}
          {tab === "candidates" && <CandidatesPane projects={projects} chains={chains} onReview={onReview} />}
          {tab === "workers"    && <WorkersPane meta={meta} />}
          {tab === "events"     && <EventsPane meta={meta} chains={chains} />}
          {tab === "discovery"  && <DiscoveryPane meta={meta} chains={chains} />}
          {tab === "settings"   && <SettingsPane defaults={window.DASHBOARD_SETTINGS_DEFAULTS} chains={chains} />}
        </main>
      </div>

      <TweaksPanel title="Tweaks">
        <TweakSection title={tt("tw.appearance")}>
          <TweakRadio
            label={tt("tw.theme")}
            value={t.theme}
            onChange={(v) => setTweak("theme", v)}
            options={[
              { value: "paper", label: tt("tw.paper") },
              { value: "light", label: tt("tw.light") },
              { value: "dark",  label: tt("tw.dark") },
            ]}
          />
          <TweakColor
            label={tt("tw.accent")}
            value={t.accent}
            onChange={(v) => setTweak("accent", v)}
            options={ACCENT_OPTIONS}
          />
          <TweakRadio
            label={tt("tw.density")}
            value={t.density}
            onChange={(v) => setTweak("density", v)}
            options={[
              { value: "compact", label: tt("tw.compact") },
              { value: "comfortable", label: tt("tw.comfortable") },
            ]}
          />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}

function Root() {
  return (
    <LangProvider>
      <App />
    </LangProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<Root />);
