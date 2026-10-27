from evaluation.evaluate import (
    answer_keyword_coverage,
    citation_accuracy,
    faithfulness,
    retrieval_hit,
)


CASE = {
    "expected_answer_keywords": ["7天退货", "15天换货"],
    "expected_evidence_keywords": ["7天退货", "15天换货"],
    "expected_pages": [3],
}


def test_retrieval_and_citation_metrics_use_expected_evidence():
    citations = [
        {"page_number": 3, "excerpt": "商城通常提供7天退货、15天换货。"},
        {"page_number": 1, "excerpt": "汇编封面。"},
    ]

    assert retrieval_hit(citations, "通常支持7天退货和15天换货。", CASE) == 1
    assert citation_accuracy(citations, CASE) == 0.5
    assert answer_keyword_coverage("通常支持7天退货和15天换货。", CASE) == 1


def test_faithfulness_penalizes_unsupported_fact_number():
    citations = [{"page_number": 3, "excerpt": "商城通常提供7天退货、15天换货。"}]

    supported = faithfulness("商城支持7天退货和15天换货。", citations, CASE)
    unsupported = faithfulness("商城支持30天退货和15天换货。", citations, CASE)

    assert supported > unsupported


def test_unanswerable_case_rewards_clear_insufficiency():
    case = {
        "expected_answer_keywords": [],
        "expected_evidence_keywords": [],
        "expected_pages": [],
        "expected_behavior": "insufficient",
    }

    assert retrieval_hit([], "资料未提供所有手机终身免费维修的承诺。", case) == 1
    assert faithfulness("现有资料不足，无法确认。", [], case) == 1
    assert citation_accuracy([], case) is None
