"""Hybrid retrieval (M8): SQLite FTS5, FAISS, and the fusion of the two.

The brief warns that "keyword search that works and is measured beats a vector
store that is never evaluated". This build takes that seriously by measuring
all three modes rather than by avoiding the vector store: `keyword`, `dense`
and `hybrid` are selectable, and the eval harness reports retrieval accuracy
for each so the choice is defended with numbers instead of an opinion.

They fail in different directions, which is the whole argument for fusing them:

  keyword  exact on names, dates, identifiers and rare words. Blind to
           paraphrase: a question about postponing something will not match a
           transcript that says defer.
  dense    good on paraphrase. Weak on exactly the tokens a person searches
           with, because "Priya Menon" and "Priya Sharma" sit close together
           in embedding space and a date is nearly meaningless to it.

Reciprocal Rank Fusion combines them by rank rather than by score. Deliberate:
BM25 scores and cosine similarities are not comparable quantities, and any
weighted sum of them needs a normalisation constant invented out of nothing.
RRF needs no such constant. A result ranked well by either method surfaces, and
one ranked well by both surfaces higher.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from app.config import Settings, get_settings
from app.db import database
from app.models.common import StrictModel
from app.retrieval.vector_index import VectorIndex

#: How far the dense half over-fetches when a scope is set, so the filter
#: still leaves `limit` results. See dense_search.
DENSE_SCOPE_OVERFETCH = 10

logger = logging.getLogger("agent.retrieval")

_WORD = re.compile(r"[A-Za-z0-9']+")

#: Dropped from a keyword query. Not from the index, only from the query: a
#: question is mostly function words, and matching on them ranks every segment
#: about equally and tells you nothing.
_QUERY_STOPWORDS = frozenset(
    """a an and are as at be been by did do does for from had has have how i in is it its
    of on or so than that the their them there these this to was we were what when where
    which who whom why will with you your our us""".split()
)


class SearchHit(StrictModel):
    """One retrieved item, with everything a citation needs.

    `keyword_rank` and `dense_rank` are kept so the interface can show which
    method found a result. Being able to see that a hit came from keyword alone
    is what makes the case for hybrid legible rather than asserted.
    """

    ref_type: str
    ref_id: str
    source_id: str
    source_title: str | None = None
    text: str
    speaker: str | None = None
    timestamp: str | None = None
    char_start: int | None = None
    char_end: int | None = None

    score: float = 0.0
    keyword_rank: int | None = None
    dense_rank: int | None = None
    keyword_score: float | None = None
    dense_score: float | None = None

    @property
    def found_by(self) -> str:
        if self.keyword_rank is not None and self.dense_rank is not None:
            return "both"
        return "keyword" if self.keyword_rank is not None else "dense"


def to_fts_query(question: str) -> str:
    """Turn a natural question into something FTS5 will accept.

    A raw question breaks FTS5 syntax on apostrophes and punctuation, so the
    words are extracted and joined with OR. Words are also prefix-matched, so
    "deferred" finds "defer" alongside the porter stemmer already configured on
    the index.
    """
    words = [w for w in _WORD.findall(question.lower()) if w not in _QUERY_STOPWORDS and len(w) > 1]
    if not words:
        words = _WORD.findall(question.lower())[:6]
    return " OR ".join(f'"{w}"*' for w in words) if words else '""'


def _source_clause(sources: set[str] | None, column: str) -> tuple[str, list]:
    """A metadata filter, as SQL rather than as a filter applied afterwards.

    Narrowing before the ranking matters: BM25 ranks within what it was given,
    so filtering after the query returns the best matches in the corpus that
    happen to be in scope, rather than the best matches in scope.
    """
    if not sources:
        return "", []
    placeholders = ",".join("?" for _ in sources)
    return f" AND {column} IN ({placeholders})", list(sources)


def keyword_search(
    conn: sqlite3.Connection,
    question: str,
    limit: int,
    sources: set[str] | None = None,
) -> list[SearchHit]:
    """BM25 over segments and approved extractions, optionally within a scope.

    bm25() returns a smaller number for a better match, so it is negated to
    give a score that sorts the same way as a cosine similarity.
    """
    match = to_fts_query(question)
    hits: list[SearchHit] = []
    seg_filter, seg_params = _source_clause(sources, "s.source_id")
    ext_filter, ext_params = _source_clause(sources, "e.source_id")

    try:
        rows = conn.execute(
            "SELECT s.id, s.source_id, s.speaker, s.start_ts, s.text, s.char_start, s.char_end,"
            "       src.title, bm25(segments_fts) AS rank"
            " FROM segments_fts"
            " JOIN segments s ON s.rowid = segments_fts.rowid"
            " JOIN sources src ON src.id = s.source_id"
            " WHERE segments_fts MATCH ? AND src.consent_flag = 1" + seg_filter +
            " ORDER BY rank LIMIT ?",
            (match, *seg_params, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("keyword query rejected by FTS5: %s", exc)
        return []

    for row in rows:
        hits.append(
            SearchHit(
                ref_type="segment",
                ref_id=row["id"],
                source_id=row["source_id"],
                source_title=row["title"],
                text=row["text"],
                speaker=row["speaker"],
                timestamp=row["start_ts"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                keyword_score=-float(row["rank"]),
            )
        )

    try:
        rows = conn.execute(
            "SELECT e.id, e.source_id, e.speaker, e.timestamp, e.search_text, e.verbatim_quote,"
            "       e.char_start, e.char_end, src.title, bm25(extractions_fts) AS rank"
            " FROM extractions_fts"
            " JOIN extractions e ON e.rowid = extractions_fts.rowid"
            " JOIN sources src ON src.id = e.source_id"
            " WHERE extractions_fts MATCH ? AND e.status = 'approved'" + ext_filter +
            " ORDER BY rank LIMIT ?",
            (match, *ext_params, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    for row in rows:
        hits.append(
            SearchHit(
                ref_type="extraction",
                ref_id=row["id"],
                source_id=row["source_id"],
                source_title=row["title"],
                text=row["verbatim_quote"],
                speaker=row["speaker"],
                timestamp=row["timestamp"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                keyword_score=-float(row["rank"]),
            )
        )

    hits.sort(key=lambda h: h.keyword_score or 0.0, reverse=True)
    for rank, hit in enumerate(hits[:limit], start=1):
        hit.keyword_rank = rank
    return hits[:limit]


def dense_search(
    conn: sqlite3.Connection,
    question: str,
    limit: int,
    settings: Settings,
    sources: set[str] | None = None,
) -> list[SearchHit]:
    """Cosine over the vector index, optionally within a scope.

    IndexFlatIP holds vectors and nothing else, so unlike the keyword half
    there is no metadata to filter on inside the index. The filter is applied
    after the search and the fetch widens to compensate, because asking for 8
    and discarding the out-of-scope ones returns fewer than 8. The multiplier
    is bounded: at this corpus size scanning the whole index is cheap, and a
    real deployment would use a FAISS IDSelector or one index per tenant.
    """
    index = VectorIndex(settings)
    if not index.ready():
        logger.info("no vector index built, dense retrieval returns nothing")
        return []

    fetch = limit if not sources else limit * DENSE_SCOPE_OVERFETCH

    hits: list[SearchHit] = []
    for _, (ref_type, ref_id, score) in enumerate(index.search(question, fetch), start=1):
        hit = _hydrate(conn, ref_type, ref_id)
        if hit is None:
            continue
        if sources and hit.source_id not in sources:
            continue
        hit.dense_score = score
        hit.dense_rank = len(hits) + 1
        hits.append(hit)
        if len(hits) >= limit:
            break
    return hits


def _hydrate(conn: sqlite3.Connection, ref_type: str, ref_id: str) -> SearchHit | None:
    """Turn a bare reference into a citable hit."""
    if ref_type == "segment":
        row = conn.execute(
            "SELECT s.id, s.source_id, s.speaker, s.start_ts, s.text, s.char_start, s.char_end, src.title"
            " FROM segments s JOIN sources src ON src.id = s.source_id WHERE s.id = ?",
            (ref_id,),
        ).fetchone()
        if row is None:
            return None
        return SearchHit(
            ref_type="segment", ref_id=row["id"], source_id=row["source_id"],
            source_title=row["title"], text=row["text"], speaker=row["speaker"],
            timestamp=row["start_ts"], char_start=row["char_start"], char_end=row["char_end"],
        )

    row = conn.execute(
        "SELECT e.id, e.source_id, e.speaker, e.timestamp, e.verbatim_quote, e.char_start,"
        " e.char_end, src.title FROM extractions e JOIN sources src ON src.id = e.source_id"
        " WHERE e.id = ? AND e.status = 'approved'",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    return SearchHit(
        ref_type="extraction", ref_id=row["id"], source_id=row["source_id"],
        source_title=row["title"], text=row["verbatim_quote"], speaker=row["speaker"],
        timestamp=row["timestamp"], char_start=row["char_start"], char_end=row["char_end"],
    )


def expand_with_neighbours(
    conn: sqlite3.Connection, hits: list[SearchHit], window: int = 1, cap: int = 24
) -> list[SearchHit]:
    """Add the turns either side of each retrieved segment.

    A meeting answers a question across turns, not within one. "How do we split
    the work for Phase 1?" retrieves at rank 1 for a question about who is doing
    what, and the answer is the three turns AFTER it: "I'll take the booking
    engine", "I'll handle reception". Those share no words with the question and
    no embedding neighbourhood with it either, so neither retrieval method can
    reach them on their own.

    Neighbours are added as SEPARATE numbered sources rather than by widening
    the text of the hit they came from. Widening would let the model quote a
    neighbour while citing the matched segment, producing a citation that
    verifies against the corpus and points at the wrong line. Every source
    stays one segment, so every citation stays exact.

    Neighbours are appended after the ranked hits and carry no rank of their
    own, because they were not retrieved. They are context, and the ordering
    should not pretend otherwise.
    """
    if not hits or window <= 0:
        return hits

    seen = {(h.ref_type, h.ref_id) for h in hits}
    neighbours: list[SearchHit] = []

    for hit in hits:
        if hit.ref_type != "segment":
            continue
        row = conn.execute(
            "SELECT source_id, segment_index FROM segments WHERE id = ?", (hit.ref_id,)
        ).fetchone()
        if row is None:
            continue

        low = row["segment_index"] - window
        high = row["segment_index"] + window
        rows = conn.execute(
            "SELECT s.id, s.source_id, s.speaker, s.start_ts, s.text, s.char_start, s.char_end,"
            "       src.title FROM segments s JOIN sources src ON src.id = s.source_id"
            " WHERE s.source_id = ? AND s.segment_index BETWEEN ? AND ? ORDER BY s.segment_index",
            (row["source_id"], low, high),
        ).fetchall()

        for neighbour in rows:
            key = ("segment", neighbour["id"])
            if key in seen:
                continue
            seen.add(key)
            neighbours.append(
                SearchHit(
                    ref_type="segment",
                    ref_id=neighbour["id"],
                    source_id=neighbour["source_id"],
                    source_title=neighbour["title"],
                    text=neighbour["text"],
                    speaker=neighbour["speaker"],
                    timestamp=neighbour["start_ts"],
                    char_start=neighbour["char_start"],
                    char_end=neighbour["char_end"],
                )
            )

    return (hits + neighbours)[:cap]


def reciprocal_rank_fusion(
    rankings: list[list[SearchHit]], k: int = 60, limit: int = 8
) -> list[SearchHit]:
    """Combine ranked lists by position rather than by score.

    score(d) = sum over lists of 1 / (k + rank(d))

    By rank, because BM25 scores and cosine similarities are not comparable
    quantities and any weighted sum of them needs a normalisation constant
    invented out of nothing. RRF needs no such constant, which is precisely why
    it is used here: there is no free parameter to tune on the golden set and
    then quietly report as a result.

    k dampens the advantage of a first place, so a result ranked second by both
    methods can outrank one ranked first by only one. The conventional 60 is
    kept rather than tuned, for the same reason.
    """
    merged: dict[tuple[str, str], SearchHit] = {}
    scores: dict[tuple[str, str], float] = {}

    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            key = (hit.ref_type, hit.ref_id)
            existing = merged.get(key)
            if existing is None:
                merged[key] = hit
            else:
                # Same document found twice: keep whichever ranks each method
                # gave it, so the interface can say it was found by both.
                existing.keyword_rank = existing.keyword_rank or hit.keyword_rank
                existing.dense_rank = existing.dense_rank or hit.dense_rank
                existing.keyword_score = existing.keyword_score or hit.keyword_score
                existing.dense_score = existing.dense_score or hit.dense_score
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)

    for key, hit in merged.items():
        hit.score = scores[key]

    return sorted(merged.values(), key=lambda h: h.score, reverse=True)[:limit]


def search(
    question: str,
    settings: Settings | None = None,
    *,
    mode: str | None = None,
    limit: int | None = None,
    neighbours: int | None = None,
    sources: set[str] | None = None,
) -> list[SearchHit]:
    """Retrieve, by whichever mode is configured or asked for.

    `neighbours` widens the result with the turns either side of each retrieved
    segment. Defaults to the configured window. Pass 0 to measure retrieval on
    its own, which is what the mode comparison does: expansion would flatter
    every mode equally and hide which one actually found the segment.
    """
    cfg = settings or get_settings()
    chosen = mode or cfg.retrieval_mode
    top_k = limit or cfg.retrieval_top_k
    window = cfg.retrieval_neighbours if neighbours is None else neighbours

    with database.connect(cfg) as conn:
        if chosen == "keyword":
            hits = keyword_search(conn, question, top_k, sources)
            for hit in hits:
                hit.score = hit.keyword_score or 0.0
        elif chosen == "dense":
            hits = dense_search(conn, question, top_k, cfg, sources)
            for hit in hits:
                hit.score = hit.dense_score or 0.0
        else:
            # Each half is asked for more than the final list needs, so a result
            # ranked mid-table by both still has the chance to win on fusion.
            wide = top_k * 3
            hits = reciprocal_rank_fusion(
                [
                    keyword_search(conn, question, wide, sources),
                    dense_search(conn, question, wide, cfg, sources),
                ],
                k=cfg.rrf_k,
                limit=top_k,
            )

        return expand_with_neighbours(conn, hits, window, cap=cfg.retrieval_max_sources)
