class LaiCacError(Exception):
    """Base error safe to present to a local app user."""


class MissingDependencyError(LaiCacError):
    pass


class InvalidInputError(LaiCacError):
    pass


class InsufficientObservationsError(LaiCacError):
    pass

