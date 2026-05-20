from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .chains import SUPPORTED_CHAIN_CHOICES
from .classify import classify_contract, source_url_candidates
from .config import Settings
from .discovery import (
    collect_log_candidates_by_block,
    discover_activity_contracts_in_range,
    discover_contract_creations,
    discover_internal_contract_creations,
    discover_internal_contract_creations_from_traces,
    discover_log_candidates,
    discover_log_candidates_from_raw,
    parse_block_number,
    select_log_candidates,
)
from .enrichment import enrich_contract, source_website_candidates, source_website_matches_contract
from .etherscan import EtherscanClient, hex_to_int
from .opentwitter import OpenTwitterClient, normalize_user
from .project_meta import augment_project_metadata
from .scoring import dex_liquidity_source, official_account_context, score_project, third_party_profile_identity_only
from .signals import infrastructure_contract_name
from .storage import (
    CREATION_ONLY_SOURCES,
    INFRASTRUCTURE_SKIP_REASON,
    LOW_SURFACE_SKIP_REASON,
    PROCESSING_STALE_MINUTES,
    Store,
)
from .website import verify_website

DAILY_SNAPSHOT_META_KEY = "last_daily_snapshot_label"
REVIEW_DECISIONS = ("confirmed", "watchlist", "needs_review", "reject")
BACKGROUND_PID_FILES = {
    "scanner": "alpha-listener.pid",
    "classifier": "alpha-classifier.pid",
    "enricher": "alpha-enricher.pid",
}
BACKGROUND_ERR_LOG_FILES = {
    "scanner": "alpha-listener.err.log",
    "classifier": "alpha-classifier.err.log",
    "enricher": "alpha-enricher.err.log",
}
WEBSITE_CHECK_REFRESH_SECONDS = 24 * 60 * 60
RUNTIME_FINGERPRINT_PATTERNS = ("src/alpha_listener/**/*.py", "scripts/*.ps1")
RUNTIME_FINGERPRINT_REQUIRED_ROLES = {"scanner", "classifier", "enricher", "combined"}
EXPORT_FIELDS = [
    "project_key",
    "label",
    "score",
    "tier",
    "project_category",
    "project_positioning",
    "project_description",
    "score_breakdown",
    "support_flags",
    "review_hints",
    "review_decision",
    "review_note",
    "reviewed_at",
    "primary_address",
    "addresses",
    "address_count",
    "twitter_account",
    "twitter_url",
    "website",
    "observation_sources",
    "observation_count",
    "first_block",
    "last_block",
    "names",
    "symbols",
    "official_identity_reason",
    "official_website_reason",
    "tweet_count",
    "address_mentions",
    "credible_address_mentions",
    "signal_address_mentions",
    "evidence_summary",
    "sample_tweets",
    "top_authors",
    "source_website_candidates",
    "website_check_status",
    "website_check_http_status",
    "website_check_title",
    "website_check_matched_terms",
    "website_check_twitter_links",
    "website_check_summary",
    "etherscan_url",
    "review_command",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="On-Chain Alpha Radar")
    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="Run one scan/enrichment cycle")
    add_common_args(once)

    run = sub.add_parser("run", help="Run continuously")
    add_common_args(run)
    run.add_argument("--interval-seconds", type=int, default=None)

    dashboard = sub.add_parser("dashboard", help="Generate and serve the local frontend dashboard")
    dashboard.add_argument("--workspace", type=Path, default=Path.cwd())
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--limit", type=int, default=100, help="Maximum project groups to include")
    dashboard.add_argument("--refresh-seconds", type=int, default=15, help="Minimum data refresh interval while serving")
    dashboard.add_argument("--no-serve", action="store_true", help="Only regenerate frontend/dashboard/data.js")

    status = sub.add_parser("status", help="Print local persistence summary")
    add_workspace_chain_args(status)

    queue = sub.add_parser("queue", help="Print enrichment queue backlog by signal bucket")
    add_workspace_chain_args(queue)

    skip_low = sub.add_parser(
        "skip-low-surface",
        help="Mark pending creation-only low-surface contracts as low without OpenTwitter lookup",
    )
    add_workspace_chain_args(skip_low)
    skip_low.add_argument("--limit", type=int, default=100)
    skip_low.add_argument("--dry-run", action="store_true")

    classify_backlog = sub.add_parser(
        "classify-backlog",
        help="Classify deferred contracts with Etherscan and safely skip low-surface rows",
    )
    add_workspace_chain_args(classify_backlog)
    classify_backlog.add_argument("--limit", type=int, default=100)
    classify_backlog.add_argument("--dry-run", action="store_true")

    source_urls = sub.add_parser(
        "backfill-source-urls",
        help="Refresh verified-source URL metadata for older classified contracts and requeue matching website evidence",
    )
    add_workspace_chain_args(source_urls)
    source_urls.add_argument("--limit", type=int, default=100)
    source_urls.add_argument("--dry-run", action="store_true")
    source_urls.add_argument("--force", action="store_true", help="Recheck rows that already have source.urls state")

    health = sub.add_parser("health", help="Print live scanner health against current chain head")
    add_workspace_chain_args(health)
    health.add_argument("--confirmations", type=int, default=None)

    coverage = sub.add_parser("coverage", help="Check recent scanned-block coverage and optionally rewind gaps")
    add_workspace_chain_args(coverage)
    coverage.add_argument("--window-blocks", type=int, default=None)
    coverage.add_argument("--repair", action="store_true", help="Rewind last_scanned_block to before the first gap")

    audit = sub.add_parser("audit", help="Summarize end-to-end readiness and evidence gaps")
    add_workspace_chain_args(audit)
    audit.add_argument("--confirmations", type=int, default=None)
    audit.add_argument("--coverage-window-blocks", type=int, default=None)
    audit.add_argument("--max-lag-blocks", type=int, default=None)
    audit.add_argument("--runtime-stale-minutes", type=int, default=None)
    audit.add_argument("--min-alpha-candidates", type=int, default=1)
    audit.add_argument("--limit", type=int, default=20, help="Maximum unreviewed/top candidate examples to include")
    audit.add_argument("--no-live", action="store_true", help="Skip live Etherscan head check")

    export = sub.add_parser("export", help="Export grouped project candidates for manual review")
    add_workspace_chain_args(export)
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--output", type=Path, default=None)
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--tier", action="append", choices=["low", "watch", "medium", "high"], default=None)
    export.add_argument("--include-reviewed", action="store_true", help="Include projects that already have review rows")

    review_pack = sub.add_parser("review-pack", help="Write a Markdown brief for manual project review")
    add_workspace_chain_args(review_pack)
    review_pack.add_argument("--output", type=Path, default=None)
    review_pack.add_argument("--limit", type=int, default=50)
    review_pack.add_argument("--tier", action="append", choices=["low", "watch", "medium", "high"], default=None)
    review_pack.add_argument("--include-reviewed", action="store_true", help="Include projects that already have review rows")

    verify_websites = sub.add_parser("verify-websites", help="Fetch and persist live checks for candidate official websites")
    add_workspace_chain_args(verify_websites)
    verify_websites.add_argument("--limit", type=int, default=50)
    verify_websites.add_argument("--tier", action="append", choices=["low", "watch", "medium", "high"], default=None)
    verify_websites.add_argument("--include-reviewed", action="store_true", help="Include projects that already have review rows")
    verify_websites.add_argument("--output", type=Path, default=None, help="Optional JSON output path for the check batch")

    website_twitter = sub.add_parser(
        "backfill-website-twitter",
        help="Use verified website X links to backfill missing official Twitter accounts",
    )
    add_workspace_chain_args(website_twitter)
    website_twitter.add_argument("--limit", type=int, default=50)
    website_twitter.add_argument("--tier", action="append", choices=["low", "watch", "medium", "high"], default=None)
    website_twitter.add_argument("--include-reviewed", action="store_true", help="Include projects that already have review rows")
    website_twitter.add_argument("--dry-run", action="store_true")

    review_import = sub.add_parser("review-import", help="Import review decisions from an exported CSV or JSON sheet")
    add_workspace_chain_args(review_import)
    review_import.add_argument("--input", type=Path, required=True, help="CSV/JSON file produced by export, with decisions filled in")
    review_import.add_argument("--reviewer", default="operator")
    review_import.add_argument("--dry-run", action="store_true")

    report = sub.add_parser("report", help="Write a Markdown report of scored candidates")
    add_workspace_chain_args(report)
    report.add_argument("--limit", type=int, default=20)
    report.add_argument("--output", type=Path, default=None)
    report.add_argument("--snapshot", action="store_true", help="Write a timestamped report under data/reports")
    report.add_argument("--snapshot-label", default=None, help="Optional timestamp/label for --snapshot output")

    recover = sub.add_parser("recover-processing", help="Move processing enrichment reservations back to retry")
    add_workspace_chain_args(recover)
    recover.add_argument("--older-than-minutes", type=int, default=0)

    requeue = sub.add_parser("requeue", help="Move completed enrichments back to retry for recalibration")
    add_workspace_chain_args(requeue)
    requeue.add_argument("--address", action="append", default=None, help="Requeue a specific contract address")
    requeue.add_argument("--tier", action="append", choices=["low", "watch", "medium", "high"], default=None)
    requeue.add_argument("--limit", type=int, default=None)

    rescore = sub.add_parser("rescore", help="Recompute scores from stored evidence without new OpenTwitter calls")
    add_workspace_chain_args(rescore)
    rescore.add_argument("--tier", action="append", choices=["low", "watch", "medium", "high"], default=None)
    rescore.add_argument("--limit", type=int, default=None)

    review = sub.add_parser("review", help="List or mark project-level review decisions")
    add_workspace_chain_args(review)
    review.add_argument("--limit", type=int, default=20)
    review.add_argument("--tier", action="append", choices=["low", "watch", "medium", "high"], default=None)
    review.add_argument("--unreviewed", action="store_true", help="Only list projects without a review decision")
    review.add_argument("--project-key", default=None, help="Project key to review, such as twitter:blockies_eth")
    review.add_argument("--address", default=None, help="Any contract address belonging to the project to review")
    review.add_argument("--decision", choices=REVIEW_DECISIONS, default=None)
    review.add_argument("--reviewer", default="operator")
    review.add_argument("--note", default="")

    return parser


def add_common_args(parser: argparse.ArgumentParser) -> None:
    add_workspace_chain_args(parser)
    parser.add_argument("--start-block", type=int, default=None, help="Explicit replay start block")
    parser.add_argument("--end-block", type=int, default=None, help="Explicit replay end block")
    parser.add_argument("--lookback-blocks", type=int, default=None)
    parser.add_argument("--max-blocks", type=int, default=None)
    parser.add_argument("--discovery-mode", choices=["block", "activity"], default=None)
    parser.add_argument("--enrich-limit", type=int, default=None)
    parser.add_argument("--force-enrich", action="store_true", help="Re-run enrichment for existing contracts")
    parser.add_argument("--confirmations", type=int, default=None)
    parser.add_argument("--no-twitter", action="store_true", help="Scan only; skip OpenTwitter enrichment")
    parser.add_argument("--report-limit", type=int, default=None, help="Refresh latest report after cycles with changes")
    parser.add_argument("--report-every-enriched", type=int, default=None, help="Refresh report every N enriched contracts")
    parser.add_argument(
        "--verify-websites-limit",
        type=int,
        default=None,
        help="Verify up to N stale or unchecked high/medium websites after an enrichment cycle",
    )
    parser.add_argument(
        "--backfill-website-twitter-limit",
        type=int,
        default=None,
        help="Backfill up to N official Twitter accounts from verified website X links after an enrichment cycle",
    )
    parser.add_argument(
        "--backfill-source-urls-limit",
        type=int,
        default=None,
        help="Refresh up to N verified-source URL metadata rows after an enrichment cycle",
    )


def add_workspace_chain_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--chain",
        choices=SUPPORTED_CHAIN_CHOICES,
        default=None,
        help="Chain profile to use: ethereum, base, or bsc/bnb",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = args.workspace.resolve()
    settings = Settings.from_env(workspace, chain=getattr(args, "chain", None))

    if args.command == "dashboard":
        from .dashboard import dashboard_dir, serve_dashboard, write_dashboard_data

        data_path = write_dashboard_data(workspace, limit=max(1, int(args.limit or 100)))
        if args.no_serve:
            print_json(
                {
                    "dashboard_data": str(data_path),
                    "dashboard_index": str(dashboard_dir(workspace) / "index.html"),
                }
            )
            return 0
        serve_dashboard(
            workspace,
            host=str(args.host or "127.0.0.1"),
            port=int(args.port or 8765),
            limit=max(1, int(args.limit or 100)),
            refresh_seconds=max(1, int(args.refresh_seconds or 15)),
        )
        return 0

    if args.command == "status":
        store = Store(settings.db_path, settings.data_dir)
        try:
            print_json(store.summary())
        finally:
            store.close()
        return 0

    if args.command == "queue":
        store = Store(settings.db_path, settings.data_dir)
        try:
            print_json(store.queue_health())
        finally:
            store.close()
        return 0

    if args.command == "skip-low-surface":
        store = Store(settings.db_path, settings.data_dir)
        try:
            limit = max(0, int(args.limit or 0))
            if args.dry_run:
                rows = store.low_surface_backlog_candidates(limit)
                print_json(
                    {
                        "dry_run": True,
                        "matched": len(rows),
                        "limit": limit,
                        "skip_reason": LOW_SURFACE_SKIP_REASON,
                        "eligible_sources": sorted(CREATION_ONLY_SOURCES),
                        "examples": rows[:20],
                        "remaining_low_surface_pending": store.low_surface_backlog_count(),
                    }
                )
            else:
                result = store.skip_low_surface_backlog(limit)
                result["dry_run"] = False
                result["skip_reason"] = LOW_SURFACE_SKIP_REASON
                result["eligible_sources"] = sorted(CREATION_ONLY_SOURCES)
                print_json(result)
        finally:
            store.close()
        return 0

    if args.command == "classify-backlog":
        settings.validate(require_twitter=False)
        print_json(handle_classify_backlog(settings, args))
        return 0

    if args.command == "backfill-source-urls":
        settings.validate(require_twitter=False)
        print_json(handle_backfill_source_urls(settings, args))
        return 0

    if args.command == "health":
        settings.validate(require_twitter=False)
        print_json(health_check(settings, args))
        return 0

    if args.command == "coverage":
        store = Store(settings.db_path, settings.data_dir)
        try:
            window = args.window_blocks if args.window_blocks is not None else settings.coverage_window_blocks
            result = store.repair_scan_coverage(window) if args.repair else store.scan_coverage(window)
        finally:
            store.close()
        print_json(result)
        return 0

    if args.command == "audit":
        print_json(handle_audit(settings, args))
        return 0

    if args.command == "export":
        print_json(handle_export(settings, args))
        return 0

    if args.command == "review-pack":
        print_json(handle_review_pack(settings, args))
        return 0

    if args.command == "verify-websites":
        print_json(handle_verify_websites(settings, args))
        return 0

    if args.command == "backfill-website-twitter":
        settings.validate(require_twitter=True)
        print_json(handle_backfill_website_twitter(settings, args))
        return 0

    if args.command == "review-import":
        try:
            print_json(handle_review_import(settings, args))
            return 0
        except ValueError as exc:
            print(f"review-import failed: {exc}", file=sys.stderr, flush=True)
            return 2

    if args.command == "report":
        report_output = args.output
        if args.snapshot and report_output is None:
            report_output = snapshot_report_path(settings, args.snapshot_label)
        output = write_report(settings, args.limit, report_output)
        print_json({"report": str(output)})
        return 0

    if args.command == "recover-processing":
        print_json(handle_recover_processing(settings, args))
        return 0

    if args.command == "requeue":
        store = Store(settings.db_path, settings.data_dir)
        try:
            requeued = store.requeue_enrichments(args.tier, args.limit, args.address)
        finally:
            store.close()
        print_json({"requeued": requeued, "addresses": args.address or [], "tiers": args.tier or [], "limit": args.limit})
        return 0

    if args.command == "rescore":
        store = Store(settings.db_path, settings.data_dir)
        try:
            rescored = store.rescore_enrichments(args.tier, args.limit)
        finally:
            store.close()
        print_json({"rescored": rescored, "tiers": args.tier or [], "limit": args.limit})
        return 0

    if args.command == "review":
        try:
            print_json(handle_review(settings, args))
            return 0
        except ValueError as exc:
            print(f"review failed: {exc}", file=sys.stderr, flush=True)
            return 2

    settings.validate(require_twitter=not args.no_twitter)

    if args.command == "once":
        result = run_cycle(settings, args)
        maybe_write_cycle_report(settings, args, result)
        print_json(result)
        return 0

    if args.command == "run":
        interval = args.interval_seconds or settings.interval_seconds
        print(f"alpha-listener started interval={interval}s workspace={workspace}", flush=True)
        while True:
            try:
                result = run_cycle(settings, args, progress=print_json_line)
                maybe_write_cycle_report(settings, args, result, progress=print_json_line)
                print_json(result)
                sleep_seconds = cycle_sleep_seconds(result, interval)
            except KeyboardInterrupt:
                print("alpha-listener stopped by keyboard interrupt", flush=True)
                return 130
            except Exception as exc:
                print(f"cycle failed: {exc}", file=sys.stderr, flush=True)
                sleep_seconds = min(interval, 30)
            time.sleep(sleep_seconds)

    return 1


def handle_review(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, int(args.limit or 20))
    store = Store(settings.db_path, settings.data_dir)
    try:
        if args.decision:
            group = resolve_review_project(store, args.project_key, args.address)
            if not group:
                raise ValueError("provide an existing --project-key or --address")
            review = store.add_project_review(
                str(group["project_key"]),
                args.decision,
                str(args.reviewer or "operator"),
                str(args.note or ""),
                group,
            )
            return {"reviewed": review, "project": group}

        if args.project_key or args.address:
            group = resolve_review_project(store, args.project_key, args.address)
            return {"limit": 1, "projects": [group] if group else []}

        groups = store.project_groups(limit=max(1000, limit))
        tiers = set(args.tier or [])
        if tiers:
            groups = [group for group in groups if str(group.get("tier") or "") in tiers]
        if args.unreviewed:
            groups = [group for group in groups if not group.get("review")]
        return {"limit": limit, "projects": groups[:limit]}
    finally:
        store.close()


def handle_review_import(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    rows = load_review_import_rows(Path(args.input))
    reviewer = str(args.reviewer or "operator")
    dry_run = bool(getattr(args, "dry_run", False))
    store = Store(settings.db_path, settings.data_dir)
    try:
        prepared = []
        skipped_blank = 0
        invalid = []
        for index, row in enumerate(rows, start=1):
            decision = normalize_review_import_value(
                row.get("review_decision") or row.get("decision")
            )
            if not decision:
                skipped_blank += 1
                continue
            project_key = normalize_review_import_value(row.get("project_key"))
            address = normalize_review_import_value(row.get("primary_address") or row.get("address"))
            note = normalize_review_import_value(row.get("review_note") or row.get("note"))
            if decision not in REVIEW_DECISIONS:
                invalid.append({"row": index, "project_key": project_key, "address": address, "error": "invalid_decision", "decision": decision})
                continue
            if not project_key and not address:
                invalid.append({"row": index, "project_key": project_key, "address": address, "error": "missing_project_key_or_address"})
                continue
            group = resolve_review_project(store, project_key, address)
            if not group:
                invalid.append({"row": index, "project_key": project_key, "address": address, "error": "project_not_found"})
                continue
            prepared.append(
                {
                    "row": index,
                    "project_key": str(group.get("project_key") or project_key),
                    "address": address,
                    "decision": decision,
                    "note": note,
                    "project": group,
                }
            )
        if invalid:
            raise ValueError(json.dumps({"invalid_rows": invalid[:20], "invalid_count": len(invalid)}, ensure_ascii=False))

        reviewed = []
        if not dry_run:
            for item in prepared:
                review = store.add_project_review(
                    item["project_key"],
                    item["decision"],
                    reviewer,
                    item["note"],
                    item["project"],
                )
                reviewed.append(review)
        return {
            "input": str(args.input),
            "dry_run": dry_run,
            "rows": len(rows),
            "skipped_blank_decision": skipped_blank,
            "importable": len(prepared),
            "imported": 0 if dry_run else len(reviewed),
            "reviewer": reviewer,
            "decisions": review_import_decision_counts(prepared),
            "projects": [
                {
                    "row": item["row"],
                    "project_key": item["project_key"],
                    "label": item["project"].get("label"),
                    "decision": item["decision"],
                    "note": item["note"],
                }
                for item in prepared[:20]
            ],
        }
    finally:
        store.close()


def load_review_import_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ValueError(f"input does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("JSON review import must be a list of objects")
        if not all(isinstance(item, dict) for item in parsed):
            raise ValueError("JSON review import contains non-object rows")
        return [dict(item) for item in parsed]

    import csv

    with path.open(encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def normalize_review_import_value(value: Any) -> str:
    return str(value or "").strip()


def review_import_decision_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        decision = str(row.get("decision") or "")
        if decision:
            counts[decision] = counts.get(decision, 0) + 1
    return counts


def handle_export(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, int(args.limit or 100))
    tiers = set(args.tier or ["high", "medium"])
    store = Store(settings.db_path, settings.data_dir)
    try:
        rows = export_candidate_rows(store, tiers, not bool(args.include_reviewed), limit, settings)
    finally:
        store.close()

    output = args.output or default_export_path(settings, args.format)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        import json

        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        import csv

        with output.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in EXPORT_FIELDS})
    return {
        "export": str(output),
        "format": args.format,
        "rows": len(rows),
        "tiers": sorted(tiers),
        "include_reviewed": bool(args.include_reviewed),
    }


def handle_review_pack(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, int(args.limit or 50))
    tiers = set(args.tier or ["high", "medium"])
    include_reviewed = bool(args.include_reviewed)
    store = Store(settings.db_path, settings.data_dir)
    try:
        rows = export_candidate_rows(store, tiers, not include_reviewed, limit, settings)
    finally:
        store.close()

    output = args.output or default_review_pack_path(settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_review_pack(rows, tiers, include_reviewed), encoding="utf-8")
    return {
        "review_pack": str(output),
        "rows": len(rows),
        "tiers": sorted(tiers),
        "include_reviewed": include_reviewed,
    }


def handle_verify_websites(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, int(args.limit or 50))
    tiers = set(args.tier or ["high", "medium"])
    include_reviewed = bool(args.include_reviewed)
    store = Store(settings.db_path, settings.data_dir)
    try:
        groups = store.project_groups(limit=10000)
        if tiers:
            groups = [group for group in groups if str(group.get("tier") or "") in tiers]
        if not include_reviewed:
            groups = [group for group in groups if not group.get("review")]
        checked = verify_website_groups(store, settings, groups, limit, refresh_all=True)
    finally:
        store.close()

    result = {
        "checked": len(checked),
        "tiers": sorted(tiers),
        "include_reviewed": include_reviewed,
        "status_counts": count_website_check_statuses(checked),
        "results": checked[:50],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["output"] = str(args.output)
    return result


def verify_website_groups(
    store: Store,
    settings: Settings,
    groups: list[dict[str, Any]],
    limit: int,
    refresh_all: bool = False,
) -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    targets = website_verification_targets(store, groups, limit, refresh_all=refresh_all)
    for group in targets:
        website = str(group.get("website") or "")
        check = verify_website(
            website,
            str(group.get("label") or ""),
            [str(item) for item in (group.get("names") or []) if item],
            [str(item) for item in (group.get("symbols") or []) if item],
            timeout=settings.request_timeout_seconds,
        )
        record = store.add_website_check(str(group.get("project_key") or ""), website, check)
        rescored = store.apply_website_check_to_enrichments(website, record)
        checked.append(
            {
                "project_key": group.get("project_key"),
                "label": group.get("label"),
                "tier": group.get("tier"),
                "score": group.get("score"),
                "website": website,
                "check": compact_website_check(record),
                "rescored_addresses": rescored,
            }
        )
    return checked


def website_verification_targets(
    store: Store,
    groups: list[dict[str, Any]],
    limit: int,
    refresh_all: bool = False,
) -> list[dict[str, Any]]:
    result = []
    for group in groups:
        website = str(group.get("website") or "").strip()
        if not website:
            continue
        if not refresh_all:
            latest = store.latest_website_check(str(group.get("project_key") or ""), website)
            if not website_check_due(latest):
                continue
        result.append(group)
        if limit > 0 and len(result) >= limit:
            break
    return result


def website_check_due(check: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not check:
        return True
    if str(check.get("status") or "").lower() != "ok":
        return True
    checked_at = str(check.get("checked_at") or "")
    if not checked_at:
        return True
    try:
        parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - parsed).total_seconds() >= WEBSITE_CHECK_REFRESH_SECONDS


def handle_backfill_website_twitter(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, int(args.limit or 50))
    tiers = set(args.tier or ["high", "medium"])
    include_reviewed = bool(args.include_reviewed)
    dry_run = bool(getattr(args, "dry_run", False))
    twitter = None if dry_run else OpenTwitterClient(
        settings.opentwitter_api_base,
        settings.opentwitter_api_key,
        timeout=settings.request_timeout_seconds,
    )
    store = Store(settings.db_path, settings.data_dir)
    try:
        groups = store.project_groups(limit=10000)
        if tiers:
            groups = [group for group in groups if str(group.get("tier") or "") in tiers]
        if not include_reviewed:
            groups = [group for group in groups if not group.get("review")]
        targets = []
        examples = []
        backfilled = 0
        skipped = 0
        errors = 0
        for group in groups:
            if len(targets) >= limit:
                break
            candidate = website_twitter_candidate(store, group)
            if not candidate:
                continue
            targets.append(candidate)
            if dry_run:
                examples.append({**candidate, "action": "would_backfill"})
                continue
            try:
                result = apply_website_twitter_backfill(store, twitter, candidate)
                if result.get("backfilled_addresses"):
                    backfilled += len(result["backfilled_addresses"])
                    examples.append(result)
                else:
                    skipped += 1
                    examples.append(result)
            except Exception as exc:
                errors += 1
                examples.append(
                    {
                        **candidate,
                        "action": "error",
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    }
                )
        return {
            "dry_run": dry_run,
            "matched_projects": len(targets),
            "backfilled_addresses": 0 if dry_run else backfilled,
            "skipped": skipped,
            "errors": errors,
            "tiers": sorted(tiers),
            "include_reviewed": include_reviewed,
            "examples": examples[:50],
        }
    finally:
        store.close()


def website_twitter_candidate(store: Store, group: dict[str, Any]) -> dict[str, Any] | None:
    if group.get("twitter_account") or not group.get("website"):
        return None
    project_key = str(group.get("project_key") or "")
    website = str(group.get("website") or "")
    check = store.latest_website_check(project_key, website)
    if not check or check.get("status") != "ok":
        return None
    handles = website_check_handles(check)
    if len(handles) != 1:
        return None
    return {
        "project_key": project_key,
        "label": group.get("label"),
        "tier": group.get("tier"),
        "score": group.get("score"),
        "website": website,
        "twitter_handle": handles[0],
        "twitter_url": f"https://x.com/{handles[0]}",
        "addresses": [str(address) for address in (group.get("addresses") or []) if address],
        "website_check_id": check.get("id"),
        "website_check_title": check.get("title"),
        "website_check_matched_terms": check.get("matched_terms") or [],
    }


def backfill_website_twitter_groups(
    store: Store,
    twitter: OpenTwitterClient,
    groups: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    results = []
    for group in groups:
        if limit > 0 and len(results) >= limit:
            break
        candidate = website_twitter_candidate(store, group)
        if not candidate:
            continue
        results.append(apply_website_twitter_backfill(store, twitter, candidate))
    return results


def website_check_handles(check: dict[str, Any]) -> list[str]:
    handles = []
    for url in check.get("twitter_links") or []:
        handle = twitter_handle_from_url(str(url))
        if handle:
            handles.append(handle)
    return dedupe_keep_order(handles)


def twitter_handle_from_url(url: str) -> str:
    match = re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#].*)?$", str(url or ""))
    if not match:
        return ""
    handle = match.group(1)
    if handle.lower() in {"home", "intent", "share", "search", "i"}:
        return ""
    return handle


def apply_website_twitter_backfill(
    store: Store,
    twitter: OpenTwitterClient | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    handle = str(candidate.get("twitter_handle") or "")
    profile = {}
    profile_error = ""
    if twitter is not None:
        try:
            profile = normalize_user(twitter.user_info(handle))
        except Exception as exc:
            profile_error = str(exc)
    backfilled_addresses = []
    skipped_addresses = []
    for address in candidate.get("addresses") or []:
        row = store.conn.execute(
            """
            SELECT
              c.raw_json,
              e.query_json,
              e.evidence_json,
              e.status,
              COALESCE(o.observation_count, 0) AS observation_count,
              COALESCE(o.discovery_sources, '') AS observation_sources
            FROM enrichments e
            JOIN contracts c ON c.address = e.address
            LEFT JOIN (
              SELECT address, COUNT(*) AS observation_count, GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ) o ON o.address = e.address
            WHERE e.address = ?
              AND e.status IN ('ok', 'partial')
            """,
            (address,),
        ).fetchone()
        if not row:
            skipped_addresses.append({"address": address, "reason": "missing_completed_enrichment"})
            continue
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        if evidence.get("twitter_account"):
            skipped_addresses.append({"address": address, "reason": "already_has_twitter"})
            continue
        try:
            contract = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            contract = {"address": address}
        if not isinstance(contract, dict):
            contract = {"address": address}
        contract["observation_count"] = row["observation_count"] or 0
        contract["observation_sources"] = row["observation_sources"] or ""
        evidence.update(
            {
                "twitter_account": handle,
                "twitter_name": profile.get("name") or handle,
                "twitter_followers": profile.get("followersCount"),
                "twitter_verified": bool(profile.get("verified")),
                "official_identity_reason": "official_website_link",
                "official_twitter_source": "website_check",
                "official_twitter_url": f"https://x.com/{handle}",
                "website_twitter_links": [f"https://x.com/{handle}"],
            }
        )
        if profile_error:
            evidence["official_twitter_profile_error"] = profile_error
        score = score_project(contract, evidence)
        try:
            queries = json.loads(row["query_json"] or "[]")
        except json.JSONDecodeError:
            queries = []
        store.upsert_enrichment(
            address,
            {
                "queries": queries if isinstance(queries, list) else [],
                "evidence": evidence,
                "score": score,
                "status": row["status"] or "ok",
            },
        )
        backfilled_addresses.append({"address": address, "score": score.get("score"), "tier": score.get("tier")})
    result = {
        **candidate,
        "action": "backfilled" if backfilled_addresses else "skipped",
        "backfilled_addresses": backfilled_addresses,
        "skipped_addresses": skipped_addresses,
        "profile_found": bool(profile),
    }
    if profile_error:
        result["profile_error"] = profile_error
    store.append_event("website_twitter_backfilled", None, result)
    return result


def compact_website_check(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "checked_at": check.get("checked_at"),
        "status": check.get("status"),
        "http_status": check.get("http_status"),
        "final_url": check.get("final_url"),
        "title": check.get("title"),
        "matched_terms": check.get("matched_terms") or [],
        "twitter_links": check.get("twitter_links") or [],
        "error": check.get("error") or "",
        "failure_kind": check.get("failure_kind") or "",
        "fallback_url": check.get("fallback_url") or "",
    }


def count_website_check_statuses(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        check = row.get("check") if isinstance(row.get("check"), dict) else {}
        status = str(check.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def export_candidate_rows(
    store: Store,
    tiers: set[str],
    unreviewed_only: bool,
    limit: int,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    groups = store.project_groups(limit=10000)
    if tiers:
        groups = [group for group in groups if str(group.get("tier") or "") in tiers]
    if unreviewed_only:
        groups = [group for group in groups if not group.get("review")]
    rows = []
    for group in groups[:limit]:
        rows.append(export_project_group_row(store, group, settings))
    return rows


def export_project_group_row(store: Store, group: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    addresses = [str(address) for address in (group.get("addresses") or []) if address]
    evidence = project_group_evidence(store, addresses)
    review = group.get("review") if isinstance(group.get("review"), dict) else {}
    twitter = str(group.get("twitter_account") or "").lstrip("@")
    primary_address = str(group.get("primary_address") or "")
    project_key = str(group.get("project_key") or "")
    score_breakdown = group_score_breakdown(group)
    support_flags = review_support_flags(group, evidence, score_breakdown)
    website = str(group.get("website") or "")
    website_check = store.latest_website_check(project_key, website) if website else None
    hints = review_hints(group, evidence, support_flags, website_check)
    return {
        "project_key": project_key,
        "label": group.get("label") or "",
        "score": group.get("score") if group.get("score") is not None else "",
        "tier": group.get("tier") or "",
        "project_category": group.get("project_category") or "",
        "project_positioning": group.get("project_positioning") or "",
        "project_description": group.get("project_description") or "",
        "score_breakdown": score_breakdown_text(score_breakdown),
        "support_flags": ",".join(support_flags),
        "review_hints": " || ".join(hints),
        "review_decision": review.get("decision") or "",
        "review_note": review.get("note") or "",
        "reviewed_at": review.get("reviewed_at") or "",
        "primary_address": primary_address,
        "addresses": ",".join(addresses),
        "address_count": group.get("address_count") or len(addresses),
        "twitter_account": twitter,
        "twitter_url": f"https://x.com/{twitter}" if twitter else "",
        "website": website,
        "observation_sources": group.get("observation_sources") or "",
        "observation_count": group.get("observation_count") or 0,
        "first_block": group.get("first_block") or "",
        "last_block": group.get("last_block") or "",
        "names": "|".join(str(item) for item in (group.get("names") or []) if item),
        "symbols": "|".join(str(item) for item in (group.get("symbols") or []) if item),
        "official_identity_reason": evidence.get("official_identity_reason") or "",
        "official_website_reason": evidence.get("official_website_reason") or "",
        "tweet_count": evidence.get("tweet_count") or 0,
        "address_mentions": evidence.get("address_mentions") or 0,
        "credible_address_mentions": evidence.get("credible_address_mentions") or 0,
        "signal_address_mentions": evidence.get("signal_address_mentions") or 0,
        "evidence_summary": export_evidence_summary(evidence),
        "sample_tweets": " || ".join(evidence.get("sample_tweets") or []),
        "top_authors": " || ".join(evidence.get("top_authors") or []),
        "source_website_candidates": ",".join(evidence.get("source_website_candidates") or []),
        "website_check_status": (website_check or {}).get("status") or "",
        "website_check_http_status": (website_check or {}).get("http_status") or "",
        "website_check_title": (website_check or {}).get("title") or "",
        "website_check_matched_terms": ",".join((website_check or {}).get("matched_terms") or []),
        "website_check_twitter_links": ",".join((website_check or {}).get("twitter_links") or []),
        "website_check_summary": export_website_check_summary(website_check),
        "etherscan_url": explorer_address_url(settings, primary_address) if primary_address else "",
        "review_command": (
            f"python -m alpha_listener.cli review{review_command_chain_arg(settings)} --project-key {project_key} --decision watchlist --note \"\""
            if project_key
            else ""
        ),
    }


def explorer_address_url(settings: Settings | None, address: str) -> str:
    base = (settings.explorer_base_url if settings else "https://etherscan.io").rstrip("/")
    return f"{base}/address/{address}"


def review_command_chain_arg(settings: Settings | None) -> str:
    if settings is None or getattr(settings, "chain_slug", "ethereum") == "ethereum":
        return ""
    return f" --chain {getattr(settings, 'chain_slug', 'ethereum')}"


def project_group_evidence(store: Store, addresses: list[str]) -> dict[str, Any]:
    if not addresses:
        return {}
    placeholders = ",".join("?" for _ in addresses)
    rows = store.conn.execute(
        f"""
        SELECT evidence_json, tweet_count, address_mentions
        FROM enrichments
        WHERE address IN ({placeholders}) AND status IN ('ok', 'partial')
        """,
        addresses,
    ).fetchall()
    evidence_items = []
    for row in rows:
        try:
            evidence = json_loads_dict(row["evidence_json"])
        except ValueError:
            evidence = {}
        evidence["tweet_count"] = max(int(evidence.get("tweet_count") or 0), int(row["tweet_count"] or 0))
        evidence["address_mentions"] = max(
            int(evidence.get("address_mentions") or 0),
            int(row["address_mentions"] or 0),
        )
        evidence_items.append(evidence)
    source_candidates: list[str] = []
    sample_tweets: list[str] = []
    top_authors: list[str] = []
    for evidence in evidence_items:
        source_candidates.extend(str(url) for url in evidence.get("source_website_candidates") or [] if url)
        sample_tweets.extend(export_sample_tweets(evidence.get("sample_tweets") or []))
        top_authors.extend(export_top_authors(evidence.get("top_authors") or []))
    official_context = first_official_account_context(evidence_items)
    return {
        "twitter_account": first_non_empty(item.get("twitter_account") for item in evidence_items),
        "official_identity_reason": first_non_empty(
            item.get("official_identity_reason") for item in evidence_items
        ),
        "official_twitter_source": first_non_empty(item.get("official_twitter_source") for item in evidence_items),
        "official_website_reason": first_non_empty(item.get("official_website_reason") for item in evidence_items),
        "official_account_address_mentions": official_context.get("address_mentions"),
        "official_account_project_mentions": official_context.get("project_mentions"),
        "official_account_own_project_mentions": official_context.get("own_project_mentions"),
        "official_account_own_crypto_project_mentions": official_context.get("own_crypto_project_mentions"),
        "official_account_flags": official_context.get("flags") or [],
        "tweet_count": sum(int(item.get("tweet_count") or 0) for item in evidence_items),
        "address_mentions": sum(int(item.get("address_mentions") or 0) for item in evidence_items),
        "credible_address_mentions": sum(int(item.get("credible_address_mentions") or 0) for item in evidence_items),
        "signal_address_mentions": sum(int(item.get("signal_address_mentions") or 0) for item in evidence_items),
        "source_website_candidates": dedupe_keep_order(source_candidates)[:5],
        "sample_tweets": dedupe_keep_order(sample_tweets)[:5],
        "top_authors": dedupe_keep_order(top_authors)[:5],
    }


def first_official_account_context(evidence_items: list[dict[str, Any]]) -> dict[str, Any]:
    for evidence in evidence_items:
        context = official_account_context(evidence)
        if context.get("found"):
            return context
    return {}


def export_evidence_summary(evidence: dict[str, Any]) -> str:
    parts = []
    if evidence.get("official_identity_reason"):
        parts.append(f"identity={evidence.get('official_identity_reason')}")
    if evidence.get("official_website_reason"):
        parts.append(f"website={evidence.get('official_website_reason')}")
    parts.append(f"tweets={int(evidence.get('tweet_count') or 0)}")
    parts.append(f"address_mentions={int(evidence.get('address_mentions') or 0)}")
    credible = int(evidence.get("credible_address_mentions") or 0)
    signal = int(evidence.get("signal_address_mentions") or 0)
    if credible:
        parts.append(f"credible={credible}")
    if signal:
        parts.append(f"signal={signal}")
    return "; ".join(parts)


def export_website_check_summary(check: dict[str, Any] | None) -> str:
    if not check:
        return ""
    parts = [f"status={check.get('status') or 'unknown'}"]
    if check.get("http_status") not in (None, ""):
        parts.append(f"http={check.get('http_status')}")
    matched_terms = check.get("matched_terms") or []
    if matched_terms:
        parts.append("matched=" + ",".join(str(item) for item in matched_terms[:3]))
    twitter_links = check.get("twitter_links") or []
    if twitter_links:
        parts.append("twitter_links=" + str(len(twitter_links)))
    if check.get("failure_kind"):
        parts.append(f"failure={check.get('failure_kind')}")
    if check.get("fallback_url"):
        parts.append(f"fallback={check.get('fallback_url')}")
    if check.get("error"):
        parts.append(f"error={compact_export_field(check.get('error'), 120)}")
    return "; ".join(parts)


def group_score_breakdown(group: dict[str, Any]) -> dict[str, Any]:
    breakdown = group.get("score_breakdown")
    if isinstance(breakdown, dict):
        return breakdown
    return {}


def score_breakdown_text(breakdown: dict[str, Any]) -> str:
    if not breakdown:
        return ""
    return json.dumps(breakdown, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def review_support_flags(group: dict[str, Any], evidence: dict[str, Any], breakdown: dict[str, Any]) -> list[str]:
    flags = []
    if group.get("twitter_account"):
        flags.append("official_twitter")
    else:
        flags.append("no_twitter")
    if group.get("website"):
        flags.append("official_website")
    else:
        flags.append("no_website")
    if str(group.get("project_category") or "") == "miner_coin":
        flags.append("miner_coin")
    website_reason = str(evidence.get("official_website_reason") or "")
    if website_reason:
        flags.append(f"website:{website_reason}")
    identity_reason = str(evidence.get("official_identity_reason") or "")
    if identity_reason:
        flags.append(f"identity:{identity_reason}")
    if evidence.get("official_twitter_source") == "website_check":
        flags.append("twitter_from_website")
    credible = int(evidence.get("credible_address_mentions") or 0)
    signal = int(evidence.get("signal_address_mentions") or 0)
    address_mentions = int(evidence.get("address_mentions") or 0)
    if credible > 0:
        flags.append("credible_address_mentions")
    elif signal > 0:
        flags.append("signal_address_mentions")
    elif address_mentions > 0:
        flags.append("address_mentions")
    else:
        flags.append("no_address_mentions")
    sources = {source.strip() for source in str(group.get("observation_sources") or "").split(",") if source.strip()}
    observation_count = int(group.get("observation_count") or 0)
    if observation_count >= 2 and any(dex_liquidity_source(source) or "mint" in source for source in sources):
        flags.append("strong_onchain_surface")
    if int(group.get("address_count") or 0) > 1:
        flags.append("multi_contract_group")
    technical = int(breakdown.get("technical_readability") or 0)
    if technical >= 18:
        flags.append("verified_or_readable_source")
    return flags


def review_hints(
    group: dict[str, Any],
    evidence: dict[str, Any],
    support_flags: list[str],
    website_check: dict[str, Any] | None = None,
) -> list[str]:
    hints = []
    tier = str(group.get("tier") or "")
    score = int(group.get("score") or 0)
    has_twitter = bool(group.get("twitter_account"))
    has_website = bool(group.get("website"))
    address_mentions = int(evidence.get("credible_address_mentions") or evidence.get("address_mentions") or 0)
    website_reason = str(evidence.get("official_website_reason") or "")
    identity_reason = str(evidence.get("official_identity_reason") or "")
    if tier in {"high", "medium"}:
        hints.append("review_decision_required")
    if str(group.get("project_category") or "") == "miner_coin":
        hints.append("miner_coin_verify_mining_mechanics")
    if has_website and not has_twitter:
        hints.append("website_only_verify_project_ownership")
    if has_website:
        website_check_status = str((website_check or {}).get("status") or "")
        if not website_check_status:
            hints.append("website_check_missing")
        elif website_check_status == "warn":
            hints.append("website_check_warn_verify_project_match")
        elif website_check_status == "fail":
            failure_kind = str((website_check or {}).get("failure_kind") or "")
            if failure_kind in {"tls_error", "timeout", "fetch_error"}:
                hints.append(f"website_check_{failure_kind}")
            elif failure_kind == "http_error":
                hints.append("website_check_http_error")
            else:
                hints.append("website_check_failed_verify_project_match")
    if "twitter_from_website" in support_flags:
        hints.append("official_twitter_from_verified_website")
    if has_twitter and not has_website and address_mentions == 0:
        hints.append("twitter_only_verify_contract_or_launch_link")
    if website_reason == "verified_source_url":
        hints.append("website_from_verified_source_metadata")
    if identity_reason and address_mentions == 0:
        hints.append("official_identity_without_contract_address_mention")
    if third_party_profile_identity_only(evidence):
        hints.append("official_twitter_mentioned_by_third_party_only")
    if "strong_onchain_surface" not in support_flags:
        hints.append("weak_onchain_surface")
    if score >= 80 and "no_address_mentions" in support_flags and "no_website" in support_flags:
        hints.append("high_score_depends_on_social_identity")
    return dedupe_keep_order(hints)


def export_sample_tweets(tweets: list[Any]) -> list[str]:
    samples = []
    for tweet in tweets:
        if not isinstance(tweet, dict):
            continue
        username = str(tweet.get("userScreenName") or "").strip()
        text = compact_export_field(tweet.get("text") or "", 220)
        if not text:
            continue
        prefix = f"@{username}: " if username else ""
        samples.append(f"{prefix}{text}")
    return samples


def export_top_authors(authors: list[Any]) -> list[str]:
    result = []
    for author in authors:
        if not isinstance(author, dict):
            continue
        username = str(author.get("username") or "").strip()
        if not username:
            continue
        details = []
        if author.get("score") is not None:
            details.append(f"score={author.get('score')}")
        if author.get("address_mentions") is not None:
            details.append(f"addr={author.get('address_mentions')}")
        flags = [str(flag) for flag in (author.get("flags") or []) if flag]
        if flags:
            details.append("flags=" + ",".join(flags[:3]))
        suffix = f" ({'; '.join(details)})" if details else ""
        result.append(f"@{username}{suffix}")
    return result


def compact_export_field(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def default_export_path(settings: Settings, export_format: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "json" if export_format == "json" else "csv"
    return settings.data_dir / "exports" / f"alpha-candidates-{stamp}.{suffix}"


def default_review_pack_path(settings: Settings) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return settings.data_dir / "exports" / f"alpha-review-pack-{stamp}.md"


def render_review_pack(rows: list[dict[str, Any]], tiers: set[str], include_reviewed: bool) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    review_scope = "reviewed and unreviewed" if include_reviewed else "unreviewed"
    tier_scope = ", ".join(sorted(tiers)) if tiers else "all tiers"
    lines = [
        "# Alpha Candidate Review Pack",
        "",
        f"- Generated: {generated_at}",
        f"- Scope: {review_scope} {tier_scope} project groups",
        f"- Rows: {len(rows)}",
        "",
    ]
    if not rows:
        lines.append("No matching project groups.")
        lines.append("")
        return "\n".join(lines)

    for index, row in enumerate(rows, start=1):
        lines.extend(render_review_pack_row(index, row))
    return "\n".join(lines)


def render_review_pack_row(index: int, row: dict[str, Any]) -> list[str]:
    label = compact_export_field(row.get("label") or row.get("project_key") or "Unknown project", 120)
    tier = row.get("tier") or ""
    score = row.get("score") if row.get("score") not in (None, "") else "n/a"
    primary_address = str(row.get("primary_address") or "")
    review_command = str(row.get("review_command") or "").strip()
    lines = [
        f"## {index}. {label} ({tier}, score {score})",
        "",
        f"- Project key: {markdown_code(row.get('project_key'))}",
        f"- Primary address: {markdown_link(primary_address, row.get('etherscan_url')) if primary_address else 'n/a'}",
        f"- Addresses: {markdown_join_code(split_export_values(row.get('addresses'), ','))}",
        f"- Links: {review_pack_links(row)}",
        f"- Category: {markdown_code(row.get('project_category'))}",
        f"- Positioning: {row.get('project_positioning') or 'n/a'}",
        f"- Description: {row.get('project_description') or 'n/a'}",
        f"- Blocks: {row.get('first_block') or 'n/a'} to {row.get('last_block') or 'n/a'}",
        f"- Observations: {row.get('observation_sources') or 'n/a'} ({row.get('observation_count') or 0})",
        f"- Names: {markdown_join_code(split_export_values(row.get('names'), '|'))}",
        f"- Symbols: {markdown_join_code(split_export_values(row.get('symbols'), '|'))}",
        f"- Score breakdown: {row.get('score_breakdown') or 'n/a'}",
        f"- Support flags: {markdown_join_code(split_export_values(row.get('support_flags'), ','))}",
        f"- Review hints: {markdown_join_code(split_export_values(row.get('review_hints'), ' || '))}",
        f"- Evidence: {row.get('evidence_summary') or 'n/a'}",
        f"- Identity reason: {markdown_code(row.get('official_identity_reason'))}",
        f"- Website reason: {markdown_code(row.get('official_website_reason'))}",
        (
            "- Counts: "
            f"tweets={row.get('tweet_count') or 0}; "
            f"address_mentions={row.get('address_mentions') or 0}; "
            f"credible={row.get('credible_address_mentions') or 0}; "
            f"signal={row.get('signal_address_mentions') or 0}"
        ),
        f"- Source website candidates: {markdown_join_links(split_export_values(row.get('source_website_candidates'), ','))}",
        f"- Website check: {row.get('website_check_summary') or 'not checked'}",
    ]
    if row.get("website_check_title"):
        lines.append(f"- Website title: {compact_export_field(row.get('website_check_title'), 220)}")
    website_twitter_links = split_export_values(row.get("website_check_twitter_links"), ",")
    if website_twitter_links:
        lines.append(f"- Website X links: {markdown_join_links(website_twitter_links)}")
    lines.extend(render_review_pack_list("Top authors", split_export_values(row.get("top_authors"), " || ")))
    lines.extend(render_review_pack_list("Sample tweets", split_export_values(row.get("sample_tweets"), " || ")))
    if review_command:
        lines.extend(["- Review command:", ""])
        lines.extend(["```powershell", '$env:PYTHONPATH = "$PWD\\src"', review_command, "```"])
    lines.append("")
    return lines


def review_pack_links(row: dict[str, Any]) -> str:
    links = []
    twitter = row.get("twitter_account")
    twitter_url = row.get("twitter_url")
    if twitter and twitter_url:
        links.append(markdown_link(f"X @{twitter}", twitter_url))
    website = row.get("website")
    if website:
        links.append(markdown_link(str(website), website))
    return " | ".join(links) if links else "n/a"


def render_review_pack_list(title: str, values: list[str]) -> list[str]:
    if not values:
        return [f"- {title}: n/a"]
    lines = [f"- {title}:"]
    for value in values[:5]:
        lines.append(f"  - {compact_export_field(value, 260)}")
    return lines


def split_export_values(value: Any, separator: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(separator) if item.strip()]


def markdown_code(value: Any) -> str:
    text = compact_export_field(value, 220).replace("`", "'")
    return f"`{text}`" if text else "n/a"


def markdown_join_code(values: list[str]) -> str:
    return ", ".join(markdown_code(value) for value in values) if values else "n/a"


def markdown_link(label: Any, url: Any) -> str:
    label_text = compact_export_field(label, 140).replace("[", "\\[").replace("]", "\\]")
    url_text = str(url or "").strip()
    return f"[{label_text}]({url_text})" if label_text and url_text else (label_text or "n/a")


def markdown_join_links(values: list[str]) -> str:
    return ", ".join(markdown_link(value, value) for value in values) if values else "n/a"


def first_non_empty(values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def handle_recover_processing(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    older_than_minutes = max(0, int(getattr(args, "older_than_minutes", 0) or 0))
    store = Store(settings.db_path, settings.data_dir)
    try:
        before = store.queue_health()
        recovered = store.reset_processing_enrichments(older_than_minutes)
        after = store.queue_health()
        result = {
            "recovered_processing": recovered,
            "older_than_minutes": older_than_minutes,
            "queue_before": {
                "processing": before.get("processing"),
                "stale_processing": before.get("stale_processing"),
                "pending_total": before.get("pending_total"),
                "retry": before.get("retry"),
            },
            "queue_after": {
                "processing": after.get("processing"),
                "stale_processing": after.get("stale_processing"),
                "pending_total": after.get("pending_total"),
                "retry": after.get("retry"),
            },
        }
        if recovered > 0:
            store.append_event(
                "stale_processing_recovered",
                None,
                {
                    "event": "stale_processing_recovered",
                    "command": "recover-processing",
                    "recovered": recovered,
                    "older_than_minutes": older_than_minutes,
                    "queue_before": result["queue_before"],
                    "queue_after": result["queue_after"],
                },
            )
        return result
    finally:
        store.close()


def handle_classify_backlog(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(0, int(getattr(args, "limit", 100) or 0))
    store = Store(settings.db_path, settings.data_dir)
    try:
        if getattr(args, "dry_run", False):
            rows = store.classification_deferred_backlog_candidates(limit)
            return {
                "dry_run": True,
                "matched": len(rows),
                "limit": limit,
                "examples": rows[:20],
                "remaining_classification_deferred_pending": store.classification_deferred_backlog_count(),
            }

        store.record_cycle_started(
            {
                "command": "classify-backlog",
                "chain": getattr(settings, "chain_slug", "ethereum"),
                "chainid": settings.chainid,
                "chain_name": getattr(settings, "chain_name", "Ethereum"),
                "limit": limit,
                "dry_run": False,
                "code_fingerprint": runtime_code_fingerprint(settings.workspace),
            },
            role="classifier",
        )
        try:
            etherscan = EtherscanClient(
                settings.etherscan_api_base,
                settings.etherscan_api_key,
                chainid=settings.chainid,
                timeout=settings.request_timeout_seconds,
                min_interval_seconds=getattr(settings, "etherscan_min_interval_seconds", 0.0),
            )
            recover_stale_processing_for_cycle(store, "classifier")
            targets = store.reserve_classification_targets(limit)
            store.record_cycle_progress(
                {"event": "classification_reserved", "reserved": len(targets), "limit": limit},
                role="classifier",
            )
            completed_addresses: set[str] = set()
            release_addresses: list[str] = []
            classified = 0
            skipped = 0
            needs_offchain = 0
            classification_errors = 0
            examples: list[dict[str, Any]] = []
            try:
                for target in targets:
                    address = target["address"]
                    payload = with_chain_metadata(contract_payload(target), settings)
                    classified_payload = ensure_classified(etherscan, store, payload)
                    if classified_payload.get("classification_error"):
                        classification_errors += 1
                        release_addresses.append(address)
                        store.append_event(
                            "classification_failed",
                            address,
                            {
                                "error": classified_payload.get("classification_error"),
                                "command": "classify-backlog",
                            },
                        )
                        if len(examples) < 20:
                            examples.append({"address": address, "action": "retry", "reason": "classification_error"})
                        store.record_cycle_progress(
                            {
                                "event": "classification_failed",
                                "address": address,
                                "processed": classified + classification_errors,
                                "classified": classified,
                                "classification_errors": classification_errors,
                            },
                            role="classifier",
                        )
                        continue

                    current_contract = store.enrichment_context(address) or classified_payload
                    contract_for_scoring = with_chain_metadata(contract_payload(current_contract), settings)
                    contract_for_scoring["observation_count"] = current_contract.get("observation_count") or 0
                    contract_for_scoring["observation_sources"] = (
                        current_contract.get("observation_sources") or current_contract.get("discovery_source") or ""
                    )
                    classified += 1
                    skip_reason = offchain_skip_reason(contract_for_scoring)
                    if skip_reason:
                        enrichment = build_skip_enrichment(contract_for_scoring, skip_reason)
                        store.upsert_enrichment(address, enrichment)
                        store.append_event("contract_enriched", address, enrichment)
                        completed_addresses.add(address)
                        skipped += 1
                        if len(examples) < 20:
                            score = enrichment.get("score") or {}
                            examples.append(
                                {
                                    "address": address,
                                    "action": "skipped",
                                    "skip_reason": skip_reason,
                                    "score": score.get("score"),
                                    "tier": score.get("tier"),
                                }
                            )
                        store.record_cycle_progress(
                            {
                                "event": "classification_skipped_low_surface",
                                "address": address,
                                "processed": classified + classification_errors,
                                "classified": classified,
                                "skipped_low_surface": skipped,
                                "needs_offchain_enrichment": needs_offchain,
                                "classification_errors": classification_errors,
                            },
                            role="classifier",
                        )
                    else:
                        release_addresses.append(address)
                        needs_offchain += 1
                        if len(examples) < 20:
                            examples.append(
                                {
                                    "address": address,
                                    "action": "retry",
                                    "reason": "needs_offchain_enrichment",
                                    "kind": contract_for_scoring.get("kind"),
                                    "name": contract_for_scoring.get("name"),
                                    "symbol": contract_for_scoring.get("symbol"),
                                    "contract_name": contract_for_scoring.get("contract_name"),
                                    "verified": bool(contract_for_scoring.get("verified")),
                                }
                            )
                        store.record_cycle_progress(
                            {
                                "event": "classification_released_to_retry",
                                "address": address,
                                "processed": classified + classification_errors,
                                "classified": classified,
                                "skipped_low_surface": skipped,
                                "needs_offchain_enrichment": needs_offchain,
                                "classification_errors": classification_errors,
                            },
                            role="classifier",
                        )
            except Exception:
                pending_release = [target["address"] for target in targets if target["address"] not in completed_addresses]
                store.release_enrichment_targets(pending_release)
                raise

            released = store.release_enrichment_targets(release_addresses)
            queue = store.queue_health()
            result = {
                "dry_run": False,
                "limit": limit,
                "reserved": len(targets),
                "classified": classified,
                "skipped_low_surface": skipped,
                "needs_offchain_enrichment": needs_offchain,
                "classification_errors": classification_errors,
                "released_to_retry": released,
                "examples": examples,
                "remaining_classification_deferred_pending": queue.get("classification_deferred_pending"),
                "queue": queue,
            }
            store.record_cycle_finished("ok", compact_classify_backlog_result(result), role="classifier")
            return result
        except Exception as exc:
            store.record_cycle_finished(
                "failed",
                {
                    "command": "classify-backlog",
                    "limit": limit,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
                role="classifier",
            )
            raise
    finally:
        store.close()


def compact_classify_backlog_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: result.get(key)
        for key in (
            "limit",
            "reserved",
            "classified",
            "skipped_low_surface",
            "needs_offchain_enrichment",
            "classification_errors",
            "released_to_retry",
            "remaining_classification_deferred_pending",
        )
    }
    queue = result.get("queue")
    if isinstance(queue, dict):
        compact["queue"] = {
            key: queue.get(key)
            for key in (
                "pending_total",
                "retry",
                "processing",
                "stale_processing",
                "classification_deferred_pending",
                "low_surface_pending",
            )
        }
    return compact


def handle_backfill_source_urls(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(0, int(getattr(args, "limit", 100) or 0))
    store = Store(settings.db_path, settings.data_dir)
    try:
        force = bool(getattr(args, "force", False))
        if getattr(args, "dry_run", False):
            rows = store.source_url_backfill_candidates(limit, force=force)
            return {
                "dry_run": True,
                "force": force,
                "matched": len(rows),
                "limit": limit,
                "examples": rows[:20],
                "remaining_source_url_backfill": store.source_url_backfill_count(force=force),
            }

        etherscan = EtherscanClient(
            settings.etherscan_api_base,
            settings.etherscan_api_key,
            chainid=settings.chainid,
            timeout=settings.request_timeout_seconds,
            min_interval_seconds=getattr(settings, "etherscan_min_interval_seconds", 0.0),
        )
        return backfill_source_urls(store, etherscan, limit=limit, force=force)
    finally:
        store.close()


def backfill_source_urls(
    store: Store,
    etherscan: EtherscanClient,
    limit: int,
    force: bool = False,
) -> dict[str, Any]:
    targets = store.source_url_backfill_candidates(limit, force=force)
    refreshed = 0
    with_source_urls = 0
    matched_source_websites = 0
    requeued = 0
    source_errors = 0
    examples: list[dict[str, Any]] = []
    for target in targets:
        address = str(target["address"])
        try:
            source = etherscan.get_source_code(address) or {}
            source_code = source.get("SourceCode") or ""
            if not isinstance(source_code, str) or not source_code:
                source_errors += 1
                if len(examples) < 20:
                    examples.append({"address": address, "action": "skip", "reason": "missing_source_code"})
                continue
            source_metadata = {
                "compiler": source.get("CompilerVersion") or "",
                "optimization_used": source.get("OptimizationUsed") or "",
                "license": source.get("LicenseType") or "",
                "urls": source_url_candidates(source_code),
            }
            updated = store.update_contract_source_metadata(address, source_metadata) or target
            refreshed += 1
            candidates = source_website_candidates(updated)
            matched = [url for url in candidates if source_website_matches_contract(url, updated)]
            if candidates:
                with_source_urls += 1
            if matched:
                matched_source_websites += 1
            was_requeued = bool(matched and store.requeue_enrichment_for_source_url(address))
            if was_requeued:
                requeued += 1
            if len(examples) < 20:
                examples.append(
                    {
                        "address": address,
                        "action": "requeued" if was_requeued else "metadata_updated",
                        "source_urls": candidates[:5],
                        "matched_source_websites": matched[:5],
                        "enrichment_status": target.get("enrichment_status"),
                        "tier": target.get("tier"),
                    }
                )
        except Exception as exc:
            source_errors += 1
            store.append_event(
                "source_url_backfill_failed",
                address,
                {
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
            )
            if len(examples) < 20:
                examples.append({"address": address, "action": "error", "error": str(exc)})

    result = {
        "dry_run": False,
        "force": force,
        "limit": limit,
        "matched": len(targets),
        "refreshed": refreshed,
        "with_source_urls": with_source_urls,
        "matched_source_websites": matched_source_websites,
        "requeued": requeued,
        "source_errors": source_errors,
        "examples": examples,
        "remaining_source_url_backfill": store.source_url_backfill_count(force=force),
        "queue": store.queue_health(),
    }
    store.append_event("source_url_backfill_completed", None, compact_source_url_backfill_result(result))
    return result


def compact_source_url_backfill_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "force",
            "limit",
            "matched",
            "refreshed",
            "with_source_urls",
            "matched_source_websites",
            "requeued",
            "source_errors",
            "remaining_source_url_backfill",
        )
    }


def handle_audit(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, int(getattr(args, "limit", 20) or 20))
    min_alpha_candidates = max(0, int(getattr(args, "min_alpha_candidates", 1) or 0))
    coverage_window = (
        args.coverage_window_blocks
        if getattr(args, "coverage_window_blocks", None) is not None
        else settings.coverage_window_blocks
    )
    max_lag_blocks = (
        int(args.max_lag_blocks)
        if getattr(args, "max_lag_blocks", None) is not None
        else settings.max_blocks_per_cycle * 4
    )
    runtime_stale_minutes = (
        int(args.runtime_stale_minutes)
        if getattr(args, "runtime_stale_minutes", None) is not None
        else settings.runtime_stale_minutes
    )

    store = Store(settings.db_path, settings.data_dir)
    try:
        summary = store.summary()
        queue = store.queue_health()
        coverage = store.scan_coverage(coverage_window)
        candidates = audit_candidate_metrics(store)
        groups = audit_project_group_metrics(store, limit)
        website_checks = audit_website_check_metrics(store, limit)
        website_twitter_backfill = audit_website_twitter_backfill_metrics(store, limit)
        runtime = store.runtime_status()
    finally:
        store.close()

    reports = audit_report_files(settings)
    discovery = audit_discovery_sources(summary)
    runtime_detail = runtime_health_detail(
        runtime,
        runtime_stale_minutes,
        current_code_fingerprint=runtime_code_fingerprint(settings.workspace),
    )
    checks: dict[str, dict[str, Any]] = {}

    add_audit_check(
        checks,
        "credentials",
        "ok" if settings.etherscan_api_key and settings.opentwitter_api_key else "fail",
        {
            "chain": getattr(settings, "chain_slug", "ethereum"),
            "chainid": settings.chainid,
            "chain_name": getattr(settings, "chain_name", "Ethereum"),
            "data_dir": str(settings.data_dir),
            "etherscan_configured": bool(settings.etherscan_api_key),
            "opentwitter_configured": bool(settings.opentwitter_api_key),
        },
    )
    add_audit_check(
        checks,
        "persistence",
        "ok" if summary.get("contracts", 0) > 0 and summary.get("observations", 0) > 0 else "fail",
        {"db_path": str(settings.db_path), "contracts": summary.get("contracts"), "observations": summary.get("observations")},
    )
    add_audit_check(
        checks,
        "discovery",
        "ok" if discovery["active_categories"] >= 3 else "warn",
        discovery,
    )
    add_audit_check(
        checks,
        "coverage",
        "ok" if coverage.get("status") == "ok" else "fail",
        coverage,
    )
    queue_detail = audit_queue_detail(queue, runtime_detail)
    add_audit_check(checks, "queue", queue_detail["status"], queue_detail)
    add_audit_check(
        checks,
        "enrichment",
        "ok" if int(summary.get("enriched") or 0) > 0 else "fail",
        {
            "enriched": summary.get("enriched"),
            "pending": summary.get("pending_enrichment"),
            "processing": summary.get("processing_enrichment"),
            "by_status": summary.get("by_status"),
        },
    )
    add_audit_check(
        checks,
        "scoring",
        "ok" if candidates["medium_or_high"] >= min_alpha_candidates else "warn",
        {"min_alpha_candidates": min_alpha_candidates, **candidates},
    )
    add_audit_check(
        checks,
        "official_evidence",
        "fail"
        if candidates["medium_high_without_official_evidence"] > 0
        else ("ok" if candidates["medium_high_with_twitter"] + candidates["medium_high_with_website"] > 0 else "warn"),
        {
            "medium_high_with_twitter": candidates["medium_high_with_twitter"],
            "medium_high_with_website": candidates["medium_high_with_website"],
            "medium_high_with_address_mentions": candidates["medium_high_with_address_mentions"],
            "medium_high_without_official_evidence": candidates["medium_high_without_official_evidence"],
            "medium_high_without_official_evidence_examples": candidates[
                "medium_high_without_official_evidence_examples"
            ],
        },
    )
    add_audit_check(
        checks,
        "website_checks",
        "warn"
        if website_checks["medium_high_websites_unchecked"] > 0 or website_checks["medium_high_websites_failed"] > 0
        else "ok",
        website_checks,
    )
    add_audit_check(
        checks,
        "website_twitter_backfill",
        "warn" if website_twitter_backfill["matched_projects"] > 0 else "ok",
        website_twitter_backfill,
    )
    add_audit_check(
        checks,
        "reports",
        "ok" if reports["latest_report_exists"] and reports["latest_project_groups_count"] > 0 else "warn",
        reports,
    )
    add_audit_check(
        checks,
        "reviews",
        "ok" if groups["medium_or_high_groups"] == 0 or groups["unreviewed_medium_or_high"] == 0 else "warn",
        groups,
    )
    if (
        runtime_detail["failed_roles"]
        or runtime_detail["stale_running_roles"]
        or runtime_detail["stale_code_fingerprint_roles"]
        or runtime_detail["missing_code_fingerprint_roles"]
    ):
        runtime_check_status = "fail"
    elif runtime_detail["healthy_roles"]:
        runtime_check_status = "ok"
    else:
        runtime_check_status = "warn"
    add_audit_check(checks, "runtime", runtime_check_status, runtime_detail)

    background_processes = audit_background_processes(settings)
    if (
        background_processes["invalid_roles"]
        or background_processes["dead_roles"]
        or background_processes["mismatched_roles"]
    ):
        background_status = "fail"
    elif background_processes["configured"] and (
        background_processes["missing_roles"] or background_processes["unknown_identity_roles"]
    ):
        background_status = "warn"
    elif background_processes["configured"]:
        background_status = "ok"
    else:
        background_status = "skipped"
    add_audit_check(checks, "background_processes", background_status, background_processes)

    background_logs = audit_background_logs(settings)
    if background_logs["nonempty_roles"] or background_logs["missing_roles"]:
        background_log_status = "warn"
    elif background_logs["configured"]:
        background_log_status = "ok"
    else:
        background_log_status = "skipped"
    add_audit_check(checks, "background_logs", background_log_status, background_logs)

    live_health: dict[str, Any] | None = None
    if getattr(args, "no_live", False):
        add_audit_check(checks, "live_health", "skipped", {"reason": "--no-live"})
    elif not settings.etherscan_api_key:
        add_audit_check(checks, "live_health", "fail", {"reason": "missing_etherscan_api_key"})
    else:
        try:
            live_health = health_check(settings, args)
            lag = live_health.get("lag_blocks")
            live_status = "ok"
            if isinstance(lag, int) and lag > max_lag_blocks:
                live_status = "fail"
            add_audit_check(
                checks,
                "live_health",
                live_status,
                {
                    "status": live_health.get("status"),
                    "latest_block": live_health.get("latest_block"),
                    "safe_latest_block": live_health.get("safe_latest_block"),
                    "lag_blocks": lag,
                    "max_lag_blocks": max_lag_blocks,
                },
            )
        except Exception as exc:
            add_audit_check(checks, "live_health", "fail", {"error": str(exc)})

    issues = [
        {"check": name, "status": check["status"], "detail": check["detail"]}
        for name, check in checks.items()
        if check["status"] in {"fail", "warn"}
    ]
    if any(check["status"] == "fail" for check in checks.values()):
        status = "fail"
    elif any(check["status"] == "warn" for check in checks.values()):
        status = "warn"
    else:
        status = "ok"

    result: dict[str, Any] = {
        "status": status,
        "chain": getattr(settings, "chain_slug", "ethereum"),
        "chainid": settings.chainid,
        "chain_name": getattr(settings, "chain_name", "Ethereum"),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
        "issues": issues,
        "summary": summary,
        "coverage": coverage,
        "queue": queue,
        "candidate_metrics": candidates,
        "project_review_metrics": groups,
        "website_check_metrics": website_checks,
        "website_twitter_backfill_metrics": website_twitter_backfill,
        "report_files": reports,
        "discovery_sources": discovery,
        "runtime": runtime,
    }
    if live_health is not None:
        result["live_health"] = live_health
    return result


def audit_queue_detail(queue: dict[str, Any], runtime_detail: dict[str, Any]) -> dict[str, Any]:
    detail = dict(queue)
    stale_processing = int(queue.get("stale_processing") or 0)
    recovery_roles = [
        role
        for role in runtime_detail.get("healthy_roles", [])
        if role in {"classifier", "enricher"}
    ]
    recoverable = stale_processing > 0 and bool(recovery_roles)
    detail["stale_processing_recoverable"] = recoverable
    detail["stale_processing_recovery_roles"] = recovery_roles
    detail["status"] = "fail" if stale_processing > 0 and not recoverable else "ok"
    return detail


def add_audit_check(checks: dict[str, dict[str, Any]], name: str, status: str, detail: Any) -> None:
    checks[name] = {"status": status, "detail": detail}


def runtime_code_fingerprint(workspace: Path) -> dict[str, Any]:
    root = Path(workspace)
    files: list[Path] = []
    for pattern in RUNTIME_FINGERPRINT_PATTERNS:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    unique_files = sorted({path.resolve() for path in files}, key=lambda path: str(path).lower())
    digest = hashlib.sha256()
    for path in unique_files:
        try:
            relative = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            relative = str(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return {
        "algorithm": "sha256",
        "digest": digest.hexdigest(),
        "files": len(unique_files),
        "patterns": list(RUNTIME_FINGERPRINT_PATTERNS),
    }


def runtime_health_detail(
    runtime: dict[str, Any],
    stale_minutes: int,
    current_code_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    roles = runtime.get("roles") if isinstance(runtime.get("roles"), dict) else {}
    threshold_minutes = max(1, int(stale_minutes or 1))
    now = datetime.now(timezone.utc)
    failed_roles = []
    stale_running_roles = []
    missing_code_fingerprint_roles = []
    stale_code_fingerprint_roles = []
    healthy_roles = []
    role_details: dict[str, dict[str, Any]] = {}
    current_digest = str((current_code_fingerprint or {}).get("digest") or "")
    for role, state in roles.items():
        if not isinstance(state, dict):
            continue
        status = str(state.get("last_cycle_status") or "")
        started_at = str(state.get("last_cycle_started_at") or "")
        progressed_at = str(state.get("last_cycle_progressed_at") or "")
        active_at = progressed_at if status == "running" and progressed_at else started_at
        age_seconds = runtime_age_seconds(active_at, now) if active_at else None
        stale = status == "running" and age_seconds is not None and age_seconds > threshold_minutes * 60
        context = state.get("last_cycle_context") if isinstance(state.get("last_cycle_context"), dict) else {}
        role_code_fingerprint = context.get("code_fingerprint") if isinstance(context, dict) else None
        role_digest = (
            str(role_code_fingerprint.get("digest") or "")
            if isinstance(role_code_fingerprint, dict)
            else ""
        )
        fingerprint_required = str(role) in RUNTIME_FINGERPRINT_REQUIRED_ROLES and status in {"ok", "running"}
        code_fingerprint_missing = fingerprint_required and not role_digest
        code_fingerprint_stale = fingerprint_required and bool(current_digest and role_digest and role_digest != current_digest)
        role_details[str(role)] = {
            **state,
            "last_active_at": active_at,
            "running_age_seconds": age_seconds,
            "stale": stale,
            "code_fingerprint_required": fingerprint_required,
            "code_fingerprint_missing": code_fingerprint_missing,
            "code_fingerprint_stale": code_fingerprint_stale,
        }
        if status == "failed":
            failed_roles.append(str(role))
        elif stale:
            stale_running_roles.append(str(role))
        elif status in {"ok", "running"} and not code_fingerprint_missing and not code_fingerprint_stale:
            healthy_roles.append(str(role))
        if code_fingerprint_missing:
            missing_code_fingerprint_roles.append(str(role))
        if code_fingerprint_stale:
            stale_code_fingerprint_roles.append(str(role))
    return {
        **runtime,
        "runtime_stale_minutes": threshold_minutes,
        "checked_at": now.isoformat(timespec="seconds"),
        "current_code_fingerprint": current_code_fingerprint or {},
        "roles": role_details,
        "failed_roles": failed_roles,
        "stale_running_roles": stale_running_roles,
        "missing_code_fingerprint_roles": missing_code_fingerprint_roles,
        "stale_code_fingerprint_roles": stale_code_fingerprint_roles,
        "healthy_roles": healthy_roles,
    }


def runtime_age_seconds(value: str, now: datetime) -> int | None:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return max(0, int((now - timestamp.astimezone(timezone.utc)).total_seconds()))


def audit_background_processes(settings: Settings) -> dict[str, Any]:
    roles: dict[str, dict[str, Any]] = {}
    missing_roles = []
    invalid_roles = []
    dead_roles = []
    mismatched_roles = []
    unknown_identity_roles = []
    configured = False
    for role, filename in BACKGROUND_PID_FILES.items():
        pid_file = settings.data_dir / filename
        detail: dict[str, Any] = {
            "pid_file": str(pid_file),
            "pid_file_exists": pid_file.exists(),
        }
        if not pid_file.exists():
            missing_roles.append(role)
            roles[role] = detail
            continue

        configured = True
        raw_pid = pid_file.read_text(encoding="utf-8", errors="ignore").strip()
        detail["pid_text"] = raw_pid
        try:
            pid = int(raw_pid)
        except ValueError:
            detail["pid"] = None
            detail["running"] = False
            invalid_roles.append(role)
            roles[role] = detail
            continue

        detail["pid"] = pid
        detail["running"] = process_exists(pid)
        if not detail["running"]:
            dead_roles.append(role)
        else:
            command_line = process_command_line(pid)
            detail["command_line"] = command_line
            identity = background_process_identity(role, command_line, settings.workspace)
            detail["identity"] = identity
            if identity["status"] == "mismatch":
                mismatched_roles.append(role)
            elif identity["status"] == "unknown":
                unknown_identity_roles.append(role)
        roles[role] = detail
    return {
        "configured": configured,
        "expected_roles": list(BACKGROUND_PID_FILES),
        "roles": roles,
        "missing_roles": missing_roles,
        "invalid_roles": invalid_roles,
        "dead_roles": dead_roles,
        "mismatched_roles": mismatched_roles,
        "unknown_identity_roles": unknown_identity_roles,
    }


def audit_background_logs(settings: Settings) -> dict[str, Any]:
    logs_dir = settings.workspace / "logs"
    roles: dict[str, dict[str, Any]] = {}
    configured = False
    nonempty_roles = []
    missing_roles = []
    pid_configured = any((settings.data_dir / filename).exists() for filename in BACKGROUND_PID_FILES.values())

    for role, filename in BACKGROUND_ERR_LOG_FILES.items():
        log_path = logs_dir / filename
        detail: dict[str, Any] = {
            "log_file": str(log_path),
            "exists": log_path.exists(),
        }
        if not log_path.exists():
            detail["bytes"] = 0
            if pid_configured:
                missing_roles.append(role)
            roles[role] = detail
            continue

        configured = True
        stat = log_path.stat()
        detail["bytes"] = stat.st_size
        detail["last_modified"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
        if stat.st_size > 0:
            nonempty_roles.append(role)
        roles[role] = detail

    return {
        "configured": configured,
        "logs_dir": str(logs_dir),
        "roles": roles,
        "missing_roles": missing_roles,
        "nonempty_roles": nonempty_roles,
    }


def background_process_identity(role: str, command_line: str | None, workspace: Path) -> dict[str, Any]:
    if not command_line:
        return {
            "status": "unknown",
            "reason": "command_line_unavailable",
            "role_matched": None,
            "workspace_matched": None,
        }

    text = command_line.casefold()
    workspace_text = str(workspace).casefold()
    workspace_posix = workspace.as_posix().casefold()
    workspace_matched = workspace_text in text or workspace_posix in text
    role_matched = background_role_matches(role, text)
    status = "ok" if role_matched and workspace_matched else "mismatch"
    return {
        "status": status,
        "role_matched": role_matched,
        "workspace_matched": workspace_matched,
        "expected": expected_background_role_description(role),
    }


def expected_background_role_description(role: str) -> str:
    if role == "classifier":
        return "run-classifier-loop.ps1 or alpha_listener.cli classify-backlog"
    if role == "enricher":
        return "run-listener.ps1 or alpha_listener.cli run with --max-blocks 0"
    if role == "scanner":
        return "run-listener.ps1 or alpha_listener.cli run without --max-blocks 0"
    return role


def background_role_matches(role: str, command_text: str) -> bool:
    max_blocks_zero = command_has_zero_arg(command_text, "--max-blocks")
    if role == "classifier":
        return "run-classifier-loop.ps1" in command_text or "alpha_listener.cli classify-backlog" in command_text
    if role == "enricher":
        return ("run-listener.ps1" in command_text or "alpha_listener.cli run" in command_text) and max_blocks_zero
    if role == "scanner":
        return ("run-listener.ps1" in command_text or "alpha_listener.cli run" in command_text) and not max_blocks_zero
    return False


def command_has_zero_arg(command_text: str, option: str) -> bool:
    option_pattern = re.escape(option)
    return bool(re.search(rf"{option_pattern}(?:=|\s+)['\"]?0['\"]?(?:\s|$)", command_text))


def process_command_line(pid: int) -> str | None:
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return windows_process_command_line(pid)

    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc_cmdline.read_bytes()
    except OSError:
        raw = b""
    if raw:
        return " ".join(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part)

    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    return output or None


def windows_process_command_line(pid: int) -> str | None:
    command = (
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = %d\"; "
        "if ($p) { $p.CommandLine }"
    ) % pid
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    return output or None


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
        except ImportError:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def audit_discovery_sources(summary: dict[str, Any]) -> dict[str, Any]:
    observations = summary.get("observations_by_source") or {}
    if not isinstance(observations, dict):
        observations = {}
    categories = {
        "direct_creation": int(observations.get("contract_creation") or 0),
        "internal_creation": int(observations.get("internal_create") or 0)
        + int(observations.get("internal_create2") or 0),
        "transfer_mints": int(observations.get("mint_transfer") or 0)
        + int(observations.get("erc1155_transfer_single_mint") or 0)
        + int(observations.get("erc1155_transfer_batch_mint") or 0),
        "activity": int(observations.get("activity_transfer") or 0)
        + int(observations.get("activity_erc1155") or 0),
        "dex_launches": int(observations.get("uniswap_v2_pair_created") or 0)
        + int(observations.get("uniswap_v3_pool_created") or 0)
        + int(observations.get("uniswap_v4_initialize") or 0)
        + int(observations.get("balancer_v2_tokens_registered") or 0),
        "uniswap_v4": int(observations.get("uniswap_v4_initialize") or 0)
        + int(observations.get("uniswap_v4_hook") or 0),
        "balancer_v2": int(observations.get("balancer_v2_tokens_registered") or 0),
    }
    return {
        "categories": categories,
        "active_categories": sum(1 for value in categories.values() if value > 0),
        "observations_by_source": observations,
    }


def audit_candidate_metrics(store: Store) -> dict[str, Any]:
    rows = store.conn.execute(
        """
        SELECT
          e.address, e.score, e.tier, e.twitter_account, e.website, e.address_mentions,
          e.tweet_count, e.evidence_json, c.name, c.symbol, c.contract_name
        FROM enrichments e
        JOIN contracts c ON c.address = e.address
        WHERE e.status IN ('ok', 'partial')
        """
    ).fetchall()
    by_tier: dict[str, int] = {}
    medium_high = []
    for row in rows:
        item = dict(row)
        tier = str(item.get("tier") or "unknown")
        by_tier[tier] = by_tier.get(tier, 0) + 1
        try:
            evidence = json_loads_dict(item.get("evidence_json"))
        except ValueError:
            evidence = {}
        item["official_identity_reason"] = evidence.get("official_identity_reason")
        item["official_website_reason"] = evidence.get("official_website_reason")
        if tier in {"medium", "high"}:
            medium_high.append(item)

    medium_high.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    without_official_evidence = [
        item for item in medium_high if not item.get("twitter_account") and not item.get("website")
    ]
    return {
        "enriched": len(rows),
        "by_tier": by_tier,
        "medium_or_high": len(medium_high),
        "high": by_tier.get("high", 0),
        "medium": by_tier.get("medium", 0),
        "medium_high_with_twitter": sum(1 for item in medium_high if item.get("twitter_account")),
        "medium_high_with_website": sum(1 for item in medium_high if item.get("website")),
        "medium_high_with_address_mentions": sum(1 for item in medium_high if int(item.get("address_mentions") or 0) > 0),
        "medium_high_with_official_identity_reason": sum(
            1 for item in medium_high if item.get("official_identity_reason")
        ),
        "medium_high_with_official_website_reason": sum(
            1 for item in medium_high if item.get("official_website_reason")
        ),
        "medium_high_without_official_evidence": len(without_official_evidence),
        "medium_high_without_official_evidence_examples": [
            {
                "address": item.get("address"),
                "name": item.get("name"),
                "symbol": item.get("symbol"),
                "score": item.get("score"),
                "tier": item.get("tier"),
            }
            for item in without_official_evidence[:10]
        ],
    }


def audit_project_group_metrics(store: Store, limit: int) -> dict[str, Any]:
    groups = store.project_groups(limit=10000)
    medium_high = [group for group in groups if group.get("tier") in {"medium", "high"}]
    reviewed_medium_high = [group for group in medium_high if group.get("review")]
    unreviewed_medium_high = [group for group in medium_high if not group.get("review")]
    by_decision: dict[str, int] = {}
    for group in reviewed_medium_high:
        review = group.get("review") or {}
        decision = str(review.get("decision") or "unknown")
        by_decision[decision] = by_decision.get(decision, 0) + 1
    examples = [
        {
            "project_key": group.get("project_key"),
            "label": group.get("label"),
            "score": group.get("score"),
            "tier": group.get("tier"),
            "primary_address": group.get("primary_address"),
            "twitter_account": group.get("twitter_account"),
            "website": group.get("website"),
        }
        for group in unreviewed_medium_high[:limit]
    ]
    return {
        "project_groups": len(groups),
        "medium_or_high_groups": len(medium_high),
        "reviewed_medium_or_high": len(reviewed_medium_high),
        "unreviewed_medium_or_high": len(unreviewed_medium_high),
        "review_decisions": by_decision,
        "unreviewed_examples": examples,
    }


def audit_website_check_metrics(store: Store, limit: int) -> dict[str, Any]:
    groups = [
        group
        for group in store.project_groups(limit=10000)
        if str(group.get("tier") or "") in {"medium", "high"} and group.get("website")
    ]
    checked = 0
    ok = 0
    warn = 0
    failed = 0
    unchecked_examples = []
    failed_examples = []
    warn_examples = []
    for group in groups:
        project_key = str(group.get("project_key") or "")
        website = str(group.get("website") or "")
        latest = store.latest_website_check(project_key, website)
        base = {
            "project_key": project_key,
            "label": group.get("label"),
            "tier": group.get("tier"),
            "score": group.get("score"),
            "website": website,
        }
        if not latest:
            if len(unchecked_examples) < limit:
                unchecked_examples.append(base)
            continue
        checked += 1
        status = str(latest.get("status") or "")
        item = {
            **base,
            "checked_at": latest.get("checked_at"),
            "status": status,
            "http_status": latest.get("http_status"),
            "title": latest.get("title"),
            "matched_terms": latest.get("matched_terms") or [],
            "error": latest.get("error") or "",
            "failure_kind": latest.get("failure_kind") or "",
            "fallback_url": latest.get("fallback_url") or "",
        }
        if status == "ok":
            ok += 1
        elif status == "fail":
            failed += 1
            if len(failed_examples) < limit:
                failed_examples.append(item)
        else:
            warn += 1
            if len(warn_examples) < limit:
                warn_examples.append(item)
    return {
        "medium_high_websites": len(groups),
        "medium_high_websites_checked": checked,
        "medium_high_websites_ok": ok,
        "medium_high_websites_warn": warn,
        "medium_high_websites_failed": failed,
        "medium_high_websites_unchecked": len(groups) - checked,
        "unchecked_examples": unchecked_examples,
        "failed_examples": failed_examples,
        "warn_examples": warn_examples,
    }


def audit_website_twitter_backfill_metrics(store: Store, limit: int) -> dict[str, Any]:
    groups = [
        group
        for group in store.project_groups(limit=10000)
        if str(group.get("tier") or "") in {"medium", "high"}
    ]
    candidates = []
    for group in groups:
        candidate = website_twitter_candidate(store, group)
        if candidate:
            candidates.append(candidate)
    return {
        "matched_projects": len(candidates),
        "examples": [
            {
                "project_key": item.get("project_key"),
                "label": item.get("label"),
                "tier": item.get("tier"),
                "score": item.get("score"),
                "website": item.get("website"),
                "twitter_handle": item.get("twitter_handle"),
                "twitter_url": item.get("twitter_url"),
                "website_check_id": item.get("website_check_id"),
            }
            for item in candidates[:limit]
        ],
    }


def audit_report_files(settings: Settings) -> dict[str, Any]:
    latest_report = settings.data_dir / "latest_report.md"
    latest_groups = settings.data_dir / "latest_project_groups.json"
    snapshots_dir = settings.data_dir / "reports"
    snapshots = sorted(snapshots_dir.glob("*_report.md")) if snapshots_dir.exists() else []
    group_count = 0
    if latest_groups.exists():
        try:
            groups = json_loads_list(latest_groups.read_text(encoding="utf-8"))
            group_count = len(groups)
        except ValueError:
            group_count = 0
    return {
        "latest_report": str(latest_report),
        "latest_report_exists": latest_report.exists(),
        "latest_report_bytes": latest_report.stat().st_size if latest_report.exists() else 0,
        "latest_project_groups": str(latest_groups),
        "latest_project_groups_exists": latest_groups.exists(),
        "latest_project_groups_count": group_count,
        "snapshot_reports": len(snapshots),
        "latest_snapshot": str(snapshots[-1]) if snapshots else None,
    }


def json_loads_dict(value: object) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    return parsed if isinstance(parsed, dict) else {}


def json_loads_list(value: str) -> list[Any]:
    import json

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    return parsed if isinstance(parsed, list) else []


def split_csv_values(value: object) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def resolve_review_project(store: Store, project_key: str | None, address: str | None) -> dict[str, Any] | None:
    normalized_key = str(project_key or "").strip()
    normalized_address = str(address or "").strip().lower()
    if not normalized_key and not normalized_address:
        return None
    for group in store.project_groups(limit=10000):
        if normalized_key and str(group.get("project_key") or "") == normalized_key:
            return group
        if normalized_address and normalized_address in {str(item).lower() for item in group.get("addresses") or []}:
            return group
    return None


def run_cycle(
    settings: Settings,
    args: argparse.Namespace,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    store = Store(settings.db_path, settings.data_dir)
    etherscan = EtherscanClient(
        base_url=settings.etherscan_api_base,
        api_key=settings.etherscan_api_key,
        chainid=settings.chainid,
        timeout=settings.request_timeout_seconds,
        min_interval_seconds=getattr(settings, "etherscan_min_interval_seconds", 0.0),
    )
    twitter = None
    if not args.no_twitter:
        twitter = OpenTwitterClient(
            base_url=settings.opentwitter_api_base,
            api_key=settings.opentwitter_api_key,
            timeout=settings.request_timeout_seconds,
        )
    explicit_replay = args.start_block is not None or args.end_block is not None
    max_blocks = args.max_blocks if args.max_blocks is not None else settings.max_blocks_per_cycle
    enrich_limit = args.enrich_limit if args.enrich_limit is not None else settings.enrich_limit_per_cycle
    discovery_mode = args.discovery_mode if getattr(args, "discovery_mode", None) else getattr(settings, "discovery_mode", "block")
    runtime_role = cycle_runtime_role(explicit_replay, twitter is not None, max_blocks, enrich_limit)

    try:
        latest = etherscan.latest_block_number()
        confirmations = args.confirmations if args.confirmations is not None else settings.confirmations
        safe_latest = max(0, latest - confirmations)
        lookback_blocks = args.lookback_blocks if args.lookback_blocks is not None else settings.lookback_blocks

        coverage_repair = None
        if not explicit_replay and settings.coverage_window_blocks > 0:
            coverage_repair = store.repair_scan_coverage(settings.coverage_window_blocks)
            if coverage_repair.get("repaired") and progress:
                progress({"event": "scan_coverage_repaired", **coverage_repair})

        last_scanned = store.get_meta_int("last_scanned_block")
        if explicit_replay:
            start_block = args.start_block if args.start_block is not None else max(0, safe_latest - lookback_blocks + 1)
            end_block = args.end_block if args.end_block is not None else min(safe_latest, start_block + max_blocks - 1)
            if end_block < start_block:
                raise RuntimeError("--end-block must be greater than or equal to --start-block")
        elif last_scanned is None:
            start_block = max(0, safe_latest - lookback_blocks + 1)
            end_block = min(safe_latest, start_block + max_blocks - 1)
        else:
            start_block = min(last_scanned + 1, safe_latest)
            end_block = min(safe_latest, start_block + max_blocks - 1)
        store.record_cycle_started(
            {
                "command": str(getattr(args, "command", "")),
                "chain": getattr(settings, "chain_slug", "ethereum"),
                "chainid": settings.chainid,
                "chain_name": getattr(settings, "chain_name", "Ethereum"),
                "explicit_replay": explicit_replay,
                "twitter_enabled": twitter is not None,
                "latest_block": latest,
                "safe_latest_block": safe_latest,
                "last_scanned_before_cycle": last_scanned,
                "start_block": start_block,
                "end_block": end_block,
                "max_blocks": max_blocks,
                "enrich_limit": enrich_limit,
                "discovery_mode": discovery_mode,
                "code_fingerprint": runtime_code_fingerprint(settings.workspace),
            },
            role=runtime_role,
        )

        def cycle_progress(payload: dict[str, Any]) -> None:
            store.record_cycle_progress(payload, role=runtime_role)
            if progress:
                progress(payload)

        scanned_blocks = 0
        observed = 0
        new_contracts = 0
        internal_traces_by_block: dict[int, list[dict[str, Any]]] | None = None
        log_candidates_by_block: dict[int, list[dict[str, Any]]] | None = None
        log_creation_cache: dict[str, dict[str, Any]] | None = None
        if start_block <= end_block and discovery_mode == "activity":
            cycle_progress(
                {
                    "event": "activity_scan_started",
                    "start_block": start_block,
                    "end_block": end_block,
                    "min_observations": getattr(settings, "activity_min_observations", 100),
                    "max_candidates": getattr(settings, "activity_max_candidates_per_cycle", 50),
                    "include_all_transfers": bool(int(getattr(settings, "activity_include_all_transfers", 1) or 0)),
                    "custom_event_topics": len(split_csv_values(getattr(settings, "activity_custom_event_topics", ""))),
                }
            )
            contracts = [
                with_chain_metadata(contract, settings)
                for contract in discover_activity_contracts_in_range(
                    etherscan,
                    start_block,
                    end_block,
                    min_observations=getattr(settings, "activity_min_observations", 100),
                    max_candidates=getattr(settings, "activity_max_candidates_per_cycle", 50),
                    new_contract_max_age_blocks=settings.new_contract_max_age_blocks,
                    chain_slug=getattr(settings, "chain_slug", "ethereum"),
                    transfer_log_max_span=getattr(settings, "activity_transfer_log_span", 50),
                    include_all_transfers=bool(int(getattr(settings, "activity_include_all_transfers", 1) or 0)),
                    all_transfer_log_max_span=getattr(settings, "activity_all_transfer_log_span", 5),
                    custom_event_topics=split_csv_values(getattr(settings, "activity_custom_event_topics", "")),
                    custom_event_log_max_span=getattr(settings, "activity_custom_event_log_span", 1000),
                    classify=False,
                    progress=cycle_progress,
                )
            ]
            for contract in contracts:
                upsert_result = store.upsert_contract_result(contract)
                is_new = bool(upsert_result.get("contract_is_new"))
                if is_new:
                    store.append_event("contract_discovered", contract["address"], contract)
                elif upsert_result.get("source_is_new"):
                    store.append_event(
                        "contract_observation_source_added",
                        contract["address"],
                        {
                            "address": contract["address"],
                            "discovery_source": contract.get("discovery_source"),
                            "origin_tx_hash": contract.get("origin_tx_hash"),
                            "block_number": contract.get("block_number"),
                            "log_index": contract.get("log_index"),
                            "enrichment_requeued": bool(upsert_result.get("enrichment_requeued")),
                        },
                    )
                observed += 1
                if is_new:
                    new_contracts += 1
            scanned_blocks = end_block - start_block + 1
            if not explicit_replay:
                store.mark_block_range(start_block, end_block, source=f"{discovery_mode}_scan")
                store.set_meta("last_scanned_block", end_block)
            cycle_progress(
                {
                    "event": "activity_scan_finished",
                    "start_block": start_block,
                    "end_block": end_block,
                    "contracts": len(contracts),
                    "observed_contracts": len(contracts),
                    "observed_total": observed,
                    "new_contracts_total": new_contracts,
                }
            )
        elif start_block <= end_block:
            cycle_progress({"event": "prefetch_started", "start_block": start_block, "end_block": end_block})
            internal_traces_by_block = prefetch_internal_traces_by_block(etherscan, start_block, end_block)
            log_candidates_by_block = collect_log_candidates_by_block(etherscan, start_block, end_block, set())
            selected_log_candidates = [
                candidate
                for raw_candidates in log_candidates_by_block.values()
                for candidate in select_log_candidates(raw_candidates, settings.max_log_candidates_per_block)
            ]
            log_creation_cache = etherscan.get_contract_creation(
                [candidate["address"] for candidate in selected_log_candidates]
            )
            cycle_progress(
                {
                    "event": "prefetch_finished",
                    "blocks": end_block - start_block + 1,
                    "internal_traces": sum(len(items) for items in internal_traces_by_block.values()),
                    "log_candidates": sum(len(items) for items in log_candidates_by_block.values()),
                    "selected_log_candidates": len(selected_log_candidates),
                    "creation_cache_size": len(log_creation_cache),
                }
            )
            for block_number in range(start_block, end_block + 1):
                block = etherscan.get_block_by_number(block_number)
                tx_count = len(block.get("transactions") or [])
                store.mark_block(block_number, str(block.get("hash") or ""), hex_to_int(block.get("timestamp")), tx_count)
                direct = discover_contract_creations(etherscan, block, classify=False)
                direct_addresses = {item["address"] for item in direct}
                if internal_traces_by_block is not None:
                    internal = discover_internal_contract_creations_from_traces(
                        etherscan,
                        internal_traces_by_block.get(block_number, []),
                        block_number,
                        exclude_addresses=direct_addresses,
                        max_internal_candidates=settings.max_internal_candidates_per_block,
                        classify=False,
                    )
                else:
                    internal = discover_internal_contract_creations(
                        etherscan,
                        block_number,
                        exclude_addresses=direct_addresses,
                        max_internal_candidates=settings.max_internal_candidates_per_block,
                        classify=False,
                    )
                if log_candidates_by_block is not None:
                    from_logs = discover_log_candidates_from_raw(
                        etherscan,
                        log_candidates_by_block.get(block_number, []),
                        block_number,
                        max_log_candidates=settings.max_log_candidates_per_block,
                        new_contract_max_age_blocks=settings.new_contract_max_age_blocks,
                        classify=False,
                        creation_cache=log_creation_cache,
                    )
                else:
                    from_logs = discover_log_candidates(
                        etherscan,
                        block_number,
                        set(),
                        max_log_candidates=settings.max_log_candidates_per_block,
                        new_contract_max_age_blocks=settings.new_contract_max_age_blocks,
                        classify=False,
                    )
                contracts = [with_chain_metadata(contract, settings) for contract in direct + internal + from_logs]
                for contract in contracts:
                    upsert_result = store.upsert_contract_result(contract)
                    is_new = bool(upsert_result.get("contract_is_new"))
                    if is_new:
                        store.append_event("contract_discovered", contract["address"], contract)
                    elif upsert_result.get("source_is_new"):
                        store.append_event(
                            "contract_observation_source_added",
                            contract["address"],
                            {
                                "address": contract["address"],
                                "discovery_source": contract.get("discovery_source"),
                                "origin_tx_hash": contract.get("origin_tx_hash"),
                                "block_number": contract.get("block_number"),
                                "log_index": contract.get("log_index"),
                                "enrichment_requeued": bool(upsert_result.get("enrichment_requeued")),
                            },
                        )
                    observed += 1
                    if is_new:
                        new_contracts += 1
                if not explicit_replay:
                    store.set_meta("last_scanned_block", block_number)
                scanned_blocks += 1
                cycle_progress(
                    {
                        "event": "block_scanned",
                        "block_number": block_number,
                        "contracts": len(contracts),
                        "observed_contracts": len(contracts),
                        "observed_total": observed,
                        "new_contracts_total": new_contracts,
                    }
                )

        source_url_backfill = None
        source_url_limit = cycle_source_url_backfill_limit(args)
        if source_url_limit > 0:
            source_url_backfill = backfill_source_urls(store, etherscan, limit=source_url_limit, force=False)
            cycle_progress(
                {
                    "event": "source_url_backfill_completed",
                    **compact_source_url_backfill_result(source_url_backfill),
                }
            )

        enriched = 0
        if twitter is not None and enrich_limit > 0:
            recover_stale_processing_for_cycle(store, runtime_role, progress=cycle_progress)
            partial_report_path = None
            attempted_enrichment_addresses: set[str] = set()
            attempted_enrichments = 0
            reservation_batch_size = cycle_enrichment_reservation_batch_size(settings)
            while attempted_enrichments < enrich_limit:
                batch_limit = min(reservation_batch_size, enrich_limit - attempted_enrichments)
                pending = store.enrichment_targets(
                    batch_limit,
                    force=args.force_enrich,
                    exclude_addresses=attempted_enrichment_addresses,
                )
                if not pending:
                    break
                cycle_progress(
                    {
                        "event": "enrichment_batch_reserved",
                        "batch_limit": batch_limit,
                        "reserved": len(pending),
                        "attempted_total": attempted_enrichments,
                        "enriched_total": enriched,
                    }
                )
                for contract in pending:
                    address = str(contract.get("address") or "")
                    if address:
                        attempted_enrichment_addresses.add(address.lower())
                    attempted_enrichments += 1
                    contract_for_scoring = with_chain_metadata(contract_payload(contract), settings)
                    classified_contract = ensure_classified(etherscan, store, contract_for_scoring)
                    if classified_contract.get("classification_error"):
                        store.release_enrichment_targets([contract["address"]])
                        store.append_event(
                            "classification_failed",
                            contract["address"],
                            {
                                "error": classified_contract.get("classification_error"),
                                "command": str(getattr(args, "command", "")),
                            },
                        )
                        cycle_progress(
                            {
                                "event": "classification_failed",
                                "address": contract["address"],
                                "error": classified_contract.get("classification_error"),
                                "attempted_total": attempted_enrichments,
                                "enriched_total": enriched,
                            }
                        )
                        continue
                    current_contract = store.enrichment_context(contract["address"]) or classified_contract
                    contract_for_scoring = with_chain_metadata(contract_payload(current_contract), settings)
                    contract_for_scoring["observation_count"] = current_contract.get("observation_count") or 0
                    contract_for_scoring["observation_sources"] = (
                        current_contract.get("observation_sources") or current_contract.get("discovery_source") or ""
                    )
                    skip_reason = offchain_skip_reason(contract_for_scoring)
                    if skip_reason:
                        enrichment = build_skip_enrichment(contract_for_scoring, skip_reason)
                    else:
                        enrichment = enrich_contract(twitter, contract_for_scoring, settings.twitter_max_results)
                    store.upsert_enrichment(contract["address"], enrichment)
                    store.append_event("contract_enriched", contract["address"], enrichment)
                    enriched += 1
                    score = enrichment.get("score") or {}
                    cycle_progress(
                        {
                            "event": "contract_enriched",
                            "address": contract["address"],
                            "score": score.get("score"),
                            "tier": score.get("tier"),
                            "attempted_total": attempted_enrichments,
                            "enriched_total": enriched,
                        }
                    )
                    report_limit = cycle_report_limit(settings, args)
                    report_every = cycle_report_every_enriched(settings, args)
                    report_reason = enrichment_report_reason(enriched, score, report_every)
                    if report_limit > 0 and report_reason:
                        partial_report_path = write_report(settings, report_limit)
                        maybe_write_daily_snapshot(settings, report_limit, progress=progress)
                        cycle_progress(
                            {
                                "event": "report_written",
                                "report": str(partial_report_path),
                                "limit": report_limit,
                                "attempted_total": attempted_enrichments,
                                "enriched_total": enriched,
                                "reason": report_reason,
                            }
                        )

        website_checks = []
        website_limit = cycle_website_verify_limit(args)
        if twitter is not None and website_limit > 0:
            groups = [
                group
                for group in store.project_groups(limit=10000)
                if str(group.get("tier") or "") in {"high", "medium"}
            ]
            website_checks = verify_website_groups(store, settings, groups, website_limit, refresh_all=False)
            for item in website_checks:
                check = item.get("check") if isinstance(item.get("check"), dict) else {}
                cycle_progress(
                    {
                        "event": "website_checked",
                        "project_key": item.get("project_key"),
                        "label": item.get("label"),
                        "website": item.get("website"),
                        "status": check.get("status"),
                        "http_status": check.get("http_status"),
                        "rescored_addresses": item.get("rescored_addresses") or [],
                    }
                )

        website_twitter_backfills = []
        website_twitter_limit = cycle_website_twitter_backfill_limit(args)
        if twitter is not None and website_twitter_limit > 0:
            groups = [
                group
                for group in store.project_groups(limit=10000)
                if str(group.get("tier") or "") in {"high", "medium"}
            ]
            website_twitter_backfills = backfill_website_twitter_groups(store, twitter, groups, website_twitter_limit)
            for item in website_twitter_backfills:
                cycle_progress(
                    {
                        "event": "website_twitter_backfilled",
                        "project_key": item.get("project_key"),
                        "label": item.get("label"),
                        "website": item.get("website"),
                        "twitter_handle": item.get("twitter_handle"),
                        "action": item.get("action"),
                        "backfilled_addresses": item.get("backfilled_addresses") or [],
                        "skipped_addresses": item.get("skipped_addresses") or [],
                    }
                )

        summary = store.summary()
        catchup_remaining = catchup_remaining_blocks(summary, safe_latest)
        result = {
            "chain": getattr(settings, "chain_slug", "ethereum"),
            "chainid": settings.chainid,
            "chain_name": getattr(settings, "chain_name", "Ethereum"),
            "latest_block": latest,
            "safe_latest_block": safe_latest,
            "start_block": start_block,
            "end_block": end_block,
            "scanned_blocks": scanned_blocks,
            "discovered_contracts": new_contracts,
            "observed_contracts": observed,
            "enriched_contracts": enriched,
            "catchup_remaining_blocks": catchup_remaining,
            "discovery_mode": discovery_mode,
            "summary": summary,
        }
        if website_checks:
            result["website_checks"] = {
                "checked": len(website_checks),
                "status_counts": count_website_check_statuses(website_checks),
            }
        if website_twitter_backfills:
            result["website_twitter_backfill"] = {
                "matched_projects": len(website_twitter_backfills),
                "backfilled_addresses": sum(len(item.get("backfilled_addresses") or []) for item in website_twitter_backfills),
                "skipped": sum(1 for item in website_twitter_backfills if item.get("action") != "backfilled"),
            }
        if source_url_backfill and any(
            int(source_url_backfill.get(key) or 0) > 0
            for key in ("matched", "refreshed", "with_source_urls", "matched_source_websites", "requeued", "source_errors")
        ):
            result["source_url_backfill"] = compact_source_url_backfill_result(source_url_backfill)
        if coverage_repair:
            result["coverage"] = coverage_repair
        if "partial_report_path" in locals() and partial_report_path is not None:
            result["report"] = str(partial_report_path)
        store.record_cycle_finished("ok", compact_cycle_result(result), role=runtime_role)
        return result
    except Exception as exc:
        store.record_cycle_finished(
            "failed",
            {
                "command": str(getattr(args, "command", "")),
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            role=runtime_role,
        )
        raise
    finally:
        store.close()


def recover_stale_processing_for_cycle(
    store: Store,
    role: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    recovered = store.reset_processing_enrichments(PROCESSING_STALE_MINUTES)
    if recovered <= 0:
        return 0
    payload = {
        "event": "stale_processing_recovered",
        "recovered": recovered,
        "older_than_minutes": PROCESSING_STALE_MINUTES,
    }
    if progress:
        progress(payload)
    else:
        store.record_cycle_progress(payload, role=role)
    return recovered


def cycle_runtime_role(explicit_replay: bool, twitter_enabled: bool, max_blocks: int, enrich_limit: int) -> str:
    if explicit_replay:
        return "replay"
    scans_blocks = int(max_blocks or 0) > 0
    enriches = twitter_enabled and int(enrich_limit or 0) > 0
    if scans_blocks and enriches:
        return "combined"
    if scans_blocks:
        return "scanner"
    if enriches:
        return "enricher"
    return "maintenance"


def compact_cycle_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: result.get(key)
        for key in (
            "chain",
            "chainid",
            "chain_name",
            "latest_block",
            "safe_latest_block",
            "start_block",
            "end_block",
            "scanned_blocks",
            "discovered_contracts",
            "observed_contracts",
            "enriched_contracts",
            "catchup_remaining_blocks",
            "discovery_mode",
            "report",
        )
        if key in result
    }
    summary = result.get("summary")
    if isinstance(summary, dict):
        compact["summary"] = {
            key: summary.get(key)
            for key in (
                "last_scanned_block",
                "contracts",
                "enriched",
                "pending_enrichment",
                "processing_enrichment",
                "medium_or_high",
                "observations",
            )
            if key in summary
        }
    coverage = result.get("coverage")
    if isinstance(coverage, dict):
        compact["coverage"] = {
            key: coverage.get(key)
            for key in (
                "status",
                "repaired",
                "last_scanned_block",
                "old_last_scanned_block",
                "new_last_scanned_block",
                "missing_blocks",
                "first_missing_block",
            )
            if key in coverage
        }
    website_checks = result.get("website_checks")
    if isinstance(website_checks, dict):
        compact["website_checks"] = {
            key: website_checks.get(key)
            for key in ("checked", "status_counts")
            if key in website_checks
        }
    website_twitter_backfill = result.get("website_twitter_backfill")
    if isinstance(website_twitter_backfill, dict):
        compact["website_twitter_backfill"] = {
            key: website_twitter_backfill.get(key)
            for key in ("matched_projects", "backfilled_addresses", "skipped")
            if key in website_twitter_backfill
        }
    source_url_backfill = result.get("source_url_backfill")
    if isinstance(source_url_backfill, dict):
        compact["source_url_backfill"] = compact_source_url_backfill_result(source_url_backfill)
    return compact


def contract_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw_json")
    if isinstance(raw, str) and raw:
        import json

        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    return dict(row)


def with_chain_metadata(contract: dict[str, Any], settings: Settings) -> dict[str, Any]:
    payload = dict(contract)
    payload.setdefault("chain", getattr(settings, "chain_slug", "ethereum"))
    payload.setdefault("chain_slug", getattr(settings, "chain_slug", "ethereum"))
    payload.setdefault("chainid", settings.chainid)
    payload.setdefault("chain_name", getattr(settings, "chain_name", "Ethereum"))
    payload.setdefault("explorer_base_url", getattr(settings, "explorer_base_url", "https://etherscan.io"))
    return payload


def ensure_classified(etherscan: EtherscanClient, store: Store, contract: dict[str, Any]) -> dict[str, Any]:
    if not contract.get("classification_deferred"):
        return contract
    try:
        classification = classify_contract(etherscan, contract["address"])
    except Exception as exc:
        return {**contract, "classification_error": str(exc)}
    classified = {**contract, **classification}
    classified.pop("classification_deferred", None)
    stored = store.update_contract_classification(contract["address"], classification)
    if stored:
        return stored
    return classified


def prefetch_internal_traces_by_block(
    etherscan: EtherscanClient,
    start_block: int,
    end_block: int,
) -> dict[int, list[dict[str, Any]]]:
    traces_by_block: dict[int, list[dict[str, Any]]] = {}
    for trace in etherscan.get_internal_transactions(start_block, end_block):
        block_number = parse_block_number(trace.get("blockNumber"))
        if block_number is None:
            continue
        traces_by_block.setdefault(block_number, []).append(trace)
    return traces_by_block


def should_skip_offchain_enrichment(contract: dict[str, Any]) -> bool:
    return offchain_skip_reason(contract) is not None


def build_skip_enrichment(contract: dict[str, Any], skip_reason: str) -> dict[str, Any]:
    evidence = {
        "twitter_account": None,
        "website": None,
        "tweet_count": 0,
        "address_mentions": 0,
        "credible_address_mentions": 0,
        "discussion_only": False,
        "skip_reason": skip_reason,
    }
    evidence = augment_project_metadata(contract, evidence)
    return {
        "status": "ok",
        "queries": [],
        "evidence": evidence,
        "score": score_project(contract, evidence),
    }


def offchain_skip_reason(contract: dict[str, Any]) -> str | None:
    if contract.get("classification_deferred") or contract.get("classification_error"):
        return None
    if contract.get("kind") == "liquidity_pool":
        return "liquidity_pool_artifact"
    if contract.get("kind") != "contract":
        return None
    sources = {
        source.strip()
        for source in str(contract.get("observation_sources") or contract.get("discovery_source") or "").split(",")
        if source.strip()
    }
    if (
        sources
        and sources.issubset(CREATION_ONLY_SOURCES)
        and not contract.get("name")
        and not contract.get("symbol")
        and infrastructure_contract_name(contract.get("contract_name"))
    ):
        return INFRASTRUCTURE_SKIP_REASON
    if contract.get("name") or contract.get("symbol") or contract.get("contract_name") or contract.get("verified"):
        return None
    if sources and sources.issubset(CREATION_ONLY_SOURCES):
        return LOW_SURFACE_SKIP_REASON
    return None


def catchup_remaining_blocks(summary: dict[str, Any], safe_latest: int) -> int | None:
    last_scanned = summary.get("last_scanned_block")
    if not isinstance(last_scanned, int):
        return None
    return max(0, safe_latest - last_scanned)


def cycle_sleep_seconds(result: dict[str, Any], interval: int, catchup_sleep: int = 1) -> int:
    remaining = result.get("catchup_remaining_blocks")
    if isinstance(remaining, int) and remaining > 0 and int(result.get("scanned_blocks") or 0) > 0:
        return catchup_sleep
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    pending = summary.get("pending_enrichment") if isinstance(summary, dict) else None
    if isinstance(pending, int) and pending > 0 and int(result.get("enriched_contracts") or 0) > 0:
        return catchup_sleep
    return interval


def maybe_write_cycle_report(
    settings: Settings,
    args: argparse.Namespace,
    result: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Path | None:
    report_limit = cycle_report_limit(settings, args)
    if report_limit is None or report_limit <= 0:
        return None
    changed = int(result.get("enriched_contracts") or 0) > 0 or int(result.get("observed_contracts") or 0) > 0
    explicit_once = args.report_limit is not None and getattr(args, "command", "") == "once"
    if not changed and not explicit_once:
        return None
    report_path = write_report(settings, report_limit)
    result["report"] = str(report_path)
    if progress:
        progress({"event": "report_written", "report": str(report_path), "limit": report_limit})
    maybe_write_daily_snapshot(settings, report_limit, progress=progress)
    return report_path


def cycle_report_limit(settings: Settings, args: argparse.Namespace) -> int:
    return args.report_limit if args.report_limit is not None else settings.report_limit_per_cycle


def cycle_report_every_enriched(settings: Settings, args: argparse.Namespace) -> int:
    value = args.report_every_enriched if args.report_every_enriched is not None else settings.report_every_enriched
    return max(0, int(value or 0))


def cycle_enrichment_reservation_batch_size(settings: Settings) -> int:
    return max(1, int(getattr(settings, "enrichment_reservation_batch_size", 5) or 5))


def cycle_website_verify_limit(args: argparse.Namespace) -> int:
    return max(0, int(getattr(args, "verify_websites_limit", None) or 0))


def cycle_website_twitter_backfill_limit(args: argparse.Namespace) -> int:
    return max(0, int(getattr(args, "backfill_website_twitter_limit", None) or 0))


def cycle_source_url_backfill_limit(args: argparse.Namespace) -> int:
    return max(0, int(getattr(args, "backfill_source_urls_limit", None) or 0))


def enrichment_report_reason(enriched_count: int, score: dict[str, Any], report_every: int) -> str | None:
    tier = str((score or {}).get("tier") or "")
    if tier in {"medium", "high"}:
        return f"tier_{tier}"
    if report_every > 0 and enriched_count > 0 and enriched_count % report_every == 0:
        return "cadence"
    return None


def daily_snapshot_label(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return f"daily-{current.strftime('%Y-%m-%d')}"


def maybe_write_daily_snapshot(
    settings: Settings,
    limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
    label: str | None = None,
) -> Path | None:
    if int(getattr(settings, "daily_snapshot_enabled", 1) or 0) <= 0 or limit <= 0:
        return None
    snapshot_label = safe_report_label(label or daily_snapshot_label())
    output_path = snapshot_report_path(settings, snapshot_label)

    store = Store(settings.db_path, settings.data_dir)
    try:
        if store.get_meta(DAILY_SNAPSHOT_META_KEY) == snapshot_label and output_path.exists():
            return None
    finally:
        store.close()

    path = write_report(settings, limit, output_path)
    store = Store(settings.db_path, settings.data_dir)
    try:
        store.set_meta(DAILY_SNAPSHOT_META_KEY, snapshot_label)
    finally:
        store.close()
    if progress:
        progress({"event": "daily_snapshot_written", "report": str(path), "limit": limit, "label": snapshot_label})
    return path


def health_check(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    store = Store(settings.db_path, settings.data_dir)
    etherscan = EtherscanClient(
        base_url=settings.etherscan_api_base,
        api_key=settings.etherscan_api_key,
        chainid=settings.chainid,
        timeout=settings.request_timeout_seconds,
        min_interval_seconds=getattr(settings, "etherscan_min_interval_seconds", 0.0),
    )
    try:
        latest = etherscan.latest_block_number()
        confirmations = args.confirmations if args.confirmations is not None else settings.confirmations
        safe_latest = max(0, latest - confirmations)
        summary = store.summary()
        last_scanned = summary.get("last_scanned_block")
        lag_blocks = None
        if isinstance(last_scanned, int):
            lag_blocks = max(0, safe_latest - last_scanned)
        if lag_blocks is None:
            status = "not_started"
        elif lag_blocks > settings.max_blocks_per_cycle * 4:
            status = "behind"
        elif lag_blocks > 0:
            status = "catching_up"
        elif summary.get("pending_enrichment", 0) > 0 or summary.get("processing_enrichment", 0) > 0:
            status = "needs_enrichment"
        else:
            status = "ok"
        return {
            "chain": getattr(settings, "chain_slug", "ethereum"),
            "chainid": settings.chainid,
            "chain_name": getattr(settings, "chain_name", "Ethereum"),
            "status": status,
            "latest_block": latest,
            "safe_latest_block": safe_latest,
            "lag_blocks": lag_blocks,
            "confirmations": confirmations,
            "coverage": store.scan_coverage(settings.coverage_window_blocks),
            "runtime": store.runtime_status(),
            "summary": summary,
        }
    finally:
        store.close()


def write_report(settings: Settings, limit: int, output: Path | None = None) -> Path:
    store = Store(settings.db_path, settings.data_dir)
    try:
        summary = store.summary()
        groups = store.project_groups(limit=limit)
        rows = store.project_rows(limit=limit)
    finally:
        store.close()

    output_path = output or (settings.data_dir / "latest_report.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    groups_path = project_groups_report_path(output_path)
    import json

    groups_path.write_text(json.dumps(groups, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        f"# {getattr(settings, 'chain_name', 'Ethereum')} Alpha Radar Report",
        "",
        f"- Chain: {getattr(settings, 'chain_name', 'Ethereum')} (`chainid={settings.chainid}`)",
        f"- Last scanned block: {summary.get('last_scanned_block')}",
        f"- Contracts: {summary.get('contracts')}",
        f"- Enriched: {summary.get('enriched')}",
        f"- Processing enrichment: {summary.get('processing_enrichment')}",
        f"- Pending enrichment: {summary.get('pending_enrichment')}",
        f"- Medium/high candidates: {summary.get('medium_or_high')}",
        f"- Discovery sources: {summary.get('by_source')}",
        f"- Observations: {summary.get('observations')}",
        f"- Observation sources: {summary.get('observations_by_source')}",
        f"- Tiers: {summary.get('by_tier')}",
        f"- Enrichment statuses: {summary.get('by_status')}",
        f"- Project groups file: {groups_path}",
        "",
        "## Project Groups",
        "",
        "| Score | Tier | Project | Category | Positioning | Contracts | Signals | Sources | Twitter | Website | Review | Primary Address |",
        "|---:|---|---|---|---|---:|---:|---|---|---|---|---|",
    ]
    for group in groups:
        lines.append(
            "| {score} | {tier} | {project} | {category} | {positioning} | {contracts} | {signals} | {sources} | {twitter} | {website} | {review} | `{address}` |".format(
                score=group.get("score") if group.get("score") is not None else "",
                tier=group.get("tier") or "",
                project=_md_cell(group.get("label")),
                category=_md_cell(group.get("project_category")),
                positioning=_md_cell(group.get("project_positioning")),
                contracts=group.get("address_count") or 0,
                signals=group.get("observation_count") or 0,
                sources=_source_cell(group.get("observation_sources") or ""),
                twitter=(_twitter_link(group.get("twitter_account")) if group.get("twitter_account") else ""),
                website=(_md_link(group.get("website")) if group.get("website") else ""),
                review=_review_cell(group.get("review")),
                address=group.get("primary_address") or "",
            )
        )
    lines.extend(
        [
            "",
            "## Contract Candidates",
            "",
            "| Score | Tier | Category | Source | Signals | Kind | Name | Symbol | Address | Twitter | Website |",
            "|---:|---|---|---|---:|---|---|---|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| {score} | {tier} | {category} | {source} | {signals} | {kind} | {name} | {symbol} | `{address}` | {twitter} | {website} |".format(
                score=row.get("score") if row.get("score") is not None else "",
                tier=row.get("tier") or "",
                category=_md_cell(row.get("project_category")),
                source=_source_cell(row.get("observation_sources") or row.get("discovery_source") or ""),
                signals=row.get("observation_count") or 0,
                kind=row.get("kind") or "",
                name=_md_cell(row.get("name")),
                symbol=_md_cell(row.get("symbol")),
                address=row.get("address") or "",
                twitter=(_twitter_link(row.get("twitter_account")) if row.get("twitter_account") else ""),
                website=(_md_link(row.get("website")) if row.get("website") else ""),
            )
        )
    lines.append("")
    lines.append("## Score Breakdowns")
    lines.append("")
    for row in rows:
        breakdown = row.get("score_breakdown") or {}
        label = row.get("name") or row.get("symbol") or row.get("address")
        lines.append(f"- `{row.get('address')}` {label}: score={row.get('score')} tier={row.get('tier')} breakdown={breakdown}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def snapshot_report_path(settings: Settings, label: str | None = None) -> Path:
    raw_label = label or datetime.now().strftime("%Y%m%d-%H%M%S")
    return settings.data_dir / "reports" / f"{safe_report_label(raw_label)}_report.md"


def safe_report_label(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value).strip())
    safe = "-".join(part for part in safe.split("-") if part)
    return safe or "snapshot"


def project_groups_report_path(report_path: Path) -> Path:
    if report_path.name == "latest_report.md":
        return report_path.with_name("latest_project_groups.json")
    return report_path.with_name(f"{report_path.stem}_project_groups.json")


def _md_cell(value: object) -> str:
    text = str(value or "")
    return text.replace("|", "\\|").replace("\n", " ")[:80]


def _source_cell(value: object, max_sources: int = 4) -> str:
    sources = [source.strip() for source in str(value or "").split(",") if source.strip()]
    if len(sources) <= max_sources:
        return _md_cell(",".join(sources))
    return _md_cell(",".join(sources[:max_sources]) + f",+{len(sources) - max_sources} more")


def _md_link(url: object) -> str:
    text = str(url or "")
    return f"[link]({text})" if text.startswith("http") else _md_cell(text)


def _review_cell(review: object) -> str:
    if not isinstance(review, dict):
        return ""
    decision = _md_cell(review.get("decision"))
    note = _md_cell(review.get("note"))
    if decision and note:
        return f"{decision}: {note}"
    return decision


def _twitter_link(username: object) -> str:
    text = str(username or "").lstrip("@")
    return f"[@{_md_cell(text)}](https://x.com/{text})"


def print_json(value: dict[str, Any]) -> None:
    import json

    print_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def print_json_line(value: dict[str, Any]) -> None:
    import json

    print_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def print_text(text: str) -> None:
    import errno

    def output_unavailable(exc: OSError) -> bool:
        return exc.errno in {errno.EINVAL, errno.EPIPE}

    try:
        print(text, flush=True)
    except BrokenPipeError:
        return
    except OSError as exc:
        if output_unavailable(exc):
            return
        raise
    except UnicodeEncodeError:
        try:
            sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
            sys.stdout.flush()
        except BrokenPipeError:
            return
        except OSError as exc:
            if output_unavailable(exc):
                return
            raise


if __name__ == "__main__":
    raise SystemExit(main())
