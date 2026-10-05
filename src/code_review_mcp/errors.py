class ReviewError(Exception):
    pass


class NotFoundError(ReviewError):
    pass


class InvalidPathError(ReviewError):
    pass


class ConflictError(ReviewError):
    pass


class ForbiddenError(ReviewError):
    pass


class StaleThreadsError(ConflictError):
    def __init__(self, message: str, thread_ids: list[str]) -> None:
        super().__init__(message)
        self.thread_ids = thread_ids
