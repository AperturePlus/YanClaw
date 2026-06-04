class BusinessException(Exception):
    code: int = 50000
    status_code: int = 500

    def __init__(self, message: str, code: int = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class LLMCallError(BusinessException):
    code = 50010
    status_code = 500

    def __init__(self, message: str = "LLM 调用失败"):
        super().__init__(message, code=self.code)


class VectorRetrievalError(BusinessException):
    code = 50020
    status_code = 500

    def __init__(self, message: str = "向量检索失败"):
        super().__init__(message, code=self.code)


class GraphRetrievalError(BusinessException):
    code = 50030
    status_code = 500

    def __init__(self, message: str = "图谱检索失败"):
        super().__init__(message, code=self.code)


class ItemNotFoundError(BusinessException):
    code = 40004
    status_code = 404

    def __init__(self, message: str = "数据不存在"):
        super().__init__(message, code=self.code)


class ValidationError(BusinessException):
    code = 40001
    status_code = 400

    def __init__(self, message: str = "参数错误"):
        super().__init__(message, code=self.code)
