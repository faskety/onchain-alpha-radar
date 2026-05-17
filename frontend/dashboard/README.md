# On-Chain Alpha Radar Dashboard

This folder contains the static frontend for On-Chain Alpha Radar.

The page is intentionally buildless: `index.html` loads React, ReactDOM, and Babel from CDN, then renders the local JSX files directly. Runtime data is generated into `data.js` by the Python CLI.

## Commands

```powershell
.\scripts\run-dashboard.ps1
.\scripts\run-dashboard.ps1 --no-serve
python -m alpha_listener.cli dashboard --workspace D:\Scripts\ether-onchain-alpha-listen --port 8765
```

`dashboard --no-serve` refreshes `frontend/dashboard/data.js` from the local SQLite stores and exits. Without `--no-serve`, the CLI starts a local HTTP server and refreshes `data.js` on page/data requests.
