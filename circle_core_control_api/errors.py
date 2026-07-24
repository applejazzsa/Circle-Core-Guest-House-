class ControlAPIError(Exception):
    """A safe, stable error that may cross the product API boundary."""

    def __init__(self, error_code, safe_message, *, status=400, retryable=False, current_state=None):
        super().__init__(safe_message)
        self.error_code = error_code
        self.safe_message = safe_message
        self.status = status
        self.retryable = retryable
        self.current_state = current_state


class AuthenticationError(ControlAPIError):
    def __init__(self, error_code="authentication_failed", safe_message="Request authentication failed.", *, status=401):
        super().__init__(error_code, safe_message, status=status, retryable=False)


class PermissionDeniedError(ControlAPIError):
    def __init__(self, safe_message="The caller is not permitted to perform this operation."):
        super().__init__("permission_denied", safe_message, status=403, retryable=False)
