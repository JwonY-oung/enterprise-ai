from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def test_minimum_knowledge_base_flow():
    with TestClient(app) as client:
        email = f"{uuid4().hex}@example.com"
        registered = client.post(
            "/api/auth/register",
            json={"email": email, "password": "password123"},
        )
        assert registered.status_code == 201
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

        created = client.post(
            "/api/knowledge-bases",
            headers=headers,
            json={"name": "Product docs", "description": "Test knowledge base"},
        )
        assert created.status_code == 201
        knowledge_base_id = created.json()["id"]

        uploaded = client.post(
            f"/api/documents/upload?knowledge_base_id={knowledge_base_id}",
            headers=headers,
            files={"file": ("product.txt", "核心卖点是部署简单，回答带有引用。", "text/plain")},
        )
        assert uploaded.status_code == 202
        assert uploaded.json()["status"] == "processing"

        answered = client.post(
            "/api/chat/ask",
            headers=headers,
            json={"knowledge_base_id": knowledge_base_id, "question": "核心卖点是什么？"},
        )
        assert answered.status_code == 200
        assert answered.json()["citations"]

