class ToolError(Exception):
    """Expected tool failure; ``message`` is a short Chinese sentence shown to the model / user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
