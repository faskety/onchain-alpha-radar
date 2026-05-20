from __future__ import annotations

from typing import Any, Callable

from .classify import classify_contract
from .etherscan import EtherscanClient, hex_to_int

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
UNISWAP_V2_PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
UNISWAP_V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
UNISWAP_V3_POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
UNISWAP_V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
UNISWAP_V4_POOL_MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"
UNISWAP_V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
BALANCER_V2_VAULT = "0xba12222222228d8ba445958a75a0704d566bf2c8"
BALANCER_V2_TOKENS_REGISTERED_TOPIC = "0xf5847d3f2197b16cdcd2098ec95d0905cd1abdaf415f07bb7cef2bba8ac5dec4"
ZERO_TOPIC = "0x" + "0" * 64
GET_LOGS_SOFT_CAP = 1000
LOG_SOURCE_PRIORITY = {
    "uniswap_v4_hook": 0,
    "uniswap_v4_initialize": 1,
    "uniswap_v3_pool_created": 2,
    "uniswap_v2_pair_created": 3,
    "balancer_v2_tokens_registered": 4,
    "erc1155_transfer_single_mint": 5,
    "erc1155_transfer_batch_mint": 6,
    "mint_transfer": 7,
    "activity_dex_swap": 8,
    "activity_transfer": 9,
    "activity_custom_event": 10,
    "activity_mint": 11,
    "activity_erc1155_mint": 12,
}


def discover_contracts_in_block(
    client: EtherscanClient,
    block: dict[str, Any],
    max_log_candidates: int,
    max_internal_candidates: int,
    new_contract_max_age_blocks: int,
    classify: bool = True,
) -> list[dict[str, Any]]:
    direct = discover_contract_creations(client, block, classify=classify)
    direct_addresses = {item["address"] for item in direct}
    internal = discover_internal_contract_creations(
        client,
        hex_to_int(block.get("number")),
        exclude_addresses=direct_addresses,
        max_internal_candidates=max_internal_candidates,
        classify=classify,
    )
    from_logs = discover_log_candidates(
        client,
        hex_to_int(block.get("number")),
        set(),
        max_log_candidates=max_log_candidates,
        new_contract_max_age_blocks=new_contract_max_age_blocks,
        classify=classify,
    )
    return direct + internal + from_logs


def discover_contract_creations(client: EtherscanClient, block: dict[str, Any], classify: bool = True) -> list[dict[str, Any]]:
    block_number = hex_to_int(block.get("number"))
    block_timestamp = hex_to_int(block.get("timestamp"))
    transactions = block.get("transactions") or []
    if not isinstance(transactions, list):
        return []

    discovered: list[dict[str, Any]] = []
    for tx in transactions:
        if not isinstance(tx, dict):
            continue
        to_address = tx.get("to")
        if to_address not in (None, "", "0x"):
            continue
        tx_hash = tx.get("hash")
        if not isinstance(tx_hash, str):
            continue
        receipt = client.get_transaction_receipt(tx_hash)
        if not receipt:
            continue
        contract_address = receipt.get("contractAddress")
        if not isinstance(contract_address, str) or not contract_address.startswith("0x"):
            continue
        base = {
            "address": contract_address.lower(),
            "tx_hash": tx_hash,
            "origin_tx_hash": tx_hash,
            "discovery_source": "contract_creation",
            "log_index": None,
            "related": {},
            "deployer": str(tx.get("from") or "").lower(),
            "block_number": block_number,
            "block_timestamp": block_timestamp,
            "tx_index": hex_to_int(tx.get("transactionIndex")),
            "value_wei": str(hex_to_int(tx.get("value"))),
            "input_prefix": str(tx.get("input") or "")[:74],
        }
        classification = classify_or_defer(client, contract_address, classify)
        discovered.append({**base, **classification})
    return discovered


def discover_internal_contract_creations(
    client: EtherscanClient,
    block_number: int,
    exclude_addresses: set[str],
    max_internal_candidates: int,
    classify: bool = True,
) -> list[dict[str, Any]]:
    traces = client.get_internal_transactions(block_number, block_number)
    return discover_internal_contract_creations_from_traces(client, traces, block_number, exclude_addresses, max_internal_candidates, classify=classify)


def discover_internal_contract_creations_from_traces(
    client: EtherscanClient,
    traces: list[dict[str, Any]],
    fallback_block_number: int,
    exclude_addresses: set[str],
    max_internal_candidates: int,
    classify: bool = True,
) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trace in traces:
        if str(trace.get("isError") or "0") != "0":
            continue
        trace_type = str(trace.get("type") or "").lower()
        if trace_type not in {"create", "create2"}:
            continue
        address = normalize_address(trace.get("contractAddress"))
        if not address or address in exclude_addresses or address in seen:
            continue
        seen.add(address)
        discovered.append(build_internal_contract(client, trace, fallback_block_number, classify=classify))
        if len(discovered) >= max_internal_candidates:
            break
    return discovered


def build_internal_contract(
    client: EtherscanClient,
    trace: dict[str, Any],
    fallback_block_number: int,
    classify: bool = True,
) -> dict[str, Any]:
    address = normalize_address(trace.get("contractAddress")) or ""
    trace_type = str(trace.get("type") or "create").lower()
    origin_tx_hash = str(trace.get("hash") or "")
    trace_id = str(trace.get("traceId") or "")
    discovery_id = f"internal_{trace_type}:{address}:{origin_tx_hash}:{trace_id}"
    base = {
        "address": address,
        "tx_hash": discovery_id,
        "origin_tx_hash": origin_tx_hash,
        "discovery_source": f"internal_{trace_type}",
        "log_index": None,
        "related": {
            "factory": normalize_address(trace.get("from")),
            "trace_id": trace_id,
            "type": trace_type,
        },
        "deployer": normalize_address(trace.get("from")) or "",
        "block_number": parse_block_number(trace.get("blockNumber")) or fallback_block_number,
        "block_timestamp": parse_block_number(trace.get("timeStamp")),
        "tx_index": None,
        "value_wei": str(trace.get("value") or "0"),
        "input_prefix": str(trace.get("input") or "")[:74],
    }
    classification = classify_or_defer(client, address, classify)
    return {**base, **classification}


def discover_log_candidates(
    client: EtherscanClient,
    block_number: int,
    exclude_addresses: set[str],
    max_log_candidates: int,
    new_contract_max_age_blocks: int,
    classify: bool = True,
) -> list[dict[str, Any]]:
    raw_candidates = collect_log_candidates(client, block_number, exclude_addresses)
    return discover_log_candidates_from_raw(client, raw_candidates, block_number, max_log_candidates, new_contract_max_age_blocks, classify=classify)


def discover_log_candidates_from_raw(
    client: EtherscanClient,
    raw_candidates: list[dict[str, Any]],
    block_number: int,
    max_log_candidates: int,
    new_contract_max_age_blocks: int,
    classify: bool = True,
    creation_cache: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    candidates = select_log_candidates(raw_candidates, max_log_candidates)
    addresses = [candidate["address"] for candidate in candidates]
    creation = creation_cache if creation_cache is not None else client.get_contract_creation(addresses)
    discovered: list[dict[str, Any]] = []
    for candidate in candidates:
        address = candidate["address"]
        creation_meta = creation.get(address)
        if not creation_meta:
            continue
        created_block = parse_block_number(creation_meta.get("blockNumber"))
        if created_block is None:
            continue
        age = block_number - created_block
        if age < 0 or age > new_contract_max_age_blocks:
            continue
        discovered.append(build_log_contract(client, candidate, creation_meta, created_block, classify=classify))
    return discovered


def collect_log_candidates_by_block(
    client: EtherscanClient,
    start_block: int,
    end_block: int,
    exclude_addresses: set[str],
) -> dict[int, list[dict[str, Any]]]:
    by_block: dict[int, list[dict[str, Any]]] = {}

    def add(log: dict[str, Any], candidate: dict[str, Any] | None) -> None:
        if not candidate:
            return
        block_number = parse_block_number(log.get("blockNumber"))
        if block_number is None:
            return
        by_block.setdefault(block_number, []).append(candidate)

    for log in fetch_logs_safely(client, start_block, end_block, TRANSFER_TOPIC, topic1=ZERO_TOPIC, max_span=5):
        add(log, parse_transfer_mint_log(log, exclude_addresses))

    for topic, source in (
        (TRANSFER_SINGLE_TOPIC, "erc1155_transfer_single_mint"),
        (TRANSFER_BATCH_TOPIC, "erc1155_transfer_batch_mint"),
    ):
        for log in fetch_logs_safely(client, start_block, end_block, topic, topic2=ZERO_TOPIC, max_span=20):
            add(log, parse_erc1155_mint_log(log, source, exclude_addresses))

    for log in fetch_logs_safely(client, start_block, end_block, UNISWAP_V2_PAIR_CREATED_TOPIC, max_span=100):
        for candidate in parse_uniswap_v2_log(log, exclude_addresses):
            add(log, candidate)

    for log in fetch_logs_safely(client, start_block, end_block, UNISWAP_V3_POOL_CREATED_TOPIC, max_span=100):
        for candidate in parse_uniswap_v3_log(log, exclude_addresses):
            add(log, candidate)

    for log in fetch_logs_safely(
        client,
        start_block,
        end_block,
        UNISWAP_V4_INITIALIZE_TOPIC,
        address=UNISWAP_V4_POOL_MANAGER,
        max_span=100,
    ):
        for candidate in parse_uniswap_v4_log(log, exclude_addresses):
            add(log, candidate)

    for log in fetch_logs_safely(
        client,
        start_block,
        end_block,
        BALANCER_V2_TOKENS_REGISTERED_TOPIC,
        address=BALANCER_V2_VAULT,
        max_span=100,
    ):
        for candidate in parse_balancer_v2_tokens_registered_log(log, exclude_addresses):
            add(log, candidate)

    return by_block


def fetch_logs_safely(
    client: EtherscanClient,
    start_block: int,
    end_block: int,
    topic0: str,
    topic1: str | None = None,
    topic2: str | None = None,
    address: str | None = None,
    max_span: int = 20,
) -> list[dict[str, Any]]:
    return list(iter_logs_safely(client, start_block, end_block, topic0, topic1=topic1, topic2=topic2, address=address, max_span=max_span))


def iter_logs_safely(
    client: EtherscanClient,
    start_block: int,
    end_block: int,
    topic0: str,
    topic1: str | None = None,
    topic2: str | None = None,
    address: str | None = None,
    max_span: int = 20,
    progress: Callable[[dict[str, Any]], None] | None = None,
    progress_stage: str | None = None,
):
    stack = [(start_block, end_block)]
    while stack:
        start, end = stack.pop()
        if end < start:
            continue
        if end - start + 1 > max_span:
            mid = min(end, start + max_span - 1)
            stack.append((mid + 1, end))
            stack.append((start, mid))
            continue
        chunk = client.get_logs(start, end, topic0, topic1=topic1, topic2=topic2, address=address)
        if len(chunk) >= GET_LOGS_SOFT_CAP and start < end:
            mid = (start + end) // 2
            stack.append((mid + 1, end))
            stack.append((start, mid))
            continue
        if progress:
            progress(
                {
                    "stage": progress_stage or "logs",
                    "from_block": start,
                    "to_block": end,
                    "logs": len(chunk),
                }
            )
        yield from chunk


def discover_activity_contracts_in_range(
    client: EtherscanClient,
    start_block: int,
    end_block: int,
    min_observations: int,
    max_candidates: int,
    new_contract_max_age_blocks: int,
    chain_slug: str = "ethereum",
    transfer_log_max_span: int = 50,
    include_all_transfers: bool = True,
    all_transfer_log_max_span: int = 5,
    custom_event_topics: list[str] | None = None,
    custom_event_log_max_span: int = 1000,
    classify: bool = True,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    activity = collect_activity_stats(
        client,
        start_block,
        end_block,
        chain_slug=chain_slug,
        transfer_log_max_span=transfer_log_max_span,
        include_all_transfers=include_all_transfers,
        all_transfer_log_max_span=all_transfer_log_max_span,
        custom_event_topics=custom_event_topics,
        custom_event_log_max_span=custom_event_log_max_span,
        progress=progress,
    )
    active = [
        stat
        for stat in activity.values()
        if int(stat.get("activity_observations") or 0) >= max(1, min_observations)
    ]
    active.sort(
        key=lambda stat: (
            int(stat.get("activity_observations") or 0),
            int(stat.get("dex_observations") or 0),
            int(stat.get("last_block") or 0),
        ),
        reverse=True,
    )
    probe_limit = max(0, max_candidates)
    if probe_limit > 0:
        probe_limit = min(len(active), max(probe_limit, probe_limit * 10))
    selected = active[:probe_limit]
    emit_activity_progress(
        progress,
        "creation_probe",
        "started",
        active_candidates=len(active),
        selected_candidates=len(selected),
    )
    creation = client.get_contract_creation([str(stat["address"]) for stat in selected])
    emit_activity_progress(
        progress,
        "creation_probe",
        "finished",
        active_candidates=len(active),
        selected_candidates=len(selected),
        creation_rows=len(creation),
    )
    discovered: list[dict[str, Any]] = []
    for stat in selected:
        address = str(stat["address"])
        creation_meta = creation.get(address)
        if not creation_meta:
            continue
        created_block = parse_block_number(creation_meta.get("blockNumber"))
        if created_block is None:
            continue
        age = end_block - created_block
        if age < 0 or age > new_contract_max_age_blocks:
            continue
        discovered.append(build_activity_contract(client, stat, creation_meta, created_block, classify=classify))
        if len(discovered) >= max(0, max_candidates):
            break
    emit_activity_progress(
        progress,
        "candidate_filter",
        "finished",
        active_candidates=len(active),
        selected_candidates=len(selected),
        discovered_contracts=len(discovered),
    )
    return discovered


def collect_activity_stats(
    client: EtherscanClient,
    start_block: int,
    end_block: int,
    chain_slug: str = "ethereum",
    transfer_log_max_span: int = 50,
    include_all_transfers: bool = True,
    all_transfer_log_max_span: int = 5,
    custom_event_topics: list[str] | None = None,
    custom_event_log_max_span: int = 1000,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    stage_counts: dict[str, dict[str, int]] = {}

    def record_stage_chunk(payload: dict[str, Any]) -> None:
        stage = str(payload.get("stage") or "logs")
        counter = stage_counts.setdefault(stage, {"chunks": 0, "logs": 0})
        counter["chunks"] += 1
        counter["logs"] += int(payload.get("logs") or 0)
        chunks = counter["chunks"]
        logs = counter["logs"]
        if chunks == 1 or chunks % 10 == 0 or int(payload.get("logs") or 0) > 0:
            emit_activity_progress(
                progress,
                stage,
                "progress",
                chunks=chunks,
                logs=logs,
                from_block=payload.get("from_block"),
                to_block=payload.get("to_block"),
            )

    def finish_stage(stage: str) -> None:
        counter = stage_counts.get(stage, {"chunks": 0, "logs": 0})
        emit_activity_progress(
            progress,
            stage,
            "finished",
            chunks=counter["chunks"],
            logs=counter["logs"],
            active_contracts=len(stats),
        )

    def stat_for(address: str) -> dict[str, Any]:
        return stats.setdefault(
            address,
            {
                "address": address,
                "activity_observations": 0,
                "dex_observations": 0,
                "sources": set(),
                "first_block": None,
                "last_block": None,
                "first_timestamp": None,
                "last_timestamp": None,
                "sample_tx_hash": None,
                "last_tx_hash": None,
                "unique_txs": set(),
                "related": {},
            },
        )

    def add_activity(log: dict[str, Any], source: str, address: str | None = None) -> None:
        normalized = normalize_address(address or log.get("address"))
        if not normalized:
            return
        stat = stat_for(normalized)
        block_number = parse_block_number(log.get("blockNumber"))
        timestamp = parse_block_number(log.get("timeStamp"))
        tx_hash = str(log.get("transactionHash") or "")
        stat["activity_observations"] = int(stat.get("activity_observations") or 0) + 1
        stat["sources"].add(source)
        if tx_hash:
            stat["unique_txs"].add(tx_hash)
            stat["sample_tx_hash"] = stat.get("sample_tx_hash") or tx_hash
            stat["last_tx_hash"] = tx_hash
        if block_number is not None:
            stat["first_block"] = block_number if stat.get("first_block") is None else min(int(stat["first_block"]), block_number)
            stat["last_block"] = block_number if stat.get("last_block") is None else max(int(stat["last_block"]), block_number)
        if timestamp is not None:
            stat["first_timestamp"] = timestamp if stat.get("first_timestamp") is None else min(int(stat["first_timestamp"]), timestamp)
            stat["last_timestamp"] = timestamp if stat.get("last_timestamp") is None else max(int(stat["last_timestamp"]), timestamp)

    def add_dex_candidate(candidate: dict[str, Any], swap_count: int = 0) -> None:
        address = normalize_address(candidate.get("address"))
        if not address:
            return
        stat = stat_for(address)
        stat["dex_observations"] = int(stat.get("dex_observations") or 0) + 1
        if swap_count > 0:
            stat["activity_observations"] = int(stat.get("activity_observations") or 0) + int(swap_count)
            stat["sources"].add("activity_dex_swap")
        stat["sources"].add(str(candidate.get("discovery_source") or "dex"))
        related = candidate.get("related") or {}
        stat["related"].setdefault("dex", []).append({**related, "swap_count": int(swap_count)})
        block_number = parse_block_number(candidate.get("block_number"))
        timestamp = parse_block_number(candidate.get("block_timestamp"))
        tx_hash = str(candidate.get("origin_tx_hash") or "")
        if tx_hash:
            stat["unique_txs"].add(tx_hash)
            stat["sample_tx_hash"] = stat.get("sample_tx_hash") or tx_hash
            stat["last_tx_hash"] = tx_hash
        if block_number is not None:
            stat["first_block"] = block_number if stat.get("first_block") is None else min(int(stat["first_block"]), block_number)
            stat["last_block"] = block_number if stat.get("last_block") is None else max(int(stat["last_block"]), block_number)
        if timestamp is not None:
            stat["first_timestamp"] = timestamp if stat.get("first_timestamp") is None else min(int(stat["first_timestamp"]), timestamp)
            stat["last_timestamp"] = timestamp if stat.get("last_timestamp") is None else max(int(stat["last_timestamp"]), timestamp)

    emit_activity_progress(progress, "mint_transfers", "started")
    for log in iter_logs_safely(
        client,
        start_block,
        end_block,
        TRANSFER_TOPIC,
        topic1=ZERO_TOPIC,
        max_span=max(1, transfer_log_max_span),
        progress=record_stage_chunk,
        progress_stage="mint_transfers",
    ):
        add_activity(log, "activity_mint")
    finish_stage("mint_transfers")

    if include_all_transfers:
        emit_activity_progress(progress, "all_transfers", "started")
        for log in iter_logs_safely(
            client,
            start_block,
            end_block,
            TRANSFER_TOPIC,
            max_span=max(1, all_transfer_log_max_span),
            progress=record_stage_chunk,
            progress_stage="all_transfers",
        ):
            topics = log.get("topics") or []
            if len(topics) > 1 and str(topics[1]).lower() == ZERO_TOPIC:
                continue
            add_activity(log, "activity_transfer")
        finish_stage("all_transfers")

    emit_activity_progress(progress, "erc1155_mints", "started")
    for topic in (TRANSFER_SINGLE_TOPIC, TRANSFER_BATCH_TOPIC):
        for log in iter_logs_safely(
            client,
            start_block,
            end_block,
            topic,
            topic2=ZERO_TOPIC,
            max_span=max(1, transfer_log_max_span * 2),
            progress=record_stage_chunk,
            progress_stage="erc1155_mints",
        ):
            add_activity(log, "activity_erc1155_mint")
    finish_stage("erc1155_mints")

    emit_activity_progress(progress, "custom_events", "started")
    for topic in normalize_event_topics(custom_event_topics):
        for log in iter_logs_safely(
            client,
            start_block,
            end_block,
            topic,
            max_span=max(1, custom_event_log_max_span),
            progress=record_stage_chunk,
            progress_stage="custom_events",
        ):
            add_activity(log, "activity_custom_event")
    finish_stage("custom_events")

    emit_activity_progress(progress, "dex_v2", "started")
    for log in fetch_logs_safely(client, start_block, end_block, UNISWAP_V2_PAIR_CREATED_TOPIC, max_span=1000):
        pair_block = parse_block_number(log.get("blockNumber")) or start_block
        swap_count = dex_swap_count(client, pair_block, end_block, UNISWAP_V2_SWAP_TOPIC, data_word_to_address(str(log.get("data") or ""), 0))
        for candidate in parse_uniswap_v2_log(log, set()):
            candidate["block_number"] = parse_block_number(log.get("blockNumber"))
            add_dex_candidate(candidate, swap_count=swap_count)
    emit_activity_progress(progress, "dex_v2", "finished", active_contracts=len(stats))

    emit_activity_progress(progress, "dex_v3", "started")
    for log in fetch_logs_safely(client, start_block, end_block, UNISWAP_V3_POOL_CREATED_TOPIC, max_span=1000):
        pair_block = parse_block_number(log.get("blockNumber")) or start_block
        pool = data_word_to_address(str(log.get("data") or ""), 1)
        swap_count = dex_swap_count(client, pair_block, end_block, UNISWAP_V3_SWAP_TOPIC, pool)
        for candidate in parse_uniswap_v3_log(log, set()):
            candidate["block_number"] = parse_block_number(log.get("blockNumber"))
            add_dex_candidate(candidate, swap_count=swap_count)
    emit_activity_progress(progress, "dex_v3", "finished", active_contracts=len(stats))

    if chain_slug != "bsc":
        emit_activity_progress(progress, "dex_v4", "started")
        for log in fetch_logs_safely(
            client,
            start_block,
            end_block,
            UNISWAP_V4_INITIALIZE_TOPIC,
            address=UNISWAP_V4_POOL_MANAGER,
            max_span=1000,
        ):
            for candidate in parse_uniswap_v4_log(log, set()):
                candidate["block_number"] = parse_block_number(log.get("blockNumber"))
                add_dex_candidate(candidate)
        emit_activity_progress(progress, "dex_v4", "finished", active_contracts=len(stats))

        emit_activity_progress(progress, "balancer_v2", "started")
        for log in fetch_logs_safely(
            client,
            start_block,
            end_block,
            BALANCER_V2_TOKENS_REGISTERED_TOPIC,
            address=BALANCER_V2_VAULT,
            max_span=1000,
        ):
            for candidate in parse_balancer_v2_tokens_registered_log(log, set()):
                candidate["block_number"] = parse_block_number(log.get("blockNumber"))
                add_dex_candidate(candidate)
        emit_activity_progress(progress, "balancer_v2", "finished", active_contracts=len(stats))

    return stats


def emit_activity_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    stage: str,
    status: str,
    **extra: Any,
) -> None:
    if not progress:
        return
    payload = {
        "event": "activity_scan_stage",
        "stage": stage,
        "status": status,
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    progress(payload)


def dex_swap_count(
    client: EtherscanClient,
    start_block: int,
    end_block: int,
    topic0: str,
    pool_address: str | None,
) -> int:
    if not pool_address:
        return 0
    return len(fetch_logs_safely(client, start_block, end_block, topic0, address=pool_address, max_span=1000))


def build_activity_contract(
    client: EtherscanClient,
    stat: dict[str, Any],
    creation_meta: dict[str, Any],
    created_block: int,
    classify: bool = True,
) -> dict[str, Any]:
    address = str(stat["address"])
    sources = sorted(str(source) for source in stat.get("sources") or [])
    if "activity_dex_swap" in sources:
        primary_source = "activity_dex_swap"
    elif "activity_transfer" in sources and "activity_mint" not in sources:
        primary_source = "activity_transfer"
    elif "activity_custom_event" in sources:
        primary_source = "activity_custom_event"
    elif "activity_erc1155_mint" in sources and "activity_mint" not in sources:
        primary_source = "activity_erc1155_mint"
    else:
        primary_source = "activity_mint"
    origin_tx_hash = str(stat.get("last_tx_hash") or stat.get("sample_tx_hash") or creation_meta.get("txHash") or "")
    first_block = int(stat.get("first_block") or created_block)
    last_block = int(stat.get("last_block") or first_block)
    discovery_id = f"{primary_source}:{address}:{first_block}-{last_block}:{int(stat.get('activity_observations') or 0)}"
    related = {
        "activity_observations": int(stat.get("activity_observations") or 0),
        "dex_observations": int(stat.get("dex_observations") or 0),
        "unique_txs": len(stat.get("unique_txs") or []),
        "sources": sources,
        "first_activity_block": stat.get("first_block"),
        "last_activity_block": stat.get("last_block"),
        "dex": (stat.get("related") or {}).get("dex") or [],
    }
    base = {
        "address": address,
        "tx_hash": discovery_id,
        "origin_tx_hash": origin_tx_hash,
        "discovery_source": primary_source,
        "log_index": None,
        "related": related,
        "deployer": normalize_address(creation_meta.get("contractCreator")) or "",
        "block_number": created_block,
        "block_timestamp": parse_block_number(creation_meta.get("timestamp")) or stat.get("first_timestamp"),
        "tx_index": None,
        "value_wei": None,
        "input_prefix": "",
    }
    classification = classify_or_defer(client, address, classify)
    return {**base, **classification}


def collect_log_candidates(
    client: EtherscanClient,
    block_number: int,
    exclude_addresses: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for log in client.get_logs(block_number, block_number, TRANSFER_TOPIC, topic1=ZERO_TOPIC):
        candidate = parse_transfer_mint_log(log, exclude_addresses)
        if candidate:
            candidates.append(candidate)

    for topic, source in (
        (TRANSFER_SINGLE_TOPIC, "erc1155_transfer_single_mint"),
        (TRANSFER_BATCH_TOPIC, "erc1155_transfer_batch_mint"),
    ):
        for log in client.get_logs(block_number, block_number, topic, topic2=ZERO_TOPIC):
            candidate = parse_erc1155_mint_log(log, source, exclude_addresses)
            if candidate:
                candidates.append(candidate)

    candidates.extend(collect_uniswap_pool_token_candidates(client, block_number, exclude_addresses))
    candidates.extend(collect_uniswap_v4_candidates(client, block_number, exclude_addresses))
    candidates.extend(collect_balancer_v2_candidates(client, block_number, exclude_addresses))
    return candidates


def collect_uniswap_pool_token_candidates(
    client: EtherscanClient,
    block_number: int,
    exclude_addresses: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for log in client.get_logs(block_number, block_number, UNISWAP_V2_PAIR_CREATED_TOPIC):
        candidates.extend(parse_uniswap_v2_log(log, exclude_addresses))

    for log in client.get_logs(block_number, block_number, UNISWAP_V3_POOL_CREATED_TOPIC):
        candidates.extend(parse_uniswap_v3_log(log, exclude_addresses))
    return candidates


def collect_uniswap_v4_candidates(
    client: EtherscanClient,
    block_number: int,
    exclude_addresses: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for log in client.get_logs(
        block_number,
        block_number,
        UNISWAP_V4_INITIALIZE_TOPIC,
        address=UNISWAP_V4_POOL_MANAGER,
    ):
        candidates.extend(parse_uniswap_v4_log(log, exclude_addresses))
    return candidates


def collect_balancer_v2_candidates(
    client: EtherscanClient,
    block_number: int,
    exclude_addresses: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for log in client.get_logs(
        block_number,
        block_number,
        BALANCER_V2_TOKENS_REGISTERED_TOPIC,
        address=BALANCER_V2_VAULT,
    ):
        candidates.extend(parse_balancer_v2_tokens_registered_log(log, exclude_addresses))
    return candidates


def parse_transfer_mint_log(log: dict[str, Any], exclude_addresses: set[str]) -> dict[str, Any] | None:
    address = normalize_address(log.get("address"))
    if not address or address in exclude_addresses:
        return None
    return {
        "address": address,
        "origin_tx_hash": str(log.get("transactionHash") or ""),
        "log_index": hex_to_int(log.get("logIndex")),
        "block_timestamp": hex_to_int(log.get("timeStamp")),
        "discovery_source": "mint_transfer",
        "related": {"to": topic_to_address((log.get("topics") or [None, None, None])[2])},
    }


def parse_erc1155_mint_log(log: dict[str, Any], source: str, exclude_addresses: set[str]) -> dict[str, Any] | None:
    address = normalize_address(log.get("address"))
    if not address or address in exclude_addresses:
        return None
    return {
        "address": address,
        "origin_tx_hash": str(log.get("transactionHash") or ""),
        "log_index": hex_to_int(log.get("logIndex")),
        "block_timestamp": hex_to_int(log.get("timeStamp")),
        "discovery_source": source,
        "related": {},
    }


def parse_uniswap_v2_log(log: dict[str, Any], exclude_addresses: set[str]) -> list[dict[str, Any]]:
    topics = log.get("topics") or []
    token0 = topic_to_address(topics[1] if len(topics) > 1 else None)
    token1 = topic_to_address(topics[2] if len(topics) > 2 else None)
    pair = data_word_to_address(str(log.get("data") or ""), word_index=0)
    candidates = []
    for token in (token0, token1):
        if token and token not in exclude_addresses:
            candidates.append(
                {
                    "address": token,
                    "origin_tx_hash": str(log.get("transactionHash") or ""),
                    "log_index": hex_to_int(log.get("logIndex")),
                    "block_timestamp": hex_to_int(log.get("timeStamp")),
                    "discovery_source": "uniswap_v2_pair_created",
                    "related": {"pair": pair, "token0": token0, "token1": token1, "factory": normalize_address(log.get("address"))},
                }
            )
    return candidates


def parse_uniswap_v3_log(log: dict[str, Any], exclude_addresses: set[str]) -> list[dict[str, Any]]:
    topics = log.get("topics") or []
    token0 = topic_to_address(topics[1] if len(topics) > 1 else None)
    token1 = topic_to_address(topics[2] if len(topics) > 2 else None)
    pool = data_word_to_address(str(log.get("data") or ""), word_index=1)
    candidates = []
    for token in (token0, token1):
        if token and token not in exclude_addresses:
            candidates.append(
                {
                    "address": token,
                    "origin_tx_hash": str(log.get("transactionHash") or ""),
                    "log_index": hex_to_int(log.get("logIndex")),
                    "block_timestamp": hex_to_int(log.get("timeStamp")),
                    "discovery_source": "uniswap_v3_pool_created",
                    "related": {"pool": pool, "token0": token0, "token1": token1, "factory": normalize_address(log.get("address"))},
                }
            )
    return candidates


def parse_uniswap_v4_log(log: dict[str, Any], exclude_addresses: set[str]) -> list[dict[str, Any]]:
    topics = log.get("topics") or []
    pool_id = topics[1] if len(topics) > 1 and isinstance(topics[1], str) else None
    currency0 = topic_to_address(topics[2] if len(topics) > 2 else None)
    currency1 = topic_to_address(topics[3] if len(topics) > 3 else None)
    data = str(log.get("data") or "")
    fee = data_word_to_int(data, 0)
    tick_spacing = data_word_to_signed_int(data, 1)
    hook = data_word_to_address(data, 2)
    sqrt_price_x96 = data_word_to_int(data, 3)
    tick = data_word_to_signed_int(data, 4)
    related = {
        "pool_id": pool_id,
        "currency0": currency0,
        "currency1": currency1,
        "fee": fee,
        "tick_spacing": tick_spacing,
        "hook": hook,
        "sqrt_price_x96": str(sqrt_price_x96) if sqrt_price_x96 is not None else None,
        "tick": tick,
        "pool_manager": UNISWAP_V4_POOL_MANAGER,
    }
    zero_address = "0x0000000000000000000000000000000000000000"
    candidates = []
    for token in (currency0, currency1):
        if token and token != zero_address and token not in exclude_addresses:
            candidates.append(
                {
                    "address": token,
                    "origin_tx_hash": str(log.get("transactionHash") or ""),
                    "log_index": hex_to_int(log.get("logIndex")),
                    "block_timestamp": hex_to_int(log.get("timeStamp")),
                    "discovery_source": "uniswap_v4_initialize",
                    "related": related,
                }
            )
    if hook and hook != zero_address and hook not in exclude_addresses:
        candidates.append(
            {
                "address": hook,
                "origin_tx_hash": str(log.get("transactionHash") or ""),
                "log_index": hex_to_int(log.get("logIndex")),
                "block_timestamp": hex_to_int(log.get("timeStamp")),
                "discovery_source": "uniswap_v4_hook",
                "related": related,
            }
        )
    return candidates


def parse_balancer_v2_tokens_registered_log(log: dict[str, Any], exclude_addresses: set[str]) -> list[dict[str, Any]]:
    topics = log.get("topics") or []
    pool_id = topics[1] if len(topics) > 1 and isinstance(topics[1], str) else None
    tokens = data_word_to_address_array(str(log.get("data") or ""), 0)
    zero_address = "0x0000000000000000000000000000000000000000"
    candidates = []
    seen = set()
    for token in tokens:
        if token in seen or token == zero_address or token == BALANCER_V2_VAULT or token in exclude_addresses:
            continue
        seen.add(token)
        candidates.append(
            {
                "address": token,
                "origin_tx_hash": str(log.get("transactionHash") or ""),
                "log_index": hex_to_int(log.get("logIndex")),
                "block_timestamp": hex_to_int(log.get("timeStamp")),
                "discovery_source": "balancer_v2_tokens_registered",
                "related": {"pool_id": pool_id, "tokens": tokens, "vault": BALANCER_V2_VAULT},
            }
        )
    return candidates


def build_log_contract(
    client: EtherscanClient,
    candidate: dict[str, Any],
    creation_meta: dict[str, Any],
    created_block: int,
    classify: bool = True,
) -> dict[str, Any]:
    address = candidate["address"]
    origin_tx_hash = str(creation_meta.get("txHash") or candidate.get("origin_tx_hash") or "")
    discovery_id = f"{candidate['discovery_source']}:{address}:{candidate.get('origin_tx_hash')}:{candidate.get('log_index')}"
    base = {
        "address": address,
        "tx_hash": discovery_id,
        "origin_tx_hash": origin_tx_hash,
        "discovery_source": candidate["discovery_source"],
        "log_index": candidate.get("log_index"),
        "related": candidate.get("related") or {},
        "deployer": normalize_address(creation_meta.get("contractCreator")) or "",
        "block_number": created_block,
        "block_timestamp": parse_block_number(creation_meta.get("timestamp")) or candidate.get("block_timestamp"),
        "tx_index": None,
        "value_wei": None,
        "input_prefix": "",
    }
    classification = classify_or_defer(client, address, classify)
    return {**base, **classification}


def classify_or_defer(client: EtherscanClient, address: str, classify: bool) -> dict[str, Any]:
    if not classify:
        return deferred_classification()
    try:
        return classify_contract(client, address)
    except Exception as exc:
        result = deferred_classification()
        result["classification_error"] = str(exc)
        return result


def deferred_classification() -> dict[str, Any]:
    return {
        "kind": "contract",
        "name": None,
        "symbol": None,
        "decimals": None,
        "total_supply": None,
        "verified": False,
        "contract_name": "",
        "source_len": 0,
        "confidence": 0.05,
        "classification_deferred": True,
    }


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for candidate in candidates:
        key = (
            candidate.get("address"),
            candidate.get("discovery_source"),
            candidate.get("origin_tx_hash"),
            candidate.get("log_index"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def select_log_candidates(candidates: list[dict[str, Any]], max_log_candidates: int) -> list[dict[str, Any]]:
    return dedupe_candidates(prioritize_candidates(candidates))[:max_log_candidates]


def prioritize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda candidate: LOG_SOURCE_PRIORITY.get(str(candidate.get("discovery_source") or ""), 100),
    )


def normalize_event_topics(topics: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for topic in topics or []:
        value = str(topic or "").strip().lower()
        if not (value.startswith("0x") and len(value) == 66):
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def normalize_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.lower()
    if value.startswith("0x") and len(value) == 42:
        return value
    return None


def topic_to_address(topic: Any) -> str | None:
    if not isinstance(topic, str) or not topic.startswith("0x") or len(topic) != 66:
        return None
    return normalize_address("0x" + topic[-40:])


def data_word_to_address(data: str, word_index: int) -> str | None:
    if not data.startswith("0x"):
        return None
    start = 2 + word_index * 64
    word = data[start : start + 64]
    if len(word) != 64:
        return None
    return normalize_address("0x" + word[-40:])


def data_word_to_address_array(data: str, offset_word_index: int, max_items: int = 32) -> list[str]:
    offset_bytes = data_word_to_int(data, offset_word_index)
    if offset_bytes is None or offset_bytes % 32 != 0:
        return []
    length_word_index = offset_bytes // 32
    length = data_word_to_int(data, length_word_index)
    if length is None or length < 0 or length > max_items:
        return []
    result = []
    for index in range(length):
        address = data_word_to_address(data, length_word_index + 1 + index)
        if address:
            result.append(address)
    return result


def data_word_to_int(data: str, word_index: int) -> int | None:
    if not data.startswith("0x"):
        return None
    start = 2 + word_index * 64
    word = data[start : start + 64]
    if len(word) != 64:
        return None
    try:
        return int(word, 16)
    except ValueError:
        return None


def data_word_to_signed_int(data: str, word_index: int) -> int | None:
    value = data_word_to_int(data, word_index)
    if value is None:
        return None
    if value >= 1 << 255:
        return value - (1 << 256)
    return value


def parse_block_number(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(value, 16) if value.startswith("0x") else int(value)
    except ValueError:
        return None
