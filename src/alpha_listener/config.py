from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .chains import ChainProfile, chain_profile

DEFAULT_ACTIVITY_CUSTOM_EVENT_TOPICS = (
    # HASH98-style proof mint event: Minted(address,uint256,bytes16,bytes32,uint256)
    "0x3da2bd01ee75c5ddd62a82c0f79e99834ee5cc995f86b9c87737292dd54bfc1d",
    # Common sparse mining/claim/reward event surfaces. These are intentionally
    # much cheaper than scanning every arbitrary call.
    "0x30385c845b448a36257a6a1716e6ad2e1bc2cbe333cde1e69fe849ad6511adfe",
    "0x0f6798a560793a54c3bcfe86a93cde1e73087d944c0ea20544137d4121396885",
    "0xd8138f8a3f377c5259ca548e70e4c2de94f129f5a11036a15b69513cba2b426a",
    "0x47cee97cb7acd717b3c0aa1435d004cd5b3c8c57d70dbceb4e4458bbd60e39d4",
    "0x3ad10ba9777a3bc21180a465e5459861d07cbdb271af9a0f10c993b365b760f8",
    "0x634a203b5db8721108a45f719c962a5efff15dfebb1005764878de1795d47716",
    "0x106f923f993c2149d49b4255ff723acafa1f2d94393f561d3eda32ae348f7241",
    "0xfc30cddea38e2bf4d6ea7d3f9ed3b6ad7f176419f4963bd81318067a4aee73fe",
    "0xc9695243a805adb74c91f28311176c65b417e842d5699893cef56d18bfa48cba",
    "0x121c5042302bae5fc561fbc64368f297ca60a880878e1e3a7f7e9380377260bf",
    "0x896e034966eaaf1adc54acc0f257056febbd300c9e47182cf761982cf1f5e430",
)
DEFAULT_ACTIVITY_CUSTOM_EVENT_TOPICS_CSV = ",".join(DEFAULT_ACTIVITY_CUSTOM_EVENT_TOPICS)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def chain_env_value(
    profile: ChainProfile,
    suffix: str,
    default: str,
    *,
    legacy_names: tuple[str, ...] = (),
    apply_legacy_to_all: bool = False,
) -> str:
    for prefix in profile.env_prefixes:
        for key in (f"ALPHA_{prefix}_{suffix}", f"{prefix}_{suffix}"):
            value = os.environ.get(key)
            if value not in (None, ""):
                return str(value)
    if profile.slug == "ethereum" or apply_legacy_to_all or os.environ.get("ALPHA_APPLY_GLOBAL_TO_ALL_CHAINS") == "1":
        for key in legacy_names:
            value = os.environ.get(key)
            if value not in (None, ""):
                return str(value)
    return default


def chain_env_int(profile: ChainProfile, suffix: str, default: int, legacy_name: str) -> int:
    return int(
        chain_env_value(
            profile,
            suffix,
            str(default),
            legacy_names=(legacy_name,),
        )
    )


def chain_env_float(profile: ChainProfile, suffix: str, default: float, legacy_name: str) -> float:
    return float(
        chain_env_value(
            profile,
            suffix,
            str(default),
            legacy_names=(legacy_name,),
            apply_legacy_to_all=True,
        )
    )


@dataclass(frozen=True)
class Settings:
    workspace: Path
    chainid: str
    etherscan_api_base: str
    etherscan_api_key: str
    opentwitter_api_base: str
    opentwitter_api_key: str
    data_dir: Path
    db_path: Path
    confirmations: int
    interval_seconds: int
    lookback_blocks: int
    max_blocks_per_cycle: int
    enrich_limit_per_cycle: int
    report_limit_per_cycle: int
    report_every_enriched: int
    twitter_max_results: int
    max_log_candidates_per_block: int
    max_internal_candidates_per_block: int
    new_contract_max_age_blocks: int
    request_timeout_seconds: int
    daily_snapshot_enabled: int = 1
    coverage_window_blocks: int = 720
    runtime_stale_minutes: int = 30
    enrichment_reservation_batch_size: int = 5
    chain_slug: str = "ethereum"
    chain_name: str = "Ethereum"
    explorer_base_url: str = "https://etherscan.io"
    block_time_seconds: float = 12.0
    etherscan_min_interval_seconds: float = 0.0
    discovery_mode: str = "activity"
    activity_min_observations: int = 100
    activity_max_candidates_per_cycle: int = 50
    activity_transfer_log_span: int = 50
    activity_include_all_transfers: int = 1
    activity_all_transfer_log_span: int = 5
    activity_custom_event_topics: str = DEFAULT_ACTIVITY_CUSTOM_EVENT_TOPICS_CSV
    activity_custom_event_log_span: int = 1000

    @classmethod
    def from_env(cls, workspace: Path, chain: str | None = None) -> "Settings":
        load_env_file(workspace / ".env")
        profile = chain_profile(chain or os.environ.get("ALPHA_CHAIN") or os.environ.get("ETHERSCAN_CHAIN"))
        data_dir = workspace / chain_env_value(
            profile,
            "DATA_DIR",
            profile.data_dir_name,
            legacy_names=("ALPHA_DATA_DIR",),
        )
        request_rate = chain_env_float(profile, "ETHERSCAN_REQUESTS_PER_SECOND", 2.0, "ALPHA_ETHERSCAN_REQUESTS_PER_SECOND")
        min_interval = 0.0 if request_rate <= 0 else 1.0 / request_rate
        return cls(
            workspace=workspace,
            chainid=chain_env_value(
                profile,
                "ETHERSCAN_CHAINID",
                profile.chainid,
                legacy_names=("ETHERSCAN_CHAINID",),
            ),
            etherscan_api_base=chain_env_value(
                profile,
                "ETHERSCAN_API_BASE",
                "https://api.etherscan.io/v2/api",
                legacy_names=("ETHERSCAN_API_BASE",),
                apply_legacy_to_all=True,
            ),
            etherscan_api_key=chain_env_value(
                profile,
                "ETHERSCAN_API_KEY",
                "",
                legacy_names=("ETHERSCAN_API_KEY", "BSCSCAN_API_KEY", "BASESCAN_API_KEY"),
                apply_legacy_to_all=True,
            ),
            opentwitter_api_base=(
                os.environ.get("OPENTWITTER_API_BASE")
                or os.environ.get("TWITTER_API_BASE")
                or "https://ai.6551.io"
            ),
            opentwitter_api_key=(
                os.environ.get("OPENTWITTER_API_KEY")
                or os.environ.get("TWITTER_TOKEN")
                or os.environ.get("OPENNEWS_TOKEN")
                or ""
            ),
            data_dir=data_dir,
            db_path=data_dir / "alpha.sqlite",
            confirmations=chain_env_int(profile, "CONFIRMATIONS", profile.default_confirmations, "ALPHA_CONFIRMATIONS"),
            interval_seconds=chain_env_int(profile, "INTERVAL_SECONDS", profile.default_interval_seconds, "ALPHA_INTERVAL_SECONDS"),
            lookback_blocks=chain_env_int(profile, "LOOKBACK_BLOCKS", profile.default_lookback_blocks, "ALPHA_LOOKBACK_BLOCKS"),
            max_blocks_per_cycle=chain_env_int(
                profile,
                "MAX_BLOCKS_PER_CYCLE",
                profile.default_max_blocks_per_cycle,
                "ALPHA_MAX_BLOCKS_PER_CYCLE",
            ),
            enrich_limit_per_cycle=env_int("ALPHA_ENRICH_LIMIT_PER_CYCLE", 10),
            report_limit_per_cycle=env_int("ALPHA_REPORT_LIMIT_PER_CYCLE", 0),
            report_every_enriched=env_int("ALPHA_REPORT_EVERY_ENRICHED", 10),
            twitter_max_results=env_int("ALPHA_TWITTER_MAX_RESULTS", 20),
            max_log_candidates_per_block=env_int("ALPHA_MAX_LOG_CANDIDATES_PER_BLOCK", 25),
            max_internal_candidates_per_block=env_int("ALPHA_MAX_INTERNAL_CANDIDATES_PER_BLOCK", 50),
            new_contract_max_age_blocks=chain_env_int(
                profile,
                "NEW_CONTRACT_MAX_AGE_BLOCKS",
                profile.default_new_contract_max_age_blocks,
                "ALPHA_NEW_CONTRACT_MAX_AGE_BLOCKS",
            ),
            request_timeout_seconds=env_int("ALPHA_REQUEST_TIMEOUT_SECONDS", 30),
            daily_snapshot_enabled=env_int("ALPHA_DAILY_SNAPSHOT_ENABLED", 1),
            coverage_window_blocks=chain_env_int(
                profile,
                "COVERAGE_WINDOW_BLOCKS",
                profile.default_coverage_window_blocks,
                "ALPHA_COVERAGE_WINDOW_BLOCKS",
            ),
            runtime_stale_minutes=env_int("ALPHA_RUNTIME_STALE_MINUTES", 30),
            enrichment_reservation_batch_size=env_int("ALPHA_ENRICHMENT_RESERVATION_BATCH_SIZE", 5),
            chain_slug=profile.slug,
            chain_name=profile.display_name,
            explorer_base_url=profile.explorer_base_url,
            block_time_seconds=profile.block_time_seconds,
            etherscan_min_interval_seconds=min_interval,
            discovery_mode=chain_env_value(
                profile,
                "DISCOVERY_MODE",
                profile.default_discovery_mode,
                legacy_names=("ALPHA_DISCOVERY_MODE",),
                apply_legacy_to_all=True,
            ).strip().lower(),
            activity_min_observations=int(
                chain_env_value(
                    profile,
                    "ACTIVITY_MIN_OBSERVATIONS",
                    str(profile.default_activity_min_observations),
                    legacy_names=("ALPHA_ACTIVITY_MIN_OBSERVATIONS",),
                    apply_legacy_to_all=True,
                )
            ),
            activity_max_candidates_per_cycle=int(
                chain_env_value(
                    profile,
                    "ACTIVITY_MAX_CANDIDATES_PER_CYCLE",
                    str(profile.default_activity_max_candidates_per_cycle),
                    legacy_names=("ALPHA_ACTIVITY_MAX_CANDIDATES_PER_CYCLE",),
                    apply_legacy_to_all=True,
                )
            ),
            activity_transfer_log_span=int(
                chain_env_value(
                    profile,
                    "ACTIVITY_TRANSFER_LOG_SPAN",
                    str(profile.default_activity_transfer_log_span),
                    legacy_names=("ALPHA_ACTIVITY_TRANSFER_LOG_SPAN",),
                    apply_legacy_to_all=True,
                )
            ),
            activity_include_all_transfers=int(
                chain_env_value(
                    profile,
                    "ACTIVITY_INCLUDE_ALL_TRANSFERS",
                    "1",
                    legacy_names=("ALPHA_ACTIVITY_INCLUDE_ALL_TRANSFERS",),
                    apply_legacy_to_all=True,
                )
            ),
            activity_all_transfer_log_span=int(
                chain_env_value(
                    profile,
                    "ACTIVITY_ALL_TRANSFER_LOG_SPAN",
                    "5",
                    legacy_names=("ALPHA_ACTIVITY_ALL_TRANSFER_LOG_SPAN",),
                    apply_legacy_to_all=True,
                )
            ),
            activity_custom_event_topics=chain_env_value(
                profile,
                "ACTIVITY_CUSTOM_EVENT_TOPICS",
                DEFAULT_ACTIVITY_CUSTOM_EVENT_TOPICS_CSV,
                legacy_names=("ALPHA_ACTIVITY_CUSTOM_EVENT_TOPICS",),
                apply_legacy_to_all=True,
            ),
            activity_custom_event_log_span=int(
                chain_env_value(
                    profile,
                    "ACTIVITY_CUSTOM_EVENT_LOG_SPAN",
                    "1000",
                    legacy_names=("ALPHA_ACTIVITY_CUSTOM_EVENT_LOG_SPAN",),
                    apply_legacy_to_all=True,
                )
            ),
        )

    def validate(self, require_twitter: bool = True) -> None:
        if self.discovery_mode not in {"block", "activity"}:
            raise RuntimeError("ALPHA_DISCOVERY_MODE must be 'block' or 'activity'")
        missing = []
        if not self.etherscan_api_key:
            missing.append("ETHERSCAN_API_KEY")
        if require_twitter and not self.opentwitter_api_key:
            missing.append("OPENTWITTER_API_KEY or TWITTER_TOKEN")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variable(s): {joined}")
