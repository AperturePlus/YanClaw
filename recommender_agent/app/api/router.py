from fastapi import APIRouter

from app.api.v1 import recommend, items, graph, feedback, users, admin

api_router = APIRouter()
api_router.include_router(recommend.router, tags=["Recommend"])
api_router.include_router(items.router, tags=["Items"])
api_router.include_router(graph.router, tags=["Graph"])
api_router.include_router(feedback.router, tags=["Feedback"])
api_router.include_router(users.router, tags=["Users"])
api_router.include_router(admin.router, tags=["Admin"])
