from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .http_json import get_json

CONTRACT_CREATION_BATCH_SIZE = 5


def hex_to_int(value: str | None, default: int = 0) -> int:
    if not value:
        return default
    try:
        return int(value, 16) if value.startswith("0x") else int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class EtherscanClient:
    base_url: str
    api_key: str
    chainid: str = "1"
    timeout: int = 30
    min_interval_seconds: float = 0.0
    _last_request_at: float = field(default=0.0, init=False, repr=False, compare=False)

    def request(self, params: dict[str, Any]) -> dict[str, Any]:
        merged = {"chainid": self.chainid, "apikey": self.api_key}
        merged.update(params)
        last_data: dict[str, Any] | None = None
        for attempt in range(4):
            self._throttle()
            data = get_json(self.base_url, merged, timeout=self.timeout)
            if not self._is_retryable_api_error(data):
                return data
            last_data = data
            time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"Etherscan retryable API error persisted: {last_data}")

    def _throttle(self) -> None:
        interval = max(0.0, float(self.min_interval_seconds or 0.0))
        if interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_at
        if self._last_request_at > 0 and elapsed < interval:
            time.sleep(interval - elapsed)
            now = time.monotonic()
        object.__setattr__(self, "_last_request_at", now)

    @staticmethod
    def _is_retryable_api_error(data: dict[str, Any]) -> bool:
        if str(data.get("status")) != "0":
            return False
        message = str(data.get("message") or "").lower()
        result = str(data.get("result") or "").lower()
        combined = f"{message} {result}"
        return any(token in combined for token in ("timeout", "busy", "try again", "rate limit", "max rate"))

    def latest_block_number(self) -> int:
        data = self.request({"module": "proxy", "action": "eth_blockNumber"})
        result = data.get("result")
        if not isinstance(result, str):
            raise RuntimeError(f"Unexpected eth_blockNumber response: {data}")
        return hex_to_int(result)

    def get_block_by_number(self, block_number: int, full_transactions: bool = True) -> dict[str, Any]:
        data = self.request(
            {
                "module": "proxy",
                "action": "eth_getBlockByNumber",
                "tag": hex(block_number),
                "boolean": "true" if full_transactions else "false",
            }
        )
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected eth_getBlockByNumber response for {block_number}: {data}")
        return result

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        data = self.request({"module": "proxy", "action": "eth_getTransactionReceipt", "txhash": tx_hash})
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def get_transaction_by_hash(self, tx_hash: str) -> dict[str, Any] | None:
        data = self.request({"module": "proxy", "action": "eth_getTransactionByHash", "txhash": tx_hash})
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def get_logs(
        self,
        from_block: int,
        to_block: int,
        topic0: str,
        topic1: str | None = None,
        topic2: str | None = None,
        address: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "module": "logs",
            "action": "getLogs",
            "fromBlock": str(from_block),
            "toBlock": str(to_block),
            "topic0": topic0,
        }
        if address:
            params["address"] = address
        if topic1:
            params["topic1"] = topic1
            params["topic0_1_opr"] = "and"
        if topic2:
            params["topic2"] = topic2
            params["topic0_2_opr"] = "and"
        data = self.request(params)
        result = data.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if data.get("message") == "No records found":
            return []
        raise RuntimeError(f"Unexpected getLogs response: {data}")

    def get_contract_creation(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        normalized = normalized_unique_addresses(addresses)
        if not normalized:
            return {}
        creation: dict[str, dict[str, Any]] = {}
        for batch in chunked(normalized, CONTRACT_CREATION_BATCH_SIZE):
            data = self.request(
                {
                    "module": "contract",
                    "action": "getcontractcreation",
                    "contractaddresses": ",".join(batch),
                }
            )
            result = data.get("result")
            if not isinstance(result, list):
                continue
            for item in result:
                if not isinstance(item, dict):
                    continue
                address = item.get("contractAddress")
                if isinstance(address, str):
                    creation[address.lower()] = item
        return creation

    def get_internal_transactions(
        self,
        start_block: int,
        end_block: int,
        address: str | None = None,
        tx_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "module": "account",
            "action": "txlistinternal",
            "sort": "asc",
        }
        if tx_hash:
            params["txhash"] = tx_hash
        else:
            params["startblock"] = str(start_block)
            params["endblock"] = str(end_block)
        if address:
            params["address"] = address
        data = self.request(params)
        result = data.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        message = str(data.get("message") or "")
        if "No" in message and "found" in message:
            return []
        raise RuntimeError(f"Unexpected txlistinternal response: {data}")

    def eth_call(self, to: str, call_data: str, tag: str = "latest") -> str | None:
        data = self.request(
            {
                "module": "proxy",
                "action": "eth_call",
                "to": to,
                "data": call_data,
                "tag": tag,
            }
        )
        result = data.get("result")
        return result if isinstance(result, str) and result.startswith("0x") else None

    def get_source_code(self, address: str) -> dict[str, Any] | None:
        data = self.request({"module": "contract", "action": "getsourcecode", "address": address})
        result = data.get("result")
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        return None


def normalized_unique_addresses(addresses: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for address in addresses:
        if not isinstance(address, str):
            continue
        value = address.lower()
        if not (value.startswith("0x") and len(value) == 42):
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
