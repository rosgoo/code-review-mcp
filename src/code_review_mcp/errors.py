class ReviewError(Exception):
    pass


class NotFoundError(ReviewError):
    pass


class InvalidPathError(ReviewError):
    pass
