"""pricewatch CLI.

Exit codes (stable contract — jobs gate on these):

  0  OK            every watched pin matches its accepted baseline
  1  DRIFT         a price/promo/provider changed; HOLD the run and re-accept deliberately
  2  UNVERIFIABLE  the catalog could not be read; HOLD by default (``--on-unreachable pass``
                   downgrades this to 0 with a warning on stderr)

Usage:
  pricewatch check --job pharmawatch          # step-0 preflight
  pricewatch accept --job pharmawatch         # human re-baseline (the explicit "we accept this")
  pricewatch sweep                            # periodic sweep, prints only on change
  pricewatch list
  pricewatch discover                          # models found in config/usage vs the registry
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from . import catalog
from .check import CheckResult, baseline_from, check_watch, is_aggregate
from .discover import discover as discover_models
from .registry import Registry

EXIT_OK, EXIT_DRIFT, EXIT_UNVERIFIABLE = 0, 1, 2


def _load(args) -> Registry:
    return Registry.load(getattr(args, "registry", None))


def _describe(w, live: dict) -> str:
    """Human view of a watch's price, whichever mode it is in."""
    if is_aggregate(w):
        return (f"floor in {catalog.per_million(live.get('min_prompt'))} "
                f"({live.get('min_prompt_provider') or '?'}) / out "
                f"{catalog.per_million(live.get('min_completion'))} "
                f"({live.get('min_completion_provider') or '?'}); "
                f"{live.get('endpoints')} endpoints")
    return (f"in {catalog.per_million(live.get('prompt'))} / "
            f"out {catalog.per_million(live.get('completion'))} / "
            f"discount {live.get('discount')}")


def cmd_check(args) -> int:
    reg = _load(args)
    watches = reg.for_job(args.job) if args.job else reg.watches
    if not watches:
        print(f"pricewatch: no watches registered for job {args.job!r} — nothing to gate on",
              file=sys.stderr)
        return EXIT_OK

    results: list[CheckResult] = [check_watch(w) for w in watches]
    ok = [r for r in results if r.status == "ok"]
    drift = [r for r in results if r.status == "drift"]
    unknown = [r for r in results if r.status == "unverifiable"]

    if args.json:
        print(json.dumps({
            "job": args.job,
            "ok": [r.watch.key for r in ok],
            "drift": [{"watch": r.watch.key, "reasons": r.reasons} for r in drift],
            "unverifiable": [{"watch": r.watch.key, "error": r.error} for r in unknown],
            "notes": [f"{r.watch.key}: {note}" for r in results for note in r.notes],
        }, indent=2))

    for r in drift:
        print(f"HOLD  {r.watch.key}: " + "; ".join(r.reasons))
    for r in results:
        for note in r.notes:
            print(f"INFO  {r.watch.key}: {note}")

    if drift:
        job = args.job or drift[0].watch.job
        print(f"PRICEWATCH: HOLD — a watched endpoint moved. Re-accept with "
              f"`pricewatch accept --job {job}` once you have decided to pay the new price.")
        return EXIT_DRIFT

    if unknown:
        for r in unknown:
            print(f"WARN  {r.watch.key}: could not verify ({r.error})", file=sys.stderr)
        if args.on_unreachable == "pass":
            print(f"PRICEWATCH: proceeding unverified ({len(unknown)} watch(es) unreachable)",
                  file=sys.stderr)
            return EXIT_OK
        print("PRICEWATCH: HOLD — could not verify price; re-run when the catalog is reachable "
              "(or pass --on-unreachable pass to override)", file=sys.stderr)
        return EXIT_UNVERIFIABLE

    for r in ok:
        print(f"OK    {r.watch.key}: {_describe(r.watch, r.live)}")
    return EXIT_OK


def cmd_accept(args) -> int:
    reg = _load(args)
    watches = [reg.find(args.key)] if args.key else reg.for_job(args.job)
    watches = [w for w in watches if w]
    if not watches:
        print(f"pricewatch: nothing to accept for {args.key or args.job!r}", file=sys.stderr)
        return EXIT_DRIFT

    written = 0
    for w in watches:
        before = dict(w.baseline or {})
        result = check_watch(w)
        if result.status == "unverifiable":
            print(f"cannot accept {w.key}: {result.error}", file=sys.stderr)
            return EXIT_UNVERIFIABLE
        if not result.live:
            print(f"cannot accept {w.key}: no usable catalog view "
                  f"({'; '.join(result.reasons) or 'empty payload'})", file=sys.stderr)
            return EXIT_DRIFT
        w.baseline = baseline_from(result)
        w.accepted_at = _now_iso()
        w.accepted_by = args.by
        if args.note:
            w.note = args.note
        written += 1
        if before:
            print(f"accepted {w.key}: "
                  + ("; ".join(result.reasons) if result.reasons else "unchanged")
                  + f" -> {_describe(w, result.live)}")
        else:
            print(f"accepted {w.key}: initial baseline -> {_describe(w, result.live)}")
    reg.save()
    print(f"registry written: {reg.path} ({written} baseline(s) set)")
    return EXIT_OK


def cmd_sweep(args) -> int:
    """Print nothing when nominal (so a cron can stay silent); full report on change."""
    reg = _load(args)
    results: list[CheckResult] = [check_watch(w) for w in reg.watches]
    drift = [r for r in results if r.status == "drift"]
    unknown = [r for r in results if r.status == "unverifiable"]
    notes = [(r, note) for r in results for note in r.notes]

    if not drift and not unknown:
        if args.verbose:
            for r, note in notes:
                print(f"INFO  {r.watch.key}: {note}")
            print(f"pricewatch: NOMINAL — {len(reg.watches)} watch(es) unchanged")
        return EXIT_OK

    lines = ["*pricewatch* — OpenRouter endpoint change detected" if drift
             else "*pricewatch* — catalog unreachable"]
    for r in drift:
        lines.append(f"• HOLD {r.watch.key}: " + ("; ".join(r.reasons) or "no baseline"))
        if r.live:
            lines.append(f"    was {_describe(r.watch, r.watch.baseline)} -> now "
                         f"{_describe(r.watch, r.live)}")
    for r in unknown:
        lines.append(f"• ? {r.watch.key}: unverifiable — {r.error}")
    for r, note in notes:
        lines.append(f"• i {r.watch.key}: {note}")
    lines.append("Re-accept with `pricewatch accept --job <job>` once decided.")
    print("\n".join(lines))
    return EXIT_DRIFT if drift else EXIT_OK


def cmd_list(args) -> int:
    reg = _load(args)
    for w in reg.watches:
        b = w.baseline or {}
        mode = "aggregate(floor)" if is_aggregate(w) else (w.provider + (f"/{w.tag}" if w.tag else ""))
        if is_aggregate(w):
            prices = (f"floor-in={catalog.per_million(_dec_or_none(b.get('min_prompt')))}"
                      f" floor-out={catalog.per_million(_dec_or_none(b.get('min_completion')))}")
        else:
            prices = (f"in={catalog.per_million(_dec_or_none(b.get('prompt')))}"
                      f" out={catalog.per_million(_dec_or_none(b.get('completion')))}"
                      f" discount={b.get('discount')}")
        print(f"{w.job:12} {w.model:34} {mode:18} {prices}  accepted={w.accepted_at or 'never'}")
    if not reg.watches:
        print("registry is empty")
    return EXIT_OK


def cmd_discover(args) -> int:
    reg = _load(args)
    found = discover_models(roots=args.root)
    registered_models = {w.model for w in reg.watches}
    print(f"discovered {len(found)} model reference(s) across "
          f"{len(set(f.source for f in found))} source(s)\n")
    for item in found:
        mark = "registered" if item.model in registered_models else "NOT REGISTERED"
        print(f"[{mark:15}] {item.model:42} <- {item.source}")
        if item.detail:
            print(f"{'':17}{item.detail}")
    unreg = sorted({f.model for f in found if f.model not in registered_models})
    if unreg:
        print(f"\n{len(unreg)} model(s) referenced but not watched. These do NOT gate any job "
              f"until they have a registry entry:")
        for m in unreg:
            print(f"  - {m}")
    return EXIT_OK


def _dec_or_none(v):
    return None if v in (None, "") else Decimal(str(v))


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pricewatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", default=None, help="path to registry.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="step-0 preflight for a job")
    c.add_argument("--job", default=None)
    c.add_argument("--on-unreachable", choices=["hold", "pass"], default="hold")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    a = sub.add_parser("accept", help="re-baseline after a deliberate decision")
    a.add_argument("--job", default=None)
    a.add_argument("--key", default=None)
    a.add_argument("--by", default="hermes")
    a.add_argument("--note", default=None)
    a.set_defaults(func=cmd_accept)

    s = sub.add_parser("sweep", help="check every watch; silent when nominal")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_sweep)

    li = sub.add_parser("list", help="show the registry")
    li.set_defaults(func=cmd_list)

    d = sub.add_parser("discover", help="models in config/usage vs the registry")
    d.add_argument("--root", action="append", default=None,
                   help="extra path to scan (repeatable)")
    d.set_defaults(func=cmd_discover)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
