import json
import logging
import math
import re
import time
from functools import lru_cache
from typing import TypedDict

import httpx
from fastembed import TextEmbedding
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ChunkEmbedding, Document, DocumentChunk, KnowledgeBase
from app.schemas import Citation

logger = logging.getLogger("app.rag")
logger.setLevel(logging.INFO)
RankedMatch = tuple[DocumentChunk, Document, float]
INSUFFICIENT_ANSWER = "当前知识库未检索到足够且可信的证据，无法依据现有资料回答。"
NOISE_MARKERS = ("目录", "附录：官方来源目录", "公开资料汇编 |")


class RAGState(TypedDict, total=False):
    db: Session
    user_id: int
    knowledge_base_id: int
    question: str
    history: list[tuple[str, str]]
    retrieval_question: str
    candidates: list[RankedMatch]
    matches: list[RankedMatch]
    evidence_sufficient: bool
    answer: str
    validation_passed: bool
    refusal_reason: str
    citations: list[Citation]
    timings: dict[str, float]


@lru_cache
def embedding_model() -> TextEmbedding:
    return TextEmbedding(model_name=settings.embedding_model)


def embed_text(text: str) -> list[float]:
    return next(embedding_model().embed([text])).tolist()


def lexical_tokens(text: str) -> set[str]:
    lowered = text.lower()
    values = set(re.findall(r"[a-z0-9_]+", lowered))
    for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
        values.update(sequence[index : index + 2] for index in range(max(1, len(sequence) - 1)))
    return values


def lexical_score(question: str, content: str) -> float:
    question_tokens = lexical_tokens(question)
    if not question_tokens:
        return 0.0
    return len(question_tokens & lexical_tokens(content)) / len(question_tokens)


def section_score(question: str, chunk: DocumentChunk) -> float:
    return lexical_score(question, getattr(chunk, "section_title", "") or "")


def noise_score(content: str) -> float:
    marker_hits = sum(marker in content for marker in NOISE_MARKERS)
    url_count = len(re.findall(r"https?://", content))
    short_lines = sum(len(line.strip()) < 8 for line in content.splitlines())
    line_count = max(1, len(content.splitlines()))
    return min(1.0, marker_hits * 0.35 + url_count * 0.08 + short_lines / line_count * 0.2)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def candidate_rows(db: Session, user_id: int, knowledge_base_id: int, query_vector: list[float]):
    base_filters = (
        KnowledgeBase.user_id == user_id,
        Document.knowledge_base_id == knowledge_base_id,
        Document.status == "ready",
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        return db.execute(
            select(DocumentChunk, Document, ChunkEmbedding)
            .join(Document, Document.id == DocumentChunk.document_id)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .join(ChunkEmbedding, ChunkEmbedding.chunk_id == DocumentChunk.id)
            .where(*base_filters, ChunkEmbedding.vector.is_not(None))
            .order_by(ChunkEmbedding.vector.cosine_distance(query_vector))
            .limit(settings.retrieval_top_k * settings.retrieval_candidate_multiplier)
        ).all()
    return db.execute(
        select(DocumentChunk, Document, ChunkEmbedding)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .join(ChunkEmbedding, ChunkEmbedding.chunk_id == DocumentChunk.id)
        .where(*base_filters)
    ).all()


def external_rerank(question: str, items: list[RankedMatch]) -> list[RankedMatch] | None:
    if not settings.rerank_api_key or not settings.rerank_base_url:
        return None
    response = httpx.post(
        f"{settings.rerank_base_url.rstrip('/')}/rerank",
        headers={"Authorization": f"Bearer {settings.rerank_api_key}"},
        json={
            "model": settings.rerank_model,
            "query": question,
            "documents": [item[0].content for item in items],
            "top_n": settings.rerank_top_k,
        },
        timeout=30,
    )
    response.raise_for_status()
    return [
        (*items[result["index"]][:2], float(result["relevance_score"]))
        for result in response.json()["results"]
    ]


def llm_rerank(question: str, items: list[RankedMatch]) -> list[RankedMatch] | None:
    if settings.rerank_provider != "llm" or not settings.llm_api_key or len(items) < 2:
        return None
    candidates = "\n".join(
        f"{index}: [{getattr(item[0], 'section_title', '')}] {item[0].content[:500]}"
        for index, item in enumerate(items)
    )
    try:
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Select only candidates that directly support answering the question. "
                            "Exclude headers, directories, source lists and tangential text. "
                            'Return JSON only: {"indexes":[candidate indexes]}.'
                        ),
                    },
                    {"role": "user", "content": f"Question: {question}\nCandidates:\n{candidates}"},
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        indexes = json.loads(response.json()["choices"][0]["message"]["content"])["indexes"]
        return [items[index] for index in indexes if isinstance(index, int) and index < len(items)]
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def retrieve_candidates(
    db: Session,
    user_id: int,
    knowledge_base_id: int,
    question: str,
) -> list[RankedMatch]:
    query_vector = embed_text(question)
    ranked = []
    for chunk, document, embedding in candidate_rows(db, user_id, knowledge_base_id, query_vector):
        stored_vector = (
            list(embedding.vector) if embedding.vector is not None else json.loads(embedding.vector_json)
        )
        semantic = max(0.0, cosine_similarity(query_vector, stored_vector))
        keyword = lexical_score(question, chunk.content)
        section = section_score(question, chunk)
        penalty = noise_score(chunk.content)
        score = semantic * 0.62 + keyword * 0.28 + section * 0.10 - penalty * 0.25
        ranked.append((chunk, document, max(0.0, score)))
    ranked.sort(key=lambda item: item[2], reverse=True)
    return [
        item
        for item in ranked[: settings.retrieval_top_k]
        if item[2] >= settings.retrieval_min_score
    ]


def content_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = lexical_tokens(left), lexical_tokens(right)
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 0.0


def filter_and_deduplicate(question: str, items: list[RankedMatch]) -> list[RankedMatch]:
    selected: list[RankedMatch] = []
    for item in items:
        chunk, _, score = item
        lexical = lexical_score(question, f"{getattr(chunk, 'section_title', '')} {chunk.content}")
        if score < settings.evidence_min_score and lexical < settings.evidence_min_lexical_score:
            continue
        if noise_score(chunk.content) >= 0.55:
            continue
        if any(content_similarity(chunk.content, existing[0].content) >= 0.72 for existing in selected):
            continue
        selected.append(item)
        if len(selected) >= settings.rerank_top_k:
            break
    return selected


def rerank(question: str, candidates: list[RankedMatch]) -> list[RankedMatch]:
    if not candidates:
        return []
    reranked = external_rerank(question, candidates) or llm_rerank(question, candidates) or candidates
    return filter_and_deduplicate(question, reranked)


def retrieve(db: Session, user_id: int, knowledge_base_id: int, question: str):
    return rerank(question, retrieve_candidates(db, user_id, knowledge_base_id, question))


def evidence_is_sufficient(question: str, matches: list[RankedMatch]) -> bool:
    if not matches:
        return False
    top_chunk, _, top_score = matches[0]
    lexical = lexical_score(
        question, f"{getattr(top_chunk, 'section_title', '')} {top_chunk.content}"
    )
    return top_score >= settings.evidence_min_score or lexical >= settings.evidence_min_lexical_score


def fallback_answer(contexts: list[str]) -> str:
    return INSUFFICIENT_ANSWER if not contexts else "根据知识库资料：\n\n" + "\n\n".join(contexts)


def format_history(history: list[tuple[str, str]]) -> str:
    labels = {"user": "User", "assistant": "Assistant"}
    return "\n".join(f"{labels.get(role, role)}: {content}" for role, content in history)


def contextualize_question(question: str, history: list[tuple[str, str]]) -> str:
    return question if not history else f"{format_history(history)}\nUser: {question}"


def generate_answer(
    question: str,
    contexts: list[str],
    history: list[tuple[str, str]] | None = None,
    retry_feedback: str = "",
) -> str:
    if not contexts or not settings.llm_api_key:
        return fallback_answer(contexts)
    history = history or []
    prompt = "\n\n".join(f"[Source {index + 1}]\n{text}" for index, text in enumerate(contexts))
    conversation = format_history(history)
    conversation_prompt = f"\n\nRecent conversation:\n{conversation}" if conversation else ""
    feedback_prompt = f"\n\nPrevious answer issue: {retry_feedback}" if retry_feedback else ""
    try:
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Answer only from the supplied knowledge-base sources. "
                            "Every important fact and number must be supported by a source. "
                            "Use [Source N] markers for important claims. "
                            "If sources are insufficient, say so clearly and do not invent facts."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Knowledge-base sources:\n{prompt}{conversation_prompt}"
                            f"{feedback_prompt}\n\nCurrent question: {question}"
                        ),
                    },
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError):
        return fallback_answer(contexts)


def factual_numbers(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text)
    values = set(re.findall(r"\d+(?:\.\d+)?(?:万|亿|天|年|个|小时|工作日)", normalized))
    values.update(re.findall(r"(?<![\d.])\d{5,}(?![\d.])", normalized))
    return values


def validate_answer(answer: str, contexts: list[str]) -> tuple[bool, str]:
    if answer == INSUFFICIENT_ANSWER:
        return True, ""
    evidence = "\n".join(contexts)
    unsupported = factual_numbers(answer) - factual_numbers(evidence)
    if unsupported:
        return False, f"Unsupported factual numbers: {', '.join(sorted(unsupported))}"
    return True, ""


def relevant_excerpt(question: str, content: str, limit: int = 260) -> str:
    if len(content) <= limit:
        return content
    tokens = lexical_tokens(question)
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[。！？\n])", content) if sentence.strip()]
    best = max(sentences, key=lambda sentence: len(tokens & lexical_tokens(sentence)), default=content)
    position = content.find(best)
    start = max(0, position - 60)
    end = min(len(content), start + limit)
    return content[start:end].strip()


def retrieval_node(state: RAGState) -> dict:
    started = time.perf_counter()
    retrieval_question = contextualize_question(state["question"], state.get("history", []))
    candidates = retrieve_candidates(
        state["db"], state["user_id"], state["knowledge_base_id"], retrieval_question
    )
    return {
        "retrieval_question": retrieval_question,
        "candidates": candidates,
        "timings": {"retrieve": time.perf_counter() - started},
    }


def rerank_node(state: RAGState) -> dict:
    started = time.perf_counter()
    matches = rerank(state["retrieval_question"], state.get("candidates", []))
    timings = {**state.get("timings", {}), "rerank": time.perf_counter() - started}
    return {"matches": matches, "timings": timings}


def assess_evidence_node(state: RAGState) -> dict:
    sufficient = evidence_is_sufficient(state["question"], state.get("matches", []))
    return {
        "evidence_sufficient": sufficient,
        "refusal_reason": "" if sufficient else "insufficient_evidence",
    }


def answer_node(state: RAGState) -> dict:
    started = time.perf_counter()
    contexts = [chunk.content for chunk, _, _ in state.get("matches", [])]
    answer = (
        generate_answer(state["question"], contexts, state.get("history", []))
        if state.get("evidence_sufficient")
        else INSUFFICIENT_ANSWER
    )
    timings = {**state.get("timings", {}), "answer": time.perf_counter() - started}
    return {"answer": answer, "timings": timings}


def validate_answer_node(state: RAGState) -> dict:
    started = time.perf_counter()
    contexts = [chunk.content for chunk, _, _ in state.get("matches", [])]
    valid, feedback = validate_answer(state["answer"], contexts)
    answer = state["answer"]
    if not valid and state.get("evidence_sufficient"):
        answer = generate_answer(
            state["question"], contexts, state.get("history", []), retry_feedback=feedback
        )
        valid, feedback = validate_answer(answer, contexts)
    if not valid:
        answer = INSUFFICIENT_ANSWER
    timings = {**state.get("timings", {}), "validate": time.perf_counter() - started}
    return {
        "answer": answer,
        "validation_passed": valid,
        "refusal_reason": state.get("refusal_reason", "") if valid else feedback,
        "timings": timings,
    }


def citations_node(state: RAGState) -> dict:
    if state["answer"] == INSUFFICIENT_ANSWER:
        return {"citations": []}
    citations = [
        Citation(
            document_id=document.id,
            filename=document.filename,
            chunk_id=chunk.id,
            page_number=chunk.page_number,
            score=round(score, 4),
            excerpt=relevant_excerpt(state["question"], chunk.content),
        )
        for chunk, document, score in state.get("matches", [])
    ]
    return {"citations": citations}


@lru_cache
def rag_graph():
    builder = StateGraph(RAGState)
    builder.add_node("retrieve", retrieval_node)
    builder.add_node("rerank", rerank_node)
    builder.add_node("assess_evidence", assess_evidence_node)
    builder.add_node("answer", answer_node)
    builder.add_node("validate_answer", validate_answer_node)
    builder.add_node("assemble_citations", citations_node)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "assess_evidence")
    builder.add_edge("assess_evidence", "answer")
    builder.add_edge("answer", "validate_answer")
    builder.add_edge("validate_answer", "assemble_citations")
    builder.add_edge("assemble_citations", END)
    return builder.compile()


def answer_question(
    db: Session,
    user_id: int,
    knowledge_base_id: int,
    question: str,
    history: list[tuple[str, str]] | None = None,
):
    started = time.perf_counter()
    result = rag_graph().invoke(
        {
            "db": db,
            "user_id": user_id,
            "knowledge_base_id": knowledge_base_id,
            "question": question,
            "history": history or [],
        }
    )
    logger.info(
        "rag_complete kb=%s candidates=%s matches=%s citations=%s refusal=%s validation=%s "
        "timings=%s total=%.3f",
        knowledge_base_id,
        [(item[0].id, round(item[2], 3)) for item in result.get("candidates", [])],
        [(item[0].id, round(item[2], 3)) for item in result.get("matches", [])],
        [item.chunk_id for item in result.get("citations", [])],
        result.get("refusal_reason") or "none",
        result.get("validation_passed"),
        {key: round(value, 3) for key, value in result.get("timings", {}).items()},
        time.perf_counter() - started,
    )
    return result["answer"], result["citations"]
