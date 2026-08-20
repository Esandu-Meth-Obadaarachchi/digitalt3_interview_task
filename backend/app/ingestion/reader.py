"""Reading source bytes and turning encoding problems into stated defects.

Decoding is strict UTF-8. A file with invalid bytes is a blocking defect that
names the byte offset, rather than a silent lossy decode that would put
mojibake into the store and later into a "verbatim" quote.
"""

from __future__ import annotations

from pathlib import Path

from app.models.ingestion import Defect, DefectCode, DefectSeverity

# Bytes read either side of a decode failure when building the excerpt.
_EXCERPT_RADIUS = 40


def read_source_text(path: Path) -> tuple[str, str, int, list[Defect]]:
    """Return (text, encoding, bytes_read, defects).

    On a decode failure the text is still returned, lossily decoded, so the
    caller can build a readable excerpt for the rejection message. The
    blocking defect means it is never stored.
    """
    defects: list[Defect] = []

    if not path.exists():
        return "", "unknown", 0, [
            Defect(
                code=DefectCode.EMPTY_FILE,
                severity=DefectSeverity.ERROR,
                detail=f"file does not exist: {path}",
            )
        ]

    raw = path.read_bytes()
    bytes_read = len(raw)

    if bytes_read == 0 or not raw.strip():
        defects.append(
            Defect(
                code=DefectCode.EMPTY_FILE,
                severity=DefectSeverity.ERROR,
                detail=f"file is empty ({bytes_read} bytes), nothing to ingest",
            )
        )
        return "", "utf-8", bytes_read, defects

    try:
        text = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError as exc:
        window = raw[max(0, exc.start - _EXCERPT_RADIUS) : exc.end + _EXCERPT_RADIUS]
        line_number = raw[: exc.start].count(b"\n") + 1
        defects.append(
            Defect(
                code=DefectCode.UNDECODABLE_BYTES,
                severity=DefectSeverity.ERROR,
                detail=(
                    f"byte 0x{raw[exc.start]:02x} at offset {exc.start} is not valid UTF-8 "
                    f"({exc.reason}). Decoding leniently would put replacement characters "
                    f"into stored text and from there into a supposedly verbatim quote."
                ),
                line_number=line_number,
                excerpt=window.decode("utf-8", errors="replace").strip(),
            )
        )
        text = raw.decode("utf-8", errors="replace")
        encoding = "utf-8 (invalid bytes present)"

    if text.startswith("﻿"):
        text = text.lstrip("﻿")

    if "�" in text and not any(d.code is DefectCode.UNDECODABLE_BYTES for d in defects):
        defects.append(
            Defect(
                code=DefectCode.REPLACEMENT_CHARACTERS,
                severity=DefectSeverity.WARNING,
                detail="the source already contains U+FFFD replacement characters",
            )
        )

    return text, encoding, bytes_read, defects
