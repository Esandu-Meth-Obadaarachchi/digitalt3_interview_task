"""Prompt loading and versioning.

The brief lists "prompts scattered inline through the code" as an anti-pattern:
the same instruction ends up in three files and has already drifted, and
nothing can be versioned, diffed or evaluated. So there is one file per
capability under `backend/app/prompts/`, loaded at runtime.

Each file opens with a small header:

    # version: 2
    # capability: extract_actions
    # changed: added the UNSPECIFIED rule after v1 guessed an owner
    ---
    <the prompt body>

The loader returns the declared version and a hash of the body. Both are
recorded on every extraction row and every llm_calls row, so:

  * the eval harness reports which prompt version produced a number
  * an edit made without bumping the version still changes the hash, so a
    result can never be attributed to a prompt that did not produce it

The body is rendered with `str.format`, so placeholders are written as
`{name}` and any literal brace in a prompt is doubled.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from app.models.common import StrictModel

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_HEADER_LINE = re.compile(r"^#\s*(?P<key>[a-z_]+)\s*:\s*(?P<value>.*)$")
_SEPARATOR = "---"


class Prompt(StrictModel):
    """A loaded prompt, with everything needed to attribute a result to it."""

    name: str
    version: str
    capability: str
    changed: str | None = None
    body: str
    body_sha256: str

    @property
    def version_tag(self) -> str:
        """What gets stored on an extraction: "2+a3f9c1"."""
        return f"{self.version}+{self.body_sha256[:6]}"

    def render(self, **values: object) -> str:
        return self.body.format(**values)


def _parse(name: str, raw: str) -> Prompt:
    lines = raw.splitlines()
    header: dict[str, str] = {}
    body_start = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == _SEPARATOR:
            body_start = index + 1
            break
        match = _HEADER_LINE.match(stripped)
        if match:
            header[match.group("key")] = match.group("value").strip()
        elif stripped:
            # Body began without a separator: treat the whole file as body.
            body_start = index
            break

    body = "\n".join(lines[body_start:]).strip()
    if not body:
        raise ValueError(f"prompt {name!r} has an empty body")

    return Prompt(
        name=name,
        version=header.get("version", "0"),
        capability=header.get("capability", name),
        changed=header.get("changed"),
        body=body,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


@lru_cache(maxsize=32)
def load_prompt(name: str) -> Prompt:
    """Load and cache a prompt by file stem. Raises if it does not exist."""
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in PROMPT_DIR.glob("*.txt"))) or "none"
        raise FileNotFoundError(f"no prompt named {name!r} in {PROMPT_DIR}. Available: {available}")
    return _parse(name, path.read_text(encoding="utf-8"))


def list_prompts() -> list[Prompt]:
    return [load_prompt(path.stem) for path in sorted(PROMPT_DIR.glob("*.txt"))]


def clear_cache() -> None:
    """Used by tests, and by anything editing prompts in a running process."""
    load_prompt.cache_clear()
