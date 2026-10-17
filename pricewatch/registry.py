"""The watch registry — one entry per model+provider a job depends on."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Watch:
    job: str
    model: str
    provider: str
    baseline: dict
    alias: str = ""
    tag: str | None = None
    accepted_at: str = ""
    accepted_by: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.job}:{self.model}@{self.provider}" + (f"/{self.tag}" if self.tag else "")

    @property
    def slug(self) -> str:
        safe = self.key.replace("/", "_").replace("@", "-at-").replace(":", "-")
        return safe

    def baseline_of(self, field_name: str):
        return (self.baseline or {}).get(field_name)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, blob: dict) -> "Watch":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in blob.items() if k in known})


class Registry:
    def __init__(self, watches: list[Watch] | None = None, path: Path | None = None):
        self.watches: list[Watch] = watches or []
        self.path = Path(path) if path else DEFAULT_REGISTRY

    # -- io ----------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Registry":
        p = Path(path) if path else Path(os.environ.get("PRICEWATCH_REGISTRY", DEFAULT_REGISTRY))
        if not p.exists():
            return cls([], p)
        blob = json.loads(p.read_text(encoding="utf-8"))
        return cls([Watch.from_json(w) for w in blob.get("watches", [])], p)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": _now(), "watches": [w.to_json() for w in self.watches]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # -- queries -----------------------------------------------------------
    def for_job(self, job: str) -> list[Watch]:
        return [w for w in self.watches if w.job == job]

    def find(self, key: str) -> Watch | None:
        for w in self.watches:
            if w.key == key or w.slug == key:
                return w
        return None

    def get_or_add(self, job: str, model: str, provider: str, tag: str | None = None) -> Watch:
        for w in self.watches:
            if (w.job, w.model, w.provider, w.tag) == (job, model, provider, tag):
                return w
        w = Watch(job=job, model=model, provider=provider, tag=tag, baseline={})
        self.watches.append(w)
        return w

    def jobs(self) -> list[str]:
        return sorted({w.job for w in self.watches})
