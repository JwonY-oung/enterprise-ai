from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.routers import chat as chat_router


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
        json={"name": "Conversation docs", "description": "History test"},
    )
    return response.json()["id"]


def test_session_history_api_and_rag_context(monkeypatch):
    captured_histories: list[list[tuple[str, str]]] = []

    def fake_answer_question(db, user_id, knowledge_base_id, question, history=None):
        captured_histories.append(history or [])
        return f"Answer to: {question}", []

    monkeypatch.setattr(chat_router, "answer_question", fake_answer_question)

    with TestClient(app) as client:
        owner = register(client)
        stranger = register(client)
        knowledge_base_id = create_kb(client, owner)

        first = client.post(
            "/api/chat/ask",
            headers=owner,
            json={"knowledge_base_id": knowledge_base_id, "question": "Tell me about Atlas."},
        )
        assert first.status_code == 200
        session_id = first.json()["session_id"]

        second = client.post(
            "/api/chat/ask",
            headers=owner,
            json={
                "knowledge_base_id": knowledge_base_id,
                "session_id": session_id,
                "question": "When does it launch?",
            },
        )
        assert second.status_code == 200
        assert captured_histories == [
            [],
            [
                ("user", "Tell me about Atlas."),
                ("assistant", "Answer to: Tell me about Atlas."),
            ],
        ]

        sessions = client.get(
            f"/api/chat/sessions?knowledge_base_id={knowledge_base_id}",
            headers=owner,
        )
        assert sessions.status_code == 200
        assert sessions.json()[0]["id"] == session_id
        assert sessions.json()[0]["message_count"] == 4

        detail = client.get(f"/api/chat/sessions/{session_id}", headers=owner)
        assert detail.status_code == 200
        assert [message["role"] for message in detail.json()["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]

        assert client.get(f"/api/chat/sessions/{session_id}", headers=stranger).status_code == 404
        assert client.delete(f"/api/chat/sessions/{session_id}", headers=stranger).status_code == 404
        assert client.delete(f"/api/chat/sessions/{session_id}", headers=owner).status_code == 204
        assert client.get(f"/api/chat/sessions/{session_id}", headers=owner).status_code == 404


def test_unknown_session_id_is_rejected(monkeypatch):
    monkeypatch.setattr(chat_router, "answer_question", lambda *args, **kwargs: ("unused", []))

    with TestClient(app) as client:
        headers = register(client)
        knowledge_base_id = create_kb(client, headers)
        response = client.post(
            "/api/chat/ask",
            headers=headers,
            json={
                "knowledge_base_id": knowledge_base_id,
                "session_id": 999_999_999,
                "question": "Continue the conversation.",
            },
        )
        assert response.status_code == 404
