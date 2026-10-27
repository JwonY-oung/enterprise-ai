import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.routers import chat as chat_router
from app.schemas import Citation


def register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": f"{uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_kb(client: TestClient, headers: dict[str, str]) -> int:
    response = client.post(
        "/api/knowledge-bases",
        headers=headers,
        json={"name": "Streaming docs", "description": "SSE test"},
    )
    return response.json()["id"]


def parse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


def test_sse_stream_persists_answer_and_citations(monkeypatch):
    citation = Citation(
        document_id=1,
        filename="source.txt",
        chunk_id=1,
        score=0.9,
        excerpt="Source",
    )
    monkeypatch.setattr(
        chat_router,
        "answer_question",
        lambda *args, **kwargs: ("Atlas launches soon.", [citation]),
    )

    with TestClient(app) as client:
        headers = register(client)
        knowledge_base_id = create_kb(client, headers)
        with client.stream(
            "POST",
            "/api/chat/ask/stream",
            headers=headers,
            json={"knowledge_base_id": knowledge_base_id, "question": "When?"},
        ) as response:
            body = response.read().decode()

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_events(body)
        assert events[0][0] == "status"
        assert "".join(data["content"] for event, data in events if event == "token") == (
            "Atlas launches soon."
        )
        assert next(data for event, data in events if event == "citations")["items"][0][
            "filename"
        ] == "source.txt"
        session_id = next(data for event, data in events if event == "done")["session_id"]
        detail = client.get(f"/api/chat/sessions/{session_id}", headers=headers)
        assert [message["role"] for message in detail.json()["messages"]] == [
            "user",
            "assistant",
        ]
