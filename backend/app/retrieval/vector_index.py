"""The FAISS index behind dense retrieval (M8).

`IndexFlatIP` over unit-length vectors, which makes the inner product a cosine
similarity. Flat means exact search with no training and no approximation.

That is a deliberate choice, not a default. The corpus here is a few hundred
transcript segments. An approximate index such as IVF or HNSW exists to trade
recall for speed at a scale this is nowhere near, and using one would mean
reporting retrieval accuracy that is partly a property of the index rather than
of the embeddings. At this size, exact search costs microseconds and removes a
variable from the measurement.

The index stores vectors against integer ids and nothing else, so the mapping
back to the row a vector came from lives in the `embedding_index` table, along
with the model that produced it and a hash of the text. Changing the embedding
model invalidates the right rows rather than the whole store, and an edited
segment is detectable without re-encoding everything to find out.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.config import Settings, get_settings
from app.db import database
from app.models.common import StrictModel
from app.retrieval.embeddings import Embedder, get_embedder

logger = logging.getLogger("agent.retrieval")


class IndexedItem(StrictModel):
    """One thing that can be retrieved."""

    ref_type: str          # segment | chat_message | extraction
    ref_id: str
    text: str


class IndexStats(StrictModel):
    vectors: int = 0
    dimensions: int = 0
    model: str = ""
    by_type: dict[str, int] = {}
    index_path: str | None = None
    built_at: str | None = None

    @property
    def empty(self) -> bool:
        return self.vectors == 0


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class VectorIndex:
    """A FAISS index plus its mapping table, kept in step."""

    def __init__(self, settings: Settings | None = None, embedder: Embedder | None = None) -> None:
        self._settings = settings or get_settings()
        self._embedder = embedder or get_embedder(self._settings)
        self._index = None
        self._ids: list[tuple[str, str]] = []   # faiss row -> (ref_type, ref_id)

    # --- paths --------------------------------------------------------------

    @property
    def _directory(self) -> Path:
        return Path(self._settings.faiss_index_dir)

    @property
    def _index_path(self) -> Path:
        # The model is part of the filename, so switching embedder cannot
        # silently load vectors produced by a different one.
        safe = self._embedder.name.replace("/", "_")
        return self._directory / f"{safe}.faiss"

    # --- building -----------------------------------------------------------

    def build(self, items: list[IndexedItem]) -> IndexStats:
        """Encode everything and write a fresh index.

        A full rebuild rather than an incremental update. The corpus is small,
        rebuilding takes under a second, and an incremental path would need
        deletion handling that nothing here exercises. Simpler is also easier
        to defend.
        """
        import faiss

        self._directory.mkdir(parents=True, exist_ok=True)
        vectors = self._embedder.encode([item.text for item in items])
        dimensions = vectors.shape[1] if len(items) else self._embedder.dimensions

        index = faiss.IndexFlatIP(dimensions)
        if len(items):
            index.add(vectors)
        faiss.write_index(index, str(self._index_path))

        self._index = index
        self._ids = [(item.ref_type, item.ref_id) for item in items]

        now = datetime.now(timezone.utc).isoformat()
        with database.transaction(self._settings) as conn:
            conn.execute("DELETE FROM embedding_index WHERE model_name = ?", (self._embedder.name,))
            conn.executemany(
                "INSERT INTO embedding_index (faiss_id, ref_type, ref_id, model_name, text_hash, created_at)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (row, item.ref_type, item.ref_id, self._embedder.name, text_hash(item.text), now)
                    for row, item in enumerate(items)
                ],
            )

        logger.info("built %s index with %s vectors", self._embedder.name, len(items))
        return self.stats()

    # --- loading ------------------------------------------------------------

    def load(self) -> bool:
        """Load a previously built index. False when there is nothing to load."""
        import faiss

        if not self._index_path.exists():
            return False

        with database.connect(self._settings) as conn:
            rows = conn.execute(
                "SELECT faiss_id, ref_type, ref_id FROM embedding_index"
                " WHERE model_name = ? ORDER BY faiss_id",
                (self._embedder.name,),
            ).fetchall()

        if not rows:
            return False

        self._index = faiss.read_index(str(self._index_path))
        self._ids = [(r["ref_type"], r["ref_id"]) for r in rows]

        if self._index.ntotal != len(self._ids):
            # The index and its mapping disagree, so neither can be trusted.
            # Refusing to load is better than returning citations that point at
            # the wrong rows, which is the one failure this system must not have.
            logger.warning(
                "index has %s vectors but the mapping has %s rows, refusing to load",
                self._index.ntotal, len(self._ids),
            )
            self._index, self._ids = None, []
            return False

        return True

    def ready(self) -> bool:
        return self._index is not None or self.load()

    # --- searching ----------------------------------------------------------

    def search(self, query: str, top_k: int = 8) -> list[tuple[str, str, float]]:
        """Return (ref_type, ref_id, cosine similarity), best first."""
        if not self.ready() or not self._ids:
            return []

        vector = self._embedder.encode([query])
        scores, positions = self._index.search(vector, min(top_k, len(self._ids)))

        results = []
        for score, position in zip(scores[0], positions[0], strict=True):
            if position < 0:      # FAISS pads with -1 when fewer results exist
                continue
            ref_type, ref_id = self._ids[position]
            results.append((ref_type, ref_id, float(score)))
        return results

    # --- reporting ----------------------------------------------------------

    def stats(self) -> IndexStats:
        with database.connect(self._settings) as conn:
            rows = conn.execute(
                "SELECT ref_type, COUNT(*) AS n FROM embedding_index WHERE model_name = ? GROUP BY ref_type",
                (self._embedder.name,),
            ).fetchall()
            built = conn.execute(
                "SELECT MAX(created_at) AS at FROM embedding_index WHERE model_name = ?",
                (self._embedder.name,),
            ).fetchone()

        by_type = {r["ref_type"]: r["n"] for r in rows}
        return IndexStats(
            vectors=sum(by_type.values()),
            dimensions=self._embedder.dimensions,
            model=self._embedder.describe(),
            by_type=by_type,
            index_path=str(self._index_path) if self._index_path.exists() else None,
            built_at=built["at"] if built else None,
        )


def collect_indexable(conn: sqlite3.Connection) -> list[IndexedItem]:
    """Everything a question may be answered from.

    Transcript segments, and approved extractions. M8's inputs are "stored
    transcripts, decisions, actions, risks and chat signals", and an extraction
    that has not been approved is not a stored fact about the meeting: it is a
    proposal a human has not yet accepted, and answering a question from one
    would route around the approval gate.
    """
    items = [
        IndexedItem(ref_type="segment", ref_id=row["id"], text=row["text"])
        for row in conn.execute(
            "SELECT s.id, s.text FROM segments s"
            " JOIN sources src ON src.id = s.source_id"
            " WHERE src.consent_flag = 1 ORDER BY s.source_id, s.segment_index"
        )
    ]
    items += [
        IndexedItem(ref_type="extraction", ref_id=row["id"], text=row["search_text"])
        for row in conn.execute(
            "SELECT id, search_text FROM extractions WHERE status = 'approved' ORDER BY id"
        )
    ]
    return items


def rebuild(settings: Settings | None = None) -> IndexStats:
    """Rebuild the index from the store. Called after ingestion or approval."""
    cfg = settings or get_settings()
    with database.connect(cfg) as conn:
        items = collect_indexable(conn)
    return VectorIndex(cfg).build(items)
