import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestHealth:
    def test_health_check(self):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRecommendAPI:
    def test_recommend_basic(self):
        response = client.post(
            "/api/v1/recommend",
            json={"query": "NLP方向导师推荐", "top_k": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "data" in data

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

    def test_recommend_home(self):
        response = client.get("/api/v1/recommend/home?top_k=5")
        assert response.status_code == 200


class TestItemsAPI:
    def test_search_items(self):
        response = client.get("/api/v1/items/search?keyword=张&page=1&page_size=10")
        assert response.status_code == 200

    def test_get_item(self):
        response = client.get("/api/v1/items/1")
        assert response.status_code == 200


class TestUsersAPI:
    def test_create_user(self):
        response = client.post(
            "/api/v1/users",
            json={"username": "test_user", "research_interests": "机器学习"},
        )
        assert response.status_code == 200

    def test_get_user(self):
        response = client.get("/api/v1/users/1")
        assert response.status_code == 200


class TestGraphAPI:
    def test_get_neighbors(self):
        response = client.get("/api/v1/graph/neighbors/Item_1?limit=10")
        assert response.status_code == 200

    def test_find_paths(self):
        response = client.get("/api/v1/graph/path?source=Item_1&target=Item_2&max_depth=3")
        assert response.status_code == 200
