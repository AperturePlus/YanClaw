import pytest
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.models.base import engine
from app.models.item import Item

client = TestClient(app)


def _real_item_ids(limit: int = 2) -> list[int]:
    with Session(engine) as session:
        return [
            item.id
            for item in session.exec(select(Item).where(Item.source_id.is_not(None)).limit(limit)).all()
            if item.id is not None
        ]


class TestHealth:
    def test_health_check(self):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRecommendAPI:
    @pytest.mark.realdata
    def test_recommend_basic(self):
        response = client.post(
            "/api/v1/recommend",
            json={"query": "NLP方向导师推荐", "top_k": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data

    @pytest.mark.realdata
    def test_recommend_with_filters(self):
        response = client.post(
            "/api/v1/recommend",
            json={
                "query": "计算机视觉教授",
                "filters": {"org_unit": "计算机学院"},
                "top_k": 3,
            },
        )
        assert response.status_code == 200

    @pytest.mark.realdata
    def test_recommend_home(self):
        response = client.get("/api/v1/recommend/home?top_k=5")
        assert response.status_code == 200


class TestItemsAPI:
    @pytest.mark.realdata
    def test_search_items(self):
        response = client.get("/api/v1/items/search?keyword=张&page=1&page_size=10")
        assert response.status_code == 200

    @pytest.mark.realdata
    def test_get_item(self):
        item_id = _real_item_ids(limit=1)[0]
        response = client.get(f"/api/v1/items/{item_id}")
        assert response.status_code == 200


class TestUsersAPI:
    @pytest.mark.realdata
    def test_create_user(self):
        username = f"test_user_{uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/users",
            json={"username": username, "research_interests": "机器学习"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["username"] == username

    @pytest.mark.realdata
    def test_get_user(self):
        response = client.get("/api/v1/users/1")
        assert response.status_code == 200


class TestGraphAPI:
    @pytest.mark.realdata
    def test_get_neighbors(self):
        item_id = _real_item_ids(limit=1)[0]
        response = client.get(f"/api/v1/graph/neighbors/Item_{item_id}?limit=10")
        assert response.status_code == 200

    @pytest.mark.realdata
    def test_find_paths(self):
        left, right = _real_item_ids(limit=2)
        response = client.get(f"/api/v1/graph/path?source=Item_{left}&target=Item_{right}&max_depth=3")
        assert response.status_code == 200
