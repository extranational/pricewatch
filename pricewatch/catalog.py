"""OpenRouter catalog access + drift detection.

The endpoints payload is public (no key), so a job can run step-0 without holding a key:

    GET https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints

Per endpoint it carries ``pricing.{prompt,completion,input_cache_read}`` (already net of any
promo), a ``discount`` field that *is* the promo flag, plus uptime/latency/throughput. A promo
expiry therefore shows up as either a changed effective price or a changed ``discount``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal

API = "https://openrouter.ai/api/v1/models"
USER_AGENT = "pricewatch/1.0 (+local job preflight)"

# Fields compared for drift. `discount` is compared because it is the promo flag: a price that
# is currently discount-driven must not be allowed to become permanently accepted silently.
COMPARED = ("prompt", "completion", "discount")


class Unverifiable(RuntimeError):
    """The catalog could not be read. Distinct from Drift: nothing is known either way."""


@dataclass
class Drift:
    model: str
    provider: str
    tag: str | None
    reasons: list[str] = field(default_factory=list)

    def one_line(self) -> str:
        return f"{self.model} on {self.provider}: " + "; ".join(self.reasons)


def fetch_endpoints(model: str, *, api_key: str | None = None, timeout: float = 25.0) -> dict:
    req = urllib.request.Request(f"{API}/{model}/endpoints", headers={"User-Agent": USER_AGENT})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Unverifiable(f"{model}: model not found in the OpenRouter catalog") from exc
        raise Unverifiable(f"{model}: HTTP {exc.code} from the catalog") from exc
    except Exception as exc:  # noqa: BLE001 - network surface, any failure is "unverifiable"
        raise Unverifiable(f"{model}: {type(exc).__name__}: {exc}") from exc
    data = payload.get("data") or {}
    if not data.get("endpoints"):
        raise Unverifiable(f"{model}: catalog returned no endpoints")
    return data


def find_endpoint(data: dict, provider: str, tag: str | None = None) -> dict | None:
    """Locate one endpoint. A provider may appear several times at different prices/tags, so an
    entry that pins a tagged tier (e.g. ``baidu/fp8``) must match the tag too."""
    matches = [
        e
        for e in data.get("endpoints", [])
        if (e.get("provider_name") or "").lower() == (provider or "").lower()
    ]
    if tag:
        tagged = [e for e in matches if (e.get("tag") or "") == tag]
        if tagged:
            return tagged[0]
        return None
    return matches[0] if matches else None


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def snapshot(endpoint: dict) -> dict:
    """The comparable price view of one endpoint: effective prices + the promo flag."""
    pricing = endpoint.get("pricing") or {}
    out = {k: _dec(pricing.get(k)) for k in COMPARED}
    out["tag"] = endpoint.get("tag") or ""
    out["uptime_last_1d"] = endpoint.get("uptime_last_1d")
    return out


def compare(baseline: dict, live: dict) -> list[str]:
    """Human-readable drift reasons. Empty list = unchanged."""
    reasons: list[str] = []
    for key in COMPARED:
        was, now = _dec(baseline.get(key)), live.get(key)
        if was is None and now is None:
            continue
        if was != now:
            label = "promo discount" if key == "discount" else f"{key} price"
            was_s = "none" if was is None else str(was)
            now_s = "none" if now is None else str(now)
            extra = _percent_note(key, was, now)
            reasons.append(f"{label} changed {was_s} -> {now_s}{extra}")
    return reasons


def _percent_note(key: str, was: Decimal | None, now: Decimal | None) -> str:
    if key == "discount" or was in (None, 0) or now is None:
        return ""
    try:
        pct = (now - was) / was * 100
    except Exception:  # noqa: BLE001
        return ""
    return f" ({'+' if pct > 0 else ''}{pct:.1f}%)"


def per_million(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"${value * Decimal(1_000_000):.4f}/M"


AGG_KEYS = ("min_prompt", "min_completion")


def aggregate_snapshot(data: dict) -> dict:
    """The price *floor* of an unpinned model: cheapest prompt and completion price on offer,
    and which endpoint(s) offer them. Used when a job pins no provider, so there is no single
    price to compare — the risk is the floor moving (a promo ending at the cheap host)."""
    endpoints = data.get("endpoints") or []

    def cheapest(key: str):
        best = None
        for e in endpoints:
            value = _dec((e.get("pricing") or {}).get(key))
            if value is None:
                continue
            if best is None or value < best[0]:
                best = (value, e.get("provider_name") or "", e.get("tag") or "")
        return best

    prompt, comp = cheapest("prompt"), cheapest("completion")
    return {
        "min_prompt": prompt[0] if prompt else None,
        "min_prompt_provider": prompt[1] if prompt else "",
        "min_completion": comp[0] if comp else None,
        "min_completion_provider": comp[1] if comp else "",
        "endpoints": len(endpoints),
    }


def compare_aggregate(baseline: dict, live: dict) -> tuple[list[str], list[str]]:
    """(reasons, notes) — reasons hold the job, notes are information only."""
    reasons: list[str] = []
    notes: list[str] = []
    for key, label in (("min_prompt", "cheapest prompt price"),
                       ("min_completion", "cheapest completion price")):
        was, now = _dec(baseline.get(key)), live.get(key)
        if was is None or now is None or was == now:
            continue
        was_host = baseline.get(f"{key}_provider") or "?"
        now_host = live.get(f"{key}_provider") or "?"
        where = "" if was_host == now_host else f" ({was_host} -> {now_host})"
        pct = _percent_note(key, was, now)
        if now > was:
            reasons.append(f"{label} ROSE {was} -> {now}{pct}{where}")
        else:
            notes.append(f"{label} fell {was} -> {now}{pct}{where} — cheaper endpoint available")
    was_n, now_n = baseline.get("endpoints"), live.get("endpoints")
    if was_n is not None and now_n is not None and was_n != now_n:
        notes.append(f"endpoint count {was_n} -> {now_n}")
    return reasons, notes



def usage_cost_usd(prompt_per_token: Decimal | None, completion_per_token: Decimal | None,
                   in_tokens: int, out_tokens: int) -> Decimal:
    return (prompt_per_token or Decimal(0)) * in_tokens + (
        completion_per_token or Decimal(0)
    ) * out_tokens
