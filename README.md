# pricewatch

Detect OpenRouter **endpoint price and promo-discount changes** for the models a job depends on,
and hold the job before it spends at a price nobody agreed to.

Born from a real burn: a provider's temporary promo discount expired and a pipeline's spend
jumped well above what had been budgeted. The failure is silent — nothing errors, the invoices
just get bigger.

## Why a pin plus a baseline

An OpenRouter model id resolves to *many* endpoints (23 for `deepseek/deepseek-v4-pro-0813`),
at up to a 9x price spread, and the `pricing` a request is billed at **already includes** any
promo. The catalog also exposes a `discount` field that *is* the promo flag. So:

* a job's cost is only meaningful together with the provider it is pinned to, and
* a promo expiry shows up either as a changed effective price or as a changed `discount`.

`pricewatch` records both, per watch, and treats any change as drift worth a human decision.

## Usage

```bash
# step-0 preflight inside a job — exit 0 = go, 1 = HOLD, 2 = could not verify (HOLD by default)
pricewatch check --job pharmawatch

# the human act of accepting a new price (this is what re-baselines)
pricewatch accept --job pharmawatch --by brett --note "promo ended, still cheapest"

# periodic sweep — prints nothing when nominal; the report itself is the alert
pricewatch sweep --verbose

# what is referenced in configs/usage vs what is watched
pricewatch discover

pricewatch list
```

Exit-code contract (jobs gate on this):

| code | meaning | job behaviour |
| --- | --- | --- |
| 0 | every watched pin matches its accepted baseline | proceed |
| 1 | DRIFT — price/discount/provider changed | **hold**, await `accept` |
| 2 | UNVERIFIABLE — catalog not readable | **hold** (override: `--on-unreachable pass`) |

`check` reads the public endpoints payload, so a job needs no OpenRouter key to run step-0.

## The registry

`registry.json` is the single source of truth. A model is watched — and therefore gates a
job — **only if it has an entry**. Entries are added by hand so that a scan can never silently
widen what a job can be blocked on.

```json
{"job": "pharmawatch", "model": "deepseek/deepseek-v4-pro-0813", "provider": "StreamLake",
 "tag": "streamlake", "alias": "pharma-deepseek",
 "baseline": {"prompt": "0.00000066", "completion": "0.00000198", "discount": "0.5"},
 "accepted_at": "2026-10-17T18:36:59+00:00", "accepted_by": "hermes"}
```

`tag` matters where one provider sells the same model on several tiers (`baidu/fp8` vs
`baidu/fp4`). Pinning a tag that has disappeared reads as drift, never as a wrong-but-cheap match.

## Discovery — keeping the registry honest

Three layers, because each fails differently:

1. **declared** — `registry.json` (authoritative).
2. **derived** — scans the config surfaces that hold model strings: LiteLLM proxy configs under
   the configured repo roots, `~/.hermes/config.yaml`, and Hermes cron jobs (which carry a
   `model`/`provider` each). Catches a *new* job before it has an entry.
3. **observed** — the run ledgers: the models that actually served traffic
   (`llm_stage_models` in PharmaWatch/eval run records). Catches a model in use that no config
   we know about mentions.

`pricewatch discover` prints the union and calls out the unregistered ones. It never adds
entries itself.

## Cron

`scripts/sweep_cron.py` follows the `no_agent` cron contract: silent when nominal, the report
itself as the delivery when something moved, diagnostics on stderr, non-zero only if the sweep
itself crashed. A drift signature is latched and re-alerts at most once per 24h while the same
condition persists — a promo that ended stays ended until someone re-accepts, and a fire-once
latch would go quiet right through the change.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
