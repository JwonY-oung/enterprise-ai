from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.main import app
from app.models import ChunkEmbedding


def register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": f"{uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_kb(client: TestClient, headers: dict[str, str], name: str = "Docs") -> int:
    response = client.post(
        "/api/knowledge-bases",
        headers=headers,
        json={"name": name, "description": "Initial"},
    )
    return response.json()["id"]


def test_management_vector_search_and_citations():
    with TestClient(app) as client:
        headers = register(client)
        knowledge_base_id = create_kb(client, headers)

        updated = client.put(
            f"/api/knowledge-bases/{knowledge_base_id}",
            headers=headers,
            json={"name": "Product Center", "description": "Updated"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Product Center"

        uploaded = client.post(
            f"/api/documents/upload?knowledge_base_id={knowledge_base_id}",
            headers=headers,
            files={"file": ("product.md", "星河系统的核心卖点是部署简单、回答可信。", "text/markdown")},
        )
        document_id = uploaded.json()["id"]

        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(ChunkEmbedding)) > 0

        answer = client.post(
            "/api/chat/ask",
            headers=headers,
            json={"knowledge_base_id": knowledge_base_id, "question": "星河系统的核心卖点是什么？"},
        )
        assert answer.status_code == 200
        assert answer.json()["citations"][0]["score"] > 0

        reprocessed = client.post(f"/api/documents/{document_id}/reprocess", headers=headers)
        assert reprocessed.status_code == 202

        deleted = client.delete(f"/api/documents/{document_id}", headers=headers)
        assert deleted.status_code == 204


def test_user_data_isolation():
    with TestClient(app) as client:
        owner = register(client)
        stranger = register(client)
        knowledge_base_id = create_kb(client, owner, "Private")

        assert client.get("/api/knowledge-bases", headers=stranger).json() == []
        assert (
            client.get(
                f"/api/documents?knowledge_base_id={knowledge_base_id}",
                headers=stranger,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/chat/ask",
                headers=stranger,
                json={"knowledge_base_id": knowledge_base_id, "question": "秘密是什么？"},
            ).status_code
            == 404
        )
