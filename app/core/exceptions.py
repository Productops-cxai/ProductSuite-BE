class AppError(Exception):
    def __init__(self, message: str, code: str = "app_error"):
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, code="not_found")


class ConflictError(AppError):
    def __init__(self, message: str = "Conflict"):
        super().__init__(message, code="conflict")


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Unauthorized", code: str = "unauthorized"):
        super().__init__(message, code=code)


class ForbiddenError(AppError):
    def __init__(self, message: str = "Forbidden"):
        super().__init__(message, code="forbidden")


class ValidationAppError(AppError):
    def __init__(self, message: str = "Validation failed"):
        super().__init__(message, code="validation_error")
