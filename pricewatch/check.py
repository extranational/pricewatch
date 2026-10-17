"""Watch-mode dispatch: one place that knows the difference between a pinned watch and an
aggregate one, so the CLI and the cron sweep can never disagree about what counts as drift.

Two modes:

* **pinned** (``provider`` names an endpoint, optionally with a ``tag``) — compare that endpoint's
  effective prices and promo flag. ANY change is drift: the job's cost is that endpoint's price.
* **aggregate** (``provider`` is ``"*"``) — the job is unpinned, so OpenRouter load-balances it
  across every endpoint by price (inverse-square weighting). There is no single price to pin, so
  watch the *floor*: the cheapest prompt and completion price available, plus who offers them.
  Here an increase or a promo-flag change is drift (that is the spend risk); a *decrease* is
  reported as information — a cheaper endpoint appearing is not a reason to block a run — and an
  endpoint-count change is informational too, since providers join and leave constantly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from . import catalog
from .registry import Watch

AGGREGATE = "*"


@dataclass
class CheckResult:
    watch: Watch
    status: str                      # "ok" | "drift" | "unverifiable"
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    live: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def is_aggregate(watch: Watch) -> bool:
    return (watch.provider or "").strip() == AGGREGATE


def check_watch(watch: Watch, *, timeout: float = 25.0) -> CheckResult:
    try:
        data = catalog.fetch_endpoints(watch.model, timeout=timeout)
    except catalog.Unverifiable as exc:
        return CheckResult(watch, "unverifiable", error=str(exc))

    if is_aggregate(watch):
        if not any(e for e in data.get("endpoints") or []):
            return CheckResult(watch, "drift", reasons=["catalog reports no endpoints for this model"])
        live = catalog.aggregate_snapshot(data)
        if not watch.baseline:
            return CheckResult(watch, "drift", reasons=["no accepted baseline (run `pricewatch accept`)"],
                               live=live)
        reasons, notes = catalog.compare_aggregate(watch.baseline, live)
        return CheckResult(watch, "drift" if reasons else "ok", reasons, notes, live)

    endpoint = catalog.find_endpoint(data, watch.provider, watch.tag)
    if endpoint is None:
        target = watch.provider + (f"/{watch.tag}" if watch.tag else "")
        return CheckResult(watch, "drift",
                           reasons=[f"provider {target} no longer serves this model"])
    live = catalog.snapshot(endpoint)
    if not watch.baseline:
        return CheckResult(watch, "drift", reasons=["no accepted baseline (run `pricewatch accept`)"],
                           live=live)
    reasons = catalog.compare(watch.baseline, live)
    return CheckResult(watch, "drift" if reasons else "ok", reasons, live=live)


def baseline_from(result: CheckResult) -> dict:
    """The dict stored on `accept` — strings, so the registry stays human-readable."""
    if is_aggregate(result.watch):
        keys = ("min_prompt", "min_prompt_provider", "min_completion",
                "min_completion_provider", "endpoints")
    else:
        keys = catalog.COMPARED
    out: dict = {}
    for key in keys:
        value = result.live.get(key)
        # `f"{d:f}"` keeps plain decimal notation (6.6E-7 -> 0.00000066) so the committed
        # registry stays readable and diff-able.
        out[key] = f"{value:f}" if isinstance(value, Decimal) else value
    return out
