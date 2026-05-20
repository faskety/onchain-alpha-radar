from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .project_meta import augment_project_metadata
from .signals import meaningful_name, meaningful_symbol

PROCESSING_STALE_MINUTES = 30
CREATION_ONLY_SOURCES = {"contract_creation", "internal_create", "internal_create2"}
LOW_SURFACE_SKIP_REASON = "low_surface_unidentified_contract"
INFRASTRUCTURE_SKIP_REASON = "infrastructure_contract_artifact"
RETRY_ON_NEW_OBSERVATION_SOURCES = {
    "mint_transfer",
    "erc1155_transfer_single_mint",
    "erc1155_transfer_batch_mint",
    "balancer_v2_tokens_registered",
}
TIER_RANK = {"low": 0, "watch": 1, "medium": 2, "high": 3}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def should_retry_enrichment_for_new_source(discovery_source: str | None) -> bool:
    source = str(discovery_source or "")
    return source.startswith("uniswap_") or source.startswith("activity_") or source in RETRY_ON_NEW_OBSERVATION_SOURCES


def normalized_address_list(addresses: Iterable[str] | None) -> list[str]:
    result = []
    seen = set()
    for address in addresses or []:
        normalized = str(address or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


class Store:
    def __init__(self, db_path: Path, data_dir: Path):
        self.db_path = db_path
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=30000;
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocks (
              block_number INTEGER PRIMARY KEY,
              block_hash TEXT,
              block_timestamp INTEGER,
              tx_count INTEGER,
              scanned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contracts (
              address TEXT PRIMARY KEY,
              tx_hash TEXT UNIQUE NOT NULL,
              origin_tx_hash TEXT,
              discovery_source TEXT,
              log_index INTEGER,
              related_json TEXT,
              deployer TEXT,
              block_number INTEGER NOT NULL,
              block_timestamp INTEGER,
              tx_index INTEGER,
              value_wei TEXT,
              input_prefix TEXT,
              kind TEXT,
              name TEXT,
              symbol TEXT,
              decimals INTEGER,
              total_supply TEXT,
              verified INTEGER,
              contract_name TEXT,
              source_len INTEGER,
              confidence REAL,
              raw_json TEXT NOT NULL,
              first_seen_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS enrichments (
              address TEXT PRIMARY KEY,
              searched_at TEXT NOT NULL,
              query_json TEXT NOT NULL,
              twitter_account TEXT,
              twitter_name TEXT,
              twitter_followers INTEGER,
              twitter_verified INTEGER,
              website TEXT,
              tweet_count INTEGER,
              address_mentions INTEGER,
              evidence_json TEXT NOT NULL,
              score INTEGER,
              tier TEXT,
              score_breakdown_json TEXT,
              project_description TEXT,
              project_category TEXT,
              project_positioning TEXT,
              status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              event_type TEXT NOT NULL,
              address TEXT,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_reviews (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_key TEXT NOT NULL,
              reviewed_at TEXT NOT NULL,
              reviewer TEXT NOT NULL,
              decision TEXT NOT NULL,
              note TEXT,
              snapshot_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_project_reviews_key_time
            ON project_reviews(project_key, reviewed_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS website_checks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_key TEXT NOT NULL,
              website TEXT NOT NULL,
              checked_at TEXT NOT NULL,
              status TEXT NOT NULL,
              http_status INTEGER,
              final_url TEXT,
              title TEXT,
              description TEXT,
              twitter_links_json TEXT NOT NULL,
              matched_terms_json TEXT NOT NULL,
              error TEXT,
              payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_website_checks_project_time
            ON website_checks(project_key, website, checked_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS contract_observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              address TEXT NOT NULL,
              discovery_source TEXT NOT NULL,
              block_number INTEGER,
              block_timestamp INTEGER,
              origin_tx_hash TEXT,
              log_index INTEGER,
              related_json TEXT,
              payload_json TEXT NOT NULL,
              observed_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_observations_unique
            ON contract_observations(
              address,
              discovery_source,
              COALESCE(origin_tx_hash, ''),
              COALESCE(log_index, -1)
            );
            """
        )
        self.migrate_schema()
        self.conn.commit()

    def migrate_schema(self) -> None:
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(contracts)").fetchall()
        }
        migrations = {
            "origin_tx_hash": "ALTER TABLE contracts ADD COLUMN origin_tx_hash TEXT",
            "discovery_source": "ALTER TABLE contracts ADD COLUMN discovery_source TEXT",
            "log_index": "ALTER TABLE contracts ADD COLUMN log_index INTEGER",
            "related_json": "ALTER TABLE contracts ADD COLUMN related_json TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                self.conn.execute(sql)
        enrichment_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(enrichments)").fetchall()
        }
        enrichment_migrations = {
            "project_description": "ALTER TABLE enrichments ADD COLUMN project_description TEXT",
            "project_category": "ALTER TABLE enrichments ADD COLUMN project_category TEXT",
            "project_positioning": "ALTER TABLE enrichments ADD COLUMN project_positioning TEXT",
        }
        for column, sql in enrichment_migrations.items():
            if column not in enrichment_columns:
                self.conn.execute(sql)
        self.backfill_observations_from_contracts()

    def backfill_observations_from_contracts(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) AS n FROM contract_observations").fetchone()["n"]
        if existing:
            return
        rows = self.conn.execute("SELECT raw_json FROM contracts ORDER BY block_number ASC").fetchall()
        for row in rows:
            raw = row["raw_json"]
            if not raw:
                continue
            try:
                contract = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(contract, dict) and contract.get("address"):
                self.record_observation(contract, commit=False)

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return str(row["value"])

    def get_meta_int(self, key: str) -> int | None:
        value = self.get_meta(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def set_meta(self, key: str, value: str | int) -> None:
        self.conn.execute(
            """
            INSERT INTO meta(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), utc_now()),
        )
        self.conn.commit()

    def get_meta_json(self, key: str) -> dict[str, Any]:
        raw = self.get_meta(key)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def set_meta_json(self, key: str, value: dict[str, Any]) -> None:
        self.set_meta(key, json.dumps(value, ensure_ascii=False, sort_keys=True))

    def record_cycle_started(self, payload: dict[str, Any], role: str = "default") -> dict[str, Any]:
        cycle_role = normalize_cycle_role(role)
        prefix = f"last_cycle_{cycle_role}_"
        started_at = utc_now()
        record = {"role": cycle_role, "started_at": started_at, **payload}
        self.set_meta(f"{prefix}started_at", started_at)
        self.set_meta(f"{prefix}status", "running")
        self.set_meta_json(f"{prefix}context_json", record)
        self.append_event("cycle_started", None, record)
        return record

    def record_cycle_progress(self, payload: dict[str, Any], role: str = "default") -> dict[str, Any]:
        cycle_role = normalize_cycle_role(role)
        prefix = f"last_cycle_{cycle_role}_"
        progressed_at = utc_now()
        record = {"role": cycle_role, "progressed_at": progressed_at, **payload}
        self.set_meta(f"{prefix}progressed_at", progressed_at)
        self.set_meta_json(f"{prefix}progress_json", record)
        self.append_event("cycle_progress", None, record)
        return record

    def record_cycle_finished(self, status: str, payload: dict[str, Any], role: str = "default") -> dict[str, Any]:
        cycle_role = normalize_cycle_role(role)
        prefix = f"last_cycle_{cycle_role}_"
        finished_at = utc_now()
        normalized_status = status if status in {"ok", "failed"} else "failed"
        record = {"role": cycle_role, "finished_at": finished_at, "status": normalized_status, **payload}
        self.set_meta(f"{prefix}finished_at", finished_at)
        self.set_meta(f"{prefix}status", normalized_status)
        self.set_meta_json(f"{prefix}result_json", record)
        event_type = "cycle_finished" if normalized_status == "ok" else "cycle_failed"
        self.append_event(event_type, None, record)
        return record

    def record_cycle_interrupted(self, payload: dict[str, Any], role: str = "default") -> dict[str, Any]:
        cycle_role = normalize_cycle_role(role)
        prefix = f"last_cycle_{cycle_role}_"
        finished_at = utc_now()
        record = {"role": cycle_role, "finished_at": finished_at, "status": "interrupted", **payload}
        self.set_meta(f"{prefix}finished_at", finished_at)
        self.set_meta(f"{prefix}status", "interrupted")
        self.set_meta_json(f"{prefix}result_json", record)
        self.append_event("cycle_interrupted", None, record)
        return record

    def runtime_status(self) -> dict[str, Any]:
        role_rows = self.conn.execute(
            """
            SELECT key
            FROM meta
            WHERE key LIKE 'last_cycle_%_status'
            ORDER BY key
            """
        ).fetchall()
        roles = {}
        for row in role_rows:
            key = str(row["key"])
            role = key.removeprefix("last_cycle_").removesuffix("_status")
            if not role:
                continue
            prefix = f"last_cycle_{role}_"
            roles[role] = {
                "last_cycle_started_at": self.get_meta(f"{prefix}started_at"),
                "last_cycle_progressed_at": self.get_meta(f"{prefix}progressed_at"),
                "last_cycle_finished_at": self.get_meta(f"{prefix}finished_at"),
                "last_cycle_status": self.get_meta(f"{prefix}status"),
                "last_cycle_context": self.get_meta_json(f"{prefix}context_json"),
                "last_cycle_progress": self.get_meta_json(f"{prefix}progress_json"),
                "last_cycle_result": self.get_meta_json(f"{prefix}result_json"),
            }
        latest_role = latest_runtime_role(roles)
        latest = roles.get(latest_role or "", {})
        return {
            "latest_role": latest_role,
            "roles": roles,
            "last_cycle_started_at": latest.get("last_cycle_started_at"),
            "last_cycle_finished_at": latest.get("last_cycle_finished_at"),
            "last_cycle_status": latest.get("last_cycle_status"),
            "last_cycle_context": latest.get("last_cycle_context", {}),
            "last_cycle_result": latest.get("last_cycle_result", {}),
        }

    def scan_coverage(self, window_blocks: int = 720) -> dict[str, Any]:
        last_scanned = self.get_meta_int("last_scanned_block")
        window = max(0, int(window_blocks or 0))
        if last_scanned is None or window <= 0:
            return {
                "status": "not_started",
                "last_scanned_block": last_scanned,
                "window_blocks": window,
                "window_start": None,
                "window_end": last_scanned,
                "expected_blocks": 0,
                "scanned_blocks": 0,
                "missing_blocks": 0,
                "missing_ranges": [],
                "first_missing_block": None,
            }
        start = max(0, last_scanned - window + 1)
        rows = self.conn.execute(
            """
            SELECT block_number
            FROM blocks
            WHERE block_number BETWEEN ? AND ?
            ORDER BY block_number ASC
            """,
            (start, last_scanned),
        ).fetchall()
        present = {int(row["block_number"]) for row in rows}
        checked_start = min(present) if present else start
        missing = [block for block in range(checked_start, last_scanned + 1) if block not in present]
        return {
            "status": "gap" if missing else "ok",
            "last_scanned_block": last_scanned,
            "window_blocks": window,
            "window_start": start,
            "checked_start": checked_start,
            "leading_untracked_blocks": max(0, checked_start - start),
            "window_end": last_scanned,
            "expected_blocks": last_scanned - checked_start + 1,
            "scanned_blocks": len(present),
            "missing_blocks": len(missing),
            "missing_ranges": compact_ranges(missing),
            "first_missing_block": missing[0] if missing else None,
        }

    def repair_scan_coverage(self, window_blocks: int = 720) -> dict[str, Any]:
        coverage = self.scan_coverage(window_blocks)
        first_missing = coverage.get("first_missing_block")
        if not isinstance(first_missing, int):
            return {**coverage, "repaired": False, "old_last_scanned_block": coverage.get("last_scanned_block")}
        old_last = coverage.get("last_scanned_block")
        new_last = max(0, first_missing - 1)
        self.set_meta("last_scanned_block", new_last)
        repaired = {
            **coverage,
            "repaired": True,
            "old_last_scanned_block": old_last,
            "new_last_scanned_block": new_last,
        }
        self.append_event("scan_coverage_repaired", None, repaired)
        return repaired

    def mark_block(self, block_number: int, block_hash: str, block_timestamp: int, tx_count: int) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO blocks(block_number, block_hash, block_timestamp, tx_count, scanned_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (block_number, block_hash, block_timestamp, tx_count, utc_now()),
        )
        self.conn.commit()

    def mark_block_range(self, start_block: int, end_block: int, source: str = "range_scan") -> None:
        if end_block < start_block:
            return
        now = utc_now()
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO blocks(block_number, block_hash, block_timestamp, tx_count, scanned_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            ((block, source, None, None, now) for block in range(start_block, end_block + 1)),
        )
        self.conn.commit()

    def upsert_contract(self, contract: dict[str, Any]) -> bool:
        return bool(self.upsert_contract_result(contract)["contract_is_new"])

    def upsert_contract_result(self, contract: dict[str, Any]) -> dict[str, Any]:
        existed = (
            self.conn.execute("SELECT 1 FROM contracts WHERE address = ?", (contract["address"],)).fetchone()
            is not None
        )
        if contract.get("classification_deferred"):
            self.preserve_existing_classification(contract)
        now = utc_now()
        raw_json = json.dumps(contract, ensure_ascii=False, sort_keys=True)
        self.conn.execute(
            """
            INSERT INTO contracts(
              address, tx_hash, origin_tx_hash, discovery_source, log_index, related_json,
              deployer, block_number, block_timestamp, tx_index, value_wei, input_prefix,
              kind, name, symbol, decimals, total_supply, verified, contract_name, source_len, confidence,
              raw_json, first_seen_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
              origin_tx_hash = CASE
                WHEN excluded.discovery_source IN ('contract_creation', 'internal_create', 'internal_create2')
                THEN COALESCE(excluded.origin_tx_hash, contracts.origin_tx_hash)
                ELSE COALESCE(contracts.origin_tx_hash, excluded.origin_tx_hash)
              END,
              discovery_source = CASE
                WHEN excluded.discovery_source IN ('contract_creation', 'internal_create', 'internal_create2')
                THEN excluded.discovery_source
                ELSE COALESCE(contracts.discovery_source, excluded.discovery_source)
              END,
              log_index = CASE
                WHEN excluded.discovery_source IN ('contract_creation', 'internal_create', 'internal_create2')
                THEN excluded.log_index
                ELSE COALESCE(contracts.log_index, excluded.log_index)
              END,
              related_json = CASE
                WHEN excluded.discovery_source IN ('contract_creation', 'internal_create', 'internal_create2')
                THEN excluded.related_json
                ELSE COALESCE(contracts.related_json, excluded.related_json)
              END,
              kind = excluded.kind,
              name = excluded.name,
              symbol = excluded.symbol,
              decimals = excluded.decimals,
              total_supply = excluded.total_supply,
              verified = excluded.verified,
              contract_name = excluded.contract_name,
              source_len = excluded.source_len,
              confidence = excluded.confidence,
              raw_json = excluded.raw_json,
              updated_at = excluded.updated_at
            """,
            (
                contract["address"],
                contract["tx_hash"],
                contract.get("origin_tx_hash"),
                contract.get("discovery_source"),
                contract.get("log_index"),
                json.dumps(contract.get("related") or {}, ensure_ascii=False, sort_keys=True),
                contract.get("deployer"),
                contract["block_number"],
                contract.get("block_timestamp"),
                contract.get("tx_index"),
                contract.get("value_wei"),
                contract.get("input_prefix"),
                contract.get("kind"),
                contract.get("name"),
                contract.get("symbol"),
                contract.get("decimals"),
                contract.get("total_supply"),
                1 if contract.get("verified") else 0,
                contract.get("contract_name"),
                contract.get("source_len"),
                contract.get("confidence"),
                raw_json,
                now,
                now,
            ),
        )
        self.conn.commit()
        observation_result = self.record_observation_result(contract)
        enrichment_requeued = self.retry_enrichment_for_new_observation_source(
            contract,
            bool(observation_result.get("source_is_new")),
        )
        self.append_jsonl("contracts.jsonl", contract)
        return {
            "contract_is_new": not existed,
            **observation_result,
            "enrichment_requeued": enrichment_requeued,
        }

    def preserve_existing_classification(self, contract: dict[str, Any]) -> None:
        row = self.conn.execute(
            """
            SELECT kind, name, symbol, decimals, total_supply, verified, contract_name,
                   source_len, confidence, raw_json
            FROM contracts
            WHERE address = ?
            """,
            (contract["address"],),
        ).fetchone()
        if not row:
            return
        fields = ("kind", "name", "symbol", "decimals", "total_supply", "contract_name", "source_len", "confidence")
        for field in fields:
            value = row[field]
            if value not in (None, ""):
                contract[field] = value
        contract["verified"] = bool(row["verified"])
        try:
            existing_raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            existing_raw = {}
        if isinstance(existing_raw, dict) and not existing_raw.get("classification_deferred"):
            if existing_raw.get("source") and not contract.get("source"):
                contract["source"] = existing_raw.get("source")
            contract.pop("classification_deferred", None)

    def record_observation(self, contract: dict[str, Any], commit: bool = True) -> bool:
        return bool(self.record_observation_result(contract, commit=commit)["source_is_new"])

    def record_observation_result(self, contract: dict[str, Any], commit: bool = True) -> dict[str, Any]:
        address = contract["address"]
        discovery_source = contract.get("discovery_source") or "unknown"
        source_already_seen = (
            self.conn.execute(
                """
                SELECT 1
                FROM contract_observations
                WHERE address = ? AND discovery_source = ?
                LIMIT 1
                """,
                (address, discovery_source),
            ).fetchone()
            is not None
        )
        related_json = json.dumps(contract.get("related") or {}, ensure_ascii=False, sort_keys=True)
        payload_json = json.dumps(contract, ensure_ascii=False, sort_keys=True)
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO contract_observations(
              address, discovery_source, block_number, block_timestamp, origin_tx_hash,
              log_index, related_json, payload_json, observed_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                address,
                discovery_source,
                contract.get("block_number"),
                contract.get("block_timestamp"),
                contract.get("origin_tx_hash"),
                contract.get("log_index"),
                related_json,
                payload_json,
                utc_now(),
            ),
        )
        if commit:
            self.conn.commit()
        observation_is_new = cursor.rowcount > 0
        return {
            "observation_is_new": observation_is_new,
            "source_is_new": observation_is_new and not source_already_seen,
        }

    def retry_enrichment_for_new_observation_source(self, contract: dict[str, Any], source_is_new: bool) -> bool:
        if not source_is_new:
            return False
        if not should_retry_enrichment_for_new_source(contract.get("discovery_source")):
            return False
        cursor = self.conn.execute(
            """
            UPDATE enrichments
            SET searched_at = ?, status = 'retry'
            WHERE address = ? AND status IN ('ok', 'partial')
            """,
            (utc_now(), contract["address"]),
        )
        self.conn.commit()
        if cursor.rowcount <= 0:
            return False
        return True

    def pending_enrichment(self, limit: int) -> list[dict[str, Any]]:
        return self._pending_enrichment_rows(limit)

    def enrichment_context(self, address: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT c.*, COALESCE(o.observation_count, 0) AS observation_count, o.discovery_sources AS observation_sources
            FROM contracts c
            LEFT JOIN (
              SELECT address, COUNT(*) AS observation_count, GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ) o ON o.address = c.address
            WHERE c.address = ?
            """,
            (address,),
        ).fetchone()
        return dict(row) if row else None

    def update_contract_classification(self, address: str, classification: dict[str, Any]) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT raw_json FROM contracts WHERE address = ?", (address,)).fetchone()
        if not row:
            return None
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {"address": address}
        if not isinstance(raw, dict):
            raw = {"address": address}
        updated = {**raw, **classification}
        updated["address"] = address
        updated.pop("classification_deferred", None)
        raw_json = json.dumps(updated, ensure_ascii=False, sort_keys=True)
        self.conn.execute(
            """
            UPDATE contracts
            SET kind = ?,
                name = ?,
                symbol = ?,
                decimals = ?,
                total_supply = ?,
                verified = ?,
                contract_name = ?,
                source_len = ?,
                confidence = ?,
                raw_json = ?,
                updated_at = ?
            WHERE address = ?
            """,
            (
                updated.get("kind"),
                updated.get("name"),
                updated.get("symbol"),
                updated.get("decimals"),
                updated.get("total_supply"),
                1 if updated.get("verified") else 0,
                updated.get("contract_name"),
                updated.get("source_len"),
                updated.get("confidence"),
                raw_json,
                utc_now(),
                address,
            ),
        )
        self.conn.commit()
        self.append_jsonl("contracts.jsonl", updated)
        return updated

    def update_contract_source_metadata(self, address: str, source_metadata: dict[str, Any]) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT raw_json FROM contracts WHERE address = ?", (address,)).fetchone()
        if not row:
            return None
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {"address": address}
        if not isinstance(raw, dict):
            raw = {"address": address}
        existing_source = raw.get("source")
        if not isinstance(existing_source, dict):
            existing_source = {}
        raw["source"] = {**existing_source, **source_metadata}
        raw["address"] = address
        raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        self.conn.execute(
            """
            UPDATE contracts
            SET raw_json = ?,
                updated_at = ?
            WHERE address = ?
            """,
            (raw_json, utc_now(), address),
        )
        self.conn.commit()
        self.append_jsonl("contracts.jsonl", raw)
        return raw

    def queue_health(self) -> dict[str, Any]:
        pending_rows = self.conn.execute(
            """
            WITH observed AS (
              SELECT
                address,
                COUNT(*) AS observation_count,
                GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ),
            pending AS (
              SELECT
                c.address,
                c.discovery_source,
                c.block_number,
                COALESCE(o.observation_count, 0) AS observation_count,
                COALESCE(o.discovery_sources, '') AS observation_sources
              FROM contracts c
              LEFT JOIN enrichments e ON e.address = c.address
              LEFT JOIN observed o ON o.address = c.address
              WHERE e.address IS NULL OR e.status = 'retry'
            ),
            bucketed AS (
              SELECT
                CASE
                  WHEN observation_sources LIKE '%uniswap_v4%' THEN 'uniswap_v4'
                  WHEN observation_sources LIKE '%uniswap_%' OR observation_sources LIKE '%balancer_v2_tokens_registered%' THEN 'other_dex'
                  WHEN observation_sources LIKE '%activity_%' THEN 'activity'
                  WHEN observation_sources LIKE '%mint%' THEN 'mint'
                  WHEN observation_sources LIKE '%contract_creation%' OR discovery_source = 'contract_creation' THEN 'direct_creation'
                  WHEN observation_sources LIKE '%internal_create%' OR discovery_source IN ('internal_create', 'internal_create2') THEN 'internal_only'
                  ELSE 'other'
                END AS bucket,
                address,
                block_number,
                observation_count
              FROM pending
            )
            SELECT
              bucket,
              COUNT(*) AS contracts,
              COALESCE(SUM(observation_count), 0) AS observations,
              MIN(block_number) AS oldest_block,
              MAX(block_number) AS newest_block
            FROM bucketed
            GROUP BY bucket
            ORDER BY
              CASE bucket
                WHEN 'uniswap_v4' THEN 0
                WHEN 'other_dex' THEN 1
                WHEN 'activity' THEN 2
                WHEN 'mint' THEN 3
                WHEN 'direct_creation' THEN 4
                WHEN 'internal_only' THEN 5
                ELSE 5
              END
            """
        ).fetchall()
        pending_by_bucket = {
            row["bucket"]: {
                "contracts": row["contracts"],
                "observations": row["observations"],
                "oldest_block": row["oldest_block"],
                "newest_block": row["newest_block"],
            }
            for row in pending_rows
        }
        retry_count = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM contracts c
            JOIN enrichments e ON e.address = c.address
            WHERE e.status = 'retry'
            """
        ).fetchone()["n"]
        never_enriched_count = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            WHERE e.address IS NULL
            """
        ).fetchone()["n"]
        processing_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM enrichments WHERE status = 'processing'"
        ).fetchone()["n"]
        stale_processing_count = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM enrichments
            WHERE status = 'processing'
              AND julianday(searched_at) < julianday('now', ?)
            """,
            (f"-{PROCESSING_STALE_MINUTES} minutes",),
        ).fetchone()["n"]
        classification_deferred_pending = self.conn.execute(
            self._classification_deferred_backlog_sql("COUNT(*) AS n")
        ).fetchone()["n"]
        pending_total = sum(int(row["contracts"] or 0) for row in pending_rows)
        low_surface_pending = self.low_surface_backlog_count()
        return {
            "pending_total": pending_total,
            "never_enriched": never_enriched_count,
            "retry": retry_count,
            "processing": processing_count,
            "stale_processing": stale_processing_count,
            "stale_after_minutes": PROCESSING_STALE_MINUTES,
            "low_surface_pending": low_surface_pending,
            "classification_deferred_pending": int(classification_deferred_pending or 0),
            "priority_order": ["uniswap_v4", "other_dex", "activity", "mint", "direct_creation", "internal_only", "other"],
            "pending_by_bucket": pending_by_bucket,
        }

    def classification_deferred_backlog_count(self) -> int:
        row = self.conn.execute(self._classification_deferred_backlog_sql("COUNT(*) AS n")).fetchone()
        return int(row["n"] or 0) if row else 0

    def classification_deferred_backlog_candidates(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self.conn.execute(
            self._classification_deferred_backlog_sql(
                """
                c.address,
                c.block_number,
                c.discovery_source,
                e.status AS enrichment_status,
                COALESCE(o.observation_count, 0) AS observation_count,
                COALESCE(o.discovery_sources, '') AS observation_sources
                """,
                order_and_limit=True,
            ),
            (f"-{PROCESSING_STALE_MINUTES} minutes", limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def reserve_classification_targets(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                self._classification_deferred_backlog_sql(
                    """
                    c.*,
                    e.status AS enrichment_status,
                    COALESCE(o.observation_count, 0) AS observation_count,
                    COALESCE(o.discovery_sources, '') AS observation_sources
                    """,
                    order_and_limit=True,
                ),
                (f"-{PROCESSING_STALE_MINUTES} minutes", limit),
            ).fetchall()
            now = utc_now()
            for row in rows:
                self.conn.execute(
                    """
                    INSERT INTO enrichments(address, searched_at, query_json, evidence_json, status)
                    VALUES(?, ?, '[]', '{}', 'processing')
                    ON CONFLICT(address) DO UPDATE SET
                      searched_at = excluded.searched_at,
                      status = excluded.status
                    """,
                    (row["address"], now),
                )
            self.conn.commit()
            return [dict(row) for row in rows]
        except Exception:
            self.conn.rollback()
            raise

    def release_enrichment_targets(self, addresses: list[str]) -> int:
        addresses = [address for address in addresses if address]
        if not addresses:
            return 0
        placeholders = ",".join("?" for _ in addresses)
        cursor = self.conn.execute(
            f"""
            UPDATE enrichments
            SET searched_at = ?, status = 'retry'
            WHERE status = 'processing'
              AND address IN ({placeholders})
            """,
            [utc_now(), *addresses],
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def low_surface_backlog_count(self) -> int:
        row = self.conn.execute(self._low_surface_backlog_sql("COUNT(*) AS n")).fetchone()
        return int(row["n"] or 0) if row else 0

    def low_surface_backlog_candidates(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self.conn.execute(
            self._low_surface_backlog_sql(
                """
                c.address,
                c.block_number,
                c.discovery_source,
                e.status AS enrichment_status,
                COALESCE(o.observation_count, 0) AS observation_count,
                COALESCE(o.discovery_sources, '') AS observation_sources
                """,
                order_and_limit=True,
            ),
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def skip_low_surface_backlog(self, limit: int) -> dict[str, Any]:
        if limit <= 0:
            return {"skipped": 0, "limit": limit, "addresses": [], "remaining_low_surface_pending": self.low_surface_backlog_count()}

        from .scoring import score_project

        self.conn.execute("BEGIN IMMEDIATE")
        records: list[tuple[str, dict[str, Any]]] = []
        try:
            rows = self.conn.execute(
                self._low_surface_backlog_sql(
                    """
                    c.*,
                    e.status AS enrichment_status,
                    COALESCE(o.observation_count, 0) AS observation_count,
                    COALESCE(o.discovery_sources, '') AS observation_sources
                    """,
                    order_and_limit=True,
                ),
                (limit,),
            ).fetchall()
            for row in rows:
                contract = dict(row)
                contract["verified"] = bool(contract.get("verified"))
                evidence = {
                    "twitter_account": None,
                    "website": None,
                    "tweet_count": 0,
                    "address_mentions": 0,
                    "credible_address_mentions": 0,
                    "discussion_only": False,
                    "skip_reason": LOW_SURFACE_SKIP_REASON,
                }
                evidence = augment_project_metadata(contract, evidence)
                enrichment = {
                    "status": "ok",
                    "queries": [],
                    "evidence": evidence,
                    "score": score_project(contract, evidence),
                }
                score = enrichment["score"]
                self.conn.execute(
                    """
                    INSERT INTO enrichments(
                      address, searched_at, query_json, twitter_account, twitter_name, twitter_followers,
                      twitter_verified, website, tweet_count, address_mentions, evidence_json, score, tier,
                      score_breakdown_json, project_description, project_category, project_positioning, status
                    )
                    VALUES(?, ?, '[]', NULL, NULL, NULL, 0, NULL, 0, 0, ?, ?, ?, ?, ?, ?, ?, 'ok')
                    ON CONFLICT(address) DO UPDATE SET
                      searched_at = excluded.searched_at,
                      query_json = excluded.query_json,
                      twitter_account = excluded.twitter_account,
                      twitter_name = excluded.twitter_name,
                      twitter_followers = excluded.twitter_followers,
                      twitter_verified = excluded.twitter_verified,
                      website = excluded.website,
                      tweet_count = excluded.tweet_count,
                      address_mentions = excluded.address_mentions,
                      evidence_json = excluded.evidence_json,
                      score = excluded.score,
                      tier = excluded.tier,
                      score_breakdown_json = excluded.score_breakdown_json,
                      project_description = excluded.project_description,
                      project_category = excluded.project_category,
                      project_positioning = excluded.project_positioning,
                      status = excluded.status
                    """,
                    (
                        row["address"],
                        utc_now(),
                        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                        score.get("score"),
                        score.get("tier"),
                        json.dumps(score.get("breakdown") or {}, ensure_ascii=False, sort_keys=True),
                        evidence.get("project_description"),
                        evidence.get("project_category"),
                        evidence.get("project_positioning"),
                    ),
                )
                records.append((row["address"], enrichment))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        for address, enrichment in records:
            self.append_jsonl("projects.jsonl", {"address": address, **enrichment})
        if records:
            self.append_event(
                "low_surface_backlog_skipped",
                None,
                {
                    "skipped": len(records),
                    "limit": limit,
                    "addresses": [address for address, _ in records[:20]],
                    "skip_reason": LOW_SURFACE_SKIP_REASON,
                },
            )
        return {
            "skipped": len(records),
            "limit": limit,
            "addresses": [address for address, _ in records[:20]],
            "remaining_low_surface_pending": self.low_surface_backlog_count(),
        }

    def _low_surface_backlog_sql(self, select_sql: str, order_and_limit: bool = False) -> str:
        source_list = ", ".join(f"'{source}'" for source in sorted(CREATION_ONLY_SOURCES))
        order_sql = """
            ORDER BY
              CASE WHEN e.status = 'retry' THEN 0 ELSE 1 END,
              c.block_number ASC,
              c.tx_index ASC
            LIMIT ?
        """ if order_and_limit else ""
        return f"""
            WITH observed AS (
              SELECT
                address,
                COUNT(*) AS observation_count,
                GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources,
                SUM(CASE WHEN discovery_source IN ({source_list}) THEN 1 ELSE 0 END) AS creation_observations,
                SUM(CASE WHEN discovery_source NOT IN ({source_list}) THEN 1 ELSE 0 END) AS non_creation_observations
              FROM contract_observations
              GROUP BY address
            )
            SELECT {select_sql}
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            LEFT JOIN observed o ON o.address = c.address
            WHERE (e.address IS NULL OR e.status = 'retry')
              AND COALESCE(c.kind, '') = 'contract'
              AND COALESCE(c.verified, 0) = 0
              AND TRIM(COALESCE(c.name, '')) = ''
              AND TRIM(COALESCE(c.symbol, '')) = ''
              AND TRIM(COALESCE(c.contract_name, '')) = ''
              AND c.raw_json NOT LIKE '%"classification_deferred"%'
              AND COALESCE(o.creation_observations, 0) > 0
              AND COALESCE(o.non_creation_observations, 0) = 0
            {order_sql}
        """

    def _classification_deferred_backlog_sql(self, select_sql: str, order_and_limit: bool = False) -> str:
        order_sql = """
            ORDER BY
              CASE
                WHEN o.discovery_sources LIKE '%uniswap_v4%' THEN 0
                WHEN o.discovery_sources LIKE '%uniswap_%' OR o.discovery_sources LIKE '%balancer_v2_tokens_registered%' THEN 1
                WHEN o.discovery_sources LIKE '%activity_%' THEN 2
                WHEN o.discovery_sources LIKE '%mint%' THEN 3
                WHEN c.discovery_source = 'contract_creation' THEN 4
                ELSE 5
              END,
              CASE WHEN e.status = 'retry' THEN 0 ELSE 1 END,
              COALESCE(o.observation_count, 0) DESC,
              c.block_number ASC,
              c.tx_index ASC
            LIMIT ?
        """ if order_and_limit else ""
        stale_clause = (
            " OR (e.status = 'processing' AND julianday(e.searched_at) < julianday('now', ?))"
            if order_and_limit
            else ""
        )
        return f"""
            WITH observed AS (
              SELECT
                address,
                COUNT(*) AS observation_count,
                GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            )
            SELECT {select_sql}
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            LEFT JOIN observed o ON o.address = c.address
            WHERE (e.address IS NULL OR e.status = 'retry'{stale_clause})
              AND c.raw_json LIKE '%"classification_deferred"%'
            {order_sql}
        """

    def _pending_enrichment_rows(self, limit: int, exclude_addresses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        excluded = normalized_address_list(exclude_addresses)
        exclude_sql = ""
        params: list[Any] = [f"-{PROCESSING_STALE_MINUTES} minutes"]
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclude_sql = f"AND LOWER(c.address) NOT IN ({placeholders})"
            params.extend(excluded)
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT c.*, COALESCE(o.observation_count, 0) AS observation_count, o.discovery_sources AS observation_sources
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            LEFT JOIN (
              SELECT address, COUNT(*) AS observation_count, GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ) o ON o.address = c.address
            WHERE (
               e.address IS NULL
               OR e.status = 'retry'
               OR (e.status = 'processing' AND julianday(e.searched_at) < julianday('now', ?))
            )
              {exclude_sql}
            ORDER BY
              CASE
                WHEN o.discovery_sources LIKE '%uniswap_v4%' THEN 0
                WHEN o.discovery_sources LIKE '%uniswap_%' OR o.discovery_sources LIKE '%balancer_v2_tokens_registered%' THEN 1
                WHEN o.discovery_sources LIKE '%activity_%' THEN 2
                WHEN o.discovery_sources LIKE '%mint%' THEN 3
                WHEN c.discovery_source = 'contract_creation' THEN 4
                ELSE 5
              END,
              CASE WHEN e.status = 'retry' THEN 0 ELSE 1 END,
              COALESCE(o.observation_count, 0) DESC,
              c.block_number ASC,
              c.tx_index ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def reserve_enrichment_targets(self, limit: int, exclude_addresses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._pending_enrichment_rows(limit, exclude_addresses)
            now = utc_now()
            for row in rows:
                self.conn.execute(
                    """
                    INSERT INTO enrichments(address, searched_at, query_json, evidence_json, status)
                    VALUES(?, ?, '[]', '{}', 'processing')
                    ON CONFLICT(address) DO UPDATE SET
                      searched_at = excluded.searched_at,
                      status = excluded.status
                    """,
                    (row["address"], now),
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise

    def reset_processing_enrichments(self, older_than_minutes: int = 0) -> int:
        if older_than_minutes > 0:
            cursor = self.conn.execute(
                """
                UPDATE enrichments
                SET status = 'retry'
                WHERE status = 'processing'
                  AND julianday(searched_at) < julianday('now', ?)
                """,
                (f"-{older_than_minutes} minutes",),
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE enrichments
                SET status = 'retry'
                WHERE status = 'processing'
                """
            )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def requeue_enrichments(
        self,
        tiers: list[str] | None = None,
        limit: int | None = None,
        addresses: list[str] | None = None,
    ) -> int:
        where = ["status IN ('ok', 'partial')"]
        params: list[Any] = []
        normalized_addresses = [str(address).lower() for address in addresses or [] if str(address or "").strip()]
        if normalized_addresses:
            placeholders = ",".join("?" for _ in normalized_addresses)
            where.append(f"LOWER(address) IN ({placeholders})")
            params.extend(normalized_addresses)
        if tiers:
            placeholders = ",".join("?" for _ in tiers)
            where.append(f"tier IN ({placeholders})")
            params.extend(tiers)
        sql = f"""
            SELECT address
            FROM enrichments
            WHERE {' AND '.join(where)}
            ORDER BY searched_at ASC
        """
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        addresses = [row["address"] for row in rows]
        if not addresses:
            return 0
        placeholders = ",".join("?" for _ in addresses)
        cursor = self.conn.execute(
            f"""
            UPDATE enrichments
            SET status = 'retry', searched_at = ?
            WHERE address IN ({placeholders})
            """,
            [utc_now(), *addresses],
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def rescore_enrichments(self, tiers: list[str] | None = None, limit: int | None = None) -> int:
        from .scoring import score_project

        where = ["e.status IN ('ok', 'partial')"]
        params: list[Any] = []
        if tiers:
            placeholders = ",".join("?" for _ in tiers)
            where.append(f"e.tier IN ({placeholders})")
            params.extend(tiers)
        sql = f"""
            SELECT
              c.raw_json,
              e.address,
              e.evidence_json,
              COALESCE(o.observation_count, 0) AS observation_count,
              o.discovery_sources AS observation_sources
            FROM enrichments e
            JOIN contracts c ON c.address = e.address
            LEFT JOIN (
              SELECT address, COUNT(*) AS observation_count, GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ) o ON o.address = e.address
            WHERE {' AND '.join(where)}
            ORDER BY e.score DESC, e.searched_at ASC
        """
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        changed = 0
        for row in rows:
            try:
                contract = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                contract = {"address": row["address"]}
            if not isinstance(contract, dict):
                contract = {"address": row["address"]}
            contract["observation_count"] = row["observation_count"] or 0
            contract["observation_sources"] = row["observation_sources"] or ""
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except json.JSONDecodeError:
                evidence = {}
            evidence = augment_project_metadata(contract, evidence if isinstance(evidence, dict) else {}, force=True)
            score = score_project(contract, evidence if isinstance(evidence, dict) else {})
            self.conn.execute(
                """
                UPDATE enrichments
                SET score = ?, tier = ?, score_breakdown_json = ?,
                    evidence_json = ?, project_description = ?, project_category = ?, project_positioning = ?
                WHERE address = ?
                """,
                (
                    score.get("score"),
                    score.get("tier"),
                    json.dumps(score.get("breakdown") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    evidence.get("project_description"),
                    evidence.get("project_category"),
                    evidence.get("project_positioning"),
                    row["address"],
                ),
            )
            changed += 1
        self.conn.commit()
        return changed

    def source_url_backfill_count(self, force: bool = False) -> int:
        return len(self.source_url_backfill_candidates(None, force=force))

    def source_url_backfill_candidates(self, limit: int | None, force: bool = False) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        sql = """
            WITH observed AS (
              SELECT
                address,
                COUNT(*) AS observation_count,
                GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            )
            SELECT
              c.address,
              c.block_number,
              c.kind,
              c.name,
              c.symbol,
              c.contract_name,
              c.source_len,
              c.raw_json,
              e.status AS enrichment_status,
              e.tier,
              e.website,
              COALESCE(o.observation_count, 0) AS observation_count,
              COALESCE(o.discovery_sources, '') AS observation_sources,
              CASE
                WHEN COALESCE(o.discovery_sources, '') LIKE '%uniswap_v4%' THEN 'uniswap_v4'
                WHEN COALESCE(o.discovery_sources, '') LIKE '%uniswap_%' OR COALESCE(o.discovery_sources, '') LIKE '%balancer_v2_tokens_registered%' THEN 'other_dex'
                WHEN COALESCE(o.discovery_sources, '') LIKE '%activity_%' THEN 'activity'
                WHEN COALESCE(o.discovery_sources, '') LIKE '%mint%' THEN 'mint'
                WHEN COALESCE(o.discovery_sources, '') LIKE '%contract_creation%' OR c.discovery_source = 'contract_creation' THEN 'direct_creation'
                WHEN COALESCE(o.discovery_sources, '') LIKE '%internal_create%' OR c.discovery_source IN ('internal_create', 'internal_create2') THEN 'internal_only'
                ELSE 'other'
              END AS source_url_backfill_bucket
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            LEFT JOIN observed o ON o.address = c.address
            WHERE COALESCE(c.verified, 0) = 1
              AND COALESCE(c.source_len, 0) > 0
              AND (e.address IS NULL OR TRIM(COALESCE(e.website, '')) = '')
            ORDER BY
              CASE WHEN e.status IN ('ok', 'partial') THEN 0
                   WHEN e.status IS NULL OR e.status = 'retry' THEN 1
                   ELSE 2
              END,
              CASE e.tier
                   WHEN 'high' THEN 0
                   WHEN 'medium' THEN 1
                   WHEN 'watch' THEN 2
                   WHEN 'low' THEN 3
                   ELSE 4
              END,
              CASE
                WHEN COALESCE(o.discovery_sources, '') LIKE '%uniswap_v4%' THEN 0
                WHEN COALESCE(o.discovery_sources, '') LIKE '%uniswap_%' OR COALESCE(o.discovery_sources, '') LIKE '%balancer_v2_tokens_registered%' THEN 1
                WHEN COALESCE(o.discovery_sources, '') LIKE '%activity_%' THEN 2
                WHEN COALESCE(o.discovery_sources, '') LIKE '%mint%' THEN 3
                WHEN COALESCE(o.discovery_sources, '') LIKE '%contract_creation%' OR c.discovery_source = 'contract_creation' THEN 4
                WHEN COALESCE(c.kind, '') IN ('erc20', 'erc721', 'erc1155', 'named_contract') THEN 5
                ELSE 6
              END,
              CASE
                WHEN COALESCE(c.kind, '') IN ('erc20', 'erc721', 'erc1155', 'named_contract')
                  AND (TRIM(COALESCE(c.name, '')) <> '' OR TRIM(COALESCE(c.symbol, '')) <> '') THEN 0
                WHEN TRIM(COALESCE(c.contract_name, '')) <> '' THEN 1
                ELSE 2
              END,
              COALESCE(o.observation_count, 0) DESC,
              c.block_number DESC
        """
        rows = self.conn.execute(sql).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_json = item.pop("raw_json", "")
            if not force and raw_payload_has_source_url_state(raw_json):
                continue
            result.append(item)
            if limit is not None and limit > 0 and len(result) >= limit:
                break
        return result

    def requeue_enrichment_for_source_url(self, address: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE enrichments
            SET searched_at = ?, status = 'retry'
            WHERE address = ?
              AND status IN ('ok', 'partial')
              AND TRIM(COALESCE(website, '')) = ''
            """,
            (utc_now(), address),
        )
        self.conn.commit()
        return bool(cursor.rowcount and cursor.rowcount > 0)

    def enrichment_targets(
        self,
        limit: int,
        force: bool = False,
        exclude_addresses: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not force:
            return self.reserve_enrichment_targets(limit, exclude_addresses)
        excluded = normalized_address_list(exclude_addresses)
        exclude_sql = ""
        params: list[Any] = []
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclude_sql = f"WHERE LOWER(c.address) NOT IN ({placeholders})"
            params.extend(excluded)
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT c.*, COALESCE(o.observation_count, 0) AS observation_count, o.discovery_sources AS observation_sources
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            LEFT JOIN (
              SELECT address, COUNT(*) AS observation_count, GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ) o ON o.address = c.address
            {exclude_sql}
            ORDER BY CASE WHEN e.searched_at IS NULL THEN 0 ELSE 1 END, e.searched_at ASC, c.block_number ASC, c.tx_index ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_enrichment(self, address: str, enrichment: dict[str, Any]) -> None:
        score = enrichment.get("score") or {}
        evidence = enrichment.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}
        context = self.enrichment_context(address)
        if context is not None:
            evidence = augment_project_metadata(context, evidence)
            enrichment["evidence"] = evidence
        self.conn.execute(
            """
            INSERT INTO enrichments(
              address, searched_at, query_json, twitter_account, twitter_name, twitter_followers,
              twitter_verified, website, tweet_count, address_mentions, evidence_json, score, tier,
              score_breakdown_json, project_description, project_category, project_positioning, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
              searched_at = excluded.searched_at,
              query_json = excluded.query_json,
              twitter_account = excluded.twitter_account,
              twitter_name = excluded.twitter_name,
              twitter_followers = excluded.twitter_followers,
              twitter_verified = excluded.twitter_verified,
              website = excluded.website,
              tweet_count = excluded.tweet_count,
              address_mentions = excluded.address_mentions,
              evidence_json = excluded.evidence_json,
              score = excluded.score,
              tier = excluded.tier,
              score_breakdown_json = excluded.score_breakdown_json,
              project_description = excluded.project_description,
              project_category = excluded.project_category,
              project_positioning = excluded.project_positioning,
              status = excluded.status
            """,
            (
                address,
                utc_now(),
                json.dumps(enrichment.get("queries") or [], ensure_ascii=False),
                evidence.get("twitter_account"),
                evidence.get("twitter_name"),
                evidence.get("twitter_followers"),
                1 if evidence.get("twitter_verified") else 0,
                evidence.get("website"),
                evidence.get("tweet_count"),
                evidence.get("address_mentions"),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                score.get("score"),
                score.get("tier"),
                json.dumps(score.get("breakdown") or {}, ensure_ascii=False, sort_keys=True),
                evidence.get("project_description"),
                evidence.get("project_category"),
                evidence.get("project_positioning"),
                enrichment.get("status", "ok"),
            ),
        )
        self.conn.commit()
        self.append_jsonl("projects.jsonl", {"address": address, **enrichment})

    def append_event(self, event_type: str, address: str | None, payload: dict[str, Any]) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.conn.execute(
            "INSERT INTO project_events(created_at, event_type, address, payload_json) VALUES(?, ?, ?, ?)",
            (utc_now(), event_type, address, payload_json),
        )
        self.conn.commit()
        self.append_jsonl("events.jsonl", {"event_type": event_type, "address": address, "payload": payload})

    def add_project_review(
        self,
        project_key: str,
        decision: str,
        reviewer: str,
        note: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        reviewed_at = utc_now()
        snapshot_json = json.dumps(snapshot or {}, ensure_ascii=False, sort_keys=True)
        cursor = self.conn.execute(
            """
            INSERT INTO project_reviews(project_key, reviewed_at, reviewer, decision, note, snapshot_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (project_key, reviewed_at, reviewer, decision, note, snapshot_json),
        )
        self.conn.commit()
        record = {
            "id": cursor.lastrowid,
            "project_key": project_key,
            "reviewed_at": reviewed_at,
            "reviewer": reviewer,
            "decision": decision,
            "note": note,
            "snapshot": snapshot or {},
        }
        self.append_jsonl("reviews.jsonl", record)
        self.append_event("project_reviewed", None, record)
        return record

    def latest_project_reviews(self, project_keys: list[str]) -> dict[str, dict[str, Any]]:
        keys = [key for key in project_keys if key]
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = self.conn.execute(
            f"""
            SELECT r.project_key, r.reviewed_at, r.reviewer, r.decision, r.note, r.snapshot_json
            FROM project_reviews r
            JOIN (
              SELECT project_key, MAX(id) AS id
              FROM project_reviews
              WHERE project_key IN ({placeholders})
              GROUP BY project_key
            ) latest ON latest.id = r.id
            """,
            keys,
        ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            try:
                item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            except json.JSONDecodeError:
                item["snapshot"] = {}
            result[item["project_key"]] = item
        return result

    def add_website_check(self, project_key: str, website: str, check: dict[str, Any]) -> dict[str, Any]:
        checked_at = utc_now()
        payload_json = json.dumps(check or {}, ensure_ascii=False, sort_keys=True)
        twitter_links = check.get("twitter_links") if isinstance(check, dict) else []
        matched_terms = check.get("matched_terms") if isinstance(check, dict) else []
        cursor = self.conn.execute(
            """
            INSERT INTO website_checks(
              project_key, website, checked_at, status, http_status, final_url,
              title, description, twitter_links_json, matched_terms_json, error, payload_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_key,
                website,
                checked_at,
                str(check.get("status") or "fail"),
                check.get("http_status"),
                check.get("final_url") or "",
                check.get("title") or "",
                check.get("description") or "",
                json.dumps(twitter_links if isinstance(twitter_links, list) else [], ensure_ascii=False),
                json.dumps(matched_terms if isinstance(matched_terms, list) else [], ensure_ascii=False),
                check.get("error") or "",
                payload_json,
            ),
        )
        self.conn.commit()
        record = {
            "id": cursor.lastrowid,
            "project_key": project_key,
            "website": website,
            "checked_at": checked_at,
            **(check or {}),
        }
        self.append_jsonl("website_checks.jsonl", record)
        self.append_event("website_checked", None, record)
        return record

    def apply_website_check_to_enrichments(self, website: str, check: dict[str, Any]) -> list[dict[str, Any]]:
        from .scoring import score_project

        if not website:
            return []
        rows = self.conn.execute(
            """
            SELECT
              c.raw_json,
              e.address,
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
            WHERE e.status IN ('ok', 'partial')
              AND LOWER(TRIM(COALESCE(e.website, ''))) = LOWER(TRIM(?))
            """,
            (website,),
        ).fetchall()
        changed: list[dict[str, Any]] = []
        for row in rows:
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except json.JSONDecodeError:
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            try:
                contract = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                contract = {"address": row["address"]}
            if not isinstance(contract, dict):
                contract = {"address": row["address"]}
            contract["observation_count"] = row["observation_count"] or 0
            contract["observation_sources"] = row["observation_sources"] or ""
            evidence.update(website_check_evidence(check))
            evidence = augment_project_metadata(contract, evidence, force=True)
            score = score_project(contract, evidence)
            self.conn.execute(
                """
                UPDATE enrichments
                SET evidence_json = ?, score = ?, tier = ?, score_breakdown_json = ?,
                    project_description = ?, project_category = ?, project_positioning = ?
                WHERE address = ?
                """,
                (
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    score.get("score"),
                    score.get("tier"),
                    json.dumps(score.get("breakdown") or {}, ensure_ascii=False, sort_keys=True),
                    evidence.get("project_description"),
                    evidence.get("project_category"),
                    evidence.get("project_positioning"),
                    row["address"],
                ),
            )
            changed.append({"address": row["address"], "score": score.get("score"), "tier": score.get("tier")})
        self.conn.commit()
        if changed:
            self.append_event(
                "website_check_applied",
                None,
                {"website": website, "status": check.get("status"), "addresses": changed},
            )
        return changed

    def latest_website_check(self, project_key: str, website: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM website_checks
            WHERE project_key = ? AND website = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (project_key, website),
        ).fetchone()
        if not row:
            row = self.conn.execute(
                """
                SELECT *
                FROM website_checks
                WHERE website = ?
                ORDER BY checked_at DESC, id DESC
                LIMIT 1
                """,
                (website,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        for source, target in (
            ("twitter_links_json", "twitter_links"),
            ("matched_terms_json", "matched_terms"),
            ("payload_json", "payload"),
        ):
            try:
                item[target] = json.loads(item.pop(source) or ("{}" if source == "payload_json" else "[]"))
            except json.JSONDecodeError:
                item[target] = {} if source == "payload_json" else []
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        for key in ("failure_kind", "fallback_url", "fallback_reason"):
            if key not in item or not item.get(key):
                item[key] = payload.get(key) or ""
        return item

    def append_jsonl(self, filename: str, payload: dict[str, Any]) -> None:
        path = self.data_dir / filename
        record = {"written_at": utc_now(), **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def summary(self) -> dict[str, Any]:
        contract_count = self.conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
        enriched_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM enrichments WHERE status IN ('ok', 'partial')"
        ).fetchone()["n"]
        processing_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM enrichments WHERE status = 'processing'"
        ).fetchone()["n"]
        high_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM enrichments WHERE status IN ('ok', 'partial') AND tier IN ('high','medium')"
        ).fetchone()["n"]
        pending_count = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            WHERE e.address IS NULL OR e.status = 'retry'
            """
        ).fetchone()["n"]
        by_source = {
            (row["discovery_source"] or "unknown"): row["n"]
            for row in self.conn.execute(
                """
                SELECT discovery_source, COUNT(*) AS n
                FROM contracts
                GROUP BY discovery_source
                ORDER BY n DESC
                """
            ).fetchall()
        }
        by_tier = {
            (row["tier"] or "unknown"): row["n"]
            for row in self.conn.execute(
                """
                SELECT tier, COUNT(*) AS n
                FROM enrichments
                WHERE status IN ('ok', 'partial')
                GROUP BY tier
                ORDER BY n DESC
                """
            ).fetchall()
        }
        by_status = {
            (row["status"] or "unknown"): row["n"]
            for row in self.conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM enrichments
                GROUP BY status
                ORDER BY n DESC
                """
            ).fetchall()
        }
        observation_count = self.conn.execute("SELECT COUNT(*) AS n FROM contract_observations").fetchone()["n"]
        observation_by_source = {
            (row["discovery_source"] or "unknown"): row["n"]
            for row in self.conn.execute(
                """
                SELECT discovery_source, COUNT(*) AS n
                FROM contract_observations
                GROUP BY discovery_source
                ORDER BY n DESC
                """
            ).fetchall()
        }
        return {
            "last_scanned_block": self.get_meta_int("last_scanned_block"),
            "contracts": contract_count,
            "enriched": enriched_count,
            "processing_enrichment": processing_count,
            "pending_enrichment": pending_count,
            "medium_or_high": high_count,
            "by_source": by_source,
            "by_tier": by_tier,
            "by_status": by_status,
            "observations": observation_count,
            "observations_by_source": observation_by_source,
        }

    def project_rows(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              c.address, c.discovery_source, c.origin_tx_hash, c.block_number, c.block_timestamp,
              c.kind, c.name, c.symbol, c.verified, c.contract_name,
              e.score, e.tier, e.twitter_account, e.twitter_name, e.twitter_followers,
              e.website, e.tweet_count, e.address_mentions, e.status, e.score_breakdown_json,
              e.project_description, e.project_category, e.project_positioning
              , COALESCE(o.observation_count, 0) AS observation_count
              , o.discovery_sources AS observation_sources
            FROM contracts c
            LEFT JOIN enrichments e ON e.address = c.address
            LEFT JOIN (
              SELECT address, COUNT(*) AS observation_count, GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ) o ON o.address = c.address
            ORDER BY COALESCE(e.score, -1) DESC, c.block_number DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["score_breakdown"] = json.loads(item.pop("score_breakdown_json") or "{}")
            except json.JSONDecodeError:
                item["score_breakdown"] = {}
            result.append(item)
        return result

    def project_groups(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._project_candidate_rows()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(project_identity_key(row), []).append(row)
        result = [build_project_group(key, items) for key, items in groups.items()]
        result.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                TIER_RANK.get(str(item.get("tier") or ""), -1),
                int(item.get("address_count") or 0),
                int(item.get("last_block") or 0),
            ),
            reverse=True,
        )
        selected = result[:limit]
        reviews = self.latest_project_reviews([str(item.get("project_key") or "") for item in selected])
        for item in selected:
            item["review"] = reviews.get(str(item.get("project_key") or ""))
        return selected

    def _project_candidate_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              c.address, c.discovery_source, c.origin_tx_hash, c.block_number, c.block_timestamp,
              c.kind, c.name, c.symbol, c.verified, c.contract_name,
              e.score, e.tier, e.twitter_account, e.twitter_name, e.twitter_followers,
              e.website, e.tweet_count, e.address_mentions, e.status, e.score_breakdown_json,
              e.project_description, e.project_category, e.project_positioning,
              COALESCE(o.observation_count, 0) AS observation_count,
              o.discovery_sources AS observation_sources
            FROM contracts c
            JOIN enrichments e ON e.address = c.address
            LEFT JOIN (
              SELECT address, COUNT(*) AS observation_count, GROUP_CONCAT(DISTINCT discovery_source) AS discovery_sources
              FROM contract_observations
              GROUP BY address
            ) o ON o.address = c.address
            WHERE e.status IN ('ok', 'partial')
            ORDER BY COALESCE(e.score, -1) DESC, c.block_number DESC
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["score_breakdown"] = json.loads(item.pop("score_breakdown_json") or "{}")
            except json.JSONDecodeError:
                item["score_breakdown"] = {}
            result.append(item)
        return result


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def website_check_evidence(check: dict[str, Any]) -> dict[str, Any]:
    payload = check if isinstance(check, dict) else {}
    return {
        "website_check_status": str(payload.get("status") or ""),
        "website_check_http_status": payload.get("http_status"),
        "website_check_final_url": payload.get("final_url") or "",
        "website_check_title": payload.get("title") or "",
        "website_check_description": payload.get("description") or "",
        "website_check_matched_terms": payload.get("matched_terms") if isinstance(payload.get("matched_terms"), list) else [],
        "website_check_twitter_links": payload.get("twitter_links") if isinstance(payload.get("twitter_links"), list) else [],
        "website_check_error": payload.get("error") or "",
        "website_check_failure_kind": payload.get("failure_kind") or "",
        "website_check_fallback_url": payload.get("fallback_url") or "",
        "website_check_fallback_reason": payload.get("fallback_reason") or "",
        "website_check_checked_at": payload.get("checked_at") or "",
    }


def raw_payload_has_source_url_state(raw_json: Any) -> bool:
    if not isinstance(raw_json, str) or not raw_json:
        return False
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    source = payload.get("source")
    if not isinstance(source, dict):
        return False
    return isinstance(source.get("urls"), list)


def compact_ranges(values: list[int]) -> list[dict[str, int]]:
    if not values:
        return []
    ranges = []
    start = values[0]
    prev = values[0]
    for value in values[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append({"start": start, "end": prev, "count": prev - start + 1})
        start = value
        prev = value
    ranges.append({"start": start, "end": prev, "count": prev - start + 1})
    return ranges


def normalize_cycle_role(role: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(role or "").lower())
    normalized = "_".join(part for part in normalized.split("_") if part)
    return normalized or "default"


def latest_runtime_role(roles: dict[str, dict[str, Any]]) -> str | None:
    if not roles:
        return None
    return max(
        roles,
        key=lambda role: str(
            roles[role].get("last_cycle_finished_at")
            or roles[role].get("last_cycle_started_at")
            or ""
        ),
    )


def project_identity_key(row: dict[str, Any]) -> str:
    twitter = str(row.get("twitter_account") or "").strip().lstrip("@").lower()
    if twitter:
        return f"twitter:{twitter}"
    domain = website_domain(row.get("website"))
    if domain:
        return f"site:{domain}"
    symbol = normalized_identity_text(meaningful_symbol(row.get("symbol")))
    if symbol:
        return f"symbol:{symbol}"
    name = normalized_identity_text(meaningful_name(row.get("name")) or meaningful_name(row.get("contract_name")))
    if name:
        return f"name:{name}"
    return f"address:{str(row.get('address') or '').lower()}"


def build_project_group(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            int(row.get("score") or 0),
            TIER_RANK.get(str(row.get("tier") or ""), -1),
            int(row.get("observation_count") or 0),
        ),
        reverse=True,
    )
    primary = sorted_rows[0]
    all_sources = sorted(
        {
            source.strip()
            for row in rows
            for source in str(row.get("observation_sources") or row.get("discovery_source") or "").split(",")
            if source.strip()
        }
    )
    names = sorted({str(row.get("name")) for row in rows if row.get("name")})
    symbols = sorted({str(row.get("symbol")) for row in rows if row.get("symbol")})
    addresses = [str(row.get("address") or "") for row in sorted_rows if row.get("address")]
    twitter = first_present(row.get("twitter_account") for row in sorted_rows)
    website = first_present(row.get("website") for row in sorted_rows)
    project_description = first_present(row.get("project_description") for row in sorted_rows)
    project_category = first_present(row.get("project_category") for row in sorted_rows)
    project_positioning = first_present(row.get("project_positioning") for row in sorted_rows)
    return {
        "project_key": key,
        "label": project_label(primary, names, symbols),
        "score": primary.get("score"),
        "tier": best_tier(row.get("tier") for row in rows),
        "score_breakdown": primary.get("score_breakdown") or {},
        "twitter_account": twitter,
        "website": website,
        "project_description": project_description,
        "project_category": project_category,
        "project_positioning": project_positioning,
        "address_count": len(set(addresses)),
        "primary_address": primary.get("address"),
        "addresses": sorted(set(addresses)),
        "symbols": symbols,
        "names": names,
        "observation_sources": ",".join(all_sources),
        "observation_count": sum(int(row.get("observation_count") or 0) for row in rows),
        "first_block": min((int(row.get("block_number") or 0) for row in rows), default=None),
        "last_block": max((int(row.get("block_number") or 0) for row in rows), default=None),
    }


def project_label(primary: dict[str, Any], names: list[str], symbols: list[str]) -> str:
    primary_name = meaningful_name(primary.get("name"))
    if primary_name:
        return primary_name
    twitter_name = str(primary.get("twitter_name") or "").strip()
    if twitter_name:
        return twitter_name
    primary_symbol = meaningful_symbol(primary.get("symbol"))
    if primary_symbol:
        return primary_symbol
    if names:
        return names[0]
    if symbols:
        return symbols[0]
    return str(primary.get("primary_address") or primary.get("address") or "")


def best_tier(tiers: Iterable[Any]) -> str | None:
    best = None
    for tier in tiers:
        tier_text = str(tier or "")
        if best is None or TIER_RANK.get(tier_text, -1) > TIER_RANK.get(best, -1):
            best = tier_text
    return best


def first_present(values: Iterable[Any]) -> Any:
    for value in values:
        if value:
            return value
    return None


def website_domain(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.netloc or parsed.path).lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def normalized_identity_text(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = " ".join(text.replace("_", " ").replace("-", " ").split())
    return normalized or None
