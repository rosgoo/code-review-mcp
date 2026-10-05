class ReviewError(Exception):
    pass


class NotFoundError(ReviewError):
    pass


class InvalidPathError(ReviewError):
    pass


class ConflictError(ReviewError):
    pass
