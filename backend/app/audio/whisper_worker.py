"""Transcribe one file in a process of its own, and print JSON.

Run as a script, never imported by the application:

    python whisper_worker.py <path> <model> <compute_type>

**Why a separate process.** faiss and ctranslate2 each link their own copy of
the OpenMP runtime. Loading both into one process aborts on macOS:

    OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib
    already initialized.

The API loads faiss for retrieval, so it cannot also load whisper. The
documented workaround, KMP_DUPLICATE_LIB_OK=TRUE, is described by the runtime
itself as unsafe, unsupported, and capable of silently producing incorrect
results. Silently incorrect is the one failure mode this build refuses
everywhere else, so it is refused here too.

A subprocess costs a few seconds of startup for a file somebody uploaded by
hand. It also means a decoder failure kills a worker rather than the API.

Nothing here imports from `app`. The worker depends on faster-whisper and the
standard library, so it starts without dragging the application in with it.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(json.dumps({"error": "usage: whisper_worker.py <path> <model> <compute_type>"}))
        return 2

    path, model_name, compute_type = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device="cpu", compute_type=compute_type)
        # vad_filter drops silence, so long pauses do not become empty segments
        # the validator would then report as defects.
        chunks, info = model.transcribe(path, vad_filter=True)

        segments = [
            {"start": float(c.start), "end": float(c.end), "text": c.text.strip()}
            for c in chunks
            if c.text and c.text.strip()
        ]
        print(json.dumps({
            "segments": segments,
            "language": info.language,
            "duration": float(info.duration),
            "model": model_name,
        }))
        return 0
    except Exception as exc:  # reported as data, so the caller can explain it
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
