class IngestionError(Exception):
    pass

class RegistryNotFoundError(IngestionError):
    pass

class InvalidSlugReferenceError(IngestionError):
    pass

class MissingCommitShaError(IngestionError):
    pass

class MissingRequiredFieldError(IngestionError):
    pass
