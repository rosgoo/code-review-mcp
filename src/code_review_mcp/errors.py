class ReviewError(Exception):
    pass


class NotFoundError(ReviewError):
    pass


class InvalidPathError(ReviewError):
    """A path the daemon cannot use."""


class ConflictError(ReviewError):
    pass


class ThrowawayError(ReviewError):
    """Only here for a live review test."""

    code = 1
