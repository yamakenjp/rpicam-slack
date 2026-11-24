"""Slack upload with retries, and best-effort alerts."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Errors that retrying cannot fix (wrong token, bot not in the channel, missing scope, ...).
PERMANENT_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "missing_scope",
        "no_permission",
        "restricted_action",
    }
)
MAX_RETRY_AFTER_S = 30.0


class UploadError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class Uploader(Protocol):
    def upload(self, path: Path, comment: str, filename: str) -> None: ...

    def alert(self, text: str) -> bool: ...


def _default_client(**kwargs: Any) -> Any:
    from slack_sdk import WebClient

    return WebClient(**kwargs)


def _api_error(exc: Exception) -> tuple[str | None, int | None, Any]:
    """(error code, HTTP status, headers) of a SlackApiError-like exception, else Nones."""
    response = getattr(exc, "response", None)
    if response is None:
        return None, None, {}
    code = response.get("error") if hasattr(response, "get") else None
    return code, getattr(response, "status_code", None), getattr(response, "headers", {}) or {}


def describe(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown error"
    code, status, _ = _api_error(exc)  # type: ignore[arg-type]
    if code:
        return f"slack error {code}" + (f" (HTTP {status})" if status else "")
    return f"{type(exc).__name__}: {exc}"


class SlackUploader:
    def __init__(
        self,
        token: str,
        channel: str,
        *,
        timeout_s: float = 30.0,
        retries: int = 3,
        retry_wait_s: float = 3.0,
        client_factory: Callable[..., Any] = _default_client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._channel = channel
        self._timeout_s = timeout_s
        self._retries = retries
        self._retry_wait_s = retry_wait_s
        self._factory = client_factory
        self._sleep = sleep
        self._client_obj: Any = None

    def _client(self) -> Any:
        if self._client_obj is None:
            self._client_obj = self._factory(token=self._token, timeout=self._timeout_s)
        return self._client_obj

    def _wait_for(self, exc: Exception, attempt: int) -> float:
        code, status, headers = _api_error(exc)
        if code == "ratelimited" or status == 429:
            try:
                return min(float(headers.get("Retry-After", self._retry_wait_s)), MAX_RETRY_AFTER_S)
            except (TypeError, ValueError):
                return self._retry_wait_s
        return self._retry_wait_s * attempt

    def upload(self, path: Path, comment: str, filename: str) -> None:
        last: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                self._client().files_upload_v2(
                    channel=self._channel,
                    file=str(path),
                    filename=filename,
                    title=filename,
                    initial_comment=comment,
                )
                return
            except Exception as exc:
                code, _, _ = _api_error(exc)
                if code in PERMANENT_ERRORS:
                    raise UploadError(f"upload rejected: {describe(exc)}", permanent=True) from exc
                last = exc
                log.warning("upload attempt %d/%d failed: %s", attempt, self._retries, describe(exc))
                if attempt < self._retries:
                    self._sleep(self._wait_for(exc, attempt))
        raise UploadError(f"upload failed after {self._retries} attempts: {describe(last)}") from last

    def alert(self, text: str) -> bool:
        """Post a short text message. Never raises: alerts must not make a failure worse."""
        try:
            self._client().chat_postMessage(channel=self._channel, text=text)
            return True
        except Exception as exc:
            log.warning("could not post alert: %s", describe(exc))
            return False
