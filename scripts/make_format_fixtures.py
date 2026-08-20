#!/usr/bin/env python
"""Generate the format fixtures under sample_data/format_fixtures/.

The challenge names txt, vtt and json as transcript formats but the supplied
sample data is entirely .txt. Rather than assert the other two parsers work,
this script converts one real transcript into both, so the test suite can prove
all three parsers normalise the same conversation to identical segments.

It also writes the two files that exercise the blocking read defects: one with
invalid UTF-8 bytes and one that is empty.

Committed output, reproducible input. Run with:  python scripts/make_format_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.ingestion.parsers import txt as txt_parser  # noqa: E402
from app.ingestion.reader import read_source_text  # noqa: E402
from app.ingestion.timestamps import format_timestamp, parse_timestamp  # noqa: E402

SOURCE = REPO_ROOT / "sample_data" / "transcripts" / "client_status_call.txt"
OUT = REPO_ROOT / "sample_data" / "format_fixtures"
PARTICIPANTS = ["Lisa Tran", "David Park", "Sarah Chen", "Priya Sharma"]

#: A lone 0xA9 byte. Valid in cp1252 (a copyright sign), never valid UTF-8.
INVALID_UTF8 = b"[00:00:05] Lisa Tran: Welcome everyone. Contract \xa9 2024 applies.\n"


def _vtt_time(ts: str | None, fallback: float) -> str:
    seconds = parse_timestamp(ts)
    seconds = fallback if seconds is None else seconds
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    read = read_source_text(SOURCE)
    parsed = txt_parser.parse(read.text, read.encoding, read.bytes_read, PARTICIPANTS)
    segments = parsed.raw_segments
    print(f"read {SOURCE.name}: {len(segments)} segments")

    # --- WebVTT, using the <v Speaker> voice tag convention ------------------
    lines = ["WEBVTT", ""]
    for index, segment in enumerate(segments):
        start = segment.start_seconds if segment.start_seconds is not None else float(index * 5)
        nxt = segments[index + 1].start_seconds if index + 1 < len(segments) else None
        end = nxt if nxt is not None else start + 5.0
        lines.append(str(index + 1))
        lines.append(f"{_vtt_time(segment.start_ts, start)} --> {_vtt_time(None, end)}")
        speaker = f"<v {segment.speaker}>" if segment.speaker else ""
        lines.append(f"{speaker}{segment.text}")
        lines.append("")
    (OUT / "client_status_call.vtt").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote client_status_call.vtt ({len(segments)} cues)")

    # --- JSON, using key spellings a speech-to-text tool would emit ----------
    document = {
        "meeting_id": "meeting-client-status-2024-11-18",
        "generated_by": "scripts/make_format_fixtures.py",
        "segments": [
            {
                "speaker": segment.speaker,
                "start_time": segment.start_ts,
                "end_time": format_timestamp(
                    segments[i + 1].start_seconds if i + 1 < len(segments) else (segment.start_seconds or 0) + 5
                ),
                "text": segment.text,
            }
            for i, segment in enumerate(segments)
        ],
    }
    (OUT / "client_status_call.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(f"wrote client_status_call.json ({len(segments)} entries)")

    # --- Blocking read defects ----------------------------------------------
    (OUT / "bad_encoding.txt").write_bytes(INVALID_UTF8)
    print("wrote bad_encoding.txt (invalid UTF-8 byte 0xa9)")

    (OUT / "empty.txt").write_bytes(b"")
    print("wrote empty.txt (0 bytes)")

    (OUT / "unknown_format.dat").write_text("neither prose, nor cues, nor JSON\x01\x02", encoding="utf-8")
    print("wrote unknown_format.dat")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
