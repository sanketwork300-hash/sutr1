class ConnectError(Exception):
    """A connected-account operation failed.

    `code` is a stable machine string the UI switches on; `message` is written
    for the person reading it and says what to do next. Named `ConnectError`
    rather than `ConnectionError` so it never shadows the builtin.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
