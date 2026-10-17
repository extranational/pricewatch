"""Discovery — what models are actually referenced, and which of those are watched.

Three layered sources, because each one fails differently:

* **declared**  — ``registry.json`` (authoritative, human-maintained; see ``registry.py``)
* **derived**   — the config surfaces that hold model strings: LiteLLM proxy configs, the Hermes
  ``config.yaml``, and Hermes cron jobs (which carry a ``model``/``provider`` per job)
* **observed**  — the run ledgers: the models that actually served traffic (``llm_stage_models``
  written by the PharmaWatch pipeline, plus eval run records)

Derived catches a *new* job before it has a registry entry; observed catches a model in use that
no config we know about mentions. Neither widens what gates a job — that stays a human edit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
HERMES = HOME / ".hermes"

DEFAULT_ROOTS = [
    HOME / "repos",
    Path("/srv/claude-repos"),
    Path("/srv/codeaf-repos"),
    HOME / "research" / "evals",
]

MODEL_LINE = re.compile(r"^\s*(?:-\s*)?(model|model_name|llm_model)\s*:\s*(\S+)", re.M)
ENV_MODEL = re.compile(r"^\s*[A-Z0-9_]*MODEL\s*=\s*(\S+)", re.M)
MODELISH = re.compile(r"\b(?:openrouter/)?([a-z0-9][a-z0-9\-]*/[a-z0-9][a-z0-9\-.]*)\b")


@dataclass
class Found:
    model: str
    source: str
    detail: str = ""


def _norm(model: str) -> str:
    model = (model or "").strip().strip('"\'')
    if model.startswith("os.environ/"):
        return ""
    if model.startswith("openrouter/"):
        model = model[len("openrouter/"):]
    return model


def _skip(model: str) -> bool:
    """Drop things that are model-*shaped* but not token-priced chat models we can watch:
    YAML artifacts, speech/transcription endpoints, and the eval fixtures' own base models."""
    if not model or ":" in model or model in {"base"}:
        return True
    if model in {"gpt-4o-mini", "claude-3-5-sonnet-20240620"} or \
            model.startswith(("openai/", "anthropic/")):
        return True
    tail = model.rsplit("/", 1)[-1]
    return bool(re.search(r"(whisper|stt|tts|embed|moderation|rerank)", tail))


def _scan_litellm(path: Path) -> list[Found]:
    out: list[Found] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    try:
        import yaml  # type: ignore
        blob = yaml.safe_load(text) or {}
        for entry in blob.get("model_list", []) or []:
            name = entry.get("model_name", "")
            params = entry.get("litellm_params", {}) or {}
            model = _norm(params.get("model", ""))
            provider = (((params.get("extra_body") or {}).get("provider") or {}).get("order") or [])
            if model and not _skip(model):
                detail = f"alias {name}"
                if provider:
                    detail += f" pinned to {provider[0]}"
                out.append(Found(model, str(path), detail))
        return out
    except Exception:  # noqa: BLE001 - fall through to the regex view
        pass
    for m in MODEL_LINE.finditer(text):
        model = _norm(m.group(2))
        if model and not _skip(model):
            out.append(Found(model, str(path), "regex view"))
    return out


def _scan_hermes_config() -> list[Found]:
    path = HERMES / "config.yaml"
    if not path.exists():
        return []
    out = []
    text = path.read_text(encoding="utf-8")
    for m in MODEL_LINE.finditer(text):
        model = _norm(m.group(2))
        if model and not _skip(model):
            out.append(Found(model, str(path)))
    return out


def _scan_cron_jobs() -> list[Found]:
    path = HERMES / "cron" / "jobs.json"
    if not path.exists():
        return []
    blob = json.loads(path.read_text(encoding="utf-8"))
    jobs = blob if isinstance(blob, list) else list(blob.get("jobs", blob).values()) \
        if isinstance(blob.get("jobs", blob), dict) else blob.get("jobs", [])
    out = []
    for job in jobs:
        model = _norm(job.get("model") or "")
        if model and not _skip(model):
            out.append(Found(model, "hermes cron", f"job {job.get('name')} "
                                                   f"({job.get('provider') or 'provider unset'})"))
    return out


def _scan_observed() -> list[Found]:
    """Traffic that actually served: llm_stage_models in run records."""
    out: list[Found] = []
    for root in [HOME / "research" / "evals"]:
        if not root.exists():
            continue
        for records in list(root.glob("*/runs/*/records.json"))[:200]:
            try:
                rows = json.loads(records.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for row in rows if isinstance(rows, list) else []:
                for stage, served in (row.get("llm_stage_models") or {}).items():
                    served = (served or "").removeprefix("served:")
                    model = _norm(served)
                    if model and not _skip(model) and "/" in model:
                        out.append(Found(model, str(records.parent.name), f"served {stage}"))
    return out


def discover(roots: list[str] | None = None) -> list[Found]:
    paths: list[Path] = []
    for root in list(roots or []) + DEFAULT_ROOTS:
        p = Path(root)
        if not p.exists():
            continue
        if p.is_file():
            paths.append(p)
            continue
        paths.extend(sorted(p.glob("*/litellm/config.yaml")))
        paths.extend(sorted(p.glob("*/litellm/*.yaml")))
        paths.extend(sorted(p.glob("*/litellm.eval.yaml")))

    found: list[Found] = []
    seen: set[tuple[str, str]] = set()

    def add(item: Found) -> None:
        key = (item.model, item.source)
        if key not in seen:
            seen.add(key)
            found.append(item)

    for path in paths:
        for item in _scan_litellm(path):
            add(item)
    for item in _scan_hermes_config() + _scan_cron_jobs() + _scan_observed():
        add(item)
    return found
