"""Offline unit tests. No network: the catalog comparisons are pure functions on payloads."""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pricewatch.catalog import aggregate_snapshot, compare, compare_aggregate, find_endpoint, snapshot
from pricewatch.check import CheckResult, baseline_from, is_aggregate
from pricewatch.discover import _skip
from pricewatch.registry import Registry, Watch


ENDPOINTS = {
    "id": "deepseek/deepseek-v4-pro-0813",
    "endpoints": [
        {"provider_name": "Baidu", "tag": "baidu/fp8",
         "pricing": {"prompt": "0.00000013332", "completion": "0.00000039996", "discount": 0.899}},
        {"provider_name": "Baidu", "tag": "baidu/fp4",
         "pricing": {"prompt": "0.0000002", "completion": "0.0000009", "discount": 0}},
        {"provider_name": "StreamLake", "tag": "streamlake",
         "pricing": {"prompt": "0.00000066", "completion": "0.00000198", "discount": 0.5}},
    ],
}


class TestFindEndpoint(unittest.TestCase):
    def test_tag_disambiguates_same_provider(self):
        ep = find_endpoint(ENDPOINTS, "Baidu", "baidu/fp8")
        self.assertEqual(ep["pricing"]["prompt"], "0.00000013332")

    def test_falls_back_to_first_endpoint_without_tag(self):
        ep = find_endpoint(ENDPOINTS, "Baidu")
        self.assertEqual(ep["tag"], "baidu/fp8")

    def test_missing_provider_is_none(self):
        self.assertIsNone(find_endpoint(ENDPOINTS, "Together", "together/fp8"))

    def test_tagged_pin_that_vanished_is_none_not_a_wrong_match(self):
        # Baidu still serves the model, but no longer on the pinned fp4 tier.
        self.assertIsNone(find_endpoint(ENDPOINTS, "Baidu", "baidu/int8"))

    def test_provider_match_is_case_insensitive(self):
        self.assertIsNotNone(find_endpoint(ENDPOINTS, "streamlake", "streamlake"))


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.baseline = snapshot(find_endpoint(ENDPOINTS, "StreamLake", "streamlake"))

    def test_identical_is_no_drift(self):
        live = snapshot(find_endpoint(ENDPOINTS, "StreamLake", "streamlake"))
        self.assertEqual(compare(self.baseline, live), [])

    def test_decimal_string_vs_float_is_not_drift(self):
        # baseline persisted as a string, live parsed as Decimal => must still compare equal
        self.assertEqual(compare({"prompt": "0.00000066", "completion": 0.00000198,
                                  "discount": 0.5}, self.baseline), [])

    def test_promo_expiry_is_drift(self):
        live = snapshot(find_endpoint(ENDPOINTS, "StreamLake", "streamlake"))
        live["prompt"] = Decimal("0.00000132")
        live["discount"] = Decimal("0")
        reasons = compare(self.baseline, live)
        self.assertTrue(any("promo discount changed" in r for r in reasons))
        self.assertTrue(any("prompt price changed" in r for r in reasons))
        self.assertTrue(any("+100.0%" in r for r in reasons))

    def test_discount_only_change_is_drift(self):
        live = snapshot(find_endpoint(ENDPOINTS, "StreamLake", "streamlake"))
        live["discount"] = Decimal("0")
        reasons = compare(self.baseline, live)
        self.assertEqual(len(reasons), 1)
        self.assertIn("promo discount", reasons[0])


class TestRegistry(unittest.TestCase):
    def test_round_trip_and_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            reg = Registry([Watch(job="pharmawatch", model="deepseek/deepseek-v4-pro-0813",
                                  provider="StreamLake", tag="streamlake",
                                  baseline={"prompt": "0.00000066"})], path)
            reg.save()
            again = Registry.load(path)
            self.assertEqual(again.watches[0].key,
                             "pharmawatch:deepseek/deepseek-v4-pro-0813@StreamLake/streamlake")
            self.assertEqual(again.for_job("pharmawatch")[0].baseline_of("prompt"), "0.00000066")
            self.assertIsNone(again.find("nope"))

    def test_slug_is_filesystem_safe(self):
        w = Watch(job="pw", model="deepseek/deepseek-v4-pro-0813", provider="StreamLake",
                  tag="streamlake", baseline={})
        self.assertNotIn("/", w.slug)
        self.assertNotIn(":", w.slug)


class TestAggregateWatch(unittest.TestCase):
    """Unpinned jobs watch the *floor*: any increase or promo change holds, a decrease informs."""

    def setUp(self):
        self.base = aggregate_snapshot(ENDPOINTS)

    def test_floor_is_the_cheapest_endpoint(self):
        self.assertEqual(self.base["min_prompt"], Decimal("0.00000013332"))
        self.assertEqual(self.base["min_prompt_provider"], "Baidu")
        self.assertEqual(self.base["endpoints"], 3)

    def test_unchanged_floor_is_no_drift(self):
        reasons, notes = compare_aggregate(self.base, aggregate_snapshot(ENDPOINTS))
        self.assertEqual(reasons, [])
        self.assertEqual(notes, [])

    def test_floor_rise_holds(self):
        live = dict(self.base, min_completion=Decimal("0.000004"), min_completion_provider="Together")
        reasons, _ = compare_aggregate(self.base, live)
        self.assertTrue(any("ROSE" in r for r in reasons))
        self.assertTrue(any("Baidu -> Together" in r for r in reasons))

    def test_floor_drop_is_a_note_not_a_hold(self):
        live = dict(self.base, min_prompt=Decimal("0.00000001"))
        reasons, notes = compare_aggregate(self.base, live)
        self.assertEqual(reasons, [])
        self.assertTrue(any("cheaper endpoint available" in n for n in notes))

    def test_endpoint_count_change_is_only_a_note(self):
        live = dict(self.base, endpoints=21)
        reasons, notes = compare_aggregate(self.base, live)
        self.assertEqual(reasons, [])
        self.assertIn("endpoint count 3 -> 21", notes)

    def test_mode_is_driven_by_the_provider_field(self):
        agg = Watch(job="b", model="m", provider="*", baseline={})
        pinned = Watch(job="b", model="m", provider="StreamLake", baseline={})
        self.assertTrue(is_aggregate(agg))
        self.assertFalse(is_aggregate(pinned))


class TestBaselineFrom(unittest.TestCase):
    def test_aggregate_baseline_is_json_safe_strings(self):
        w = Watch(job="b", model="m", provider="*", baseline={})
        result = CheckResult(watch=w, status="ok", live=aggregate_snapshot(ENDPOINTS))
        blob = baseline_from(result)
        json.dumps(blob)  # must not raise: everything is a str/int
        self.assertEqual(blob["min_prompt"], "0.00000013332")
        self.assertEqual(blob["endpoints"], 3)


class TestDiscoveryFilter(unittest.TestCase):
    def test_skips_speech_and_artifacts(self):
        for junk in ["provider:", "base", "whisper-1", "gpt-4o-mini-tts",
                     "openai/gpt-4o-mini", "anthropic/claude-3-5-sonnet-20240620"]:
            self.assertTrue(_skip(junk), junk)

    def test_keeps_real_chat_models(self):
        for keep in ["deepseek/deepseek-v4-pro-0813", "qwen/qwen3.7-max", "moonshotai/kimi-k3",
                     "xiaomi/mimo-v2.6-flash"]:
            self.assertFalse(_skip(keep), keep)


if __name__ == "__main__":
    unittest.main()
