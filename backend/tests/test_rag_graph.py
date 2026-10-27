from types import SimpleNamespace

from app.schemas import Citation
from app.services import rag


def test_langgraph_rag_node_order(monkeypatch):
    events: list[str] = []
    candidate = (
        SimpleNamespace(content="Source", id=1, page_number=None),
        SimpleNamespace(id=1, filename="source.txt"),
        0.8,
    )

    def fake_retrieve(db, user_id, knowledge_base_id, question):
        events.append(f"retrieve:{question}")
        return [candidate]

    def fake_rerank(question, candidates):
        events.append("rerank")
        return candidates

    def fake_generate(question, contexts, history):
        events.append("answer")
        return "Graph answer"

    def fake_assess(state):
        events.append("assess")
        return {"evidence_sufficient": True, "refusal_reason": ""}

    def fake_validate(state):
        events.append("validate")
        return {"answer": state["answer"], "validation_passed": True}

    def fake_citations(state):
        events.append("citations")
        return {
            "citations": [
                Citation(
                    document_id=1,
                    filename="source.txt",
                    chunk_id=1,
                    score=0.8,
                    excerpt="Source",
                )
            ]
        }

    monkeypatch.setattr(rag, "retrieve_candidates", fake_retrieve)
    monkeypatch.setattr(rag, "rerank", fake_rerank)
    monkeypatch.setattr(rag, "generate_answer", fake_generate)
    monkeypatch.setattr(rag, "assess_evidence_node", fake_assess)
    monkeypatch.setattr(rag, "validate_answer_node", fake_validate)
    monkeypatch.setattr(rag, "citations_node", fake_citations)
    rag.rag_graph.cache_clear()

    answer, citations = rag.answer_question(
        db=object(),
        user_id=1,
        knowledge_base_id=2,
        question="When does it launch?",
        history=[("user", "Tell me about Atlas.")],
    )

    assert events == [
        "retrieve:User: Tell me about Atlas.\nUser: When does it launch?",
        "rerank",
        "assess",
        "answer",
        "validate",
        "citations",
    ]
    assert answer == "Graph answer"
    assert citations[0].filename == "source.txt"
    rag.rag_graph.cache_clear()
