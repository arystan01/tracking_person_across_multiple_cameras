class ReplayEvidenceIncomplete(ValueError):
    """The immutable evidence ledger cannot prove a safe route replay."""


class ReferenceUploadError(ValueError):
    """Stable authenticated reference-image upload failure."""

    def __init__(self, code: str, detail: str, *, status: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status
