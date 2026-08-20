#!/usr/bin/env python
"""Rebuild the store from schema.sql and load the committed sample data.

Run with `make seed`. Destructive by design: the database holds disposable
seed data, so rebuilding from the authoritative schema is simpler and more
honest than migration tooling that would never be exercised.

Rebuilds the store, then runs every declared source through the ingestion
pipeline and prints what happened to each. The chat export is wired in at
Phase 7, where the classifier exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pydantic import ValidationError  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import database  # noqa: E402
from app.ingestion.service import ingest_transcript  # noqa: E402
from app.models.common import SourceType  # noqa: E402
from app.models.source import SourceMetadata  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def load_manifest(sample_dir: Path) -> list[SourceMetadata]:
    """Read sources.json and validate every entry against the contract.

    A source with no explicit consent flag fails validation here rather than
    being defaulted, which is the first place the consent rule is enforced.
    """
    manifest_path = sample_dir / "metadata" / "sources.json"
    if not manifest_path.exists():
        raise SystemExit(f"{RED}missing manifest: {manifest_path}{RESET}")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries: list[SourceMetadata] = []
    for item in raw["sources"]:
        try:
            entries.append(SourceMetadata(**item))
        except ValidationError as exc:
            raise SystemExit(f"{RED}invalid manifest entry {item.get('id')}: {exc}{RESET}") from exc
    return entries


def check_files(entries: list[SourceMetadata], sample_dir: Path) -> int:
    """Every manifest path must resolve to a file on disk."""
    missing = 0
    for entry in entries:
        if entry.file_path is None:
            print(f"  {YELLOW}?{RESET} {entry.id}{DIM} - no file_path in manifest{RESET}")
            continue
        target = sample_dir / entry.file_path
        consent = f"{GREEN}consent{RESET}" if entry.consent_flag else f"{RED}NO CONSENT{RESET}"
        if target.exists():
            size = target.stat().st_size
            print(f"  {GREEN}ok{RESET} {entry.id:<42} {consent:<22}{DIM}{size:>7} B  {entry.file_path}{RESET}")
        else:
            missing += 1
            print(f"  {RED}MISSING{RESET} {entry.id:<38} -> {target}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="apply schema without dropping the existing database")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_directories()

    print(f"\n{DIM}database{RESET} {settings.db_path}")
    path = database.init_db(settings) if args.keep else database.reset_db(settings)
    print(f"  {GREEN}ok{RESET} schema applied, version {database.schema_version(settings)}"
          f"{'' if args.keep else ' (rebuilt from scratch)'}")

    print(f"\n{DIM}sample data manifest{RESET} {settings.sample_data_dir}/metadata/sources.json")
    entries = load_manifest(settings.sample_data_dir)
    missing = check_files(entries, settings.sample_data_dir)

    consented = sum(1 for e in entries if e.consent_flag)
    print(f"\n  {len(entries)} sources declared, {consented} consented, "
          f"{len(entries) - consented} withheld consent")

    if missing:
        print(f"\n{RED}{missing} declared file(s) missing from sample_data/{RESET}\n")
        return 1

    print(f"\n{DIM}ingesting{RESET}")
    failures = 0
    for entry in entries:
        if entry.source_type is not SourceType.TRANSCRIPT:
            print(f"  {YELLOW}skip{RESET} {entry.id:<42}{DIM} chat export, wired in at Phase 7{RESET}")
            continue

        outcome = ingest_transcript(entry, settings=settings)
        report = outcome.report
        label = {
            "ingested": f"{GREEN}ingested{RESET}",
            "refused": f"{RED}refused {RESET}",
            "error": f"{YELLOW}error   {RESET}",
        }[report.status.value]

        print(
            f"  {label} {entry.id:<42}"
            f"{DIM}{report.bytes_read:>7} B  {report.segments_parsed:>3} segments  "
            f"{len(report.warnings)} warning(s){RESET}"
        )
        if report.rejection_reason:
            print(f"           {DIM}{report.rejection_reason[:150]}{RESET}")
        if report.silent_participants:
            print(f"           {DIM}listed but never spoke: {', '.join(report.silent_participants)}{RESET}")
        if report.status.value == "error":
            failures += 1

    print(f"\n{DIM}store ready at {path}{RESET}")
    print(f"{DIM}{failures} source(s) rejected as malformed, which is expected: the sample set "
          f"contains one deliberately broken file{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
