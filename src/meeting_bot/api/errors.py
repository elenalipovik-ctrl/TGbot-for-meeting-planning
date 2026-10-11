"""Safe errors; never attach request bodies, tokens or upstream exceptions."""

class ApiError(Exception):
    def __init__(self, status, code, message, *, retryable=False, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.retry_after = retryable, retry_after
