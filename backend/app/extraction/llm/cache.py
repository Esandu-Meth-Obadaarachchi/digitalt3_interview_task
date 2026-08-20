"""On-disk cache of model responses.

The brief: "Cache responses during development so you are not re-billing tokens
on every test run." Free-tier limits are part of the exercise, and a full eval
run makes tens of calls.

The key is a hash of everything that could change the answer: provider, model,
prompt text, prompt version, temperature and the JSON schema. Editing a prompt
therefore misses the cache automatically, which is the property that matters:
a cached result can never be attributed to a prompt that did not produce it.

Caching makes an eval run reproducible, and it could also hide a model that has
become unreliable. Two things guard against that: the hit rate is reported in
the eval output, and `make eval-fresh` bypasses the cache entirely to prove the
committed numbers reproduce against live calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.extraction.llm.base import LLMRequest, LLMResponse


def cache_key(provider: str, model: str, request: LLMRequest, prompt_version: str | None) -> str:
    payload = json.dumps(
        {
            "provider": provider,
            "model": model,
            "prompt": request.prompt,
            "system": request.system,
            "schema": request.json_schema,
            "temperature": request.temperature,
            "prompt_version": prompt_version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResponseCache:
    """One JSON file per entry, named by its key. Inspectable by design.

    A directory of readable files beats an opaque store here: during the
    walkthrough the cache can be opened and shown, and a stale entry can be
    deleted by hand.
    """

    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        if enabled:
            directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> LLMResponse | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            response = LLMResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt entry is discarded rather than crashing a run.
            path.unlink(missing_ok=True)
            return None
        return response.model_copy(update={"cache_hit": True})

    def put(self, key: str, response: LLMResponse) -> None:
        if not self.enabled:
            return
        self._path(key).write_text(
            response.model_copy(update={"cache_hit": False}).model_dump_json(indent=2),
            encoding="utf-8",
        )

    def clear(self) -> int:
        removed = 0
        if self.directory.exists():
            for path in self.directory.glob("*.json"):
                path.unlink()
                removed += 1
        return removed

    def size(self) -> int:
        return len(list(self.directory.glob("*.json"))) if self.directory.exists() else 0
