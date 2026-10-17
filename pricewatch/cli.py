"""pricewatch CLI.

Exit codes (stable contract — jobs gate on these):

  0  OK            every watched pin matches its accepted baseline
  1  DRIFT         a price/discount/provider changed; HOLD the run and re-accept deliberately
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
from .discover import discover as discover_models
from .registry import Registry, Watch

EXIT_OK, EXIT_DRIFT, EXIT_UNVERIFIABLE = 0, 1, 2


def _load(args) -> Registry:
    return Registry.load(getattr(args, "registry", None))


def _check_watch(w: Watch) -> tuple[str, object]:
    """Returns (status, payload) where status in {ok, drift, unverifiable}."""
    try:
        data = catalog.fetch_endpoints(w.model)
    except catalog.Unverifiable as exc:
        return "unverifiable", str(exc)
    endpoint = catalog.find_endpoint(data, w.provider, w.tag)
    if endpoint is None:
        return "drift", f"provider {w.provider}" + (f"/{w.tag}" if w.tag else "") + \
            " no longer serves this model"
    live = catalog.snapshot(endpoint)
    if not w.baseline:
        return "drift", "no accepted baseline (run `pricewatch accept`)"
    reasons = catalog.compare(w.baseline, live)
    return ("drift", reasons) if reasons else ("ok", live)


def cmd_check(args) -> int:
    reg = _load(args)
    watches = reg.for_job(args.job) if args.job else reg.watches
    if not watches:
        print(f"pricewatch: no watches registered for job {args.job!r} — nothing to gate on",
              file=sys.stderr)
        return EXIT_OK

    holds, unknowns, oks = [], [], []
    for w in watches:
        status, payload = _check_watch(w)
        if status == "ok":
            oks.append(w)
        elif status == "drift":
            holds.append((w, payload))
        else:
            unknowns.append((w, payload))

    if args.json:
        print(json.dumps({
            "job": args.job, "ok": [w.key for w in oks],
            "drift": [{"watch": w.key, "reasons": p} for w, p in holds],
            "unverifiable": [{"watch": w.key, "error": p} for w, p in unknowns],
        }, indent=2))

    for w, reasons in holds:
        if isinstance(reasons, list):
            print(f"HOLD  {w.key}: " + "; ".join(reasons))
        else:
            print(f"HOLD  {w.key}: {reasons}")

    if holds:
        print("PRICEWATCH: HOLD — a watched endpoint moved. Re-accept with "
              "`pricewatch accept --job %s` once you have decided to pay the new price." % args.job)
        return EXIT_DRIFT

    if unknowns:
        for w, err in unknowns:
            print(f"WARN  {w.key}: could not verify ({err})", file=sys.stderr)
        if args.on_unreachable == "pass":
            print(f"PRICEWATCH: proceeding unverified ({len(unknowns)} watch(es) unreachable)",
                  file=sys.stderr)
            return EXIT_OK
        print("PRICEWATCH: HOLD — could not verify price; re-run when the catalog is reachable "
              "(or pass --on-unreachable pass to override)", file=sys.stderr)
        return EXIT_UNVERIFIABLE

    return EXIT_OK


def cmd_accept(args) -> int:
    reg = _load(args)
    watches = [reg.find(args.key)] if args.key else reg.for_job(args.job)
    watches = [w for w in watches if w]
    if not watches:
        print(f"pricewatch: nothing to accept for {args.key or args.job!r}", file=sys.stderr)
        return EXIT_DRIFT

    changed = 0
    for w in watches:
        try:
            data = catalog.fetch_endpoints(w.model)
        except catalog.Unverifiable as exc:
            print(f"cannot accept {w.key}: {exc}", file=sys.stderr)
            return EXIT_UNVERIFIABLE
        endpoint = catalog.find_endpoint(data, w.provider, w.tag)
        if endpoint is None:
            print(f"cannot accept {w.key}: provider no longer serves the model", file=sys.stderr)
            return EXIT_DRIFT
        live = catalog.snapshot(endpoint)
        before = w.baseline or {}
        reasons = catalog.compare(before, live) if before else ["initial baseline"]
        w.baseline = {k: (str(live[k]) if isinstance(live[k], Decimal) else live[k])
                      for k in catalog.COMPARED}
        w.accepted_at = _now_iso()
        w.accepted_by = args.by
        if args.note:
            w.note = args.note
        if reasons:
            changed += 1
            print(f"accepted {w.key}: " + ("; ".join(reasons) if before else "initial baseline")
                  + f" -> now {catalog.per_million(Decimal(str(w.baseline['prompt'])))} in / "
                  f"{catalog.per_million(Decimal(str(w.baseline['completion'])))} out"
                  + (f", discount {w.baseline['discount']}" if w.baseline.get("discount") else ""))
        else:
            print(f"accepted {w.key}: unchanged (baseline refreshed)")
    reg.save()
    print(f"registry written: {reg.path} ({changed} changed)")
    return EXIT_OK


def cmd_sweep(args) -> int:
    """Print nothing when nominal (so a cron can stay silent); full report on change."""
    reg = _load(args)
    changed, unknown = [], []
    for w in reg.watches:
        status, payload = _check_watch(w)
        if status == "drift":
            changed.append((w, payload))
        elif status == "unverifiable":
            unknown.append((w, payload))

    if not changed and not unknown:
        if args.verbose:
            print(f"pricewatch: NOMINAL — {len(reg.watches)} watch(es) unchanged")
        return EXIT_OK

    lines = ["*pricewatch* — OpenRouter endpoint change detected" if changed
             else "*pricewatch* — catalog unreachable"]
    for w, reasons in changed:
        why = "; ".join(reasons) if isinstance(reasons, list) else reasons
        lines.append(f"• HOLD {w.key}: {why}")
        before, live = w.baseline or {}, None
        try:
            live = catalog.snapshot(catalog.find_endpoint(catalog.fetch_endpoints(w.model),
                                                          w.provider, w.tag) or {})
        except Exception:  # noqa: BLE001 - report is best-effort
            live = None
        if live:
            lines.append(f"    was {catalog.per_million(Decimal(str(before.get('prompt'))))} in / "
                         f"{catalog.per_million(Decimal(str(before.get('completion'))))} out "
                         f"(discount {before.get('discount')}) -> now "
                         f"{catalog.per_million(live['prompt'])} in / "
                         f"{catalog.per_million(live['completion'])} out "
                         f"(discount {live['discount']})")
    for w, err in unknown:
        lines.append(f"• ? {w.key}: unverifiable — {err}")
    lines.append("Re-accept with `pricewatch accept --job <job>` once decided.")
    print("\n".join(lines))
    return EXIT_DRIFT if changed else EXIT_OK


def cmd_list(args) -> int:
    reg = _load(args)
    for w in reg.watches:
        b = w.baseline or {}
        print(f"{w.job:12} {w.model:38} {w.provider:12}"
              + (f"/{w.tag}" if w.tag else "")
              + f"  in={catalog.per_million(_dec_or_none(b.get('prompt')))}"
              + f" out={catalog.per_million(_dec_or_none(b.get('completion')))}"
              + f" discount={b.get('discount')}  accepted={w.accepted_at or 'never'}")
    if not reg.watches:
        print("registry is empty")
    return EXIT_OK


def cmd_discover(args) -> int:
    reg = _load(args)
    found = discover_models(roots=args.root)
    registered = {(w.model, w.provider.lower()) for w in reg.watches}
    registered_models = {w.model for w in reg.watches}
    print(f"discovered {len(found)} model reference(s) across {len(set(f.source for f in found))} "
          f"source(s)\n")
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
