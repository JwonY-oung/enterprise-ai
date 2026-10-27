from types import SimpleNamespace

from app.services import rag


def match(content: str, score: float = 0.8, section: str = ""):
    return (
        SimpleNamespace(
            id=1,
            content=content,
            section_title=section,
            page_number=1,
        ),
        SimpleNamespace(id=1, filename="source.pdf"),
        score,
    )


def test_deduplicate_and_filter_noise():
    items = [
        match("服务热线为950800，提供7天24小时服务。", 0.8),
        match("服务热线为950800，提供7天24小时服务。", 0.79),
        match("附录：官方来源目录\nhttps://example.com\nhttps://example.org", 0.7),
    ]

    selected = rag.filter_and_deduplicate("服务热线是多少？", items)

    assert len(selected) == 1
    assert "950800" in selected[0][0].content


def test_relevant_excerpt_centers_on_question_match():
    content = "无关开头。" * 80 + "退款通常原路退回，处理周期为3至5个工作日。" + "无关结尾。" * 80

    excerpt = rag.relevant_excerpt("退款处理周期多久？", content)

    assert "3至5个工作日" in excerpt
    assert len(excerpt) <= 260


def test_evidence_threshold_and_number_validation():
    assert not rag.evidence_is_sufficient("完全无关的问题", [match("另一份资料", 0.1)])
    assert rag.evidence_is_sufficient("服务热线是多少", [match("服务热线为950800", 0.8)])
    assert rag.validate_answer("服务热线为950800。", ["服务热线为950800。"])[0]
    assert not rag.validate_answer("服务热线为950805。", ["服务热线为950800。"])[0]
