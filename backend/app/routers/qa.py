"""HTTP surface for M8.

Read-only. Question answering writes nothing, so there is no approval gate here
and nothing to guard beyond the consent rule already applied at indexing time:
only consented sources are searchable, and only approved extractions.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.config import get_settings
from app.models.common import StrictModel
from app.retrieval.qa import Answer, answer_question
from app.retrieval.search import SearchHit, search
from app.retrieval.vector_index import IndexStats, VectorIndex, rebuild

router = APIRouter(prefix="/api/qa", tags=["question answering"])


class Question(StrictModel):
    question: str
    mode: str | None = None
    limit: int | None = None


@router.post("", response_model=Answer, summary="Answer a question with citations")
def ask(request: Question) -> Answer:
    """Returns `found: false` and a not-found answer when nothing supports one.

    That is a successful response, not an error. A question the corpus cannot
    answer has a correct answer, and it is "not found in the available sources".
    """
    return answer_question(request.question, get_settings(), mode=request.mode, limit=request.limit)


@router.post("/search", response_model=list[SearchHit], summary="Retrieval only, no model")
def retrieve(request: Question) -> list[SearchHit]:
    """The retrieval half on its own, with the ranks each method gave.

    Exposed so the pipeline can be inspected rather than trusted: every hit
    carries its keyword rank, its dense rank and the fused score, which is what
    makes the case for hybrid legible.
    """
    return search(request.question, get_settings(), mode=request.mode, limit=request.limit)


@router.get("/index", response_model=IndexStats, summary="What the vector index holds")
def index_stats() -> IndexStats:
    return VectorIndex(get_settings()).stats()


@router.post("/index/rebuild", response_model=IndexStats, summary="Rebuild the vector index")
def rebuild_index() -> IndexStats:
    """Re-encodes every consented segment and every approved extraction.

    A full rebuild rather than an incremental update: the corpus is small
    enough that it takes seconds, and an incremental path would need deletion
    handling that nothing here exercises.
    """
    return rebuild(get_settings())
