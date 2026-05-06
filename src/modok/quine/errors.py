class QuineError(Exception):
    pass


class QuineNodeNotFoundError(QuineError):
    pass


class QuineNodeTypeConflictError(QuineError):
    pass


class QuineDeserializationError(QuineError):
    pass
