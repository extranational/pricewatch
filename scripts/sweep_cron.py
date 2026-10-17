#!/usr/bin/env python3
"""Cron entry point: sweep every watch, alert ONLY when something changed.

Written to the `no_agent` cron contract (see the cron-alert-watchdogs skill):

  stdout empty + exit 0  -> silent. The normal steady state.
  stdout text  + exit 0  -> delivered verbatim to the cron's `deliver` target.
  stdout text  + exit >0 -> delivered as an *error* alert.

So: a detected change is the *delivery* (exit 0 — it is not a failure), diagnostics go to
stderr, and only a crash of the sweep itself exits non-zero.

Dedupe/throttle: a drift signature is latched. The same unchanged condition re-alerts at most
once per THROTTLE_HOURS, because a promo that ends stays ended until a human re-accepts — a
fire-once latch would make the second and subsequent 6-hourly sweeps silent forever.

Watch-mode dispatch lives in `pricewatch.check`, so this script and the CLI can never disagree
about what counts as drift (pinned endpoint vs the floor of an unpinned model).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pricewatch.check import check_watch, is_aggregate  # noqa: E402
from pricewatch.registry import Registry  # noqa: E402

THROTTLE_HOURS = 24
LATCH = Path(os.environ.get("PRICEWATCH_LATCH", Path.home() / ".hermes" / "state"
                            / "pricewatch_sweep.latch"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def signature_of(results: list) -> str:
    parts = []
    for r in results:
        for reason in r.reasons:
            parts.append(f"{r.watch.key}::drift::{reason}")
        if r.status == "unverifiable":
            parts.append(f"{r.watch.key}::unverifiable::{r.error}")
    return hashlib.sha1("\n".join(sorted(parts)).encode()).hexdigest()[:16]


def read_latch() -> dict:
    try:
        return json.loads(LATCH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent/corrupt latch = never alerted
        return {}


def write_latch(sig: str) -> None:
    LATCH.parent.mkdir(parents=True, exist_ok=True)
    LATCH.write_text(json.dumps({"signature": sig,
                                 "alerted_at": datetime.now(timezone.utc).isoformat(
                                     timespec="seconds")}, indent=2) + "\n", encoding="utf-8")


def per_million(v) -> str:
    if v in (None, ""):
        return "-"
    return f"${Decimal(str(v)) * Decimal(1_000_000):.4f}/M"


def describe(w, live: dict) -> str:
    if is_aggregate(w):
        return (f"floor {per_million(live.get('min_prompt'))} in "
                f"({live.get('min_prompt_provider') or '?'}) / "
                f"{per_million(live.get('min_completion'))} out "
                f"({live.get('min_completion_provider') or '?'}); "
                f"{live.get('endpoints')} endpoints")
    return (f"{per_million(live.get('prompt'))} in / {per_million(live.get('completion'))} out "
            f"(discount {live.get('discount')})")


def target_of(w) -> str:
    if is_aggregate(w):
        return "any endpoint (unpinned: watching the price floor)"
    return w.provider + (f"/{w.tag}" if w.tag else "")


def main() -> int:
    reg = Registry.load()
    if not reg.watches:
        log("pricewatch: registry empty — nothing to sweep")
        return 0

    results = [check_watch(w) for w in reg.watches]
    drift = [r for r in results if r.status == "drift"]
    unknown = [r for r in results if r.status == "unverifiable"]
    notes = [(r, note) for r in results for note in r.notes]

    if not drift and not unknown:
        for r, note in notes:
            log(f"pricewatch: {r.watch.key}: {note}")
        log(f"pricewatch: nominal — {len(reg.watches)} watch(es) unchanged")
        return 0

    sig = signature_of(results)
    latch = read_latch()
    if latch.get("signature") == sig:
        try:
            last = datetime.fromisoformat(latch.get("alerted_at", ""))
        except ValueError:
            last = None
        if last and datetime.now(timezone.utc) - last < timedelta(hours=THROTTLE_HOURS):
            log(f"pricewatch: same condition ({sig}) already alerted {latch.get('alerted_at')} "
                f"— throttled")
            return 0

    lines = ["*pricewatch* — watched OpenRouter endpoint changed" if drift
             else "*pricewatch* — catalog unreachable"]
    for r in drift:
        lines.append(f"• HOLD `{r.watch.job}` — {r.watch.model} @ {target_of(r.watch)}")
        lines.append(f"    {'; '.join(r.reasons) or 'no baseline'}")
        if r.live:
            lines.append(f"    accepted {describe(r.watch, r.watch.baseline)}  ->  now "
                         f"{describe(r.watch, r.live)}")
    for r in unknown:
        lines.append(f"• CANNOT VERIFY `{r.watch.job}` — {r.watch.model}: {r.error}")
    for r, note in notes:
        lines.append(f"• info `{r.watch.job}`: {note}")
    lines.append("Nothing is blocked until a job's step-0 runs. To accept the new price: "
                 "`pricewatch accept --job <job>`.")

    print("\n".join(lines))
    # Latched only because the send above is this process's own stdout — a cron delivery that
    # fails is reported by the scheduler itself, so there is no partial-send state to guard.
    write_latch(sig)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a crashed sweep must be visible, not silent
        print(f"pricewatch sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"*pricewatch* sweep FAILED: {type(exc).__name__}: {exc}")
        raise SystemExit(3)
