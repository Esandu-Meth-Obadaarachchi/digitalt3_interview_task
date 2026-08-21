#!/usr/bin/env python
"""Regenerate docs/outcome_schema.json from the Pydantic contract.

    make outcome-schema

The schema is a published deliverable and the contract is the code, so the
document is generated rather than maintained by hand. A hand-written schema
drifts from what is actually emitted, and a consumer trusting the drifted
version is worse off than one with no schema at all.

The consumer contract at the top is written by hand, because it says what the
fields MEAN, and a JSON Schema cannot carry that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.models.outcome import SCHEMA_VERSION, OutcomeRecord  # noqa: E402

TARGET = REPO_ROOT / "docs" / "outcome_schema.json"

CONSUMER_CONTRACT = [
    f"Check schema_version before reading. This document describes {SCHEMA_VERSION}.",
    "Every item in actions, decisions, risks and signals was approved by a named human.",
    "consent_flag is the consent state of the meeting these items came from. "
    "Do not act on items from a record whose consent_flag is false; none should exist.",
    "citation.quote is a literal substring of the source transcript. "
    "citation.quote_verified says whether that was machine-checked.",
    "pending_not_included, rejected_not_included and expired_not_included let you tell "
    "an empty record that means 'nothing was found' from one that means "
    "'nothing has been reviewed yet'.",
]


def build() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/Esandu-Meth-Obadaarachchi/digitalt3_interview_task/docs/outcome_schema.json",
        "title": "Meeting outcome record",
        "description": (
            "The versioned artefact a downstream delivery agent consumes. Contains only "
            "items a human approved, and carries everything needed to check each one "
            "without access to the transcript store. Generated from "
            "backend/app/models/outcome.py, which is the authoritative definition; "
            "regenerate with `make outcome-schema` after changing it."
        ),
        "x-schema-version": SCHEMA_VERSION,
        "x-consumer-contract": CONSUMER_CONTRACT,
        **OutcomeRecord.model_json_schema(),
    }


def main() -> int:
    current = json.loads(TARGET.read_text(encoding="utf-8")) if TARGET.exists() else None
    fresh = build()

    if current == fresh:
        print(f"docs/outcome_schema.json is up to date (schema version {SCHEMA_VERSION})")
        return 0

    TARGET.write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
    print(f"docs/outcome_schema.json regenerated (schema version {SCHEMA_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
