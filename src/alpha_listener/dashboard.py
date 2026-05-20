from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .chains import CHAIN_PROFILES
from .config import Settings
from .etherscan import EtherscanClient
from .opentwitter import OpenTwitterClient, normalize_search_items
from .storage import Store

DASHBOARD_CHAINS = ("ethereum", "base", "bsc")
CHAIN_SHORT_NAMES = {"ethereum": "ETH", "base": "BASE", "bsc": "BSC"}
DATA_JS_NAME = "data.js"
SECRET_MASK = "****"
BACKGROUND_WORKER_ROLES = {"scanner", "classifier", "enricher", "combined"}

CHAIN_ENV_PREFIX = {"ethereum": "ETHEREUM", "base": "BASE", "bsc": "BSC"}
PER_CHAIN_ENV_FIELDS = {
    "intervalSeconds": "INTERVAL_SECONDS",
    "confirmations": "CONFIRMATIONS",
    "lookbackBlocks": "LOOKBACK_BLOCKS",
    "maxBlocksPerCycle": "MAX_BLOCKS_PER_CYCLE",
    "discoveryMode": "DISCOVERY_MODE",
    "activityMinObservations": "ACTIVITY_MIN_OBSERVATIONS",
    "includeAllTransfers": "ACTIVITY_INCLUDE_ALL_TRANSFERS",
    "newContractMaxAgeBlocks": "NEW_CONTRACT_MAX_AGE_BLOCKS",
    "customEventTopics": "ACTIVITY_CUSTOM_EVENT_TOPICS",
}


def dashboard_dir(workspace: Path) -> Path:
    return workspace / "frontend" / "dashboard"


def write_dashboard_data(workspace: Path, limit: int = 100, output: Path | None = None) -> Path:
    workspace = workspace.resolve()
    output_path = output or dashboard_dir(workspace) / DATA_JS_NAME
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_dashboard_payload(workspace, limit=limit)
    output_path.write_text(render_dashboard_js(payload), encoding="utf-8")
    return output_path


def build_dashboard_payload(workspace: Path, limit: int = 100) -> dict[str, Any]:
    chain_payloads = {}
    projects: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    queues: list[dict[str, Any]] = []
    source_by_chain: dict[str, dict[str, int]] = {}
    source_trends: dict[str, list[int]] = {}
    all_scores: list[int] = []
    all_finished_cycles: list[tuple[datetime, datetime]] = []
    hourly_discoveries = {chain: [0] * 24 for chain in DASHBOARD_CHAINS}
    hourly_medium_high = [0] * 24
    heatmap = [[0 for _ in range(24)] for _ in range(7)]

    for chain in DASHBOARD_CHAINS:
        settings = Settings.from_env(workspace, chain=chain)
        chain_data = collect_chain_data(settings, limit=limit)
        chain_payloads[chain] = chain_data["chain"]
        projects.extend(chain_data["projects"])
        events.extend(chain_data["events"])
        workers.extend(chain_data["workers"])
        queues.append(chain_data["queue"])
        source_by_chain[chain] = chain_data["source_by_chain"]
        merge_source_trends(source_trends, chain_data["source_trends"])
        all_scores.extend(chain_data["scores"])
        merge_hourly(hourly_discoveries[chain], chain_data["hourly_discoveries"])
        merge_hourly(hourly_medium_high, chain_data["hourly_medium_high"])
        merge_heatmap(heatmap, chain_data["heatmap"])
        all_finished_cycles.extend(chain_data["cycle_durations"])

    projects.sort(
        key=lambda item: (
            tier_rank(item.get("tier")),
            int(item.get("score") or 0),
            int(item.get("lastBlock") or 0),
        ),
        reverse=True,
    )
    projects = projects[: max(1, int(limit or 100))]
    events.sort(key=lambda item: str(item.get("sort_time") or ""), reverse=True)
    for event in events:
        event.pop("sort_time", None)

    return {
        "timeseries": {
            "hours": last_24_hour_labels(),
            "ethereum": hourly_discoveries["ethereum"],
            "base": hourly_discoveries["base"],
            "bsc": hourly_discoveries["bsc"],
            "mediumHigh": hourly_medium_high,
        },
        "score_dist": score_distribution(all_scores),
        "category_mix": category_mix(projects),
        "heatmap": heatmap,
        "pool_events": pool_events(source_by_chain),
        "cycle_durations": cycle_duration_distribution(all_finished_cycles),
        "source_trends": source_trends,
        "source_by_chain": source_by_chain,
        "chains": chain_payloads,
        "meta": {
            "generatedAt": utc_now(),
            "workers": workers,
            "workerSummary": worker_summary(workers),
            "chainHealth": chain_health_summary(chain_payloads),
            "queueBuckets": aggregate_queue_buckets(queues),
            "events": events[:50],
        },
        "projects": projects,
        "settings": settings_defaults(workspace),
    }


def collect_chain_data(settings: Settings, limit: int) -> dict[str, Any]:
    empty_queue = {
        "pending_by_bucket": {},
        "pending_total": 0,
        "processing": 0,
        "retry": 0,
        "stale_processing": 0,
        "classification_deferred_pending": 0,
        "low_surface_pending": 0,
    }
    if not settings.db_path.exists():
        return {
            "chain": chain_payload(settings, empty_summary(), empty_queue, {}),
            "projects": [],
            "events": [],
            "workers": [],
            "queue": empty_queue,
            "source_by_chain": {},
            "source_trends": {},
            "scores": [],
            "hourly_discoveries": [0] * 24,
            "hourly_medium_high": [0] * 24,
            "heatmap": [[0 for _ in range(24)] for _ in range(7)],
            "cycle_durations": [],
        }

    store = Store(settings.db_path, settings.data_dir)
    try:
        summary = store.summary()
        queue = store.queue_health()
        runtime = store.runtime_status()
        groups = store.project_groups(limit=max(1, int(limit or 100)))
        events = recent_events(store, settings.chain_slug, limit=50)
        scores = enrichment_scores(store)
        hourly_discoveries = hourly_contract_counts(store)
        hourly_medium_high = hourly_medium_high_counts(store)
        source_trends = hourly_source_counts(store)
        heatmap = event_heatmap(store)
        cycle_durations = matched_cycle_durations(store)
    finally:
        store.close()

    return {
        "chain": chain_payload(settings, summary, queue, runtime),
        "projects": [project_payload(settings.chain_slug, group) for group in groups],
        "events": events,
        "workers": worker_payloads(settings, runtime),
        "queue": queue,
        "source_by_chain": dict(summary.get("observations_by_source") or {}),
        "source_trends": source_trends,
        "scores": scores,
        "hourly_discoveries": hourly_discoveries,
        "hourly_medium_high": hourly_medium_high,
        "heatmap": heatmap,
        "cycle_durations": cycle_durations,
    }


def chain_payload(
    settings: Settings,
    summary: dict[str, Any],
    queue: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    latest, safe_latest = latest_runtime_blocks(runtime)
    last_scanned = summary.get("last_scanned_block")
    lag = 0
    if isinstance(last_scanned, int) and isinstance(safe_latest, int):
        lag = max(0, safe_latest - last_scanned)
    elif isinstance(last_scanned, int) and isinstance(latest, int):
        lag = max(0, latest - settings.confirmations - last_scanned)
    runtime_health = chain_runtime_health(settings, runtime)
    return {
        "id": settings.chain_slug,
        "label": settings.chain_name,
        "short": CHAIN_SHORT_NAMES.get(settings.chain_slug, settings.chain_slug.upper()),
        "chainId": int(settings.chainid),
        "explorer": settings.explorer_base_url,
        "lastScannedBlock": last_scanned,
        "currentHead": latest if latest is not None else last_scanned,
        "lagBlocks": lag,
        "confirmations": settings.confirmations,
        "contracts": int(summary.get("contracts") or 0),
        "enriched": int(summary.get("enriched") or 0),
        "processing": int(summary.get("processing_enrichment") or queue.get("processing") or 0),
        "pending": int(summary.get("pending_enrichment") or queue.get("pending_total") or 0),
        "mediumHigh": int(summary.get("medium_or_high") or 0),
        "observations": int(summary.get("observations") or 0),
        "cycleIntervalSeconds": settings.interval_seconds,
        "discoveryMode": settings.discovery_mode,
        "activityMinObservations": settings.activity_min_observations,
        "maxBlocksPerCycle": settings.max_blocks_per_cycle,
        "lookbackBlocks": settings.lookback_blocks,
        "tiers": dict(summary.get("by_tier") or {}),
        **runtime_health,
    }


def project_payload(chain: str, group: dict[str, Any]) -> dict[str, Any]:
    symbols = [str(value) for value in (group.get("symbols") or []) if value]
    sources = [
        source.strip()
        for source in str(group.get("observation_sources") or "").split(",")
        if source.strip()
    ]
    review = normalize_review(group.get("review"))
    return {
        "chain": chain,
        "primary": str(group.get("primary_address") or ""),
        "label": str(group.get("label") or group.get("project_key") or "Unknown project"),
        "symbol": symbols[0] if symbols else "",
        "score": int(group.get("score") or 0),
        "tier": str(group.get("tier") or "watch"),
        "category": str(group.get("project_category") or "contract_app"),
        "positioning": str(group.get("project_positioning") or ""),
        "description": str(group.get("project_description") or ""),
        "addresses": int(group.get("address_count") or len(group.get("addresses") or []) or 1),
        "observations": int(group.get("observation_count") or 0),
        "sources": sources,
        "twitter": normalize_twitter(group.get("twitter_account")),
        "website": normalize_url(group.get("website")),
        "firstBlock": group.get("first_block"),
        "lastBlock": group.get("last_block"),
        "review": review,
        "breakdown": dict(group.get("score_breakdown") or {}),
    }


def normalize_review(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    decision = str(value.get("decision") or "").strip()
    if not decision:
        return None
    return {
        "decision": decision,
        "note": str(value.get("note") or ""),
        "reviewedAt": value.get("reviewed_at"),
        "reviewer": value.get("reviewer"),
    }


def recent_events(store: Store, chain: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        """
        SELECT created_at, event_type, address, payload_json
        FROM project_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        level = "error" if "failed" in str(row["event_type"]) or payload.get("error") else "info"
        if str(row["event_type"]) in {"website_checked"} and str(payload.get("status") or "") in {"warn", "fail"}:
            level = "warn"
        result.append(
            {
                "t": compact_time(row["created_at"]),
                "chain": chain,
                "level": level,
                "kind": row["event_type"],
                "text": event_text(row["event_type"], row["address"], payload),
                "sort_time": row["created_at"],
            }
        )
    return result


def event_text(event_type: str, address: str | None, payload: dict[str, Any]) -> str:
    pieces = []
    if address:
        pieces.append(short_address(address))
    for key in (
        "event",
        "stage",
        "status",
        "role",
        "chunks",
        "logs",
        "tier",
        "score",
        "active_contracts",
        "active_candidates",
        "selected_candidates",
        "discovered_contracts",
        "enriched_contracts",
        "observed_contracts",
        "reserved",
        "checked",
        "matched_projects",
        "backfilled_addresses",
        "error",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            pieces.append(f"{key}={value}")
    if not pieces:
        return event_type
    return " | ".join(str(piece) for piece in pieces)[:260]


def worker_payloads(settings: Settings, runtime: dict[str, Any]) -> list[dict[str, Any]]:
    roles = runtime.get("roles") if isinstance(runtime.get("roles"), dict) else {}
    result = []
    now = datetime.now(timezone.utc)
    for role, state in sorted(roles.items()):
        last_cycle = runtime_active_at(state)
        raw_status = str(state.get("last_cycle_status") or "unknown")
        background = role in BACKGROUND_WORKER_ROLES
        pid = pid_for_role(settings, role)
        age_seconds = runtime_age_seconds(last_cycle, now)
        stale_after_seconds = role_stale_after_seconds(settings, raw_status, background)
        stale = background and age_seconds is not None and age_seconds > stale_after_seconds
        status = dashboard_worker_status(raw_status, pid, background, stale)
        result.append(
            {
                "id": f"{settings.chain_slug}-{role}",
                "name": f"{settings.chain_name} {role}",
                "chain": settings.chain_slug,
                "pid": pid,
                "status": status,
                "rawStatus": raw_status,
                "lastCycle": last_cycle,
                "role": role,
                "background": background,
                "ageSeconds": age_seconds,
                "staleAfterSeconds": stale_after_seconds if background else None,
                "notes": worker_notes(state),
            }
        )
    return result


def dashboard_worker_status(raw_status: str, pid: int | None, background: bool, stale: bool) -> str:
    if stale:
        return "stale"
    if raw_status == "failed":
        return "failed"
    if background and pid is not None and raw_status in {"ok", "running"}:
        return "running"
    return raw_status or "unknown"


def runtime_active_at(state: dict[str, Any]) -> str:
    raw_status = str(state.get("last_cycle_status") or "")
    if raw_status == "running":
        return (
            str(state.get("last_cycle_progressed_at") or "")
            or str(state.get("last_cycle_started_at") or "")
            or str(state.get("last_cycle_finished_at") or "")
        )
    return (
        str(state.get("last_cycle_finished_at") or "")
        or str(state.get("last_cycle_progressed_at") or "")
        or str(state.get("last_cycle_started_at") or "")
    )


def role_stale_after_seconds(settings: Settings, raw_status: str, background: bool) -> int:
    if not background:
        return 0
    runtime_threshold = max(1, int(settings.runtime_stale_minutes or 30)) * 60
    if raw_status == "running":
        return runtime_threshold
    cycle_threshold = max(1, int(settings.interval_seconds or 0)) * 2
    return max(runtime_threshold, cycle_threshold)


def runtime_age_seconds(value: Any, now: datetime) -> int | None:
    dt = parse_dt(value)
    if not dt:
        return None
    return max(0, int((now - dt).total_seconds()))


def chain_runtime_health(settings: Settings, runtime: dict[str, Any]) -> dict[str, Any]:
    roles = runtime.get("roles") if isinstance(runtime.get("roles"), dict) else {}
    now = datetime.now(timezone.utc)
    role_states = []
    for role, state in roles.items():
        if role not in BACKGROUND_WORKER_ROLES:
            continue
        last_cycle = runtime_active_at(state)
        raw_status = str(state.get("last_cycle_status") or "unknown")
        age_seconds = runtime_age_seconds(last_cycle, now)
        stale_after_seconds = role_stale_after_seconds(settings, raw_status, True)
        stale = age_seconds is not None and age_seconds > stale_after_seconds
        status = dashboard_worker_status(raw_status, pid_for_role(settings, role), True, stale)
        role_states.append(
            {
                "status": status,
                "raw_status": raw_status,
                "last_cycle": last_cycle,
                "age_seconds": age_seconds,
            }
        )

    if not role_states:
        return {
            "runtimeStatus": "idle",
            "lastRuntimeAt": None,
            "runtimeAgeSeconds": None,
        }

    latest = min(
        (state for state in role_states if state["age_seconds"] is not None),
        key=lambda state: int(state["age_seconds"]),
        default=None,
    )
    statuses = {str(state["status"]) for state in role_states}
    if "running" in statuses or "ok" in statuses:
        status = "active"
    elif "failed" in statuses:
        status = "failed"
    elif "stale" in statuses:
        status = "stale"
    else:
        status = "idle"
    return {
        "runtimeStatus": status,
        "lastRuntimeAt": latest["last_cycle"] if latest else None,
        "runtimeAgeSeconds": latest["age_seconds"] if latest else None,
    }


def worker_summary(workers: list[dict[str, Any]]) -> dict[str, int]:
    background = [worker for worker in workers if worker.get("background")]
    return {
        "total": len(background),
        "running": sum(1 for worker in background if worker.get("status") == "running"),
        "ok": sum(1 for worker in background if worker.get("status") == "ok"),
        "stale": sum(1 for worker in background if worker.get("status") == "stale"),
        "failed": sum(1 for worker in background if worker.get("status") == "failed"),
    }


def chain_health_summary(chains: dict[str, dict[str, Any]]) -> dict[str, int]:
    values = list(chains.values())
    return {
        "total": len(values),
        "active": sum(1 for chain in values if chain.get("runtimeStatus") == "active"),
        "stale": sum(1 for chain in values if chain.get("runtimeStatus") == "stale"),
        "failed": sum(1 for chain in values if chain.get("runtimeStatus") == "failed"),
        "idle": sum(1 for chain in values if chain.get("runtimeStatus") == "idle"),
        "pending": sum(int(chain.get("pending") or 0) for chain in values),
    }


def worker_notes(state: dict[str, Any]) -> str:
    context = state.get("last_cycle_context") if isinstance(state.get("last_cycle_context"), dict) else {}
    result = state.get("last_cycle_result") if isinstance(state.get("last_cycle_result"), dict) else {}
    parts = []
    if context.get("command"):
        parts.append(f"command={context.get('command')}")
    if context.get("max_blocks") is not None:
        parts.append(f"max_blocks={context.get('max_blocks')}")
    if context.get("enrich_limit") is not None:
        parts.append(f"enrich_limit={context.get('enrich_limit')}")
    if result.get("pending_enrichment") is not None:
        parts.append(f"pending={result.get('pending_enrichment')}")
    return " | ".join(parts) or "runtime metadata"


def pid_for_role(settings: Settings, role: str) -> int | None:
    if settings.chain_slug != "ethereum":
        pid_file = settings.workspace / "data" / "alpha-multichain.pid"
    else:
        pid_file = {
            "scanner": settings.data_dir / "alpha-listener.pid",
            "classifier": settings.data_dir / "alpha-classifier.pid",
            "enricher": settings.data_dir / "alpha-enricher.pid",
        }.get(role)
    if not pid_file or not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def enrichment_scores(store: Store) -> list[int]:
    rows = store.conn.execute(
        "SELECT score FROM enrichments WHERE status IN ('ok', 'partial') AND score IS NOT NULL"
    ).fetchall()
    return [int(row["score"]) for row in rows if row["score"] is not None]


def hourly_contract_counts(store: Store) -> list[int]:
    counts = [0] * 24
    start = hour_floor(datetime.now(timezone.utc)) - timedelta(hours=23)
    rows = store.conn.execute("SELECT first_seen_at FROM contracts").fetchall()
    for row in rows:
        index = hour_bucket(row["first_seen_at"], start)
        if index is not None:
            counts[index] += 1
    return counts


def hourly_medium_high_counts(store: Store) -> list[int]:
    counts = [0] * 24
    start = hour_floor(datetime.now(timezone.utc)) - timedelta(hours=23)
    rows = store.conn.execute(
        """
        SELECT searched_at
        FROM enrichments
        WHERE status IN ('ok', 'partial') AND tier IN ('medium', 'high')
        """
    ).fetchall()
    for row in rows:
        index = hour_bucket(row["searched_at"], start)
        if index is not None:
            counts[index] += 1
    return counts


def hourly_source_counts(store: Store) -> dict[str, list[int]]:
    counts: dict[str, list[int]] = {}
    start = hour_floor(datetime.now(timezone.utc)) - timedelta(hours=23)
    rows = store.conn.execute("SELECT discovery_source, observed_at FROM contract_observations").fetchall()
    for row in rows:
        index = hour_bucket(row["observed_at"], start)
        if index is None:
            continue
        source = str(row["discovery_source"] or "unknown")
        counts.setdefault(source, [0] * 24)[index] += 1
    return counts


def event_heatmap(store: Store) -> list[list[int]]:
    grid = [[0 for _ in range(24)] for _ in range(7)]
    now = datetime.now(timezone.utc)
    start = hour_floor(now) - timedelta(days=6)
    rows = store.conn.execute(
        "SELECT created_at FROM project_events WHERE event_type IN ('cycle_finished', 'cycle_failed')"
    ).fetchall()
    for row in rows:
        dt = parse_dt(row["created_at"])
        if not dt or dt < start or dt > now:
            continue
        day_index = (dt.date() - start.date()).days
        if 0 <= day_index < 7:
            grid[day_index][dt.hour] += 1
    return grid


def matched_cycle_durations(store: Store) -> list[tuple[datetime, datetime]]:
    rows = store.conn.execute(
        """
        SELECT created_at, event_type, payload_json
        FROM project_events
        WHERE event_type IN ('cycle_started', 'cycle_finished', 'cycle_failed', 'cycle_interrupted')
        ORDER BY id ASC
        LIMIT 1000
        """
    ).fetchall()
    starts: dict[str, datetime] = {}
    durations: list[tuple[datetime, datetime]] = []
    for row in rows:
        dt = parse_dt(row["created_at"])
        if not dt:
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        role = str(payload.get("role") or "default")
        event_type = str(row["event_type"])
        if event_type == "cycle_started":
            starts[role] = dt
        elif event_type in {"cycle_finished", "cycle_failed", "cycle_interrupted"} and role in starts:
            durations.append((starts.pop(role), dt))
    return durations[-200:]


def latest_runtime_blocks(runtime: dict[str, Any]) -> tuple[int | None, int | None]:
    latest_values = []
    safe_values = []
    roles = runtime.get("roles") if isinstance(runtime.get("roles"), dict) else {}
    for state in roles.values():
        for key in ("last_cycle_context", "last_cycle_progress", "last_cycle_result"):
            value = state.get(key) if isinstance(state, dict) else None
            if not isinstance(value, dict):
                continue
            if isinstance(value.get("latest_block"), int):
                latest_values.append(value["latest_block"])
            if isinstance(value.get("safe_latest_block"), int):
                safe_values.append(value["safe_latest_block"])
    return (max(latest_values) if latest_values else None, max(safe_values) if safe_values else None)


def score_distribution(scores: list[int]) -> list[dict[str, Any]]:
    bins = [
        ("0-9", 0, 9),
        ("10-19", 10, 19),
        ("20-29", 20, 29),
        ("30-39", 30, 39),
        ("40-49", 40, 49),
        ("50-59", 50, 59),
        ("60-69", 60, 69),
        ("70-79", 70, 79),
        ("80-89", 80, 89),
        ("90-99", 90, 99),
        ("100", 100, 100),
    ]
    result = []
    for label, lower, upper in bins:
        result.append({"bin": label, "count": sum(1 for score in scores if lower <= score <= upper)})
    return result


def category_mix(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for project in projects:
        if project.get("tier") not in {"medium", "high"}:
            continue
        category = str(project.get("category") or "contract_app")
        counts[category] = counts.get(category, 0) + 1
    return [{"category": key, "count": value} for key, value in sorted(counts.items(), key=lambda item: item[1], reverse=True)]


def pool_events(source_by_chain: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    totals: dict[str, int] = {
        "Uniswap V4": 0,
        "Uniswap V3": 0,
        "Uniswap V2": 0,
        "Balancer V2": 0,
        "Pancake V3": 0,
    }
    for chain, values in source_by_chain.items():
        totals["Uniswap V4"] += int(values.get("uniswap_v4_initialize", 0)) + int(values.get("uniswap_v4_hook", 0))
        v3_count = int(values.get("uniswap_v3_pool_created", 0))
        if chain == "bsc":
            totals["Pancake V3"] += v3_count
        else:
            totals["Uniswap V3"] += v3_count
        totals["Uniswap V2"] += int(values.get("uniswap_v2_pair_created", 0))
        totals["Balancer V2"] += int(values.get("balancer_v2_tokens_registered", 0))
    colors = {
        "Uniswap V4": "var(--accent)",
        "Uniswap V3": "oklch(0.55 0.13 245)",
        "Uniswap V2": "oklch(0.55 0.10 270)",
        "Balancer V2": "oklch(0.72 0.14 85)",
        "Pancake V3": "oklch(0.55 0.04 260)",
    }
    return [{"protocol": key, "count": value, "color": colors[key]} for key, value in totals.items()]


def cycle_duration_distribution(cycles: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
    buckets = [
        ("<30s", 0, 29),
        ("30-60", 30, 60),
        ("60-90", 61, 90),
        ("90-120", 91, 120),
        ("120-180", 121, 180),
        ("180-300", 181, 300),
        (">300", 301, 10**9),
    ]
    seconds = [max(0, int((end - start).total_seconds())) for start, end in cycles]
    return [
        {"bin": label, "count": sum(1 for value in seconds if lower <= value <= upper)}
        for label, lower, upper in buckets
    ]


def aggregate_queue_buckets(queues: list[dict[str, Any]]) -> dict[str, int]:
    buckets = {
        "uniswap_v4": 0,
        "other_dex": 0,
        "activity": 0,
        "mint": 0,
        "direct_creation": 0,
        "internal_only": 0,
        "other": 0,
    }
    for queue in queues:
        pending = queue.get("pending_by_bucket") if isinstance(queue.get("pending_by_bucket"), dict) else {}
        for key in buckets:
            item = pending.get(key) if isinstance(pending.get(key), dict) else {}
            buckets[key] += int(item.get("contracts") or 0)
    buckets["total"] = sum(buckets.values())
    return buckets


def mask_secret(value: str, reveal: bool = False) -> str:
    text = str(value or "")
    if reveal or not text:
        return text
    suffix = text[-4:] if len(text) >= 4 else "set"
    return f"{SECRET_MASK}{suffix}"


def is_masked_secret(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.startswith(SECRET_MASK)


def bool_env(value: Any) -> str:
    return "1" if bool(value) else "0"


def env_text(value: Any) -> str:
    if isinstance(value, bool):
        return bool_env(value)
    if value is None:
        return ""
    return str(value)


def settings_defaults(workspace: Path, reveal_secrets: bool = False) -> dict[str, Any]:
    per_chain = {}
    for chain in DASHBOARD_CHAINS:
        settings = Settings.from_env(workspace, chain=chain)
        per_chain[chain] = {
            "enabled": True,
            "intervalSeconds": settings.interval_seconds,
            "confirmations": settings.confirmations,
            "lookbackBlocks": settings.lookback_blocks,
            "maxBlocksPerCycle": settings.max_blocks_per_cycle,
            "discoveryMode": settings.discovery_mode,
            "activityMinObservations": settings.activity_min_observations,
            "includeAllTransfers": bool(settings.activity_include_all_transfers),
            "enrichLimitPerCycle": settings.enrich_limit_per_cycle,
            "newContractMaxAgeBlocks": settings.new_contract_max_age_blocks,
            "customEventTopics": settings.activity_custom_event_topics,
        }
    eth_settings = Settings.from_env(workspace, chain="ethereum")
    etherscan_key = eth_settings.etherscan_api_key
    opentwitter_key = eth_settings.opentwitter_api_key
    return {
        "apiKeys": {
            "etherscan": mask_secret(etherscan_key, reveal=reveal_secrets),
            "etherscanBase": eth_settings.etherscan_api_base,
            "opentwitter": mask_secret(opentwitter_key, reveal=reveal_secrets),
        },
        "apiKeyStatus": {
            "etherscanConfigured": bool(etherscan_key),
            "opentwitterConfigured": bool(opentwitter_key),
            "secretsRevealed": bool(reveal_secrets),
        },
        "global": {
            "etherscanRequestsPerSecond": round(1 / eth_settings.etherscan_min_interval_seconds, 2)
            if eth_settings.etherscan_min_interval_seconds
            else 0,
            "twitterMaxResults": eth_settings.twitter_max_results,
            "enableTwitter": bool(eth_settings.opentwitter_api_key),
            "dailySnapshotEnabled": bool(eth_settings.daily_snapshot_enabled),
            "runtimeStaleMinutes": eth_settings.runtime_stale_minutes,
            "coverageWindowBlocks": eth_settings.coverage_window_blocks,
        },
        "perChain": per_chain,
    }


def save_dashboard_settings(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, str] = {}
    api_keys = payload.get("apiKeys") if isinstance(payload.get("apiKeys"), dict) else {}
    global_settings = payload.get("global") if isinstance(payload.get("global"), dict) else {}
    per_chain = payload.get("perChain") if isinstance(payload.get("perChain"), dict) else {}

    etherscan_key = str(api_keys.get("etherscan") or "").strip()
    if etherscan_key and not is_masked_secret(etherscan_key):
        updates["ETHERSCAN_API_KEY"] = etherscan_key
    etherscan_base = str(api_keys.get("etherscanBase") or "").strip()
    if etherscan_base:
        updates["ETHERSCAN_API_BASE"] = etherscan_base
    opentwitter_key = str(api_keys.get("opentwitter") or "").strip()
    if opentwitter_key and not is_masked_secret(opentwitter_key):
        updates["OPENTWITTER_API_KEY"] = opentwitter_key

    global_map = {
        "twitterMaxResults": "ALPHA_TWITTER_MAX_RESULTS",
        "dailySnapshotEnabled": "ALPHA_DAILY_SNAPSHOT_ENABLED",
        "runtimeStaleMinutes": "ALPHA_RUNTIME_STALE_MINUTES",
        "coverageWindowBlocks": "ALPHA_COVERAGE_WINDOW_BLOCKS",
    }
    if "etherscanRequestsPerSecond" in global_settings:
        updates["ALPHA_ETHERSCAN_REQUESTS_PER_SECOND"] = env_text(global_settings.get("etherscanRequestsPerSecond"))
    for source_key, env_key in global_map.items():
        if source_key in global_settings:
            updates[env_key] = env_text(global_settings.get(source_key))

    enrich_limit_saved = False
    for chain, values in per_chain.items():
        if chain not in CHAIN_ENV_PREFIX or not isinstance(values, dict):
            continue
        prefix = CHAIN_ENV_PREFIX[chain]
        for source_key, suffix in PER_CHAIN_ENV_FIELDS.items():
            if source_key not in values:
                continue
            updates[f"ALPHA_{prefix}_{suffix}"] = env_text(values.get(source_key))
        if "enrichLimitPerCycle" in values and not enrich_limit_saved:
            updates["ALPHA_ENRICH_LIMIT_PER_CYCLE"] = env_text(values.get("enrichLimitPerCycle"))
            enrich_limit_saved = True

    write_env_updates(workspace / ".env", updates)
    for key, value in updates.items():
        os.environ[key] = value
    return {
        "settings": settings_defaults(workspace, reveal_secrets=False),
        "savedKeys": sorted(updates),
        "savedAt": utc_now(),
    }


def test_dashboard_connection(workspace: Path, service: str) -> dict[str, Any]:
    settings = Settings.from_env(workspace, chain="ethereum")
    service = service.strip().lower()
    if service == "etherscan":
        if not settings.etherscan_api_key:
            raise RuntimeError("ETHERSCAN_API_KEY is not configured")
        client = EtherscanClient(
            settings.etherscan_api_base,
            settings.etherscan_api_key,
            chainid=settings.chainid,
            timeout=min(settings.request_timeout_seconds, 10),
            min_interval_seconds=0,
        )
        block = client.latest_block_number()
        return {"ok": True, "service": service, "message": f"latest block {block}"}
    if service == "opentwitter":
        if not settings.opentwitter_api_key:
            raise RuntimeError("OPENTWITTER_API_KEY or TWITTER_TOKEN is not configured")
        client = OpenTwitterClient(
            settings.opentwitter_api_base,
            settings.opentwitter_api_key,
            timeout=min(settings.request_timeout_seconds, 10),
        )
        response = client.search("ethereum", max_results=1, product="Latest")
        count = len(normalize_search_items(response, limit=1))
        return {"ok": True, "service": service, "message": f"search ok, {count} item(s)"}
    raise RuntimeError(f"Unsupported service: {service}")


def write_env_updates(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_lines: list[str] = []
    seen: set[str] = set()
    key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in lines:
        match = key_re.match(line)
        if not match:
            updated_lines.append(line)
            continue
        key = match.group(1)
        if key in updates:
            updated_lines.append(f"{key}={quote_env_value(updates[key])}")
            seen.add(key)
        else:
            updated_lines.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append("# Dashboard settings")
        for key in missing:
            updated_lines.append(f"{key}={quote_env_value(updates[key])}")
    path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")


def quote_env_value(value: str) -> str:
    text = str(value)
    if text == "":
        return ""
    if re.search(r"\s|#|\"|'|`", text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def is_loopback_client(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost"}


def render_dashboard_js(payload: dict[str, Any]) -> str:
    sections = [
        ("DASHBOARD_TIMESERIES", payload["timeseries"]),
        ("DASHBOARD_SCORE_DIST", payload["score_dist"]),
        ("DASHBOARD_CATEGORY_MIX", payload["category_mix"]),
        ("DASHBOARD_HEATMAP", payload["heatmap"]),
        ("DASHBOARD_POOL_EVENTS", payload["pool_events"]),
        ("DASHBOARD_CYCLE_DURATIONS", payload["cycle_durations"]),
        ("DASHBOARD_SOURCE_TRENDS", payload["source_trends"]),
        ("DASHBOARD_SOURCE_BY_CHAIN", payload["source_by_chain"]),
        ("DASHBOARD_CHAINS", payload["chains"]),
        ("DASHBOARD_META", payload["meta"]),
        ("DASHBOARD_PROJECTS", payload["projects"]),
        ("DASHBOARD_SETTINGS_DEFAULTS", payload["settings"]),
    ]
    lines = [
        "// Generated by `python -m alpha_listener.cli dashboard`.",
        "// Do not edit by hand; source data comes from local SQLite/report state.",
        "",
    ]
    for name, value in sections:
        lines.append(f"window.{name} = {json.dumps(value, ensure_ascii=False, indent=2)};")
        lines.append("")
    return "\n".join(lines)


def serve_dashboard(
    workspace: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    limit: int = 100,
    refresh_seconds: int = 15,
) -> None:
    workspace = workspace.resolve()
    directory = dashboard_dir(workspace)
    last_refresh = {"at": 0.0}

    def refresh(force: bool = False) -> None:
        now = time.monotonic()
        if force or now - last_refresh["at"] >= max(1, int(refresh_seconds or 1)):
            write_dashboard_data(workspace, limit=limit, output=directory / DATA_JS_NAME)
            last_refresh["at"] = now

    refresh(force=True)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            if parsed.path == "/api/settings/test":
                if not is_loopback_client(str(self.client_address[0])):
                    self.send_json({"error": "Connection tests are only available from localhost."}, status=403)
                    return
                query = parse_qs(parsed.query)
                service = str(query.get("service", [""])[0])
                try:
                    self.send_json(test_dashboard_connection(workspace, service))
                except Exception as exc:
                    self.send_json({"ok": False, "service": service, "error": str(exc)}, status=400)
                return
            if parsed.path == "/api/settings":
                query = parse_qs(parsed.query)
                reveal_requested = str(query.get("reveal", ["0"])[0]).lower() in {"1", "true", "yes"}
                reveal = reveal_requested and is_loopback_client(str(self.client_address[0]))
                self.send_json(settings_defaults(workspace, reveal_secrets=reveal))
                return
            if parsed.path in {"/", "/index.html", f"/{DATA_JS_NAME}"}:
                refresh()
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            if parsed.path != "/api/settings":
                self.send_error(404, "Not found")
                return
            if not is_loopback_client(str(self.client_address[0])):
                self.send_json({"error": "Settings writes are only available from localhost."}, status=403)
                return
            try:
                size = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(min(size, 2_000_000))
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                result = save_dashboard_settings(workspace, payload)
                refresh(force=True)
            except Exception as exc:  # pragma: no cover - exercised through manual API checks
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result)

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self) -> None:
            if self.path.endswith(DATA_JS_NAME):
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[dashboard] {self.address_string()} - {format % args}", flush=True)

    server = ThreadingHTTPServer((host, int(port)), Handler)
    actual_host, actual_port = server.server_address
    print(f"dashboard serving http://{actual_host}:{actual_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("dashboard stopped by keyboard interrupt", flush=True)
    finally:
        server.server_close()


def last_24_hour_labels() -> list[str]:
    start = hour_floor(datetime.now(timezone.utc)) - timedelta(hours=23)
    return [(start + timedelta(hours=i)).strftime("%H") for i in range(24)]


def hour_bucket(value: Any, start: datetime) -> int | None:
    dt = parse_dt(value)
    if not dt:
        return None
    delta_hours = int((hour_floor(dt) - start).total_seconds() // 3600)
    if 0 <= delta_hours < 24:
        return delta_hours
    return None


def parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hour_floor(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def merge_hourly(target: list[int], source: list[int]) -> None:
    for index, value in enumerate(source[: len(target)]):
        target[index] += int(value or 0)


def merge_heatmap(target: list[list[int]], source: list[list[int]]) -> None:
    for day_index, row in enumerate(source[: len(target)]):
        for hour_index, value in enumerate(row[: len(target[day_index])]):
            target[day_index][hour_index] += int(value or 0)


def merge_source_trends(target: dict[str, list[int]], source: dict[str, list[int]]) -> None:
    for key, values in source.items():
        target.setdefault(key, [0] * 24)
        merge_hourly(target[key], values)


def compact_time(value: Any) -> str:
    dt = parse_dt(value)
    return dt.strftime("%H:%M:%S") if dt else str(value or "")


def short_address(value: str) -> str:
    text = str(value or "")
    if len(text) <= 12:
        return text
    return f"{text[:6]}...{text[-4:]}"


def normalize_twitter(value: Any) -> str | None:
    text = str(value or "").strip().lstrip("@")
    return text or None


def normalize_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return text
    return f"https://{text}"


def tier_rank(value: Any) -> int:
    return {"low": 0, "watch": 1, "medium": 2, "high": 3}.get(str(value or ""), -1)


def empty_summary() -> dict[str, Any]:
    return {
        "last_scanned_block": None,
        "contracts": 0,
        "enriched": 0,
        "processing_enrichment": 0,
        "pending_enrichment": 0,
        "medium_or_high": 0,
        "by_source": {},
        "by_tier": {},
        "by_status": {},
        "observations": 0,
        "observations_by_source": {},
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
