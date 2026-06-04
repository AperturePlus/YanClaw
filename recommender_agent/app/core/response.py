from typing import TypeVar, Generic, Optional, Any
from pydantic import BaseModel

T = TypeVar("T")


class BaseResponse(BaseModel, Generic[T]):
    code: int = 0
    message: str = "success"
    data: Optional[T] = None
    request_id: str = ""


def success_response(data: Any, request_id: str = "") -> dict:
    return {"code": 0, "message": "success", "data": data, "request_id": request_id}


def error_response(code: int, message: str, request_id: str = "") -> dict:
    return {"code": code, "message": message, "data": None, "request_id": request_id}
