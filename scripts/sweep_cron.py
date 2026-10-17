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

from pricewatch import catalog  # noqa: E402
from pricewatch.registry import Registry  # noqa: E402

THROTTLE_HOURS = 24
LATCH = Path(os.environ.get("PRICEWATCH_LATCH", Path.home() / ".hermes" / "state"
                            / "pricewatch_sweep.latch"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def signature_of(drift: list[tuple], unverifiable: list[tuple]) -> str:
    parts = [f"{w.key}::{'|'.join(r) if isinstance(r, list) else r}" for w, r in drift]
    parts += [f"{w.key}::unverifiable::{e}" for w, e in unverifiable]
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


def main() -> int:
    reg = Registry.load()
    if not reg.watches:
        log("pricewatch: registry empty — nothing to sweep")
        return 0

    drift, unverifiable = [], []
    for w in reg.watches:
        try:
            data = catalog.fetch_endpoints(w.model)
            endpoint = catalog.find_endpoint(data, w.provider, w.tag)
            if endpoint is None:
                drift.append((w, f"provider {w.provider}"
                                 + (f"/{w.tag}" if w.tag else "")
                                 + " no longer serves this model"))
                continue
            live = catalog.snapshot(endpoint)
            if not w.baseline:
                drift.append((w, "no accepted baseline (run `pricewatch accept`)"))
                continue
            reasons = catalog.compare(w.baseline, live)
            if reasons:
                drift.append((w, reasons, live))
        except catalog.Unverifiable as exc:
            unverifiable.append((w, str(exc)))

    if not drift and not unverifiable:
        log(f"pricewatch: nominal — {len(reg.watches)} watch(es) unchanged")
        return 0

    sig = signature_of([(w, r) for w, r, *_ in drift], unverifiable)
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

    lines = ["*pricewatch* — watched OpenRouter endpoint changed"]
    for row in drift:
        w, reasons = row[0], row[1]
        live = row[2] if len(row) > 2 else None
        why = "; ".join(reasons) if isinstance(reasons, list) else reasons
        lines.append(f"• HOLD `{w.job}` — {w.model} @ {w.provider}"
                     + (f"/{w.tag}" if w.tag else ""))
        lines.append(f"    {why}")
        if live:
            b = w.baseline or {}
            lines.append(f"    accepted {per_million(b.get('prompt'))} in / "
                         f"{per_million(b.get('completion'))} out (discount {b.get('discount')})"
                         f"  ->  now {per_million(live['prompt'])} in / "
                         f"{per_million(live['completion'])} out (discount {live['discount']})")
    for w, err in unverifiable:
        lines.append(f"• CANNOT VERIFY `{w.job}` — {w.model} @ {w.provider}: {err}")
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
