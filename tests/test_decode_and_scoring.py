import csv
import errno
import json
import os
import unittest

from alpha_listener.chains import chain_context_in_text
from alpha_listener.classify import (
    classify_contract,
    decode_string_result,
    decode_uint_result,
    is_liquidity_pool_artifact,
    supports_interface_data,
)
from alpha_listener.discovery import (
    build_internal_contract,
    collect_balancer_v2_candidates,
    collect_uniswap_v4_candidates,
    data_word_to_address_array,
    data_word_to_address,
    data_word_to_int,
    data_word_to_signed_int,
    dedupe_candidates,
    discover_activity_contracts_in_range,
    discover_log_candidates_from_raw,
    prioritize_candidates,
    select_log_candidates,
    topic_to_address,
    UNISWAP_V2_SWAP_TOPIC,
    UNISWAP_V3_SWAP_TOPIC,
)
from alpha_listener.enrichment import (
    build_queries,
    build_evidence,
    dedupe_tweets,
    extract_mentioned_handles,
    profile_has_strong_project_identity,
    profile_match_bonus,
    project_name_aliases,
    source_website_matches_contract,
)
from alpha_listener.etherscan import CONTRACT_CREATION_BATCH_SIZE, EtherscanClient
from alpha_listener.http_json import _redact_url
from alpha_listener.opentwitter import choose_website, expand_short_urls, extract_urls_from_value
from alpha_listener.scoring import score_project
from alpha_listener.signals import meaningful_name, meaningful_symbol, social_aggregator_profile
from alpha_listener.storage import Store
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


class DecodeAndScoringTests(unittest.TestCase):
    def test_decode_dynamic_string(self):
        raw = (
            "0x"
            + f"{32:064x}"
            + f"{5:064x}"
            + "416c706861"
            + "0" * 54
        )
        self.assertEqual(decode_string_result(raw), "Alpha")

    def test_decode_uint(self):
        self.assertEqual(decode_uint_result("0x" + "0" * 63 + "a"), 10)

    def test_classify_contract_extracts_source_urls(self):
        def encoded_string(text):
            body = text.encode("utf-8").hex()
            padding = "0" * ((64 - len(body) % 64) % 64)
            return "0x" + f"{32:064x}" + f"{len(text):064x}" + body + padding

        class FakeClient:
            def eth_call(self, _address, data):
                if data == "0x06fdde03":
                    return encoded_string("MoltenBear")
                if data == "0x95d89b41":
                    return encoded_string("MLT")
                if data == "0x313ce567":
                    return "0x" + f"{18:064x}"
                if data == "0x18160ddd":
                    return "0x" + f"{1000:064x}"
                return "0x" + "0" * 64

            def get_source_code(self, _address):
                return {
                    "SourceCode": "\n".join(
                        [
                            "// Website: https://moltenbear.xyz/",
                            "// Escaped: https://moltenbear.xyz/\\n",
                            "// Library: https://github.com/OpenZeppelin/openzeppelin-contracts",
                            "// Spec: https://eips.ethereum.org/EIPS/eip-20",
                            "// Docs: https://docs.ethers.io/v5/api/signer/#Signer-signMessage[ethers",
                            "// MDN: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/indexOf",
                            "// SVG: http://www.w3.org/2000/svg",
                            "// Bits: https://graphics.stanford.edu/~seander/bithacks.html#ReverseParallel",
                            "contract MoltenBear {}",
                        ]
                    ),
                    "ContractName": "MoltenBearToken",
                    "ABI": "[]",
                    "CompilerVersion": "v0.8.24",
                }

        contract = classify_contract(FakeClient(), "0x1111111111111111111111111111111111111111")
        self.assertEqual(contract["source"]["urls"], ["https://moltenbear.xyz"])

    def test_supports_interface_data_left_aligns_bytes4(self):
        self.assertEqual(
            supports_interface_data("80ac58cd"),
            "0x01ffc9a7" + "80ac58cd".ljust(64, "0"),
        )

    def test_classify_contract_detects_erc721_from_supports_interface(self):
        def encoded_string(text):
            body = text.encode("utf-8").hex()
            padding = "0" * ((64 - len(body) % 64) % 64)
            return "0x" + f"{32:064x}" + f"{len(text):064x}" + body + padding

        class FakeClient:
            def eth_call(self, _address, data):
                if data == "0x06fdde03":
                    return encoded_string("Mergeable Rectangles")
                if data == "0x95d89b41":
                    return encoded_string("MRECT")
                if data == "0x18160ddd":
                    return "0x" + f"{10000:064x}"
                if data == supports_interface_data("80ac58cd"):
                    return "0x" + f"{1:064x}"
                return "0x" + "0" * 64

            def get_source_code(self, _address):
                return {
                    "SourceCode": "contract MergeableRectangles is ERC721 {}",
                    "ContractName": "MergeableRectangles",
                    "ABI": "[]",
                    "CompilerVersion": "v0.8.33",
                }

        contract = classify_contract(FakeClient(), "0x0e74363bba068f2a9ce31aa035a0610b020ab41a")
        self.assertEqual(contract["kind"], "erc721")
        self.assertEqual(contract["name"], "Mergeable Rectangles")
        self.assertEqual(contract["symbol"], "MRECT")

    def test_classify_contract_detects_erc1155_from_supports_interface(self):
        class FakeClient:
            def eth_call(self, _address, data):
                if data == supports_interface_data("d9b67a26"):
                    return "0x" + f"{1:064x}"
                return "0x" + "0" * 64

            def get_source_code(self, _address):
                return {
                    "SourceCode": "contract AlphaItems is ERC1155 {}",
                    "ContractName": "AlphaItems",
                    "ABI": "[]",
                    "CompilerVersion": "v0.8.24",
                }

        contract = classify_contract(FakeClient(), "0x1111111111111111111111111111111111111111")
        self.assertEqual(contract["kind"], "erc1155")

    def test_score_high_identity_token(self):
        contract = {
            "kind": "erc20",
            "name": "Example Protocol",
            "symbol": "EXA",
            "verified": True,
            "contract_name": "ExampleToken",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_count": 1,
        }
        evidence = {
            "twitter_account": "ExampleProtocol",
            "twitter_followers": 12000,
            "twitter_verified": False,
            "website": "https://example.org",
            "tweet_count": 5,
            "address_mentions": 2,
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 60)
        self.assertIn(scored["tier"], {"medium", "high"})
        self.assertIn("onchain_provenance", scored["breakdown"])

    def test_balancer_registration_counts_as_strong_dex_surface(self):
        contract = {
            "kind": "erc20",
            "name": "Balancer Alpha",
            "symbol": "BALA",
            "verified": True,
            "contract_name": "BalancerAlphaToken",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,balancer_v2_tokens_registered",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": "BalancerAlpha",
            "twitter_followers": 1000,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 3,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "account_crypto_project_context",
        }
        scored = score_project(contract, evidence)
        self.assertEqual(scored["tier"], "high")
        self.assertEqual(scored["breakdown"]["onchain_provenance"], 14)

    def test_liquidity_pool_artifact_scores_low(self):
        self.assertTrue(is_liquidity_pool_artifact("Uniswap V2", "UNI-V2", "UniswapV2Pair"))
        contract = {
            "kind": "liquidity_pool",
            "name": "Uniswap V2",
            "symbol": "UNI-V2",
            "verified": True,
            "contract_name": "UniswapV2Pair",
            "source_len": 5000,
            "discovery_source": "internal_create2",
            "observation_sources": "internal_create2,mint_transfer",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": None,
            "website": None,
            "tweet_count": 0,
            "address_mentions": 0,
        }
        scored = score_project(contract, evidence)
        self.assertEqual(scored["tier"], "low")
        self.assertLess(scored["score"], 30)

    def test_dedupe_tweets_by_id(self):
        tweets = [
            {"id": "1", "text": "alpha"},
            {"id": "1", "text": "alpha duplicate"},
            {"id": "2", "text": "beta"},
        ]
        self.assertEqual(len(dedupe_tweets(tweets)), 2)

    def test_topic_to_address(self):
        topic = "0x" + "0" * 24 + "1234567890abcdef1234567890abcdef12345678"
        self.assertEqual(topic_to_address(topic), "0x1234567890abcdef1234567890abcdef12345678")

    def test_data_word_to_address(self):
        data = "0x" + "0" * 64 + "0" * 24 + "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
        self.assertEqual(data_word_to_address(data, 1), "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")

    def test_data_word_to_address_array(self):
        token_a = "1111111111111111111111111111111111111111"
        token_b = "2222222222222222222222222222222222222222"
        data = (
            "0x"
            + f"{64:064x}"
            + f"{160:064x}"
            + f"{2:064x}"
            + "0" * 24
            + token_a
            + "0" * 24
            + token_b
            + f"{2:064x}"
            + "0" * 64
            + "0" * 64
        )
        self.assertEqual(
            data_word_to_address_array(data, 0),
            ["0x" + token_a, "0x" + token_b],
        )

    def test_data_word_to_int(self):
        data = "0x" + f"{1234:064x}"
        self.assertEqual(data_word_to_int(data, 0), 1234)

    def test_data_word_to_signed_int(self):
        data = "0x" + f"{(1 << 256) - 60:064x}"
        self.assertEqual(data_word_to_signed_int(data, 0), -60)

    def test_collect_uniswap_v4_candidates(self):
        class FakeClient:
            def get_logs(self, *_args, **_kwargs):
                return [
                    {
                        "topics": [
                            "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438",
                            "0x" + "a" * 64,
                            "0x" + "0" * 24 + "1111111111111111111111111111111111111111",
                            "0x" + "0" * 24 + "2222222222222222222222222222222222222222",
                        ],
                        "data": (
                            "0x"
                            + f"{3000:064x}"
                            + f"{60:064x}"
                            + "0" * 24
                            + "3333333333333333333333333333333333333333"
                            + f"{2**96:064x}"
                            + f"{1:064x}"
                        ),
                        "transactionHash": "0xabc",
                        "logIndex": "0x2",
                        "timeStamp": "0x10",
                    }
                ]

        candidates = collect_uniswap_v4_candidates(FakeClient(), 1, set())
        self.assertEqual({item["discovery_source"] for item in candidates}, {"uniswap_v4_initialize", "uniswap_v4_hook"})
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["related"]["fee"], 3000)
        self.assertEqual(candidates[0]["related"]["hook"], "0x3333333333333333333333333333333333333333")

    def test_collect_balancer_v2_candidates(self):
        token_a = "1111111111111111111111111111111111111111"
        token_b = "2222222222222222222222222222222222222222"

        class FakeClient:
            def __init__(self):
                self.calls = []

            def get_logs(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return [
                    {
                        "topics": [
                            "0xf5847d3f2197b16cdcd2098ec95d0905cd1abdaf415f07bb7cef2bba8ac5dec4",
                            "0x" + "a" * 64,
                        ],
                        "data": (
                            "0x"
                            + f"{64:064x}"
                            + f"{160:064x}"
                            + f"{2:064x}"
                            + "0" * 24
                            + token_a
                            + "0" * 24
                            + token_b
                            + f"{2:064x}"
                            + "0" * 64
                            + "0" * 64
                        ),
                        "transactionHash": "0xdef",
                        "logIndex": "0x3",
                        "timeStamp": "0x20",
                    }
                ]

        client = FakeClient()
        candidates = collect_balancer_v2_candidates(client, 1, {"0x" + token_b})
        self.assertEqual([item["address"] for item in candidates], ["0x" + token_a])
        self.assertEqual(candidates[0]["discovery_source"], "balancer_v2_tokens_registered")
        self.assertEqual(candidates[0]["related"]["pool_id"], "0x" + "a" * 64)
        self.assertEqual(client.calls[0][1]["address"], "0xba12222222228d8ba445958a75a0704d566bf2c8")

    def test_get_contract_creation_batches_and_dedupes_addresses(self):
        class FakeClient(EtherscanClient):
            def __init__(self):
                object.__setattr__(self, "calls", [])

            def request(self, params):
                batch = params["contractaddresses"].split(",")
                self.calls.append(batch)
                return {
                    "result": [
                        {"contractAddress": address, "txHash": f"0x{index:064x}"}
                        for index, address in enumerate(batch)
                    ]
                }

        addresses = [f"0x{index:040x}" for index in range(CONTRACT_CREATION_BATCH_SIZE + 3)]
        addresses.append(addresses[0].upper())
        client = FakeClient()
        creation = client.get_contract_creation(addresses)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(client.calls[0]), CONTRACT_CREATION_BATCH_SIZE)
        self.assertEqual(len(creation), CONTRACT_CREATION_BATCH_SIZE + 3)
        self.assertIn(addresses[0], creation)

    def test_base_chain_profile_uses_separate_defaults_and_data_dir(self):
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "ETHERSCAN_API_KEY": "key",
                "ALPHA_INTERVAL_SECONDS": "60",
                "ALPHA_MAX_BLOCKS_PER_CYCLE": "60",
            }
            with patch.dict(os.environ, env, clear=True):
                base = Settings.from_env(root, chain="base")
                bsc = Settings.from_env(root, chain="bsc")
                ethereum = Settings.from_env(root, chain="ethereum")

        self.assertEqual(base.chain_slug, "base")
        self.assertEqual(base.chainid, "8453")
        self.assertEqual(base.data_dir, root / "data" / "base")
        self.assertEqual(base.interval_seconds, 1800)
        self.assertEqual(base.max_blocks_per_cycle, 300)
        self.assertEqual(base.discovery_mode, "activity")
        self.assertEqual(base.activity_min_observations, 100)
        self.assertEqual(base.activity_include_all_transfers, 1)
        self.assertEqual(base.activity_all_transfer_log_span, 5)
        self.assertIn("0x3da2bd01ee75c5ddd62a82c0f79e99834ee5cc995f86b9c87737292dd54bfc1d", base.activity_custom_event_topics)
        self.assertEqual(bsc.chainid, "56")
        self.assertEqual(bsc.block_time_seconds, 0.45)
        self.assertEqual(bsc.max_blocks_per_cycle, 5000)
        self.assertEqual(bsc.confirmations, 64)
        self.assertEqual(bsc.discovery_mode, "activity")
        self.assertEqual(bsc.activity_min_observations, 100)
        self.assertEqual(ethereum.interval_seconds, 60)
        self.assertEqual(ethereum.max_blocks_per_cycle, 60)
        self.assertEqual(ethereum.discovery_mode, "activity")
        self.assertEqual(ethereum.activity_min_observations, 100)

    def test_chain_specific_queries_and_context(self):
        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "Alpha Project",
            "chain_slug": "base",
        }
        queries = build_queries(contract)
        self.assertIn("Alpha Project base", queries)
        self.assertNotIn("Alpha Project ethereum", queries)
        self.assertTrue(chain_context_in_text("Alpha Project is live on Base", "base"))
        self.assertTrue(chain_context_in_text("Alpha Project listed on PancakeSwap", "bsc"))

    def test_swap_topics_are_valid_topic0_hashes(self):
        self.assertEqual(len(UNISWAP_V2_SWAP_TOPIC), 66)
        self.assertEqual(UNISWAP_V2_SWAP_TOPIC, "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822")
        self.assertEqual(len(UNISWAP_V3_SWAP_TOPIC), 66)
        self.assertEqual(UNISWAP_V3_SWAP_TOPIC, "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67")

    def test_log_candidate_priority_preserves_distinct_sources(self):
        address = "0x1111111111111111111111111111111111111111"
        candidates = [
            {"address": address, "discovery_source": "mint_transfer", "origin_tx_hash": "0x1", "log_index": 1},
            {"address": address, "discovery_source": "uniswap_v4_initialize", "origin_tx_hash": "0x2", "log_index": 2},
            {"address": address, "discovery_source": "uniswap_v4_initialize", "origin_tx_hash": "0x2", "log_index": 2},
            {"address": address, "discovery_source": "balancer_v2_tokens_registered", "origin_tx_hash": "0x3", "log_index": 3},
        ]
        prioritized = dedupe_candidates(prioritize_candidates(candidates))
        self.assertEqual(
            [item["discovery_source"] for item in prioritized],
            ["uniswap_v4_initialize", "balancer_v2_tokens_registered", "mint_transfer"],
        )

    def test_select_log_candidates_applies_limit_after_priority_and_dedupe(self):
        candidates = [
            {
                "address": "0x1111111111111111111111111111111111111111",
                "discovery_source": "mint_transfer",
                "origin_tx_hash": "0x1",
                "log_index": 1,
            },
            {
                "address": "0x2222222222222222222222222222222222222222",
                "discovery_source": "uniswap_v4_initialize",
                "origin_tx_hash": "0x2",
                "log_index": 2,
            },
            {
                "address": "0x2222222222222222222222222222222222222222",
                "discovery_source": "uniswap_v4_initialize",
                "origin_tx_hash": "0x2",
                "log_index": 2,
            },
        ]
        selected = select_log_candidates(candidates, 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["discovery_source"], "uniswap_v4_initialize")

    def test_log_candidate_discovery_can_use_prefetched_creation_cache(self):
        class FakeClient:
            def get_contract_creation(self, _addresses):
                raise AssertionError("creation lookup should come from cache")

        address = "0x1111111111111111111111111111111111111111"
        candidates = [
            {
                "address": address,
                "discovery_source": "uniswap_v4_initialize",
                "origin_tx_hash": "0xabc",
                "log_index": 1,
                "block_timestamp": 100,
                "related": {},
            }
        ]
        creation_cache = {
            address: {
                "contractAddress": address,
                "txHash": "0xdef",
                "blockNumber": "10",
                "timestamp": "100",
                "contractCreator": "0x2222222222222222222222222222222222222222",
            }
        }
        discovered = discover_log_candidates_from_raw(
            FakeClient(),
            candidates,
            block_number=12,
            max_log_candidates=5,
            new_contract_max_age_blocks=100,
            classify=False,
            creation_cache=creation_cache,
        )
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["address"], address)
        self.assertEqual(discovered[0]["origin_tx_hash"], "0xdef")

    def test_activity_discovery_filters_to_new_high_activity_contracts(self):
        active = "0x1111111111111111111111111111111111111111"
        quiet = "0x2222222222222222222222222222222222222222"
        old = "0x3333333333333333333333333333333333333333"

        def transfer_log(address, block_number, tx_index):
            return {
                "address": address,
                "blockNumber": hex(block_number),
                "timeStamp": hex(1000 + block_number),
                "transactionHash": f"0x{tx_index:064x}",
                "logIndex": hex(tx_index),
                "topics": [],
            }

        class FakeClient:
            def get_logs(self, _from_block, _to_block, topic0, **_kwargs):
                if topic0 != "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef":
                    return []
                return [
                    transfer_log(active, 100, 1),
                    transfer_log(active, 101, 2),
                    transfer_log(active, 102, 3),
                    transfer_log(quiet, 102, 4),
                    transfer_log(old, 103, 5),
                    transfer_log(old, 104, 6),
                    transfer_log(old, 105, 7),
                ]

            def get_contract_creation(self, addresses):
                return {
                    active: {
                        "contractAddress": active,
                        "txHash": "0xaaa",
                        "blockNumber": "90",
                        "timestamp": "990",
                        "contractCreator": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    },
                    quiet: {
                        "contractAddress": quiet,
                        "txHash": "0xbbb",
                        "blockNumber": "91",
                        "timestamp": "991",
                        "contractCreator": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    },
                    old: {
                        "contractAddress": old,
                        "txHash": "0xccc",
                        "blockNumber": "1",
                        "timestamp": "1",
                        "contractCreator": "0xcccccccccccccccccccccccccccccccccccccccc",
                    },
                }

        discovered = discover_activity_contracts_in_range(
            FakeClient(),
            start_block=100,
            end_block=110,
            min_observations=3,
            max_candidates=10,
            new_contract_max_age_blocks=30,
            chain_slug="bsc",
            transfer_log_max_span=100,
            include_all_transfers=False,
            classify=False,
        )
        self.assertEqual([item["address"] for item in discovered], [active])
        self.assertEqual(discovered[0]["discovery_source"], "activity_mint")
        self.assertEqual(discovered[0]["related"]["activity_observations"], 3)

    def test_activity_discovery_can_count_regular_transfer_activity(self):
        active = "0x1111111111111111111111111111111111111111"
        zero = "0x" + "0" * 64
        user = "0x" + "0" * 24 + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        def transfer_log(block_number, tx_index, from_topic=user):
            return {
                "address": active,
                "blockNumber": hex(block_number),
                "timeStamp": hex(1000 + block_number),
                "transactionHash": f"0x{tx_index:064x}",
                "logIndex": hex(tx_index),
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    from_topic,
                    user,
                ],
            }

        class FakeClient:
            def get_logs(self, from_block, to_block, topic0, **kwargs):
                if topic0 != "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef":
                    return []
                if kwargs.get("topic1") == zero:
                    logs = [transfer_log(100, 1, from_topic=zero)]
                else:
                    logs = [
                    transfer_log(100, 1, from_topic=zero),
                    transfer_log(101, 2),
                    transfer_log(102, 3),
                    transfer_log(103, 4),
                    ]
                return [log for log in logs if from_block <= int(log["blockNumber"], 16) <= to_block]

            def get_contract_creation(self, addresses):
                return {
                    active: {
                        "contractAddress": active,
                        "txHash": "0xaaa",
                        "blockNumber": "90",
                        "timestamp": "990",
                        "contractCreator": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    }
                    for _address in addresses
                }

        progress_events = []
        discovered = discover_activity_contracts_in_range(
            FakeClient(),
            start_block=100,
            end_block=110,
            min_observations=3,
            max_candidates=10,
            new_contract_max_age_blocks=30,
            include_all_transfers=True,
            all_transfer_log_max_span=5,
            custom_event_topics=[],
            classify=False,
            progress=progress_events.append,
        )
        self.assertEqual([item["address"] for item in discovered], [active])
        self.assertEqual(discovered[0]["discovery_source"], "activity_mint")
        self.assertIn("activity_transfer", discovered[0]["related"]["sources"])
        self.assertEqual(discovered[0]["related"]["activity_observations"], 4)
        self.assertTrue(
            any(
                event.get("event") == "activity_scan_stage"
                and event.get("stage") == "all_transfers"
                and event.get("status") == "progress"
                for event in progress_events
            )
        )
        self.assertTrue(
            any(
                event.get("stage") == "creation_probe"
                and event.get("status") == "finished"
                and event.get("creation_rows") == 1
                for event in progress_events
            )
        )

    def test_activity_discovery_can_count_custom_event_topics(self):
        active = "0x1111111111111111111111111111111111111111"
        custom_topic = "0x" + "a" * 64

        def custom_log(block_number, tx_index):
            return {
                "address": active,
                "blockNumber": hex(block_number),
                "timeStamp": hex(1000 + block_number),
                "transactionHash": f"0x{tx_index:064x}",
                "logIndex": hex(tx_index),
                "topics": [custom_topic],
            }

        class FakeClient:
            def get_logs(self, _from_block, _to_block, topic0, **_kwargs):
                if topic0 != custom_topic:
                    return []
                return [custom_log(100, 1), custom_log(101, 2), custom_log(102, 3)]

            def get_contract_creation(self, addresses):
                return {
                    active: {
                        "contractAddress": active,
                        "txHash": "0xaaa",
                        "blockNumber": "90",
                        "timestamp": "990",
                        "contractCreator": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    }
                    for _address in addresses
                }

        discovered = discover_activity_contracts_in_range(
            FakeClient(),
            start_block=100,
            end_block=110,
            min_observations=3,
            max_candidates=10,
            new_contract_max_age_blocks=30,
            custom_event_topics=[custom_topic, custom_topic, "bad"],
            classify=False,
        )
        self.assertEqual([item["address"] for item in discovered], [active])
        self.assertEqual(discovered[0]["discovery_source"], "activity_custom_event")
        self.assertEqual(discovered[0]["related"]["activity_observations"], 3)

    def test_summary_includes_source_and_pending(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            store.upsert_contract(
                {
                    "address": "0x1111111111111111111111111111111111111111",
                    "tx_hash": "mint:1",
                    "origin_tx_hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "discovery_source": "mint_transfer",
                    "log_index": 1,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": 1,
                    "block_timestamp": 1,
                    "tx_index": None,
                    "value_wei": None,
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": "Alpha",
                    "symbol": "ALP",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": False,
                    "contract_name": "",
                    "source_len": 0,
                    "confidence": 0.5,
                }
            )
            summary = store.summary()
            store.close()
            self.assertEqual(summary["pending_enrichment"], 1)
            self.assertEqual(summary["by_source"]["mint_transfer"], 1)
            self.assertEqual(summary["observations"], 1)
            self.assertEqual(summary["observations_by_source"]["mint_transfer"], 1)

    def test_upsert_contract_result_separates_new_contract_observation_and_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            base = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "mint:1",
                "origin_tx_hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "discovery_source": "mint_transfer",
                "log_index": 1,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": None,
                "value_wei": None,
                "input_prefix": "",
                "kind": "erc721",
                "name": "Alpha NFT",
                "symbol": "ANFT",
                "decimals": None,
                "total_supply": "1",
                "verified": True,
                "contract_name": "AlphaNFT",
                "source_len": 5000,
                "confidence": 0.8,
            }

            first = store.upsert_contract_result(base)
            duplicate = store.upsert_contract_result(base)
            same_source_new_log = store.upsert_contract_result({**base, "tx_hash": "mint:2", "origin_tx_hash": "0xbb", "log_index": 2})
            new_source = store.upsert_contract_result(
                {
                    **base,
                    "tx_hash": "v4:1",
                    "origin_tx_hash": "0xcc",
                    "discovery_source": "uniswap_v4_initialize",
                    "log_index": 3,
                }
            )
            store.close()

            self.assertEqual(
                first,
                {"contract_is_new": True, "observation_is_new": True, "source_is_new": True, "enrichment_requeued": False},
            )
            self.assertFalse(duplicate["contract_is_new"])
            self.assertFalse(duplicate["observation_is_new"])
            self.assertFalse(duplicate["source_is_new"])
            self.assertFalse(same_source_new_log["contract_is_new"])
            self.assertTrue(same_source_new_log["observation_is_new"])
            self.assertFalse(same_source_new_log["source_is_new"])
            self.assertFalse(new_source["contract_is_new"])
            self.assertTrue(new_source["observation_is_new"])
            self.assertTrue(new_source["source_is_new"])

    def test_deferred_observation_preserves_existing_classification(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            base = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Alpha Token",
                "symbol": "ALPHA",
                "decimals": 18,
                "total_supply": "1000",
                "verified": True,
                "contract_name": "AlphaToken",
                "source_len": 5000,
                "confidence": 0.9,
            }
            self.assertTrue(store.upsert_contract(base))
            deferred = {
                **base,
                "tx_hash": "mint:1",
                "origin_tx_hash": "0xbbb",
                "discovery_source": "mint_transfer",
                "log_index": 2,
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
            self.assertFalse(store.upsert_contract(deferred))
            row = store.conn.execute("SELECT kind, name, symbol, verified, contract_name FROM contracts WHERE address = ?", (base["address"],)).fetchone()
            store.close()
            self.assertEqual(row["kind"], "erc20")
            self.assertEqual(row["name"], "Alpha Token")
            self.assertEqual(row["symbol"], "ALPHA")
            self.assertEqual(row["verified"], 1)
            self.assertEqual(row["contract_name"], "AlphaToken")

    def test_pending_enrichment_prioritizes_dex_and_multi_signal_candidates(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(address, source, block):
                return {
                    "address": address,
                    "tx_hash": f"{source}:{address}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": source,
                    "log_index": block,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
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

            old_internal = contract("0x1111111111111111111111111111111111111111", "internal_create2", 1)
            v4_candidate = contract("0x2222222222222222222222222222222222222222", "uniswap_v4_initialize", 2)
            mint_candidate = contract("0x3333333333333333333333333333333333333333", "mint_transfer", 3)
            balancer_candidate = contract("0x4444444444444444444444444444444444444444", "balancer_v2_tokens_registered", 4)
            store.upsert_contract(old_internal)
            store.upsert_contract(mint_candidate)
            store.upsert_contract(v4_candidate)
            store.upsert_contract(balancer_candidate)
            targets = store.pending_enrichment(4)
            store.close()
            self.assertEqual(
                [item["address"] for item in targets],
                [v4_candidate["address"], balancer_candidate["address"], mint_candidate["address"], old_internal["address"]],
            )

    def test_queue_health_separates_v4_backlog(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(address, source, block):
                return {
                    "address": address,
                    "tx_hash": f"{source}:{address}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": source,
                    "log_index": block,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
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

            v4_candidate = contract("0x1111111111111111111111111111111111111111", "uniswap_v4_initialize", 1)
            mint_candidate = contract("0x2222222222222222222222222222222222222222", "mint_transfer", 2)
            internal_candidate = contract("0x3333333333333333333333333333333333333333", "internal_create2", 3)
            balancer_candidate = contract("0x4444444444444444444444444444444444444444", "balancer_v2_tokens_registered", 4)
            store.upsert_contract(v4_candidate)
            store.upsert_contract(mint_candidate)
            store.upsert_contract(internal_candidate)
            store.upsert_contract(balancer_candidate)
            store.upsert_enrichment(internal_candidate["address"], {"queries": [], "evidence": {}, "score": {}, "status": "retry"})

            health = store.queue_health()
            store.close()

            self.assertEqual(health["pending_total"], 4)
            self.assertEqual(health["never_enriched"], 3)
            self.assertEqual(health["retry"], 1)
            self.assertEqual(health["pending_by_bucket"]["uniswap_v4"]["contracts"], 1)
            self.assertEqual(health["pending_by_bucket"]["other_dex"]["contracts"], 1)
            self.assertEqual(health["pending_by_bucket"]["mint"]["contracts"], 1)
            self.assertEqual(health["pending_by_bucket"]["internal_only"]["contracts"], 1)

    def test_reserve_enrichment_targets_prevents_duplicate_workers(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contracts = []
            for index, source in enumerate(("uniswap_v4_initialize", "mint_transfer"), start=1):
                item = {
                    "address": f"0x{index:040x}",
                    "tx_hash": f"{source}:{index}",
                    "origin_tx_hash": f"0x{index:064x}",
                    "discovery_source": source,
                    "log_index": index,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": index,
                    "block_timestamp": index,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
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
                contracts.append(item)
                store.upsert_contract(item)
            first = store.reserve_enrichment_targets(1)
            second = store.reserve_enrichment_targets(2)
            store.close()
            self.assertEqual([item["address"] for item in first], [contracts[0]["address"]])
            self.assertEqual([item["address"] for item in second], [contracts[1]["address"]])

    def test_enrichment_context_refreshes_sources_added_after_reservation(self):
        from alpha_listener.cli import contract_payload, ensure_classified

        class FakeEtherscan:
            def eth_call(self, _address, data):
                if data == "0x06fdde03":
                    return "0x" + f"{32:064x}" + f"{5:064x}" + "416c706861" + "0" * 54
                if data == "0x95d89b41":
                    return "0x" + f"{32:064x}" + f"{3:064x}" + "414c50" + "0" * 58
                if data == "0x313ce567":
                    return "0x" + f"{18:064x}"
                if data == "0x18160ddd":
                    return "0x" + f"{1000:064x}"
                return "0x" + "0" * 64

            def get_source_code(self, _address):
                return {"SourceCode": "contract Alpha {}", "ContractName": "Alpha", "ABI": "[]"}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            address = "0x1111111111111111111111111111111111111111"
            base = {
                "address": address,
                "tx_hash": "internal:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "internal_create2",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
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
            store.upsert_contract(base)
            reserved = store.reserve_enrichment_targets(1)[0]
            self.assertEqual(reserved["observation_sources"], "internal_create2")

            store.upsert_contract(
                {
                    **base,
                    "tx_hash": "v4:1",
                    "origin_tx_hash": "0xbbb",
                    "discovery_source": "uniswap_v4_initialize",
                    "log_index": 9,
                    "related": {"pool_id": "0xabc"},
                }
            )
            stale_payload = contract_payload(reserved)
            ensure_classified(FakeEtherscan(), store, stale_payload)
            current = store.enrichment_context(address)
            store.close()

            self.assertIsNotNone(current)
            sources = set(str(current["observation_sources"]).split(","))
            self.assertEqual(sources, {"internal_create2", "uniswap_v4_initialize"})
            self.assertEqual(current["observation_count"], 2)
            self.assertEqual(current["kind"], "erc20")
            self.assertEqual(current["name"], "Alpha")

    def test_reset_processing_enrichments_recovers_interrupted_worker(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            item = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
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
            store.upsert_contract(item)
            store.reserve_enrichment_targets(1)
            recovered = store.reset_processing_enrichments()
            summary = store.summary()
            store.close()
            self.assertEqual(recovered, 1)
            self.assertEqual(summary["processing_enrichment"], 0)
            self.assertEqual(summary["pending_enrichment"], 1)
            self.assertEqual(summary["by_status"]["retry"], 1)

    def test_recover_processing_command_records_queue_transition(self):
        from alpha_listener.cli import handle_recover_processing
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            item = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
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
            store.upsert_contract(item)
            store.reserve_enrichment_targets(1)
            store.conn.execute("UPDATE enrichments SET searched_at = datetime('now', '-31 minutes')")
            store.conn.commit()
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            result = handle_recover_processing(settings, SimpleNamespace(older_than_minutes=30))

            self.assertEqual(result["recovered_processing"], 1)
            self.assertEqual(result["queue_before"]["stale_processing"], 1)
            self.assertEqual(result["queue_after"]["stale_processing"], 0)
            self.assertEqual(result["queue_after"]["retry"], 1)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(
                any(
                    event["event_type"] == "stale_processing_recovered"
                    and event["payload"].get("command") == "recover-processing"
                    and event["payload"].get("recovered") == 1
                    for event in events
                )
            )

    def test_run_cycle_recovers_stale_processing_before_enrichment_reserve(self):
        from alpha_listener.cli import run_cycle
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            item = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "internal:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "internal_create2",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "contract",
                "name": None,
                "symbol": None,
                "decimals": None,
                "total_supply": None,
                "verified": False,
                "contract_name": "",
                "source_len": 0,
                "confidence": 0.05,
            }
            store.upsert_contract(item)
            store.reserve_enrichment_targets(1)
            store.conn.execute("UPDATE enrichments SET searched_at = datetime('now', '-31 minutes')")
            store.conn.commit()
            self.assertEqual(store.queue_health()["stale_processing"], 1)
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            args = SimpleNamespace(
                command="once",
                no_twitter=False,
                confirmations=None,
                lookback_blocks=None,
                max_blocks=0,
                enrich_limit=1,
                force_enrich=False,
                report_limit=None,
                report_every_enriched=None,
                start_block=None,
                end_block=None,
            )

            with patch("alpha_listener.cli.EtherscanClient") as etherscan_cls, patch(
                "alpha_listener.cli.OpenTwitterClient"
            ):
                etherscan_cls.return_value.latest_block_number.return_value = 110
                result = run_cycle(settings, args)

            store = Store(root / "alpha.sqlite", root)
            queue = store.queue_health()
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            store.close()

            self.assertEqual(result["enriched_contracts"], 1)
            self.assertEqual(result["summary"]["processing_enrichment"], 0)
            self.assertEqual(queue["stale_processing"], 0)
            self.assertTrue(
                any(
                    event["event_type"] == "cycle_progress"
                    and event["payload"].get("event") == "stale_processing_recovered"
                    and event["payload"].get("recovered") == 1
                    for event in events
                )
            )

    def test_run_cycle_reserves_enrichment_in_rolling_batches(self):
        from alpha_listener.cli import run_cycle
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            for index in range(3):
                address = f"0x{index + 1:040x}"
                store.upsert_contract(
                    {
                        "address": address,
                        "tx_hash": f"direct:{index}",
                        "origin_tx_hash": f"0x{index + 1:064x}",
                        "discovery_source": "contract_creation",
                        "log_index": None,
                        "related": {},
                        "deployer": "0x2222222222222222222222222222222222222222",
                        "block_number": index + 1,
                        "block_timestamp": index + 1,
                        "tx_index": index,
                        "value_wei": "0",
                        "input_prefix": "",
                        "kind": "erc20",
                        "name": f"Rolling {index}",
                        "symbol": f"R{index}",
                        "decimals": 18,
                        "total_supply": 1000,
                        "verified": True,
                        "contract_name": f"Rolling{index}",
                        "source_len": 3000,
                        "confidence": 0.8,
                    }
                )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
                enrichment_reservation_batch_size=1,
            )
            args = SimpleNamespace(
                command="once",
                no_twitter=False,
                confirmations=None,
                lookback_blocks=None,
                max_blocks=0,
                enrich_limit=3,
                force_enrich=False,
                report_limit=None,
                report_every_enriched=None,
                start_block=None,
                end_block=None,
                verify_websites_limit=0,
                backfill_website_twitter_limit=0,
            )
            processing_counts = []

            def fake_enrich(_twitter, contract, _max_results):
                reader = Store(root / "alpha.sqlite", root)
                try:
                    processing_counts.append(reader.summary()["processing_enrichment"])
                finally:
                    reader.close()
                evidence = {
                    "twitter_account": None,
                    "website": None,
                    "tweet_count": 0,
                    "address_mentions": 0,
                    "credible_address_mentions": 0,
                }
                return {"queries": [], "evidence": evidence, "score": score_project(contract, evidence), "status": "ok"}

            with patch("alpha_listener.cli.EtherscanClient") as etherscan_cls, patch(
                "alpha_listener.cli.OpenTwitterClient"
            ), patch("alpha_listener.cli.enrich_contract", side_effect=fake_enrich):
                etherscan_cls.return_value.latest_block_number.return_value = 110
                result = run_cycle(settings, args)

            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            reserved_events = [
                event["payload"]
                for event in events
                if event["event_type"] == "cycle_progress"
                and event["payload"].get("event") == "enrichment_batch_reserved"
            ]

            self.assertEqual(result["enriched_contracts"], 3)
            self.assertEqual(processing_counts, [1, 1, 1])
            self.assertEqual([event["reserved"] for event in reserved_events], [1, 1, 1])
            self.assertEqual([event["batch_limit"] for event in reserved_events], [1, 1, 1])

    def test_processing_reservations_are_not_counted_as_enriched(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            item = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
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
            store.upsert_contract(item)
            store.reserve_enrichment_targets(1)
            summary = store.summary()
            store.close()
            self.assertEqual(summary["enriched"], 0)
            self.assertEqual(summary["processing_enrichment"], 1)
            self.assertEqual(summary["by_status"]["processing"], 1)
            self.assertNotIn("unknown", summary["by_tier"])

    def test_retry_rows_are_not_counted_as_enriched_tiers(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            item = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
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
            store.upsert_contract(item)
            store.upsert_enrichment(item["address"], {"queries": [], "evidence": {}, "score": {}, "status": "retry"})
            summary = store.summary()
            store.close()
            self.assertEqual(summary["enriched"], 0)
            self.assertEqual(summary["by_status"]["retry"], 1)
            self.assertEqual(summary["by_tier"], {})

    def test_requeue_enrichments_filters_by_tier(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(index):
                address = f"0x{index:040x}"
                return {
                    "address": address,
                    "tx_hash": f"direct:{index}",
                    "origin_tx_hash": f"0x{index:064x}",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": index,
                    "block_timestamp": index,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": f"Alpha {index}",
                    "symbol": f"A{index}",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "Alpha",
                    "source_len": 5000,
                    "confidence": 0.9,
                }

            rows = [(contract(1), "high"), (contract(2), "medium"), (contract(3), "watch")]
            for item, tier in rows:
                store.upsert_contract(item)
                store.upsert_enrichment(
                    item["address"],
                    {"queries": [], "evidence": {}, "score": {"score": 80, "tier": tier}, "status": "ok"},
                )
            requeued = store.requeue_enrichments(["high", "medium"])
            statuses = {
                row["tier"]: row["status"]
                for row in store.conn.execute("SELECT tier, status FROM enrichments").fetchall()
            }
            store.close()

            self.assertEqual(requeued, 2)
            self.assertEqual(statuses["high"], "retry")
            self.assertEqual(statuses["medium"], "retry")
            self.assertEqual(statuses["watch"], "ok")

    def test_requeue_enrichments_can_target_addresses(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(index):
                address = f"0x{index:040x}"
                return {
                    "address": address,
                    "tx_hash": f"direct:{index}",
                    "origin_tx_hash": f"0x{index:064x}",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": index,
                    "block_timestamp": index,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": f"Alpha {index}",
                    "symbol": f"A{index}",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "Alpha",
                    "source_len": 5000,
                    "confidence": 0.9,
                }

            first = contract(1)
            second = contract(2)
            for item in (first, second):
                store.upsert_contract(item)
                store.upsert_enrichment(
                    item["address"],
                    {"queries": [], "evidence": {}, "score": {"score": 80, "tier": "medium"}, "status": "ok"},
                )

            requeued = store.requeue_enrichments(addresses=[first["address"].upper()])
            statuses = {
                row["address"]: row["status"]
                for row in store.conn.execute("SELECT address, status FROM enrichments").fetchall()
            }
            store.close()

            self.assertEqual(requeued, 1)
            self.assertEqual(statuses[first["address"]], "retry")
            self.assertEqual(statuses[second["address"]], "ok")

    def test_source_url_backfill_candidates_skip_existing_url_state_and_websites(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(index, **overrides):
                item = {
                    "address": f"0x{index:040x}",
                    "tx_hash": f"direct:{index}",
                    "origin_tx_hash": f"0x{index:064x}",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": index,
                    "block_timestamp": index,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": f"Alpha {index}",
                    "symbol": f"ALP{index}",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "AlphaToken",
                    "source_len": 5000,
                    "confidence": 0.9,
                    "source": {"compiler": "v0.8.24"},
                }
                item.update(overrides)
                return item

            needs_backfill = contract(1, name="MoltenBear", symbol="MLTB", contract_name="MoltenBearToken")
            already_checked = contract(2, source={"compiler": "v0.8.24", "urls": []})
            has_website = contract(3)
            unverified = contract(4, verified=False, source_len=0)
            for item in (needs_backfill, already_checked, has_website, unverified):
                store.upsert_contract(item)
            store.upsert_enrichment(
                has_website["address"],
                {
                    "queries": [],
                    "evidence": {"website": "https://alpha.example"},
                    "score": {"score": 70, "tier": "medium"},
                    "status": "ok",
                },
            )
            store.upsert_enrichment(
                needs_backfill["address"],
                {"queries": [], "evidence": {}, "score": {"score": 50, "tier": "watch"}, "status": "ok"},
            )

            candidates = store.source_url_backfill_candidates(None)
            forced_candidates = store.source_url_backfill_candidates(None, force=True)
            requeued = store.requeue_enrichment_for_source_url(needs_backfill["address"])
            row = store.conn.execute(
                "SELECT status FROM enrichments WHERE address = ?",
                (needs_backfill["address"],),
            ).fetchone()
            store.close()

            self.assertEqual([item["address"] for item in candidates], [needs_backfill["address"]])
            self.assertEqual(
                [item["address"] for item in forced_candidates],
                [needs_backfill["address"], already_checked["address"]],
            )
            self.assertTrue(requeued)
            self.assertEqual(row["status"], "retry")

    def test_source_url_backfill_candidates_prioritize_alpha_and_signal_surface(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(index, **overrides):
                item = {
                    "address": f"0x{index:040x}",
                    "tx_hash": f"direct:{index}",
                    "origin_tx_hash": f"0x{index:064x}",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": index,
                    "block_timestamp": index,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": f"Alpha {index}",
                    "symbol": f"ALP{index}",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "AlphaToken",
                    "source_len": 5000,
                    "confidence": 0.9,
                    "source": {"compiler": "v0.8.24"},
                }
                item.update(overrides)
                return item

            low_new = contract(
                4,
                kind="contract",
                name=None,
                symbol=None,
                contract_name="Router",
            )
            watch_direct = contract(3, name="DirectAlpha", symbol="DALP")
            watch_dex = contract(2, name="DexAlpha", symbol="DXA", discovery_source="uniswap_v4_initialize")
            high_old = contract(1, name="HighAlpha", symbol="HALP", discovery_source="internal_create2")

            for item in (low_new, watch_direct, watch_dex, high_old):
                store.upsert_contract(item)

            for item, tier, score in (
                (low_new, "low", 20),
                (watch_direct, "watch", 55),
                (watch_dex, "watch", 56),
                (high_old, "high", 80),
            ):
                store.upsert_enrichment(
                    item["address"],
                    {"queries": [], "evidence": {}, "score": {"score": score, "tier": tier}, "status": "ok"},
                )

            candidates = store.source_url_backfill_candidates(None)
            store.close()

            self.assertEqual(
                [item["address"] for item in candidates],
                [high_old["address"], watch_dex["address"], watch_direct["address"], low_new["address"]],
            )
            self.assertEqual(candidates[1]["source_url_backfill_bucket"], "uniswap_v4")

    def test_rescore_enrichments_uses_stored_evidence(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            item = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "named_contract",
                "name": "Viktor",
                "symbol": "Dudka",
                "decimals": None,
                "total_supply": None,
                "verified": True,
                "contract_name": "ERC721SeaDrop",
                "source_len": 5000,
                "confidence": 0.9,
            }
            store.upsert_contract(item)
            store.upsert_enrichment(
                item["address"],
                {
                    "queries": ["Viktor"],
                    "evidence": {
                        "twitter_account": "0xViktordudka",
                        "twitter_followers": 98,
                        "twitter_verified": False,
                        "tweet_count": 13,
                        "address_mentions": 0,
                        "credible_address_mentions": 0,
                        "official_identity_reason": "profile_project_identity",
                    },
                    "score": {"score": 78, "tier": "high", "breakdown": {}},
                    "status": "ok",
                },
            )
            rescored = store.rescore_enrichments(["high"])
            row = store.conn.execute("SELECT tier, score FROM enrichments WHERE address = ?", (item["address"],)).fetchone()
            store.close()

            self.assertEqual(rescored, 1)
            self.assertEqual(row["tier"], "medium")
            self.assertGreaterEqual(row["score"], 70)

    def test_new_high_signal_observation_retries_completed_enrichment(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(source, tx_hash, log_index):
                return {
                    "address": "0x1111111111111111111111111111111111111111",
                    "tx_hash": tx_hash,
                    "origin_tx_hash": tx_hash,
                    "discovery_source": source,
                    "log_index": log_index,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": 1,
                    "block_timestamp": 1,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
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

            store.upsert_contract(contract("internal_create2", "0xaaa", None))
            store.upsert_enrichment(
                "0x1111111111111111111111111111111111111111",
                {"queries": [], "evidence": {}, "score": {"score": 5, "tier": "low"}, "status": "ok"},
            )
            store.upsert_contract(contract("mint_transfer", "0xbbb", 7))
            row = store.conn.execute(
                "SELECT status FROM enrichments WHERE address = ?",
                ("0x1111111111111111111111111111111111111111",),
            ).fetchone()
            summary = store.summary()
            store.close()
            self.assertEqual(row["status"], "retry")
            self.assertEqual(summary["pending_enrichment"], 1)
            self.assertEqual(summary["enriched"], 0)

    def test_repeated_same_high_signal_source_does_not_requeue_completed_enrichment(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(source, tx_hash, log_index):
                return {
                    "address": "0x1111111111111111111111111111111111111111",
                    "tx_hash": tx_hash,
                    "origin_tx_hash": tx_hash,
                    "discovery_source": source,
                    "log_index": log_index,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": 1,
                    "block_timestamp": 1,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
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

            address = "0x1111111111111111111111111111111111111111"
            store.upsert_contract(contract("internal_create2", "0xaaa", None))
            store.upsert_contract(contract("mint_transfer", "0xbbb", 7))
            store.upsert_enrichment(
                address,
                {"queries": [], "evidence": {}, "score": {"score": 40, "tier": "watch"}, "status": "ok"},
            )
            store.upsert_contract(contract("mint_transfer", "0xccc", 8))
            row = store.conn.execute("SELECT status FROM enrichments WHERE address = ?", (address,)).fetchone()
            observation_sources = store.conn.execute(
                "SELECT COUNT(DISTINCT discovery_source) AS n FROM contract_observations WHERE address = ?",
                (address,),
            ).fetchone()
            store.close()
            self.assertEqual(row["status"], "ok")
            self.assertEqual(observation_sources["n"], 2)

    def test_project_groups_merge_duplicate_project_identities(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(address, symbol):
                return {
                    "address": address,
                    "tx_hash": f"direct:{address}",
                    "origin_tx_hash": f"tx:{address}",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": 1,
                    "block_timestamp": 1,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": "Taxcoin",
                    "symbol": symbol,
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "Taxcoin",
                    "source_len": 5000,
                    "confidence": 0.9,
                }

            first = contract("0x1111111111111111111111111111111111111111", "TAX")
            second = contract("0x2222222222222222222222222222222222222222", "TAX")
            store.upsert_contract(first)
            store.upsert_contract(second)
            for address, score in ((first["address"], 83), (second["address"], 80)):
                store.upsert_enrichment(
                    address,
                    {
                        "queries": ["Taxcoin"],
                        "evidence": {
                            "twitter_account": "Taxcoin",
                            "twitter_name": "taxcoin",
                            "tweet_count": 3,
                            "address_mentions": 1,
                        },
                        "score": {"score": score, "tier": "high", "breakdown": {}},
                        "status": "ok",
                    },
                )
            groups = store.project_groups(10)
            store.close()
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["project_key"], "twitter:taxcoin")
            self.assertEqual(groups[0]["address_count"], 2)
            self.assertEqual(groups[0]["score"], 83)

    def test_build_internal_contract_base_fields(self):
        class FakeClient:
            def eth_call(self, *_args, **_kwargs):
                return None

            def get_source_code(self, _address):
                return None

        trace = {
            "blockNumber": "25107429",
            "timeStamp": "1778930867",
            "hash": "0xabc",
            "from": "0x008ebccae39d001200c3003c3225ce0a00690066",
            "contractAddress": "0x314679db68cc4005d09bdd835a0d43e4b9e22465",
            "type": "create2",
            "traceId": "0_1",
            "value": "0",
        }
        contract = build_internal_contract(FakeClient(), trace, 25107429)
        self.assertEqual(contract["address"], "0x314679db68cc4005d09bdd835a0d43e4b9e22465")
        self.assertEqual(contract["discovery_source"], "internal_create2")
        self.assertEqual(contract["origin_tx_hash"], "0xabc")
        self.assertEqual(contract["related"]["factory"], "0x008ebccae39d001200c3003c3225ce0a00690066")

    def test_low_signal_identifiers_are_not_meaningful(self):
        self.assertIsNone(meaningful_name("my collection"))
        self.assertIsNone(meaningful_symbol("s"))

    def test_generic_name_with_twitter_noise_scores_low(self):
        contract = {
            "kind": "named_contract",
            "name": "my collection",
            "symbol": "s",
            "verified": True,
            "contract_name": "ERC721Token",
            "source_len": 5000,
            "discovery_source": "internal_create2",
            "observation_sources": "internal_create2,mint_transfer",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": None,
            "twitter_followers": None,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 40,
            "address_mentions": 0,
            "discussion_only": True,
        }
        scored = score_project(contract, evidence)
        self.assertEqual(scored["tier"], "low")

    def test_unresolved_offchain_candidate_is_capped_at_watch(self):
        contract = {
            "kind": "named_contract",
            "name": "Cryptade Goblinz",
            "symbol": "Crgo",
            "verified": True,
            "contract_name": "ERC721Token",
            "source_len": 5000,
            "discovery_source": "internal_create2",
            "observation_sources": "internal_create2,mint_transfer",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": None,
            "website": None,
            "tweet_count": 0,
            "address_mentions": 0,
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 50)
        self.assertEqual(scored["tier"], "watch")

    def test_failed_website_check_does_not_support_medium(self):
        contract = {
            "kind": "erc20",
            "name": "uniRock",
            "symbol": "uROCK",
            "verified": True,
            "contract_name": "UniRock",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,mint_transfer,uniswap_v4_initialize",
            "observation_count": 3,
        }
        evidence = {
            "twitter_account": None,
            "website": "https://unirock.art",
            "official_website_reason": "verified_source_url",
            "tweet_count": 1,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "discussion_only": True,
            "website_check_status": "fail",
            "website_check_failure_kind": "http_error",
        }

        scored = score_project(contract, evidence)

        self.assertLess(scored["score"], 50)
        self.assertEqual(scored["tier"], "watch")

    def test_source_website_with_only_unresolved_discussion_stays_watch(self):
        contract = {
            "kind": "named_contract",
            "name": "Needle",
            "symbol": None,
            "verified": True,
            "contract_name": "NeedleDrop",
            "source_len": 5000,
            "discovery_source": "uniswap_v4_initialize",
            "observation_sources": "contract_creation,mint_transfer,uniswap_v4_initialize",
            "observation_count": 3,
        }
        evidence = {
            "twitter_account": None,
            "website": "https://needle.example",
            "official_website_reason": "verified_source_url",
            "tweet_count": 40,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "discussion_only": True,
        }

        scored = score_project(contract, evidence)

        self.assertGreaterEqual(scored["score"], 50)
        self.assertEqual(scored["tier"], "watch")

    def test_generic_token_factory_source_website_stays_watch(self):
        contract = {
            "kind": "erc20",
            "name": "Masturbation",
            "symbol": "MAST",
            "verified": True,
            "contract_name": "Token",
            "source_len": 5000,
            "discovery_source": "uniswap_v4_initialize",
            "observation_sources": "contract_creation,uniswap_v4_initialize",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": None,
            "website": "https://masturbation.eth-token.com",
            "official_website_reason": "verified_source_url",
            "tweet_count": 0,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "discussion_only": False,
        }

        scored = score_project(contract, evidence)

        self.assertGreaterEqual(scored["score"], 50)
        self.assertEqual(scored["tier"], "watch")
        self.assertLess(scored["breakdown"]["risk_penalty"], 0)

    def test_source_website_rejects_generic_token_factory_subdomain(self):
        contract = {
            "name": "Masturbation",
            "symbol": "MAST",
            "contract_name": "Token",
        }

        self.assertFalse(source_website_matches_contract("https://masturbation.eth-token.com", contract))
        self.assertTrue(source_website_matches_contract("https://masturbation.xyz", contract))

    def test_source_website_without_unresolved_discussion_can_support_medium(self):
        contract = {
            "kind": "named_contract",
            "name": "Needle",
            "symbol": None,
            "verified": True,
            "contract_name": "NeedleDrop",
            "source_len": 5000,
            "discovery_source": "uniswap_v4_initialize",
            "observation_sources": "contract_creation,mint_transfer,uniswap_v4_initialize",
            "observation_count": 3,
        }
        evidence = {
            "twitter_account": None,
            "website": "https://needle.example",
            "official_website_reason": "verified_source_url",
            "tweet_count": 0,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "discussion_only": False,
        }

        scored = score_project(contract, evidence)

        self.assertGreaterEqual(scored["score"], 50)
        self.assertEqual(scored["tier"], "medium")

    def test_address_mentions_without_official_identity_stay_watch(self):
        contract = {
            "kind": "erc20",
            "name": "Bagels",
            "symbol": "BAGELS",
            "verified": True,
            "contract_name": "Bagels",
            "source_len": 5000,
            "discovery_source": "uniswap_v4_initialize",
            "observation_sources": "contract_creation,uniswap_v4_initialize",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": None,
            "website": None,
            "tweet_count": 20,
            "address_mentions": 4,
            "credible_address_mentions": 4,
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "watch")

    def test_weak_twitter_identity_without_project_support_stays_watch(self):
        contract = {
            "kind": "named_contract",
            "name": "Viktor",
            "symbol": "Dudka",
            "verified": True,
            "contract_name": "ERC721SeaDrop",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation",
            "observation_count": 1,
        }
        evidence = {
            "twitter_account": "0xViktordudka",
            "twitter_followers": 98,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 13,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "account_crypto_project_context",
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "watch")

    def test_official_identity_with_dex_signal_can_be_high(self):
        contract = {
            "kind": "erc20",
            "name": "Erebus",
            "symbol": "EREBUS",
            "verified": True,
            "contract_name": "BasicERC20",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,uniswap_v2_pair_created",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": "ErebusNFT",
            "twitter_followers": 100,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 10,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "account_crypto_project_context",
        }
        scored = score_project(contract, evidence)
        self.assertEqual(scored["tier"], "high")

    def test_thin_account_crypto_context_does_not_become_medium_from_onchain_only(self):
        contract = {
            "kind": "erc20",
            "name": "ChainMaxi",
            "symbol": "CMX",
            "verified": True,
            "contract_name": "LaunchToken",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,mint_transfer,uniswap_v2_pair_created",
            "observation_count": 3,
        }
        evidence = {
            "twitter_account": "chainmaxii",
            "twitter_followers": 99,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 10,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "account_crypto_project_context",
            "official_account_address_mentions": 0,
            "official_account_ethereum_project_mentions": 0,
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "watch")

    def test_account_crypto_context_with_ethereum_context_can_be_medium_not_high(self):
        contract = {
            "kind": "erc20",
            "name": "TRUMP007",
            "symbol": "TRUMP007",
            "verified": True,
            "contract_name": "LaunchToken",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,mint_transfer,uniswap_v2_pair_created",
            "observation_count": 3,
        }
        evidence = {
            "twitter_account": "TRUMP007Eth",
            "twitter_followers": 10,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 10,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "account_crypto_project_context",
            "official_account_address_mentions": 0,
            "official_account_ethereum_project_mentions": 1,
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "medium")

    def test_twitter_only_foreign_chain_context_stays_watch(self):
        contract = {
            "kind": "erc20",
            "name": "memelon",
            "symbol": "memelon",
            "verified": False,
            "contract_name": "",
            "source_len": 0,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,mint_transfer,uniswap_v2_pair_created",
            "observation_count": 3,
        }
        evidence = {
            "twitter_account": "MemelonTusk",
            "twitter_followers": 2280,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 37,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "account_crypto_project_context",
            "sample_tweets": [
                {
                    "text": "$memelon vote us onto Moonshot H5jJp6y4tBpTX4UkYRvReX74fYocP7z5uHkmQyrEpump",
                    "userScreenName": "caller1",
                },
                {
                    "text": "#SolanaTokens $memelon is trending on pump.fun",
                    "userScreenName": "caller2",
                },
            ],
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 50)
        self.assertEqual(scored["tier"], "watch")
        self.assertLessEqual(scored["breakdown"]["risk_penalty"], -16)

    def test_weak_profile_crypto_identity_needs_stronger_support_for_medium(self):
        contract = {
            "kind": "erc20",
            "name": "GeckoParty",
            "symbol": "GCKO",
            "verified": True,
            "contract_name": "LaunchToken",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,mint_transfer",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": "GeckoParty_",
            "twitter_followers": 28,
            "twitter_verified": True,
            "website": None,
            "tweet_count": 18,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "profile_crypto_identity",
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "watch")

    def test_profile_crypto_identity_with_strong_support_and_followers_can_rank(self):
        contract = {
            "kind": "erc20",
            "name": "LuckyNFT",
            "symbol": "LNFT",
            "verified": True,
            "contract_name": "LuckyNFT",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,mint_transfer",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": "LuckyNFT_VIP",
            "twitter_followers": 1742,
            "twitter_verified": False,
            "website": None,
            "tweet_count": 12,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "profile_crypto_identity",
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "high")

    def test_third_party_profile_project_identity_alone_is_capped_below_high(self):
        contract = {
            "kind": "erc20",
            "name": "Pepe",
            "symbol": "PEPE",
            "verified": True,
            "contract_name": "LaunchTokenV4",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation",
            "observation_count": 1,
        }
        evidence = {
            "twitter_account": "PepeExplorers",
            "twitter_followers": 9000,
            "twitter_verified": True,
            "website": None,
            "tweet_count": 40,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "profile_project_identity",
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "medium")

    def test_third_party_profile_identity_with_dex_signal_is_capped_at_medium(self):
        contract = {
            "kind": "erc20",
            "name": "Block Street",
            "symbol": "BSB",
            "verified": True,
            "contract_name": "LaunchTokenV4",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,uniswap_v2_pair_created",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": "BlockSt_HQ",
            "twitter_followers": 62072,
            "twitter_verified": True,
            "website": None,
            "tweet_count": 40,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "profile_project_identity",
            "official_account_address_mentions": 0,
            "official_account_project_mentions": 2,
            "official_account_own_project_mentions": 0,
            "official_account_own_crypto_project_mentions": 0,
            "official_account_flags": ["mentioned_by_project_context"],
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["tier"], "medium")
        self.assertLess(scored["breakdown"]["risk_penalty"], 0)

    def test_third_party_profile_identity_without_strong_onchain_stays_watch(self):
        contract = {
            "kind": "named_contract",
            "name": "LuckyNFT",
            "symbol": "LNFT",
            "verified": False,
            "contract_name": "LuckyNFT",
            "source_len": 0,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation",
            "observation_count": 1,
        }
        evidence = {
            "twitter_account": "LuckyNFT_VIP",
            "twitter_followers": 1742,
            "twitter_verified": True,
            "website": None,
            "tweet_count": 12,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "profile_project_identity",
            "official_account_address_mentions": 0,
            "official_account_project_mentions": 7,
            "official_account_own_project_mentions": 0,
            "official_account_own_crypto_project_mentions": 0,
            "official_account_flags": ["mentioned_by_project_context"],
        }
        scored = score_project(contract, evidence)
        self.assertGreaterEqual(scored["score"], 50)
        self.assertEqual(scored["tier"], "watch")

    def test_profile_identity_with_own_project_context_can_rank_high(self):
        contract = {
            "kind": "erc20",
            "name": "Block Street",
            "symbol": "BSB",
            "verified": True,
            "contract_name": "LaunchTokenV4",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,uniswap_v2_pair_created",
            "observation_count": 2,
        }
        evidence = {
            "twitter_account": "BlockSt_HQ",
            "twitter_followers": 62072,
            "twitter_verified": True,
            "website": None,
            "tweet_count": 4,
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_identity_reason": "profile_project_identity",
            "official_account_address_mentions": 0,
            "official_account_project_mentions": 2,
            "official_account_own_project_mentions": 1,
            "official_account_own_crypto_project_mentions": 1,
            "official_account_flags": [],
        }
        scored = score_project(contract, evidence)
        self.assertEqual(scored["tier"], "high")

    def test_signal_address_mentions_without_official_identity_stay_watch(self):
        contract = {
            "kind": "contract",
            "name": "Fiona",
            "symbol": None,
            "verified": True,
            "contract_name": "Fiona",
            "source_len": 5000,
            "discovery_source": "contract_creation",
            "observation_sources": "contract_creation,uniswap_v4_initialize",
            "observation_count": 3,
        }
        evidence = {
            "twitter_account": None,
            "website": None,
            "tweet_count": 40,
            "address_mentions": 4,
            "credible_address_mentions": 0,
            "signal_address_mentions": 4,
            "discussion_only": True,
        }
        scored = score_project(contract, evidence)
        self.assertEqual(scored["tier"], "watch")

    def test_social_aggregator_profile_detection(self):
        self.assertTrue(social_aggregator_profile("BscKOLScanner", "BSC KOL Scanner", "DEX signals and token alerts"))
        self.assertTrue(social_aggregator_profile("ETHAlphaRadar", "ETH 链上掘金雷达", "new mint monitor"))
        self.assertFalse(social_aggregator_profile("Bagels", "Bagels", "Official Bagels token"))

    def test_build_evidence_does_not_choose_scanner_as_official(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "BSC KOL Scanner",
                        "description": "DEX signals and token alerts",
                        "followersCount": 100000,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "Bagels",
            "symbol": "BAGELS",
        }
        tweets = [
            {
                "id": "1",
                "text": "New $BAGELS pool 0x1111111111111111111111111111111111111111",
                "userScreenName": "BscKOLScanner",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertEqual(evidence["address_mentions"], 1)
        self.assertEqual(evidence["credible_address_mentions"], 0)
        self.assertEqual(evidence["signal_address_mentions"], 1)
        self.assertIn("aggregator_or_signal_account", evidence["top_authors"][0]["flags"])

    def test_build_evidence_rejects_username_match_without_project_context(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "moltenbear",
                        "description": "personal account",
                        "followersCount": 63,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "MoltenBear",
            "symbol": "MLT",
        }
        tweets = [
            {
                "id": "1",
                "text": "I need help with a game account issue",
                "userScreenName": "moltenbear",
                "userName": "moltenbear",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertEqual(evidence["top_authors"][0]["profile_match_bonus"], 8)
        self.assertEqual(evidence["top_authors"][0]["project_mentions"], 0)

    def test_official_non_crypto_brand_profile_is_not_project_identity(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "Ledo Pizza",
                        "description": "Official Ledo Pizza account",
                        "followersCount": 51000,
                        "verified": True,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "2026 Pizza Day",
            "symbol": "PIZZA",
        }
        tweets = [
            {
                "id": "1",
                "text": "Happy 2026 Pizza Day giveaway. Win a Ledo Pizza gift card.",
                "userScreenName": "LedoPizza",
                "userName": "Ledo Pizza",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertEqual(evidence["top_authors"][0]["profile_match_bonus"], 8)
        self.assertEqual(evidence["top_authors"][0]["own_crypto_project_mentions"], 0)

    def test_mentioned_eth_profile_without_project_identity_is_not_official(self):
        class FakeTwitter:
            def user_info(self, username):
                profiles = {
                    "OisinKyne": {
                        "screenName": "OisinKyne",
                        "name": "oisin.eth | Obol",
                        "description": "Chief protocol officer at Obol",
                        "followersCount": 1200,
                    },
                    "caller": {
                        "screenName": "caller",
                        "name": "Caller",
                        "description": "market notes",
                        "followersCount": 100,
                    },
                }
                return {"data": profiles.get(username, profiles["caller"])}

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "Obol",
            "symbol": "OBOL",
        }
        tweets = [
            {
                "id": "1",
                "text": "@OisinKyne $OBOL launched on ETH",
                "userScreenName": "caller",
                "userName": "Caller",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertIn("mentioned_by_project_context", evidence["top_authors"][0]["flags"])

    def test_build_evidence_accepts_profile_match_with_project_context(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "MoltenBear",
                        "description": "MoltenBear token updates",
                        "followersCount": 1000,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "MoltenBear",
            "symbol": "MLT",
        }
        tweets = [
            {
                "id": "1",
                "text": "MoltenBear $MLT community update",
                "userScreenName": "moltenbear",
                "userName": "MoltenBear",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["twitter_account"], "moltenbear")
        self.assertGreater(evidence["top_authors"][0]["project_mentions"], 0)
        self.assertGreater(evidence["official_account_own_project_mentions"], 0)

    def test_build_evidence_marks_foreign_chain_project_context(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "Memelon Tusk",
                        "description": "Official Memelon token updates",
                        "followersCount": 2280,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "memelon",
            "symbol": "memelon",
        }
        tweets = [
            {
                "id": "1",
                "text": "$memelon vote us onto Moonshot H5jJp6y4tBpTX4UkYRvReX74fYocP7z5uHkmQyrEpump #SolanaTokens",
                "userScreenName": "MemelonTusk",
                "userName": "Memelon Tusk",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertEqual(evidence["foreign_chain_project_mentions"], 1)
        self.assertIn("foreign_chain_project_context", evidence["top_authors"][0]["flags"])

    def test_build_evidence_can_resolve_mentioned_official_account(self):
        class FakeTwitter:
            def user_info(self, username):
                profiles = {
                    "Bagelserc": {
                        "screenName": "Bagelserc",
                        "name": "Bagels",
                        "description": "Official Bagels token on ETH",
                        "followersCount": 1200,
                    },
                    "caller": {
                        "screenName": "caller",
                        "name": "Caller",
                        "description": "market notes",
                        "followersCount": 100,
                    },
                }
                return {"data": profiles.get(username, profiles["caller"])}

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "Bagels",
            "symbol": "BAGELS",
        }
        tweets = [
            {
                "id": "1",
                "text": "@Bagelserc clean launch for $BAGELS on ETH",
                "userScreenName": "caller",
                "userName": "Caller",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["twitter_account"], "Bagelserc")
        self.assertEqual(evidence["official_identity_reason"], "profile_project_identity")
        self.assertEqual(evidence["official_account_own_project_mentions"], 0)
        self.assertIn("mentioned_by_project_context", evidence["official_account_flags"])
        self.assertIn("mentioned_by_project_context", evidence["top_authors"][0]["flags"])

    def test_mentions_without_crypto_context_are_not_official(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "Dangs",
                        "description": "personal account",
                        "followersCount": 300,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "DANGS",
            "symbol": "DANGS",
        }
        tweets = [
            {
                "id": "1",
                "text": "@Dangs_Fur You like the Dangs too?",
                "userScreenName": "friend",
                "userName": "Friend",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertNotIn("mentioned_by_project_context", evidence["top_authors"][0]["flags"])

    def test_personal_profile_match_with_crypto_mention_is_not_official_without_identity_cue(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "kingvamp",
                        "description": "personal account",
                        "followersCount": 400,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "Vamp",
            "symbol": "VAMP",
        }
        tweets = [
            {
                "id": "1",
                "text": "@penguinman32 $VAMP launched on ETH",
                "userScreenName": "caller",
                "userName": "Caller",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertGreaterEqual(evidence["top_authors"][0]["profile_match_bonus"], 8)

    def test_mentions_without_profile_match_are_not_official(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "Elon Musk",
                        "description": "Mars",
                        "followersCount": 100000000,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "Official Tesla dog",
            "symbol": "SPARKY",
        }
        tweets = [
            {
                "id": "1",
                "text": "@elonmusk $SPARKY is the Official Tesla dog",
                "userScreenName": "caller",
                "userName": "Caller",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])

    def test_profile_match_uses_delimited_name_alias(self):
        bonus = profile_match_bonus(
            {
                "screenName": "BagelsOnETH",
                "name": "Bagels",
                "description": "Bagels the token",
            },
            {"name": "Bagels - Tesla's Mascot", "symbol": "BAGELS"},
        )
        self.assertGreaterEqual(bonus, 8)
        self.assertEqual(project_name_aliases("bagels - tesla's mascot"), ["bagels - tesla's mascot", "bagels"])

    def test_miner_hash_token_alias_and_queries(self):
        self.assertIn("ethgpu", project_name_aliases("ethgpu hash token"))
        queries = build_queries(
            {
                "address": "0x1111111111111111111111111111111111111111",
                "name": "ETHGPU Hash Token",
                "symbol": "HASH",
            }
        )
        self.assertIn("ETHGPU Hash Token HASH mining", queries)
        self.assertIn("$HASH miner", queries)
        self.assertLessEqual(len(queries), 6)

    def test_build_evidence_classifies_miner_coin_and_summarizes_positioning(self):
        class FakeTwitter:
            def user_info(self, username):
                profiles = {
                    "ethgpu": {
                        "screenName": "ethgpu",
                        "name": "ETHGPU",
                        "description": "Ethereum GPU mining and hash token protocol",
                        "followersCount": 5000,
                    },
                    "caller": {
                        "screenName": "caller",
                        "name": "Caller",
                        "description": "market notes",
                        "followersCount": 100,
                    },
                }
                return {"data": profiles.get(username, profiles["caller"])}

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "kind": "erc20",
            "name": "ETHGPU Hash Token",
            "symbol": "HASH",
        }
        tweets = [
            {
                "id": "1",
                "text": "ASSET TO WATCH: $HASH on Ethereum via @ethgpu. GPU miners earn hash token rewards.",
                "userScreenName": "caller",
                "userName": "Caller",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["twitter_account"], "ethgpu")
        self.assertEqual(evidence["project_category"], "miner_coin")
        self.assertEqual(evidence["project_positioning"], "Mineable or mining-themed EVM token")
        self.assertRegex(evidence["project_description"].lower(), r"min(er|ing)|hash")
        self.assertTrue(evidence["miner_signal"])

    def test_build_evidence_keeps_nft_hash_project_out_of_miner_category(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "HashSkulls",
                        "description": "NFT collection on Ethereum",
                        "followersCount": 1000,
                    }
                }

        contract = {
            "address": "0x2222222222222222222222222222222222222222",
            "kind": "erc721",
            "name": "HashSkulls",
            "symbol": "SKULL",
        }
        tweets = [
            {
                "id": "1",
                "text": "Get ready to mint for FREE #hashskulls #NFTGiveaway",
                "userScreenName": "HashSkulls",
                "userName": "HashSkulls",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["project_category"], "nft_collection")
        self.assertFalse(evidence["miner_signal"])

    def test_official_project_text_prevents_third_party_mining_false_positive(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "PYRE",
                        "description": "On-chain guessing game with Chainlink VRF and an ETH pot",
                        "followersCount": 1000,
                    }
                }

        contract = {
            "address": "0x3333333333333333333333333333333333333333",
            "kind": "erc20",
            "name": "PYRE",
            "symbol": "$v4PYRE",
        }
        tweets = [
            {
                "id": "1",
                "text": "PYRE is live on Ethereum. Closest guess wins the pot. Chainlink VRF, Uniswap V4 hook.",
                "userScreenName": "pyreoneth",
                "userName": "PYRE",
            },
            {
                "id": "2",
                "text": "Trending Now: Pyre Mining PYRE.",
                "userScreenName": "chartcryptoio",
                "userName": "ChartCrypto",
            },
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["twitter_account"], "pyreoneth")
        self.assertEqual(evidence["project_category"], "game_or_lottery")
        self.assertFalse(evidence["miner_signal"])

    def test_extract_mentioned_handles_dedupes_handles(self):
        self.assertEqual(extract_mentioned_handles("@Bagelserc @bagelserc hi @elon_musk"), ["Bagelserc", "elon_musk"])

    def test_nested_profile_url_becomes_official_website(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "MoltenBear",
                        "description": "MoltenBear token updates",
                        "followersCount": 1000,
                        "entities": {
                            "url": {
                                "urls": [
                                    {
                                        "url": "https://t.co/example",
                                        "expanded_url": "https://moltenbear.xyz/",
                                    }
                                ]
                            }
                        },
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "MoltenBear",
            "symbol": "MLT",
        }
        tweets = [
            {
                "id": "1",
                "text": "MoltenBear $MLT chart https://dexscreener.com/ethereum/0xabc",
                "userScreenName": "moltenbear",
                "userName": "MoltenBear",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["twitter_account"], "moltenbear")
        self.assertEqual(evidence["website"], "https://moltenbear.xyz")
        self.assertEqual(evidence["official_website_reason"], "official_profile_url")

    def test_official_project_tweet_url_becomes_website(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "MoltenBear",
                        "description": "MoltenBear official Ethereum token",
                        "followersCount": 1000,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "MoltenBear",
            "symbol": "MLT",
        }
        tweets = [
            {
                "id": "1",
                "text": "MoltenBear $MLT launched on Ethereum. Mint at https://moltenbear.xyz/mint",
                "userScreenName": "moltenbear",
                "userName": "MoltenBear",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["twitter_account"], "moltenbear")
        self.assertEqual(evidence["website"], "https://moltenbear.xyz/mint")
        self.assertEqual(evidence["official_website_reason"], "official_project_tweet_url")

    def test_official_project_tweet_url_requires_project_identity_match(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "memelon",
                        "description": "memelon official Ethereum token",
                        "followersCount": 2000,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "memelon",
            "symbol": "memelon",
        }
        tweets = [
            {
                "id": "1",
                "text": "memelon is live on Ethereum. Mint at https://globalkey-nft.com",
                "userScreenName": "MemelonTusk",
                "userName": "memelon",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertEqual(evidence["twitter_account"], "MemelonTusk")
        self.assertIsNone(evidence["website"])
        self.assertIsNone(evidence["official_website_reason"])

    def test_discussion_urls_do_not_become_official_website_without_official_account(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "Unrelated Trader",
                        "description": "market notes",
                        "followersCount": 1000,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "MoltenBear",
            "symbol": "MLT",
        }
        tweets = [
            {
                "id": "1",
                "text": "MoltenBear $MLT mentioned here https://example.org/project",
                "userScreenName": "trader",
                "userName": "Unrelated Trader",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertIsNone(evidence["website"])

    def test_verified_source_url_becomes_website_when_it_matches_project(self):
        class FakeTwitter:
            def user_info(self, username):
                raise AssertionError("no profile lookup expected")

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "MoltenBear",
            "symbol": "MLT",
            "source": {"urls": ["https://moltenbear.xyz/", "https://randomdocs.example"]},
        }
        evidence = build_evidence(FakeTwitter(), contract, [])
        self.assertIsNone(evidence["twitter_account"])
        self.assertEqual(evidence["website"], "https://moltenbear.xyz")
        self.assertEqual(evidence["official_website_reason"], "verified_source_url")
        self.assertEqual(evidence["source_website_candidates"][0], "https://moltenbear.xyz")

    def test_verified_source_url_without_project_match_is_not_website(self):
        class FakeTwitter:
            def user_info(self, username):
                raise AssertionError("no profile lookup expected")

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "MoltenBear",
            "symbol": "MLT",
            "source": {"urls": ["https://randomdocs.example/launch"]},
        }
        evidence = build_evidence(FakeTwitter(), contract, [])
        self.assertIsNone(evidence["twitter_account"])
        self.assertIsNone(evidence["website"])
        self.assertEqual(evidence["source_website_candidates"], ["https://randomdocs.example/launch"])

    def test_choose_website_filters_market_and_social_links(self):
        profile = {
            "profileImageUrl": "https://pbs.twimg.com/profile_images/example.jpg",
            "entities": {
                "url": {
                    "urls": [
                        {"expanded_url": "https://dexscreener.com/ethereum/0xabc"},
                        {"expanded_url": "https://x.com/example"},
                        {"expanded_url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/indexOf"},
                        {"expanded_url": "http://www.w3.org/2000/svg"},
                        {"expanded_url": "https://graphics.stanford.edu/~seander/bithacks.html#ReverseParallel"},
                        {"expanded_url": "https://example.org/path/"},
                    ]
                }
            }
        }
        urls = extract_urls_from_value(profile)
        self.assertIn("https://example.org/path/", urls)
        self.assertNotIn("https://pbs.twimg.com/profile_images/example.jpg", urls)
        self.assertEqual(choose_website(urls), "https://example.org/path")

    def test_short_urls_can_be_expanded_before_website_choice(self):
        with patch("alpha_listener.opentwitter.resolve_redirect_url", return_value="https://alpha.example/"):
            urls = expand_short_urls(["https://t.co/abc"])
        self.assertEqual(choose_website(urls), "https://alpha.example")

    def test_build_evidence_rejects_address_mention_without_profile_match(self):
        class FakeTwitter:
            def user_info(self, username):
                return {
                    "data": {
                        "screenName": username,
                        "name": "AstroChill",
                        "description": "trading notes",
                        "followersCount": 170,
                    }
                }

        contract = {
            "address": "0x1111111111111111111111111111111111111111",
            "name": "Official Tesla dog",
            "symbol": "SPARKY",
        }
        tweets = [
            {
                "id": "1",
                "text": "$SPARKY will pump hard soon 0x1111111111111111111111111111111111111111",
                "userScreenName": "cristo0081",
                "userName": "AstroChill",
            }
        ]
        evidence = build_evidence(FakeTwitter(), contract, tweets)
        self.assertIsNone(evidence["twitter_account"])
        self.assertEqual(evidence["credible_address_mentions"], 1)

    def test_profile_description_match_alone_is_not_official_identity(self):
        bonus = profile_match_bonus(
            {
                "screenName": "squirrelbbg",
                "name": "SquirrelBBG",
                "description": "staking tETH and watching tAssets",
            },
            {"name": "TETH", "symbol": "TETH"},
        )
        self.assertEqual(bonus, 0)

    def test_profile_project_identity_cues_use_word_boundaries(self):
        self.assertFalse(
            profile_has_strong_project_identity(
                {
                    "screenName": "TokenWorks",
                    "name": "TokenWorks Inc.",
                    "description": "Age Verification, Fake ID Detection, and Form Filling.",
                }
            )
        )
        self.assertFalse(
            profile_has_strong_project_identity(
                {
                    "screenName": "marvin_tong",
                    "name": "Marvin Tong (t/acc)",
                    "description": "Trust Machines @PhalaNetwork / Privacy-Preserving LLM / Investment @pakafund",
                }
            )
        )
        self.assertTrue(
            profile_has_strong_project_identity(
                {
                    "screenName": "BlockSt_HQ",
                    "name": "Block Street",
                    "description": "Unified Liquidity Layer for Tokenized Assets.",
                }
            )
        )

    def test_health_check_reports_lag(self):
        from alpha_listener.cli import health_check
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            store.set_meta("last_scanned_block", 90)
            store.close()
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            with patch("alpha_listener.cli.EtherscanClient") as client_cls:
                client_cls.return_value.latest_block_number.return_value = 110
                result = health_check(settings, SimpleNamespace(confirmations=None))
            self.assertEqual(result["safe_latest_block"], 104)
            self.assertEqual(result["lag_blocks"], 14)
            self.assertEqual(result["status"], "catching_up")
            self.assertEqual(result["coverage"]["status"], "gap")

    def test_run_cycle_persists_runtime_status(self):
        from alpha_listener.cli import run_cycle
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            args = SimpleNamespace(
                command="once",
                no_twitter=True,
                confirmations=None,
                lookback_blocks=None,
                max_blocks=0,
                enrich_limit=0,
                force_enrich=False,
                report_limit=None,
                report_every_enriched=None,
                start_block=None,
                end_block=None,
            )

            with patch("alpha_listener.cli.EtherscanClient") as client_cls:
                client_cls.return_value.latest_block_number.return_value = 110
                result = run_cycle(settings, args)

            store = Store(root / "alpha.sqlite", root)
            runtime = store.runtime_status()
            events = [json.loads(line)["event_type"] for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            store.close()

            self.assertEqual(result["safe_latest_block"], 104)
            self.assertEqual(runtime["last_cycle_status"], "ok")
            self.assertEqual(runtime["last_cycle_context"]["safe_latest_block"], 104)
            self.assertEqual(runtime["last_cycle_result"]["summary"]["pending_enrichment"], 0)
            self.assertIn("cycle_started", events)
            self.assertIn("cycle_finished", events)

    def test_run_cycle_persists_failure_status(self):
        from alpha_listener.cli import run_cycle
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            args = SimpleNamespace(
                command="once",
                no_twitter=True,
                confirmations=None,
                lookback_blocks=None,
                max_blocks=0,
                enrich_limit=0,
                force_enrich=False,
                report_limit=None,
                report_every_enriched=None,
                start_block=None,
                end_block=None,
            )

            with patch("alpha_listener.cli.EtherscanClient") as client_cls:
                client_cls.return_value.latest_block_number.side_effect = RuntimeError("head unavailable")
                with self.assertRaises(RuntimeError):
                    run_cycle(settings, args)

            store = Store(root / "alpha.sqlite", root)
            runtime = store.runtime_status()
            events = [json.loads(line)["event_type"] for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            store.close()

            self.assertEqual(runtime["last_cycle_status"], "failed")
            self.assertEqual(runtime["last_cycle_result"]["error_type"], "RuntimeError")
            self.assertIn("head unavailable", runtime["last_cycle_result"]["error"])
            self.assertIn("cycle_failed", events)

    def test_scan_coverage_repairs_recent_gap(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            for block_number in (96, 97, 99, 100):
                store.mark_block(block_number, f"0x{block_number:x}", block_number, 1)
            store.set_meta("last_scanned_block", 100)

            coverage = store.scan_coverage(5)
            repaired = store.repair_scan_coverage(5)
            new_coverage = store.scan_coverage(5)
            store.close()

            self.assertEqual(coverage["status"], "gap")
            self.assertEqual(coverage["first_missing_block"], 98)
            self.assertEqual(coverage["missing_ranges"], [{"start": 98, "end": 98, "count": 1}])
            self.assertTrue(repaired["repaired"])
            self.assertEqual(repaired["new_last_scanned_block"], 97)
            self.assertEqual(new_coverage["status"], "ok")
            self.assertEqual(new_coverage["leading_untracked_blocks"], 3)
            self.assertTrue((root / "events.jsonl").exists())

    def test_audit_summarizes_end_to_end_readiness(self):
        from alpha_listener.cli import handle_audit, runtime_code_fingerprint, write_report
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            for block_number in (10, 11, 12):
                store.mark_block(block_number, f"0x{block_number:x}", block_number, 1)
            store.set_meta("last_scanned_block", 12)

            def contract(address, source, block, name=None, symbol=None):
                return {
                    "address": address,
                    "tx_hash": f"{source}:{address}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": source,
                    "log_index": block,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20" if symbol else "contract",
                    "name": name,
                    "symbol": symbol,
                    "decimals": 18 if symbol else None,
                    "total_supply": "1000000000000000000" if symbol else None,
                    "verified": bool(symbol),
                    "contract_name": f"{name}Token" if name else "",
                    "source_len": 1000 if symbol else 0,
                    "confidence": 0.9 if symbol else 0.05,
                }

            alpha = contract("0x1111111111111111111111111111111111111111", "contract_creation", 10, "Alpha", "ALPHA")
            internal = contract("0x2222222222222222222222222222222222222222", "internal_create2", 11)
            mint = contract("0x3333333333333333333333333333333333333333", "mint_transfer", 12)
            v4 = contract("0x4444444444444444444444444444444444444444", "uniswap_v4_initialize", 12)
            for item in (alpha, internal, mint, v4):
                store.upsert_contract(item)
            store.upsert_enrichment(
                alpha["address"],
                {
                    "queries": [{"query": "Alpha ALPHA"}],
                    "evidence": {
                        "twitter_account": "AlphaProtocol",
                        "twitter_followers": 2000,
                        "website": "https://alpha.example",
                        "tweet_count": 3,
                        "address_mentions": 1,
                        "official_identity_reason": "profile_project_identity",
                        "official_website_reason": "official_profile_url",
                    },
                    "score": {"score": 88, "tier": "high", "breakdown": {"official_identity": 20}},
                    "status": "ok",
                },
            )
            group = store.project_groups(10)[0]
            store.add_website_check(
                group["project_key"],
                "https://alpha.example",
                {
                    "status": "ok",
                    "http_status": 200,
                    "final_url": "https://alpha.example",
                    "title": "Alpha Protocol",
                    "description": "",
                    "twitter_links": ["https://x.com/AlphaProtocol"],
                    "matched_terms": ["Alpha"],
                    "error": "",
                },
            )
            store.add_project_review(group["project_key"], "confirmed", "tester", "fixture", group)
            stale = contract("0x5555555555555555555555555555555555555555", "internal_create2", 12)
            store.upsert_contract(stale)
            store.upsert_enrichment(
                stale["address"],
                {
                    "queries": [],
                    "evidence": {},
                    "score": {},
                    "status": "processing",
                },
            )
            store.conn.execute(
                "UPDATE enrichments SET searched_at = datetime('now', '-31 minutes') WHERE address = ?",
                (stale["address"],),
            )
            store.conn.commit()
            store.record_cycle_started(
                {
                    "command": "once",
                    "start_block": 10,
                    "end_block": 12,
                    "code_fingerprint": runtime_code_fingerprint(root),
                }
            )
            store.record_cycle_finished("ok", {"summary": store.summary(), "scanned_blocks": 3})
            store.record_cycle_started(
                {
                    "command": "classify-backlog",
                    "limit": 25,
                    "code_fingerprint": runtime_code_fingerprint(root),
                },
                role="classifier",
            )
            store.record_cycle_progress({"event": "classification_reserved", "reserved": 1}, role="classifier")
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            write_report(settings, 10)

            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["checks"]["coverage"]["status"], "ok")
            self.assertEqual(result["checks"]["queue"]["status"], "ok")
            self.assertEqual(result["checks"]["queue"]["detail"]["stale_processing"], 1)
            self.assertTrue(result["checks"]["queue"]["detail"]["stale_processing_recoverable"])
            self.assertEqual(result["checks"]["official_evidence"]["status"], "ok")
            self.assertEqual(result["checks"]["website_checks"]["status"], "ok")
            self.assertEqual(result["checks"]["website_twitter_backfill"]["status"], "ok")
            self.assertEqual(result["checks"]["reviews"]["status"], "ok")
            self.assertEqual(result["checks"]["background_processes"]["status"], "skipped")
            self.assertEqual(result["checks"]["background_logs"]["status"], "skipped")
            self.assertEqual(result["candidate_metrics"]["medium_high_with_website"], 1)
            self.assertEqual(result["candidate_metrics"]["medium_high_without_official_evidence"], 0)
            self.assertEqual(result["project_review_metrics"]["reviewed_medium_or_high"], 1)
            self.assertEqual(result["website_check_metrics"]["medium_high_websites_ok"], 1)
            self.assertEqual(result["website_twitter_backfill_metrics"]["matched_projects"], 0)
            self.assertEqual(result["report_files"]["latest_project_groups_count"], 1)
            self.assertGreaterEqual(result["discovery_sources"]["active_categories"], 4)

    def test_audit_warns_when_website_twitter_backfill_is_pending(self):
        from alpha_listener.cli import handle_audit, runtime_code_fingerprint, write_report
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            for block_number in (10, 11, 12):
                store.mark_block(block_number, f"0x{block_number:x}", block_number, 1)
            store.set_meta("last_scanned_block", 12)

            def contract(address, source, block, name=None, symbol=None):
                return {
                    "address": address,
                    "tx_hash": f"{source}:{address}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": source,
                    "log_index": block,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20" if symbol else "contract",
                    "name": name,
                    "symbol": symbol,
                    "decimals": 18 if symbol else None,
                    "total_supply": "1000000000000000000" if symbol else None,
                    "verified": bool(symbol),
                    "contract_name": f"{name}Token" if name else "",
                    "source_len": 1000 if symbol else 0,
                    "confidence": 0.9 if symbol else 0.05,
                }

            alpha = contract("0x1111111111111111111111111111111111111111", "contract_creation", 10, "Alpha", "ALPHA")
            for item in (
                alpha,
                contract("0x2222222222222222222222222222222222222222", "internal_create2", 11),
                contract("0x3333333333333333333333333333333333333333", "mint_transfer", 12),
                contract("0x4444444444444444444444444444444444444444", "uniswap_v4_initialize", 12),
            ):
                store.upsert_contract(item)
            store.upsert_enrichment(
                alpha["address"],
                {
                    "queries": [],
                    "evidence": {
                        "website": "https://alpha.example",
                        "tweet_count": 0,
                        "address_mentions": 0,
                        "official_website_reason": "verified_source_url",
                    },
                    "score": {"score": 72, "tier": "high", "breakdown": {}},
                    "status": "ok",
                },
            )
            group = store.project_groups(10)[0]
            store.add_website_check(
                group["project_key"],
                "https://alpha.example",
                {
                    "status": "ok",
                    "http_status": 200,
                    "final_url": "https://alpha.example",
                    "title": "Alpha Protocol",
                    "description": "",
                    "twitter_links": ["https://x.com/AlphaProtocol"],
                    "matched_terms": ["Alpha"],
                    "error": "",
                },
            )
            store.add_project_review(group["project_key"], "confirmed", "tester", "fixture", group)
            store.record_cycle_started(
                {
                    "command": "once",
                    "start_block": 10,
                    "end_block": 12,
                    "code_fingerprint": runtime_code_fingerprint(root),
                }
            )
            store.record_cycle_finished("ok", {"summary": store.summary(), "scanned_blocks": 3})
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            write_report(settings, 10)

            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            self.assertEqual(result["status"], "warn")
            self.assertEqual(result["checks"]["website_twitter_backfill"]["status"], "warn")
            self.assertEqual(result["website_twitter_backfill_metrics"]["matched_projects"], 1)
            self.assertEqual(
                result["website_twitter_backfill_metrics"]["examples"][0]["twitter_handle"],
                "AlphaProtocol",
            )

    def test_audit_fails_medium_high_without_official_evidence(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            for block_number in (10, 11, 12):
                store.mark_block(block_number, f"0x{block_number:x}", block_number, 1)
            store.set_meta("last_scanned_block", 12)
            address = "0x1111111111111111111111111111111111111111"
            store.upsert_contract(
                {
                    "address": address,
                    "tx_hash": "direct:1",
                    "origin_tx_hash": "0xaaa",
                    "discovery_source": "uniswap_v4_initialize",
                    "log_index": 1,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": 10,
                    "block_timestamp": 10,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": "Bagels",
                    "symbol": "BAGELS",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "BagelsToken",
                    "source_len": 5000,
                    "confidence": 0.9,
                }
            )
            store.upsert_enrichment(
                address,
                {
                    "queries": [],
                    "evidence": {
                        "twitter_account": None,
                        "website": None,
                        "tweet_count": 20,
                        "address_mentions": 4,
                        "credible_address_mentions": 4,
                    },
                    "score": {"score": 73, "tier": "medium", "breakdown": {}},
                    "status": "ok",
                },
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )

            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["checks"]["official_evidence"]["status"], "fail")
            self.assertEqual(result["candidate_metrics"]["medium_high_without_official_evidence"], 1)
            self.assertEqual(
                result["candidate_metrics"]["medium_high_without_official_evidence_examples"][0]["address"],
                address,
            )

    def test_audit_warns_when_medium_high_review_backlog_exists(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            for block_number in (10, 11, 12):
                store.mark_block(block_number, f"0x{block_number:x}", block_number, 1)
            store.set_meta("last_scanned_block", 12)

            def contract(address, block, name, symbol):
                return {
                    "address": address,
                    "tx_hash": f"direct:{block}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": name,
                    "symbol": symbol,
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": f"{name}Token",
                    "source_len": 5000,
                    "confidence": 0.9,
                }

            reviewed = contract("0x1111111111111111111111111111111111111111", 10, "Reviewed", "RVW")
            unreviewed = contract("0x2222222222222222222222222222222222222222", 11, "Fresh", "FRSH")
            for item in (reviewed, unreviewed):
                store.upsert_contract(item)
                store.upsert_enrichment(
                    item["address"],
                    {
                        "queries": [],
                        "evidence": {
                            "twitter_account": item["name"],
                            "twitter_followers": 1000,
                            "website": f"https://{item['symbol'].lower()}.example",
                            "tweet_count": 3,
                            "address_mentions": 1,
                            "official_identity_reason": "account_address_mention",
                            "official_website_reason": "official_profile_url",
                        },
                        "score": {"score": 80, "tier": "medium", "breakdown": {}},
                        "status": "ok",
                    },
                )
            reviewed_group = [group for group in store.project_groups(10) if group["label"] == "Reviewed"][0]
            store.add_project_review(reviewed_group["project_key"], "confirmed", "tester", "done", reviewed_group)
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )

            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            self.assertEqual(result["status"], "warn")
            self.assertEqual(result["checks"]["reviews"]["status"], "warn")
            self.assertEqual(result["project_review_metrics"]["reviewed_medium_or_high"], 1)
            self.assertEqual(result["project_review_metrics"]["unreviewed_medium_or_high"], 1)
            self.assertEqual(result["project_review_metrics"]["unreviewed_examples"][0]["label"], "Fresh")

    def test_audit_fails_stale_running_cycle(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            store.record_cycle_started({"command": "run"}, role="enricher")
            stale_at = "2026-01-01T00:00:00+00:00"
            store.set_meta("last_cycle_enricher_started_at", stale_at)
            store.set_meta_json("last_cycle_enricher_context_json", {"role": "enricher", "started_at": stale_at})
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    runtime_stale_minutes=1,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            self.assertEqual(result["checks"]["runtime"]["status"], "fail")
            self.assertEqual(result["checks"]["runtime"]["detail"]["stale_running_roles"], ["enricher"])
            self.assertTrue(result["checks"]["runtime"]["detail"]["roles"]["enricher"]["stale"])

    def test_audit_uses_cycle_progress_as_runtime_heartbeat(self):
        from alpha_listener.cli import handle_audit, runtime_code_fingerprint
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            code_fingerprint = runtime_code_fingerprint(root)
            store.record_cycle_started(
                {
                    "command": "run",
                    "role": "enricher",
                    "code_fingerprint": code_fingerprint,
                },
                role="enricher",
            )
            stale_at = "2026-01-01T00:00:00+00:00"
            store.set_meta("last_cycle_enricher_started_at", stale_at)
            store.set_meta_json(
                "last_cycle_enricher_context_json",
                {
                    "command": "run",
                    "role": "enricher",
                    "started_at": stale_at,
                    "code_fingerprint": code_fingerprint,
                },
            )
            progress = store.record_cycle_progress(
                {"event": "contract_enriched", "address": "0x1111111111111111111111111111111111111111"},
                role="enricher",
            )
            runtime = store.runtime_status()
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    runtime_stale_minutes=1,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            self.assertEqual(runtime["roles"]["enricher"]["last_cycle_progressed_at"], progress["progressed_at"])
            runtime_check = result["checks"]["runtime"]
            self.assertEqual(runtime_check["status"], "ok")
            self.assertEqual(runtime_check["detail"]["stale_running_roles"], [])
            self.assertFalse(runtime_check["detail"]["roles"]["enricher"]["stale"])
            self.assertEqual(runtime_check["detail"]["roles"]["enricher"]["last_active_at"], progress["progressed_at"])

    def test_audit_fails_stale_runtime_code_fingerprint(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "alpha_listener").mkdir(parents=True)
            (root / "src" / "alpha_listener" / "cli.py").write_text("print('new')\n", encoding="utf-8")
            store = Store(root / "alpha.sqlite", root)
            store.record_cycle_started(
                {
                    "command": "run",
                    "code_fingerprint": {"algorithm": "sha256", "digest": "0" * 64, "files": 1},
                },
                role="scanner",
            )
            store.record_cycle_finished("ok", {"summary": {}, "scanned_blocks": 0}, role="scanner")
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    runtime_stale_minutes=1,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            runtime = result["checks"]["runtime"]
            self.assertEqual(runtime["status"], "fail")
            self.assertEqual(runtime["detail"]["stale_code_fingerprint_roles"], ["scanner"])
            self.assertTrue(runtime["detail"]["roles"]["scanner"]["code_fingerprint_stale"])

    def test_audit_ignores_stale_maintenance_code_fingerprint(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "alpha_listener").mkdir(parents=True)
            (root / "src" / "alpha_listener" / "cli.py").write_text("print('new')\n", encoding="utf-8")
            store = Store(root / "alpha.sqlite", root)
            store.record_cycle_started(
                {
                    "command": "once",
                    "code_fingerprint": {"algorithm": "sha256", "digest": "0" * 64, "files": 1},
                },
                role="maintenance",
            )
            store.record_cycle_finished("ok", {"summary": {}, "scanned_blocks": 0}, role="maintenance")
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    runtime_stale_minutes=1,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            runtime = result["checks"]["runtime"]
            self.assertNotEqual(runtime["status"], "fail")
            self.assertEqual(runtime["detail"]["stale_code_fingerprint_roles"], [])
            self.assertFalse(runtime["detail"]["roles"]["maintenance"]["code_fingerprint_required"])
            self.assertFalse(runtime["detail"]["roles"]["maintenance"]["code_fingerprint_stale"])

    def test_audit_fails_dead_background_pid(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha-listener.pid").write_text("999999", encoding="utf-8")
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )

            with patch("alpha_listener.cli.process_exists", return_value=False):
                result = handle_audit(
                    settings,
                    SimpleNamespace(
                        no_live=True,
                        confirmations=None,
                        coverage_window_blocks=3,
                        max_lag_blocks=None,
                        runtime_stale_minutes=1,
                        min_alpha_candidates=1,
                        limit=5,
                    ),
                )

            background = result["checks"]["background_processes"]
            self.assertEqual(background["status"], "fail")
            self.assertEqual(background["detail"]["dead_roles"], ["scanner"])
            self.assertIn("classifier", background["detail"]["missing_roles"])

    def test_audit_fails_background_pid_with_wrong_role_identity(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha-classifier.pid").write_text("1234", encoding="utf-8")
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            wrong_command = f"powershell.exe -File {root}\\scripts\\run-listener.ps1 --no-twitter --enrich-limit 0"

            with patch("alpha_listener.cli.process_exists", return_value=True), patch(
                "alpha_listener.cli.process_command_line", return_value=wrong_command
            ):
                result = handle_audit(
                    settings,
                    SimpleNamespace(
                        no_live=True,
                        confirmations=None,
                        coverage_window_blocks=3,
                        max_lag_blocks=None,
                        runtime_stale_minutes=1,
                        min_alpha_candidates=1,
                        limit=5,
                    ),
                )

            background = result["checks"]["background_processes"]
            self.assertEqual(background["status"], "fail")
            self.assertEqual(background["detail"]["mismatched_roles"], ["classifier"])
            identity = background["detail"]["roles"]["classifier"]["identity"]
            self.assertEqual(identity["status"], "mismatch")
            self.assertFalse(identity["role_matched"])
            self.assertTrue(identity["workspace_matched"])

    def test_audit_warns_on_nonempty_background_error_log(self):
        from alpha_listener.cli import handle_audit
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            (logs / "alpha-listener.err.log").write_text("boom\n", encoding="utf-8")
            (logs / "alpha-classifier.err.log").write_text("", encoding="utf-8")
            (logs / "alpha-enricher.err.log").write_text("", encoding="utf-8")
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )

            result = handle_audit(
                settings,
                SimpleNamespace(
                    no_live=True,
                    confirmations=None,
                    coverage_window_blocks=3,
                    max_lag_blocks=None,
                    runtime_stale_minutes=1,
                    min_alpha_candidates=1,
                    limit=5,
                ),
            )

            logs_check = result["checks"]["background_logs"]
            self.assertEqual(logs_check["status"], "warn")
            self.assertEqual(logs_check["detail"]["nonempty_roles"], ["scanner"])
            self.assertGreater(logs_check["detail"]["roles"]["scanner"]["bytes"], 0)

    def test_background_role_match_accepts_common_max_blocks_zero_forms(self):
        from alpha_listener.cli import background_role_matches

        self.assertTrue(background_role_matches("enricher", "python -m alpha_listener.cli run --max-blocks=0"))
        self.assertTrue(background_role_matches("enricher", "powershell.exe -file run-listener.ps1 --max-blocks \"0\""))
        self.assertFalse(background_role_matches("scanner", "powershell.exe -file run-listener.ps1 --max-blocks 0"))
        self.assertTrue(background_role_matches("scanner", "powershell.exe -file run-listener.ps1 --max-blocks 60"))

    def test_export_writes_unreviewed_candidate_sheet(self):
        from alpha_listener.cli import handle_export
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(address, source, block, name, symbol):
                return {
                    "address": address,
                    "tx_hash": f"{source}:{address}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": source,
                    "log_index": block,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": name,
                    "symbol": symbol,
                    "decimals": 18,
                    "total_supply": "1000000000000000000",
                    "verified": True,
                    "contract_name": f"{name}Token",
                    "source_len": 1000,
                    "confidence": 0.9,
                }

            reviewed = contract("0x1111111111111111111111111111111111111111", "contract_creation", 1, "Reviewed", "RVW")
            unreviewed = contract("0x2222222222222222222222222222222222222222", "uniswap_v4_initialize", 2, "Fresh", "FRSH")
            store.upsert_contract(reviewed)
            store.upsert_contract(unreviewed)
            store.upsert_enrichment(
                reviewed["address"],
                {
                    "queries": [],
                    "evidence": {
                        "twitter_account": "Reviewed",
                        "tweet_count": 2,
                        "address_mentions": 1,
                        "official_identity_reason": "account_address_support",
                    },
                    "score": {"score": 91, "tier": "high", "breakdown": {}},
                    "status": "ok",
                },
            )
            store.upsert_enrichment(
                unreviewed["address"],
                {
                    "queries": [],
                    "evidence": {
                        "twitter_account": "FreshProtocol",
                        "website": "https://fresh.example",
                        "tweet_count": 4,
                        "address_mentions": 2,
                        "credible_address_mentions": 2,
                        "official_identity_reason": "profile_project_identity",
                        "official_website_reason": "official_profile_url",
                        "project_category": "defi",
                        "project_positioning": "DeFi trading or liquidity protocol",
                        "project_description": "Fresh: Fresh launch is live as a DeFi trading protocol.",
                        "source_website_candidates": ["https://fresh.example"],
                        "sample_tweets": [
                            {
                                "userScreenName": "FreshProtocol",
                                "text": "Fresh launch is live at 0x2222222222222222222222222222222222222222",
                            }
                        ],
                        "top_authors": [
                            {
                                "username": "FreshProtocol",
                                "score": 22,
                                "address_mentions": 1,
                                "flags": [],
                            }
                        ],
                    },
                    "score": {
                        "score": 75,
                        "tier": "medium",
                        "breakdown": {
                            "identity_resolution": 25,
                            "onchain_provenance": 14,
                            "project_surface": 20,
                            "social_signal": 10,
                            "technical_readability": 22,
                            "risk_penalty": -16,
                        },
                    },
                    "status": "ok",
                },
            )
            reviewed_group = [group for group in store.project_groups(10) if group["label"] == "Reviewed"][0]
            store.add_project_review(reviewed_group["project_key"], "confirmed", "tester", "done", reviewed_group)
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            output = root / "fresh.csv"
            result = handle_export(
                settings,
                SimpleNamespace(format="csv", output=output, limit=10, tier=None, include_reviewed=False),
            )
            with output.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))

            self.assertEqual(result["rows"], 1)
            self.assertEqual(rows[0]["label"], "Fresh")
            self.assertEqual(rows[0]["tier"], "medium")
            self.assertEqual(rows[0]["project_category"], "defi")
            self.assertEqual(rows[0]["project_positioning"], "DeFi trading or liquidity protocol")
            self.assertIn("Fresh launch is live", rows[0]["project_description"])
            self.assertEqual(rows[0]["official_identity_reason"], "profile_project_identity")
            self.assertEqual(rows[0]["official_website_reason"], "official_profile_url")
            self.assertIn('"social_signal":10', rows[0]["score_breakdown"])
            self.assertIn("official_twitter", rows[0]["support_flags"])
            self.assertIn("official_website", rows[0]["support_flags"])
            self.assertIn("credible_address_mentions", rows[0]["support_flags"])
            self.assertIn("review_decision_required", rows[0]["review_hints"])
            self.assertIn("identity=profile_project_identity", rows[0]["evidence_summary"])
            self.assertIn("@FreshProtocol: Fresh launch is live", rows[0]["sample_tweets"])
            self.assertIn("@FreshProtocol", rows[0]["top_authors"])
            self.assertEqual(rows[0]["source_website_candidates"], "https://fresh.example")
            self.assertIn("--project-key", rows[0]["review_command"])
            self.assertTrue(rows[0]["etherscan_url"].endswith(unreviewed["address"]))

            json_result = handle_export(
                settings,
                SimpleNamespace(
                    format="json",
                    output=root / "all.json",
                    limit=10,
                    tier=["high", "medium"],
                    include_reviewed=True,
                ),
            )
            exported = json.loads((root / "all.json").read_text(encoding="utf-8"))
            self.assertEqual(json_result["rows"], 2)
            self.assertEqual({row["label"] for row in exported}, {"Reviewed", "Fresh"})

    def test_review_pack_writes_markdown_brief(self):
        from alpha_listener.cli import handle_review_pack
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x2222222222222222222222222222222222222222",
                "tx_hash": "uniswap_v4_initialize:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "uniswap_v4_initialize",
                "log_index": 1,
                "related": {},
                "deployer": "0x3333333333333333333333333333333333333333",
                "block_number": 12,
                "block_timestamp": 12,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Fresh",
                "symbol": "FRSH",
                "decimals": 18,
                "total_supply": "1000000000000000000",
                "verified": True,
                "contract_name": "FreshToken",
                "source_len": 1000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": [],
                    "evidence": {
                        "twitter_account": "FreshProtocol",
                        "website": "https://fresh.example",
                        "tweet_count": 4,
                        "address_mentions": 2,
                        "credible_address_mentions": 2,
                        "official_identity_reason": "profile_project_identity",
                        "official_website_reason": "official_profile_url",
                        "project_category": "defi",
                        "project_positioning": "DeFi trading or liquidity protocol",
                        "project_description": "Fresh: Fresh launch is live as a DeFi trading protocol.",
                        "source_website_candidates": ["https://fresh.example"],
                        "sample_tweets": [
                            {
                                "userScreenName": "FreshProtocol",
                                "text": "Fresh launch is live at 0x2222222222222222222222222222222222222222",
                            }
                        ],
                        "top_authors": [
                            {
                                "username": "FreshProtocol",
                                "score": 22,
                                "address_mentions": 1,
                                "flags": ["official"],
                            }
                        ],
                    },
                    "score": {
                        "score": 75,
                        "tier": "medium",
                        "breakdown": {
                            "identity_resolution": 25,
                            "onchain_provenance": 14,
                            "project_surface": 20,
                            "social_signal": 10,
                            "technical_readability": 22,
                            "risk_penalty": -16,
                        },
                    },
                    "status": "ok",
                },
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            output = root / "review-pack.md"
            result = handle_review_pack(
                settings,
                SimpleNamespace(output=output, limit=10, tier=["high", "medium"], include_reviewed=False),
            )
            markdown = output.read_text(encoding="utf-8")

            self.assertEqual(result["rows"], 1)
            self.assertIn("# Alpha Candidate Review Pack", markdown)
            self.assertIn("## 1. Fresh (medium, score 75)", markdown)
            self.assertIn("[X @FreshProtocol](https://x.com/FreshProtocol)", markdown)
            self.assertIn("[https://fresh.example](https://fresh.example)", markdown)
            self.assertIn("Category: `defi`", markdown)
            self.assertIn("Positioning: DeFi trading or liquidity protocol", markdown)
            self.assertIn("Description: Fresh: Fresh launch is live", markdown)
            self.assertIn('Score breakdown: {"identity_resolution":25', markdown)
            self.assertIn("Support flags: `official_twitter`, `official_website`", markdown)
            self.assertIn("Review hints: `review_decision_required`", markdown)
            self.assertIn("identity=profile_project_identity", markdown)
            self.assertIn("website=official_profile_url", markdown)
            self.assertIn("@FreshProtocol: Fresh launch is live", markdown)
            self.assertIn("@FreshProtocol (score=22; addr=1; flags=official)", markdown)
            self.assertIn("python -m alpha_listener.cli review --project-key", markdown)
            self.assertIn('$env:PYTHONPATH = "$PWD\\src"', markdown)

    def test_review_hints_include_website_check_risk(self):
        from alpha_listener.cli import review_hints

        group = {
            "tier": "high",
            "score": 90,
            "twitter_account": "FreshProtocol",
            "website": "https://fresh.example",
        }
        evidence = {
            "official_identity_reason": "profile_project_identity",
            "credible_address_mentions": 1,
        }
        support_flags = ["official_twitter", "official_website", "credible_address_mentions"]

        self.assertIn("website_check_missing", review_hints(group, evidence, support_flags))
        self.assertIn(
            "website_check_warn_verify_project_match",
            review_hints(group, evidence, support_flags, {"status": "warn"}),
        )
        self.assertIn(
            "website_check_failed_verify_project_match",
            review_hints(group, evidence, support_flags, {"status": "fail"}),
        )
        self.assertIn(
            "website_check_http_error",
            review_hints(group, evidence, support_flags, {"status": "fail", "failure_kind": "http_error"}),
        )
        self.assertIn(
            "website_check_tls_error",
            review_hints(group, evidence, support_flags, {"status": "fail", "failure_kind": "tls_error"}),
        )
        self.assertNotIn(
            "website_check_warn_verify_project_match",
            review_hints(group, evidence, support_flags, {"status": "ok"}),
        )

        third_party = {
            "tier": "high",
            "score": 90,
            "twitter_account": "FreshProtocol",
            "website": "",
        }
        third_party_evidence = {
            "twitter_account": "FreshProtocol",
            "official_identity_reason": "profile_project_identity",
            "address_mentions": 0,
            "credible_address_mentions": 0,
            "official_account_own_project_mentions": 0,
            "official_account_own_crypto_project_mentions": 0,
            "official_account_address_mentions": 0,
            "official_account_flags": ["mentioned_by_project_context"],
        }
        self.assertIn(
            "official_twitter_mentioned_by_third_party_only",
            review_hints(third_party, third_party_evidence, ["official_twitter", "no_website"]),
        )

    def test_export_hints_include_grouped_third_party_profile_identity(self):
        from alpha_listener.cli import handle_export
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x2222222222222222222222222222222222222222",
                "tx_hash": "uniswap_v2_pair_created:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "uniswap_v2_pair_created",
                "log_index": 1,
                "related": {},
                "deployer": "0x3333333333333333333333333333333333333333",
                "block_number": 12,
                "block_timestamp": 12,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Block Street",
                "symbol": "BSB",
                "decimals": 18,
                "total_supply": "1000000000000000000",
                "verified": True,
                "contract_name": "LaunchTokenV4",
                "source_len": 5000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": [],
                    "evidence": {
                        "twitter_account": "BlockSt_HQ",
                        "twitter_followers": 62072,
                        "twitter_verified": True,
                        "tweet_count": 40,
                        "address_mentions": 0,
                        "credible_address_mentions": 0,
                        "official_identity_reason": "profile_project_identity",
                        "top_authors": [
                            {
                                "username": "BlockSt_HQ",
                                "address_mentions": 0,
                                "project_mentions": 2,
                                "own_project_mentions": 0,
                                "own_crypto_project_mentions": 0,
                                "flags": ["mentioned_by_project_context"],
                            }
                        ],
                    },
                    "score": {
                        "score": 91,
                        "tier": "medium",
                        "breakdown": {
                            "identity_resolution": 23,
                            "onchain_provenance": 14,
                            "project_surface": 20,
                            "social_signal": 24,
                            "technical_readability": 22,
                            "risk_penalty": -12,
                        },
                    },
                    "status": "ok",
                },
            )
            store.close()
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            output = root / "third-party.csv"
            handle_export(settings, SimpleNamespace(format="csv", output=output, limit=10, tier=["medium"], include_reviewed=False))
            with output.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))

            self.assertEqual(rows[0]["label"], "Block Street")
            self.assertIn("official_twitter_mentioned_by_third_party_only", rows[0]["review_hints"])

    def test_verify_websites_persists_checks_for_export_and_review_pack(self):
        from alpha_listener.cli import handle_export, handle_review_pack, handle_verify_websites
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x2222222222222222222222222222222222222222",
                "tx_hash": "uniswap_v4_initialize:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "uniswap_v4_initialize",
                "log_index": 1,
                "related": {},
                "deployer": "0x3333333333333333333333333333333333333333",
                "block_number": 12,
                "block_timestamp": 12,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Fresh",
                "symbol": "FRSH",
                "decimals": 18,
                "total_supply": "1000000000000000000",
                "verified": True,
                "contract_name": "FreshToken",
                "source_len": 1000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": [],
                    "evidence": {
                        "website": "https://fresh.example",
                        "tweet_count": 1,
                        "official_website_reason": "verified_source_url",
                    },
                    "score": {
                        "score": 68,
                        "tier": "medium",
                        "breakdown": {
                            "identity_resolution": 20,
                            "onchain_provenance": 14,
                            "project_surface": 20,
                            "social_signal": 1,
                            "technical_readability": 22,
                            "risk_penalty": -9,
                        },
                    },
                    "status": "ok",
                },
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            fake_check = {
                "status": "ok",
                "http_status": 200,
                "final_url": "https://fresh.example/",
                "title": "Fresh Protocol",
                "description": "Fresh protocol launch",
                "twitter_links": ["https://x.com/FreshProtocol"],
                "matched_terms": ["Fresh"],
                "error": "",
            }
            with patch("alpha_listener.cli.verify_website", return_value=fake_check) as mocked:
                result = handle_verify_websites(
                    settings,
                    SimpleNamespace(output=root / "website-checks.json", limit=10, tier=["medium"], include_reviewed=False),
                )

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["status_counts"], {"ok": 1})
            mocked.assert_called_once()
            self.assertTrue((root / "website_checks.jsonl").exists())
            self.assertTrue((root / "website-checks.json").exists())

            export_output = root / "fresh.csv"
            handle_export(
                settings,
                SimpleNamespace(format="csv", output=export_output, limit=10, tier=["high", "medium"], include_reviewed=False),
            )
            with export_output.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["website_check_status"], "ok")
            self.assertEqual(rows[0]["website_check_http_status"], "200")
            self.assertEqual(rows[0]["website_check_title"], "Fresh Protocol")
            self.assertIn("matched=Fresh", rows[0]["website_check_summary"])
            self.assertIn("https://x.com/FreshProtocol", rows[0]["website_check_twitter_links"])

            pack_output = root / "review-pack.md"
            handle_review_pack(
                settings,
                SimpleNamespace(output=pack_output, limit=10, tier=["high", "medium"], include_reviewed=False),
            )
            markdown = pack_output.read_text(encoding="utf-8")
            self.assertIn("Website check: status=ok; http=200; matched=Fresh; twitter_links=1", markdown)
            self.assertIn("Website title: Fresh Protocol", markdown)
            self.assertIn("[https://x.com/FreshProtocol](https://x.com/FreshProtocol)", markdown)

    def test_website_verifier_uses_powershell_fallback_on_urllib_timeout(self):
        import urllib.error

        from alpha_listener.website import verify_website

        html = b"""
        <html>
          <head><title>Proof of Satoshi - 10,000 on-chain portraits</title></head>
          <body><a href="https://x.com/ProofOfSatoshi\\">X</a></body>
        </html>
        """
        with patch("alpha_listener.website.fetch_website_urllib", side_effect=urllib.error.URLError("timed out")):
            with patch(
                "alpha_listener.website.fetch_website_powershell",
                return_value=(html, "https://proofofsatoshi.io", 200, "text/html; charset=utf-8"),
            ):
                result = verify_website(
                    "https://proofofsatoshi.io",
                    "Proof of Satoshi",
                    ["Proof of Satoshi"],
                    ["POS"],
                    1,
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_status"], 200)
        self.assertIn("Proof of Satoshi", result["matched_terms"])
        self.assertEqual(result["twitter_links"], ["https://x.com/ProofOfSatoshi"])

    def test_website_verifier_records_http_fallback_after_https_failure(self):
        import urllib.error

        from alpha_listener.website import verify_website

        html = b"<html><head><title>Bad Gateway</title></head><body>upstream failed</body></html>"
        with patch(
            "alpha_listener.website.fetch_website_urllib",
            side_effect=urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"),
        ):
            with patch("alpha_listener.website.fetch_website_powershell", return_value=None):
                with patch(
                    "alpha_listener.website.fetch_website_with_fallback",
                    return_value=(html, "http://unirock.art", 502, "text/html; charset=utf-8"),
                ):
                    result = verify_website("https://unirock.art", "uniRock", ["uniRock"], [], 1)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["http_status"], 502)
        self.assertEqual(result["failure_kind"], "http_error")
        self.assertEqual(result["fallback_url"], "http://unirock.art")
        self.assertIn("primary_fetch_failed=", result["error"])
        self.assertIn("http_status=502", result["error"])

    def test_latest_website_check_exposes_payload_failure_metadata(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            try:
                store.add_website_check(
                    "site:unirock.art",
                    "https://unirock.art",
                    {
                        "status": "fail",
                        "http_status": 502,
                        "final_url": "http://unirock.art",
                        "title": "",
                        "description": "",
                        "twitter_links": [],
                        "matched_terms": [],
                        "error": "http_status=502",
                        "failure_kind": "http_error",
                        "fallback_url": "http://unirock.art",
                        "fallback_reason": "ssl eof",
                    },
                )
                latest = store.latest_website_check("site:unirock.art", "https://unirock.art")
            finally:
                store.close()

        self.assertEqual(latest["failure_kind"], "http_error")
        self.assertEqual(latest["fallback_url"], "http://unirock.art")
        self.assertEqual(latest["fallback_reason"], "ssl eof")

    def test_verify_websites_applies_failed_check_to_scoring(self):
        from alpha_listener.cli import handle_verify_websites
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x3333333333333333333333333333333333333333",
                "tx_hash": "contract_creation:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x4444444444444444444444444444444444444444",
                "block_number": 12,
                "block_timestamp": 12,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "uniRock",
                "symbol": "uROCK",
                "decimals": 18,
                "total_supply": "1000000000000000000",
                "verified": True,
                "contract_name": "UniRock",
                "source_len": 4000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": [],
                    "evidence": {
                        "website": "https://unirock.art",
                        "tweet_count": 1,
                        "address_mentions": 0,
                        "credible_address_mentions": 0,
                        "discussion_only": True,
                        "official_website_reason": "verified_source_url",
                    },
                    "score": {
                        "score": 68,
                        "tier": "medium",
                        "breakdown": {
                            "identity_resolution": 20,
                            "onchain_provenance": 14,
                            "project_surface": 20,
                            "social_signal": 4,
                            "technical_readability": 22,
                            "risk_penalty": -12,
                        },
                    },
                    "status": "ok",
                },
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            with patch(
                "alpha_listener.cli.verify_website",
                return_value={
                    "status": "fail",
                    "http_status": 502,
                    "final_url": "http://unirock.art",
                    "title": "",
                    "description": "",
                    "twitter_links": [],
                    "matched_terms": ["uniRock"],
                    "error": "http_status=502",
                    "failure_kind": "http_error",
                    "fallback_url": "http://unirock.art",
                },
            ):
                result = handle_verify_websites(
                    settings,
                    SimpleNamespace(limit=10, tier=["medium"], include_reviewed=True, output=None),
                )

            store = Store(root / "alpha.sqlite", root)
            row = store.conn.execute(
                "SELECT tier, score, evidence_json FROM enrichments WHERE address = ?",
                (contract["address"],),
            ).fetchone()
            evidence = json.loads(row["evidence_json"])
            store.close()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["results"][0]["rescored_addresses"][0]["tier"], "watch")
        self.assertEqual(row["tier"], "watch")
        self.assertEqual(evidence["website_check_status"], "fail")
        self.assertEqual(evidence["website_check_failure_kind"], "http_error")

    def test_cycle_website_verification_targets_only_due_checks(self):
        from datetime import datetime, timezone

        from alpha_listener.cli import website_check_due

        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

        self.assertTrue(website_check_due(None, now=now))
        self.assertTrue(website_check_due({"status": "fail", "checked_at": "2026-05-17T11:59:00+00:00"}, now=now))
        self.assertFalse(website_check_due({"status": "ok", "checked_at": "2026-05-17T11:00:00+00:00"}, now=now))
        self.assertTrue(website_check_due({"status": "ok", "checked_at": "2026-05-15T11:00:00+00:00"}, now=now))

    def test_run_cycle_backfills_twitter_from_verified_website(self):
        from alpha_listener.cli import run_cycle
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x4444444444444444444444444444444444444444",
                "tx_hash": "uniswap_v4_initialize:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "uniswap_v4_initialize",
                "log_index": 1,
                "related": {},
                "deployer": "0x5555555555555555555555555555555555555555",
                "block_number": 12,
                "block_timestamp": 12,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Fresh",
                "symbol": "FRSH",
                "decimals": 18,
                "total_supply": "1000000000000000000",
                "verified": True,
                "contract_name": "FreshToken",
                "source_len": 4000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": [],
                    "evidence": {
                        "website": "https://fresh.example",
                        "tweet_count": 2,
                        "official_website_reason": "verified_source_url",
                    },
                    "score": {"score": 68, "tier": "medium", "breakdown": {}},
                    "status": "ok",
                },
            )
            group = store.project_groups(10)[0]
            store.add_website_check(
                group["project_key"],
                "https://fresh.example",
                {
                    "status": "ok",
                    "http_status": 200,
                    "final_url": "https://fresh.example",
                    "title": "Fresh Protocol",
                    "description": "",
                    "twitter_links": ["https://x.com/FreshProtocol"],
                    "matched_terms": ["Fresh"],
                    "error": "",
                },
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            args = SimpleNamespace(
                command="once",
                no_twitter=False,
                confirmations=None,
                lookback_blocks=None,
                max_blocks=0,
                enrich_limit=0,
                force_enrich=False,
                report_limit=0,
                report_every_enriched=None,
                verify_websites_limit=0,
                backfill_website_twitter_limit=5,
                start_block=None,
                end_block=None,
            )

            with patch("alpha_listener.cli.EtherscanClient") as etherscan_cls, patch(
                "alpha_listener.cli.OpenTwitterClient"
            ) as twitter_cls:
                etherscan_cls.return_value.latest_block_number.return_value = 110
                twitter_cls.return_value.user_info.return_value = {
                    "screenName": "FreshProtocol",
                    "name": "Fresh Protocol",
                    "followersCount": 1234,
                    "verified": True,
                }
                result = run_cycle(settings, args)

            store = Store(root / "alpha.sqlite", root)
            row = store.conn.execute(
                "SELECT twitter_account, tier, evidence_json FROM enrichments WHERE address = ?",
                (contract["address"],),
            ).fetchone()
            evidence = json.loads(row["evidence_json"])
            store.close()

        self.assertEqual(result["website_twitter_backfill"]["matched_projects"], 1)
        self.assertEqual(result["website_twitter_backfill"]["backfilled_addresses"], 1)
        self.assertEqual(row["twitter_account"], "FreshProtocol")
        self.assertEqual(evidence["official_identity_reason"], "official_website_link")
        self.assertEqual(evidence["official_twitter_source"], "website_check")

    def test_backfill_website_twitter_uses_verified_website_link(self):
        from alpha_listener.cli import handle_backfill_website_twitter, handle_export
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x2222222222222222222222222222222222222222",
                "tx_hash": "uniswap_v4_initialize:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "uniswap_v4_initialize",
                "log_index": 1,
                "related": {},
                "deployer": "0x3333333333333333333333333333333333333333",
                "block_number": 12,
                "block_timestamp": 12,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "UniMixer",
                "symbol": "UNIMIXER",
                "decimals": 18,
                "total_supply": "1000000000000000000",
                "verified": True,
                "contract_name": "UniMixerToken",
                "source_len": 4000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": [],
                    "evidence": {
                        "website": "https://unimixer.tech",
                        "tweet_count": 2,
                        "official_website_reason": "verified_source_url",
                    },
                    "score": {
                        "score": 68,
                        "tier": "medium",
                        "breakdown": {
                            "identity_resolution": 20,
                            "onchain_provenance": 14,
                            "project_surface": 20,
                            "social_signal": 4,
                            "technical_readability": 22,
                            "risk_penalty": -12,
                        },
                    },
                    "status": "ok",
                },
            )
            group = store.project_groups(10)[0]
            store.add_website_check(
                group["project_key"],
                "https://unimixer.tech",
                {
                    "status": "ok",
                    "http_status": 200,
                    "final_url": "https://unimixer.tech",
                    "title": "UniMixer - Swap without getting sandwiched",
                    "description": "",
                    "twitter_links": ["https://x.com/unimixerxyz"],
                    "matched_terms": ["UniMixer", "UNIMIXER"],
                    "error": "",
                },
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            dry_run = handle_backfill_website_twitter(
                settings,
                SimpleNamespace(limit=10, tier=["medium"], include_reviewed=False, dry_run=True),
            )
            self.assertEqual(dry_run["matched_projects"], 1)
            store = Store(root / "alpha.sqlite", root)
            before = store.conn.execute("SELECT twitter_account FROM enrichments WHERE address = ?", (contract["address"],)).fetchone()
            store.close()
            self.assertIsNone(before["twitter_account"])

            fake_profile = {"data": {"username": "unimixerxyz", "name": "UniMixer", "followersCount": 1234, "verified": False}}
            with patch("alpha_listener.cli.OpenTwitterClient.user_info", return_value=fake_profile):
                result = handle_backfill_website_twitter(
                    settings,
                    SimpleNamespace(limit=10, tier=["medium"], include_reviewed=False, dry_run=False),
                )
            self.assertEqual(result["matched_projects"], 1)
            self.assertEqual(result["backfilled_addresses"], 1)

            store = Store(root / "alpha.sqlite", root)
            row = store.conn.execute(
                "SELECT twitter_account, twitter_name, twitter_followers, evidence_json, score, tier FROM enrichments WHERE address = ?",
                (contract["address"],),
            ).fetchone()
            events = [json.loads(line)["event_type"] for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            store.close()
            evidence = json.loads(row["evidence_json"])
            self.assertEqual(row["twitter_account"], "unimixerxyz")
            self.assertEqual(row["twitter_name"], "UniMixer")
            self.assertEqual(row["twitter_followers"], 1234)
            self.assertEqual(evidence["official_identity_reason"], "official_website_link")
            self.assertEqual(evidence["official_twitter_source"], "website_check")
            self.assertIn("website_twitter_backfilled", events)
            self.assertGreaterEqual(row["score"], 70)

            export_output = root / "unimixer.csv"
            handle_export(settings, SimpleNamespace(format="csv", output=export_output, limit=10, tier=["high", "medium"], include_reviewed=False))
            with export_output.open(encoding="utf-8-sig", newline="") as f:
                exported = list(csv.DictReader(f))
            self.assertEqual(exported[0]["twitter_account"], "unimixerxyz")
            self.assertEqual(exported[0]["website_check_status"], "ok")
            self.assertIn("twitter_from_website", exported[0]["support_flags"])
            self.assertIn("official_twitter_from_verified_website", exported[0]["review_hints"])

    def test_review_import_applies_exported_decisions(self):
        from alpha_listener.cli import handle_export, handle_review_import
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(address, block, name, symbol):
                return {
                    "address": address,
                    "tx_hash": f"direct:{block}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": name,
                    "symbol": symbol,
                    "decimals": 18,
                    "total_supply": "1000000000000000000",
                    "verified": True,
                    "contract_name": f"{name}Token",
                    "source_len": 1000,
                    "confidence": 0.9,
                }

            for item, decision_score in (
                (contract("0x1111111111111111111111111111111111111111", 1, "Alpha", "ALP"), 88),
                (contract("0x2222222222222222222222222222222222222222", 2, "Beta", "BTA"), 72),
            ):
                store.upsert_contract(item)
                store.upsert_enrichment(
                    item["address"],
                    {
                        "queries": [],
                        "evidence": {
                            "twitter_account": item["name"],
                            "website": f"https://{item['symbol'].lower()}.example",
                            "tweet_count": 3,
                            "address_mentions": 1,
                            "official_identity_reason": "account_address_mention",
                            "official_website_reason": "official_profile_url",
                        },
                        "score": {"score": decision_score, "tier": "high" if decision_score >= 80 else "medium", "breakdown": {}},
                        "status": "ok",
                    },
                )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            output = root / "reviews.csv"
            handle_export(settings, SimpleNamespace(format="csv", output=output, limit=10, tier=["high", "medium"], include_reviewed=False))
            with output.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                fieldnames = reader.fieldnames or []
            for row in rows:
                row["review_decision"] = "confirmed" if row["label"] == "Alpha" else "reject"
                row["review_note"] = f"batch reviewed {row['label']}"
            with output.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            dry_run = handle_review_import(settings, SimpleNamespace(input=output, reviewer="tester", dry_run=True))
            store = Store(root / "alpha.sqlite", root)
            groups_after_dry_run = store.project_groups(10)
            store.close()
            imported = handle_review_import(settings, SimpleNamespace(input=output, reviewer="tester", dry_run=False))
            store = Store(root / "alpha.sqlite", root)
            groups = store.project_groups(10)
            events = [json.loads(line)["event_type"] for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            store.close()

            self.assertEqual(dry_run["importable"], 2)
            self.assertEqual(dry_run["imported"], 0)
            self.assertTrue(all(not group.get("review") for group in groups_after_dry_run))
            self.assertEqual(imported["imported"], 2)
            decisions = {group["label"]: group["review"]["decision"] for group in groups}
            self.assertEqual(decisions["Alpha"], "confirmed")
            self.assertEqual(decisions["Beta"], "reject")
            self.assertIn("project_reviewed", events)

    def test_low_surface_internal_contract_skips_offchain_lookup(self):
        from alpha_listener.cli import offchain_skip_reason

        self.assertEqual(
            offchain_skip_reason(
                {
                    "kind": "contract",
                    "verified": False,
                    "name": None,
                    "symbol": None,
                    "contract_name": "",
                    "discovery_source": "internal_create2",
                    "observation_sources": "internal_create2",
                }
            ),
            "low_surface_unidentified_contract",
        )
        self.assertEqual(
            offchain_skip_reason(
                {
                    "kind": "contract",
                    "verified": True,
                    "name": None,
                    "symbol": None,
                    "contract_name": "SafeProxy",
                    "discovery_source": "internal_create2",
                    "observation_sources": "internal_create2",
                }
            ),
            "infrastructure_contract_artifact",
        )
        self.assertIsNone(
            offchain_skip_reason(
                {
                    "kind": "contract",
                    "verified": False,
                    "name": None,
                    "symbol": None,
                    "contract_name": "",
                    "discovery_source": "mint_transfer",
                    "observation_sources": "internal_create2,mint_transfer",
                }
            )
        )
        self.assertIsNone(
            offchain_skip_reason(
                {
                    "kind": "contract",
                    "verified": True,
                    "name": None,
                    "symbol": None,
                    "contract_name": "SafeProxy",
                    "discovery_source": "internal_create2",
                    "observation_sources": "internal_create2,mint_transfer",
                }
            )
        )
        self.assertIsNone(
            offchain_skip_reason(
                {
                    "kind": "contract",
                    "verified": False,
                    "name": None,
                    "symbol": None,
                    "contract_name": "",
                    "discovery_source": "internal_create2",
                    "observation_sources": "internal_create2",
                    "classification_deferred": True,
                }
            )
        )
        self.assertIsNone(
            offchain_skip_reason(
                {
                    "kind": "contract",
                    "verified": False,
                    "name": None,
                    "symbol": None,
                    "contract_name": "",
                    "discovery_source": "internal_create2",
                    "observation_sources": "internal_create2",
                    "classification_error": "temporary etherscan failure",
                }
            )
        )

    def test_skip_low_surface_backlog_marks_only_creation_only_contracts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(address, source, block, *, name=None, symbol=None, verified=False, status=None, deferred=False):
                item = {
                    "address": address,
                    "tx_hash": f"{source}:{address}:{block}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": source,
                    "log_index": block,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "contract",
                    "name": name,
                    "symbol": symbol,
                    "decimals": None,
                    "total_supply": None,
                    "verified": verified,
                    "contract_name": "",
                    "source_len": 0,
                    "confidence": 0.05,
                }
                if deferred:
                    item["classification_deferred"] = True
                store.upsert_contract(item)
                if status:
                    store.upsert_enrichment(item["address"], {"queries": [], "evidence": {}, "score": {}, "status": status})
                return item

            low = contract("0x1111111111111111111111111111111111111111", "internal_create2", 1)
            direct = contract("0x2222222222222222222222222222222222222222", "contract_creation", 2)
            mint = contract("0x3333333333333333333333333333333333333333", "internal_create2", 3)
            store.upsert_contract({**mint, "tx_hash": "mint:3", "discovery_source": "mint_transfer", "log_index": 9})
            contract("0x4444444444444444444444444444444444444444", "internal_create2", 4, name="Alpha")
            contract("0x5555555555555555555555555555555555555555", "internal_create2", 5, status="processing")
            deferred = contract("0x6666666666666666666666666666666666666666", "internal_create2", 6, deferred=True)

            dry_run = store.low_surface_backlog_candidates(10)
            result = store.skip_low_surface_backlog(10)
            rows = {
                row["address"]: row
                for row in store.conn.execute("SELECT address, status, tier, score, evidence_json FROM enrichments").fetchall()
            }
            queue = store.queue_health()
            store.close()

            self.assertEqual({row["address"] for row in dry_run}, {low["address"], direct["address"]})
            self.assertEqual(result["skipped"], 2)
            self.assertEqual(rows[low["address"]]["status"], "ok")
            self.assertEqual(rows[low["address"]]["tier"], "low")
            self.assertEqual(rows[direct["address"]]["tier"], "low")
            self.assertNotIn(mint["address"], rows)
            self.assertNotIn(deferred["address"], rows)
            evidence = json.loads(rows[low["address"]]["evidence_json"])
            self.assertEqual(evidence["skip_reason"], "low_surface_unidentified_contract")
            self.assertEqual(queue["low_surface_pending"], 0)
            self.assertEqual(queue["processing"], 1)

    def test_skipped_low_surface_contract_retries_when_high_signal_observation_arrives(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            address = "0x1111111111111111111111111111111111111111"
            base = {
                "address": address,
                "tx_hash": "internal:1",
                "origin_tx_hash": "0xaaa",
                "discovery_source": "internal_create2",
                "log_index": 1,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "contract",
                "name": None,
                "symbol": None,
                "decimals": None,
                "total_supply": None,
                "verified": False,
                "contract_name": "",
                "source_len": 0,
                "confidence": 0.05,
            }
            store.upsert_contract(base)
            store.skip_low_surface_backlog(10)
            store.upsert_contract(
                {
                    **base,
                    "tx_hash": "mint:2",
                    "origin_tx_hash": "0xbbb",
                    "discovery_source": "mint_transfer",
                    "log_index": 2,
                    "block_number": 2,
                    "kind": "erc20",
                    "name": "Alpha",
                    "symbol": "ALPHA",
                }
            )
            row = store.conn.execute("SELECT status FROM enrichments WHERE address = ?", (address,)).fetchone()
            queue = store.queue_health()
            store.close()

            self.assertEqual(row["status"], "retry")
            self.assertEqual(queue["pending_by_bucket"]["mint"]["contracts"], 1)

    def test_classify_backlog_classifies_before_skip_and_releases_real_projects(self):
        from alpha_listener.cli import handle_classify_backlog

        low_address = "0x1111111111111111111111111111111111111111"
        token_address = "0x2222222222222222222222222222222222222222"
        fail_address = "0x3333333333333333333333333333333333333333"

        class FakeEtherscan:
            def __init__(self, *_args, **_kwargs):
                pass

            def eth_call(self, address, data):
                if address == fail_address:
                    raise RuntimeError("temporary etherscan failure")
                if address == token_address and data == "0x06fdde03":
                    return "0x" + f"{32:064x}" + f"{5:064x}" + "416c706861" + "0" * 54
                if address == token_address and data == "0x95d89b41":
                    return "0x" + f"{32:064x}" + f"{3:064x}" + "414c50" + "0" * 58
                if address == token_address and data == "0x313ce567":
                    return "0x" + f"{18:064x}"
                if address == token_address and data == "0x18160ddd":
                    return "0x" + f"{1000:064x}"
                return "0x" + "0" * 64

            def get_source_code(self, _address):
                return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)

            def contract(address, block):
                return {
                    "address": address,
                    "tx_hash": f"internal:{address}",
                    "origin_tx_hash": f"0x{block:064x}",
                    "discovery_source": "internal_create2",
                    "log_index": block,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": block,
                    "block_timestamp": block,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
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

            for index, address in enumerate((low_address, token_address, fail_address), start=1):
                store.upsert_contract(contract(address, index))
            store.close()

            settings = SimpleNamespace(
                workspace=root,
                db_path=root / "alpha.sqlite",
                data_dir=root,
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="test",
                chainid="1",
                request_timeout_seconds=1,
            )
            with patch("alpha_listener.cli.EtherscanClient", FakeEtherscan):
                result = handle_classify_backlog(settings, SimpleNamespace(limit=10, dry_run=False))

            store = Store(root / "alpha.sqlite", root)
            rows = {
                row["address"]: row
                for row in store.conn.execute(
                    "SELECT c.address, c.name, c.symbol, c.raw_json, e.status, e.tier, e.evidence_json "
                    "FROM contracts c LEFT JOIN enrichments e ON e.address = c.address"
                ).fetchall()
            }
            store.close()

            self.assertEqual(result["reserved"], 3)
            self.assertEqual(result["classified"], 2)
            self.assertEqual(result["skipped_low_surface"], 1)
            self.assertEqual(result["needs_offchain_enrichment"], 1)
            self.assertEqual(result["classification_errors"], 1)
            self.assertEqual(rows[low_address]["status"], "ok")
            self.assertEqual(rows[low_address]["tier"], "low")
            self.assertEqual(json.loads(rows[low_address]["evidence_json"])["skip_reason"], "low_surface_unidentified_contract")
            self.assertEqual(rows[token_address]["status"], "retry")
            self.assertEqual(rows[token_address]["name"], "Alpha")
            self.assertEqual(rows[token_address]["symbol"], "ALP")
            self.assertNotIn("classification_deferred", rows[token_address]["raw_json"])
            self.assertEqual(rows[fail_address]["status"], "retry")
            self.assertIn("classification_deferred", rows[fail_address]["raw_json"])

            store = Store(root / "alpha.sqlite", root)
            runtime = store.runtime_status()
            store.close()
            classifier = runtime["roles"]["classifier"]
            self.assertEqual(classifier["last_cycle_status"], "ok")
            self.assertIn("code_fingerprint", classifier["last_cycle_context"])
            self.assertRegex(classifier["last_cycle_context"]["code_fingerprint"]["digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(classifier["last_cycle_result"]["reserved"], 3)
            self.assertEqual(classifier["last_cycle_result"]["skipped_low_surface"], 1)
            self.assertEqual(classifier["last_cycle_result"]["needs_offchain_enrichment"], 1)
            self.assertEqual(classifier["last_cycle_result"]["classification_errors"], 1)

    def test_backfill_source_urls_updates_raw_json_and_requeues_matching_completed_enrichment(self):
        from alpha_listener.cli import handle_backfill_source_urls

        address = "0x1111111111111111111111111111111111111111"

        class FakeEtherscan:
            def __init__(self, *_args, **_kwargs):
                pass

            def get_source_code(self, _address):
                return {
                    "SourceCode": "\n".join(
                        [
                            "// Website: https://moltenbear.xyz/",
                            "// Dev: https://github.com/example/moltenbear",
                            "contract MoltenBearToken {}",
                        ]
                    ),
                    "CompilerVersion": "v0.8.24",
                    "OptimizationUsed": "1",
                    "LicenseType": "MIT",
                }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            store.upsert_contract(
                {
                    "address": address,
                    "tx_hash": "direct:1",
                    "origin_tx_hash": "0xaaa",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x2222222222222222222222222222222222222222",
                    "block_number": 1,
                    "block_timestamp": 1,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": "MoltenBear",
                    "symbol": "MLTB",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "MoltenBearToken",
                    "source_len": 5000,
                    "confidence": 0.9,
                    "source": {"compiler": "old"},
                }
            )
            store.upsert_enrichment(
                address,
                {"queries": [], "evidence": {}, "score": {"score": 60, "tier": "watch"}, "status": "ok"},
            )
            store.close()

            settings = SimpleNamespace(
                db_path=root / "alpha.sqlite",
                data_dir=root,
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="test",
                chainid="1",
                request_timeout_seconds=1,
            )
            with patch("alpha_listener.cli.EtherscanClient", FakeEtherscan):
                result = handle_backfill_source_urls(settings, SimpleNamespace(limit=10, dry_run=False))

            store = Store(root / "alpha.sqlite", root)
            row = store.conn.execute(
                "SELECT raw_json, status FROM contracts c JOIN enrichments e ON e.address = c.address WHERE c.address = ?",
                (address,),
            ).fetchone()
            store.close()

            raw = json.loads(row["raw_json"])
            self.assertEqual(result["matched"], 1)
            self.assertEqual(result["refreshed"], 1)
            self.assertEqual(result["with_source_urls"], 1)
            self.assertEqual(result["matched_source_websites"], 1)
            self.assertEqual(result["requeued"], 1)
            self.assertEqual(raw["source"]["urls"], ["https://moltenbear.xyz"])
            self.assertEqual(raw["source"]["compiler"], "v0.8.24")
            self.assertEqual(row["status"], "retry")

    def test_run_cycle_backfills_source_urls_as_maintenance(self):
        from alpha_listener.cli import run_cycle
        from alpha_listener.config import Settings

        address = "0x2222222222222222222222222222222222222222"

        class FakeEtherscan:
            def __init__(self, *_args, **_kwargs):
                pass

            def latest_block_number(self):
                return 120

            def get_source_code(self, _address):
                return {
                    "SourceCode": "\n".join(
                        [
                            "// Website: https://moltenbear.xyz",
                            "contract MoltenBearToken {}",
                        ]
                    ),
                    "CompilerVersion": "v0.8.24",
                    "OptimizationUsed": "1",
                    "LicenseType": "MIT",
                }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            store.upsert_contract(
                {
                    "address": address,
                    "tx_hash": "direct:2",
                    "origin_tx_hash": "0xbbb",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x3333333333333333333333333333333333333333",
                    "block_number": 2,
                    "block_timestamp": 2,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": "MoltenBear",
                    "symbol": "MLTB",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "MoltenBearToken",
                    "source_len": 5000,
                    "confidence": 0.9,
                    "source": {"compiler": "old"},
                }
            )
            store.upsert_enrichment(
                address,
                {"queries": [], "evidence": {}, "score": {"score": 61, "tier": "watch"}, "status": "ok"},
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            args = SimpleNamespace(
                command="once",
                no_twitter=True,
                confirmations=None,
                lookback_blocks=None,
                max_blocks=0,
                enrich_limit=0,
                force_enrich=False,
                report_limit=0,
                report_every_enriched=None,
                start_block=None,
                end_block=None,
                verify_websites_limit=0,
                backfill_website_twitter_limit=0,
                backfill_source_urls_limit=1,
            )

            with patch("alpha_listener.cli.EtherscanClient", FakeEtherscan):
                result = run_cycle(settings, args)

            store = Store(root / "alpha.sqlite", root)
            row = store.conn.execute(
                "SELECT raw_json, status FROM contracts c JOIN enrichments e ON e.address = c.address WHERE c.address = ?",
                (address,),
            ).fetchone()
            runtime = store.runtime_status()
            events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            store.close()

            raw = json.loads(row["raw_json"])

        self.assertEqual(result["source_url_backfill"]["matched"], 1)
        self.assertEqual(result["source_url_backfill"]["refreshed"], 1)
        self.assertEqual(result["source_url_backfill"]["matched_source_websites"], 1)
        self.assertEqual(result["source_url_backfill"]["requeued"], 1)
        self.assertEqual(raw["source"]["urls"], ["https://moltenbear.xyz"])
        self.assertEqual(row["status"], "retry")
        self.assertEqual(runtime["roles"]["maintenance"]["last_cycle_result"]["source_url_backfill"]["requeued"], 1)
        self.assertTrue(
            any(
                event["event_type"] == "cycle_progress"
                and event["payload"].get("event") == "source_url_backfill_completed"
                and event["payload"].get("requeued") == 1
                for event in events
            )
        )

    def test_run_cycle_enriches_source_url_requeues_in_same_cycle(self):
        from alpha_listener.cli import run_cycle
        from alpha_listener.config import Settings

        address = "0x3333333333333333333333333333333333333333"

        class FakeEtherscan:
            def __init__(self, *_args, **_kwargs):
                pass

            def latest_block_number(self):
                return 120

            def get_source_code(self, _address):
                return {
                    "SourceCode": "\n".join(
                        [
                            "// Website: https://moltenbear.xyz",
                            "contract MoltenBearToken {}",
                        ]
                    ),
                    "CompilerVersion": "v0.8.24",
                    "OptimizationUsed": "1",
                    "LicenseType": "MIT",
                }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            store.upsert_contract(
                {
                    "address": address,
                    "tx_hash": "direct:3",
                    "origin_tx_hash": "0xccc",
                    "discovery_source": "contract_creation",
                    "log_index": None,
                    "related": {},
                    "deployer": "0x4444444444444444444444444444444444444444",
                    "block_number": 3,
                    "block_timestamp": 3,
                    "tx_index": 0,
                    "value_wei": "0",
                    "input_prefix": "",
                    "kind": "erc20",
                    "name": "MoltenBear",
                    "symbol": "MLTB",
                    "decimals": 18,
                    "total_supply": "1000",
                    "verified": True,
                    "contract_name": "MoltenBearToken",
                    "source_len": 5000,
                    "confidence": 0.9,
                    "source": {"compiler": "old"},
                }
            )
            store.upsert_enrichment(
                address,
                {"queries": [], "evidence": {}, "score": {"score": 61, "tier": "watch"}, "status": "ok"},
            )
            store.close()

            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="etherscan-key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter-key",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            args = SimpleNamespace(
                command="once",
                no_twitter=False,
                confirmations=None,
                lookback_blocks=None,
                max_blocks=0,
                enrich_limit=1,
                force_enrich=False,
                report_limit=0,
                report_every_enriched=None,
                start_block=None,
                end_block=None,
                verify_websites_limit=0,
                backfill_website_twitter_limit=0,
                backfill_source_urls_limit=1,
            )
            seen_contracts = []

            def fake_enrich(_twitter, contract, _max_results):
                seen_contracts.append(contract)
                evidence = {
                    "twitter_account": None,
                    "website": "https://moltenbear.xyz",
                    "tweet_count": 0,
                    "address_mentions": 0,
                    "credible_address_mentions": 0,
                    "official_website_reason": "verified_source_url",
                }
                return {"queries": [], "evidence": evidence, "score": score_project(contract, evidence), "status": "ok"}

            with patch("alpha_listener.cli.EtherscanClient", FakeEtherscan), patch(
                "alpha_listener.cli.OpenTwitterClient"
            ), patch("alpha_listener.cli.enrich_contract", side_effect=fake_enrich):
                result = run_cycle(settings, args)

            store = Store(root / "alpha.sqlite", root)
            row = store.conn.execute(
                "SELECT raw_json, status, website FROM contracts c JOIN enrichments e ON e.address = c.address WHERE c.address = ?",
                (address,),
            ).fetchone()
            store.close()
            raw = json.loads(row["raw_json"])

        self.assertEqual(result["source_url_backfill"]["requeued"], 1)
        self.assertEqual(result["enriched_contracts"], 1)
        self.assertEqual(len(seen_contracts), 1)
        self.assertEqual(seen_contracts[0]["source"]["urls"], ["https://moltenbear.xyz"])
        self.assertEqual(raw["source"]["urls"], ["https://moltenbear.xyz"])
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["website"], "https://moltenbear.xyz")

    def test_run_loop_sleeps_fast_while_catching_up(self):
        from alpha_listener.cli import catchup_remaining_blocks, cycle_sleep_seconds

        self.assertEqual(catchup_remaining_blocks({"last_scanned_block": 90}, 104), 14)
        fast = cycle_sleep_seconds(
            {
                "catchup_remaining_blocks": 14,
                "scanned_blocks": 10,
                "enriched_contracts": 0,
                "summary": {"pending_enrichment": 0},
            },
            interval=300,
        )
        self.assertEqual(fast, 1)
        normal = cycle_sleep_seconds(
            {
                "catchup_remaining_blocks": 0,
                "scanned_blocks": 0,
                "enriched_contracts": 0,
                "summary": {"pending_enrichment": 0},
            },
            interval=300,
        )
        self.assertEqual(normal, 300)

    def test_report_writer_creates_markdown(self):
        from alpha_listener.cli import write_report
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Alpha Protocol",
                "symbol": "ALP",
                "decimals": 18,
                "total_supply": "1000",
                "verified": True,
                "contract_name": "Alpha",
                "source_len": 5000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": ["Alpha Protocol"],
                    "evidence": {
                        "twitter_account": "AlphaProtocol",
                        "twitter_name": "Alpha Protocol",
                        "twitter_followers": 1000,
                        "twitter_verified": False,
                        "website": "https://alpha.example",
                        "tweet_count": 2,
                        "address_mentions": 1,
                    },
                    "score": {"score": 72, "tier": "high", "breakdown": {"identity_resolution": 30}},
                    "status": "ok",
                },
            )
            groups_before_review = store.project_groups(10)
            store.add_project_review(
                groups_before_review[0]["project_key"],
                "confirmed",
                "tester",
                "looks real",
                groups_before_review[0],
            )
            store.close()
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            path = write_report(settings, 10)
            text = path.read_text(encoding="utf-8")
            groups = json.loads((root / "latest_project_groups.json").read_text(encoding="utf-8"))
            self.assertIn("## Project Groups", text)
            self.assertIn("## Contract Candidates", text)
            self.assertIn("Alpha Protocol", text)
            self.assertIn("@AlphaProtocol", text)
            self.assertIn("confirmed: looks real", text)
            self.assertEqual(groups[0]["label"], "Alpha Protocol")
            self.assertEqual(groups[0]["review"]["decision"], "confirmed")

    def test_review_command_marks_project_by_address(self):
        from alpha_listener.cli import handle_review
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Alpha Protocol",
                "symbol": "ALP",
                "decimals": 18,
                "total_supply": "1000",
                "verified": True,
                "contract_name": "Alpha",
                "source_len": 5000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": ["Alpha Protocol"],
                    "evidence": {"twitter_account": "AlphaProtocol", "tweet_count": 1},
                    "score": {"score": 72, "tier": "high", "breakdown": {}},
                    "status": "ok",
                },
            )
            store.close()
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            result = handle_review(
                settings,
                SimpleNamespace(
                    limit=10,
                    tier=None,
                    unreviewed=False,
                    project_key=None,
                    address=contract["address"],
                    decision="watchlist",
                    reviewer="tester",
                    note="monitor",
                ),
            )
            listed = handle_review(
                settings,
                SimpleNamespace(
                    limit=10,
                    tier=None,
                    unreviewed=False,
                    project_key=None,
                    address=None,
                    decision=None,
                    reviewer="tester",
                    note="",
                ),
            )
            filtered = handle_review(
                settings,
                SimpleNamespace(
                    limit=10,
                    tier=None,
                    unreviewed=False,
                    project_key=result["reviewed"]["project_key"],
                    address=None,
                    decision=None,
                    reviewer="tester",
                    note="",
                ),
            )

            self.assertEqual(result["reviewed"]["decision"], "watchlist")
            self.assertEqual(listed["projects"][0]["review"]["decision"], "watchlist")
            self.assertEqual(len(filtered["projects"]), 1)
            self.assertEqual(filtered["projects"][0]["project_key"], result["reviewed"]["project_key"])
            self.assertTrue((root / "reviews.jsonl").exists())

    def test_snapshot_report_uses_matching_project_groups_file(self):
        from alpha_listener.cli import project_groups_report_path, safe_report_label, snapshot_report_path, write_report
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Alpha Protocol",
                "symbol": "ALP",
                "decimals": 18,
                "total_supply": "1000",
                "verified": True,
                "contract_name": "Alpha",
                "source_len": 5000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": ["Alpha Protocol"],
                    "evidence": {"twitter_account": "AlphaProtocol", "tweet_count": 1},
                    "score": {"score": 72, "tier": "high", "breakdown": {}},
                    "status": "ok",
                },
            )
            store.close()
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=0,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            output = snapshot_report_path(settings, "2026/05/17 00:05")
            path = write_report(settings, 10, output)
            groups_path = project_groups_report_path(path)

            self.assertEqual(safe_report_label("2026/05/17 00:05"), "2026-05-17-00-05")
            self.assertEqual(path, root / "reports" / "2026-05-17-00-05_report.md")
            self.assertEqual(groups_path, root / "reports" / "2026-05-17-00-05_report_project_groups.json")
            self.assertTrue(path.exists())
            self.assertTrue(groups_path.exists())
            self.assertIn(str(groups_path), path.read_text(encoding="utf-8"))

    def test_daily_snapshot_writes_once_per_label(self):
        from alpha_listener.cli import maybe_write_daily_snapshot
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "uniswap_v4_initialize",
                "log_index": 1,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Alpha Protocol",
                "symbol": "ALP",
                "decimals": 18,
                "total_supply": "1000",
                "verified": True,
                "contract_name": "Alpha",
                "source_len": 5000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": ["Alpha Protocol"],
                    "evidence": {"twitter_account": "AlphaProtocol", "tweet_count": 1},
                    "score": {"score": 72, "tier": "high", "breakdown": {}},
                    "status": "ok",
                },
            )
            store.close()
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=5,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )

            first = maybe_write_daily_snapshot(settings, 10, label="daily-2026-05-17")
            second = maybe_write_daily_snapshot(settings, 10, label="daily-2026-05-17")
            groups_path = root / "reports" / "daily-2026-05-17_report_project_groups.json"
            store = Store(root / "alpha.sqlite", root)
            try:
                saved_label = store.get_meta("last_daily_snapshot_label")
            finally:
                store.close()

            self.assertEqual(first, root / "reports" / "daily-2026-05-17_report.md")
            self.assertIsNone(second)
            self.assertTrue(first.exists())
            self.assertTrue(groups_path.exists())
            self.assertEqual(saved_label, "daily-2026-05-17")

    def test_cycle_report_refreshes_after_changed_cycle_when_configured(self):
        from alpha_listener.cli import maybe_write_cycle_report
        from alpha_listener.config import Settings

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "alpha.sqlite", root)
            contract = {
                "address": "0x1111111111111111111111111111111111111111",
                "tx_hash": "direct:1",
                "origin_tx_hash": "0xabc",
                "discovery_source": "contract_creation",
                "log_index": None,
                "related": {},
                "deployer": "0x2222222222222222222222222222222222222222",
                "block_number": 1,
                "block_timestamp": 1,
                "tx_index": 0,
                "value_wei": "0",
                "input_prefix": "",
                "kind": "erc20",
                "name": "Alpha Protocol",
                "symbol": "ALP",
                "decimals": 18,
                "total_supply": "1000",
                "verified": True,
                "contract_name": "Alpha",
                "source_len": 5000,
                "confidence": 0.9,
            }
            store.upsert_contract(contract)
            store.upsert_enrichment(
                contract["address"],
                {
                    "queries": ["Alpha Protocol"],
                    "evidence": {"twitter_account": "AlphaProtocol", "tweet_count": 1},
                    "score": {"score": 72, "tier": "high", "breakdown": {}},
                    "status": "ok",
                },
            )
            store.close()
            settings = Settings(
                workspace=root,
                chainid="1",
                etherscan_api_base="https://example.invalid",
                etherscan_api_key="key",
                opentwitter_api_base="https://example.invalid",
                opentwitter_api_key="twitter",
                data_dir=root,
                db_path=root / "alpha.sqlite",
                confirmations=6,
                interval_seconds=300,
                lookback_blocks=10,
                max_blocks_per_cycle=10,
                enrich_limit_per_cycle=5,
                report_limit_per_cycle=5,
                report_every_enriched=10,
                twitter_max_results=10,
                max_log_candidates_per_block=25,
                max_internal_candidates_per_block=50,
                new_contract_max_age_blocks=7200,
                request_timeout_seconds=1,
            )
            result = {"observed_contracts": 0, "enriched_contracts": 1}
            path = maybe_write_cycle_report(settings, SimpleNamespace(report_limit=None, command="run"), result)
            self.assertEqual(path, root / "latest_report.md")
            self.assertEqual(result["report"], str(root / "latest_report.md"))
            self.assertTrue((root / "latest_project_groups.json").exists())

    def test_enrichment_report_reason_refreshes_on_alpha_tiers(self):
        from alpha_listener.cli import enrichment_report_reason

        self.assertEqual(enrichment_report_reason(1, {"tier": "high"}, 10), "tier_high")
        self.assertEqual(enrichment_report_reason(2, {"tier": "medium"}, 10), "tier_medium")
        self.assertEqual(enrichment_report_reason(10, {"tier": "low"}, 10), "cadence")
        self.assertIsNone(enrichment_report_reason(9, {"tier": "watch"}, 10))
        self.assertIsNone(enrichment_report_reason(10, {"tier": "watch"}, 0))

    def test_etherscan_retryable_api_error_detection(self):
        from alpha_listener.etherscan import EtherscanClient

        data = {"status": "0", "message": "Unexpected error, timeout or server too busy. Please try again later", "result": None}
        self.assertTrue(EtherscanClient._is_retryable_api_error(data))
        self.assertFalse(EtherscanClient._is_retryable_api_error({"status": "0", "message": "No records found", "result": []}))

    def test_http_error_url_redacts_secret_params(self):
        url = "https://api.example.test?module=logs&apikey=secret&token=also-secret&address=0x1"
        redacted = _redact_url(url)
        self.assertIn("apikey=%2A%2A%2A", redacted)
        self.assertIn("token=%2A%2A%2A", redacted)
        self.assertNotIn("secret", redacted)

    def test_print_text_ignores_unavailable_output_handle(self):
        from alpha_listener.cli import print_text

        with patch("builtins.print", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            print_text("progress payload")

        with patch("builtins.print", side_effect=BrokenPipeError(errno.EPIPE, "Broken pipe")):
            print_text("progress payload")

        with patch("builtins.print", side_effect=OSError(errno.EIO, "IO error")):
            with self.assertRaises(OSError):
                print_text("progress payload")


if __name__ == "__main__":
    unittest.main()
