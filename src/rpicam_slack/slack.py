"""Slack upload with retries, and best-effort alerts."""

from __future__ import annotations

import logging
import math
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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


class UploadError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after_s = retry_after_s


class UploadFileError(UploadError):
    """This particular file cannot be sent; other queued files still can.

    ``permanent`` is set when trying the same file again cannot help (Slack refused it, or it is empty).
    """


class UploadDeadline(UploadError):
    """The wall-clock budget for Slack calls has expired."""


class Uploader(Protocol):
    def upload(self, path: Path, comment: str, filename: str, *, deadline: float | None = None) -> None: ...

    def alert(self, text: str, *, deadline: float | None = None) -> bool: ...


def _default_client(**kwargs: Any) -> Any:
    from slack_sdk import WebClient

    # One retry policy owns both the attempt count and the deadline.
    return WebClient(retry_handlers=[], **kwargs)


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if retries < 1:
            raise ValueError("retries must be at least 1")
        self._token = token
        self._channel = channel
        self._timeout_s = timeout_s
        self._retries = retries
        self._retry_wait_s = retry_wait_s
        self._factory = client_factory
        self._sleep = sleep
        self._clock = clock

    @contextmanager
    def _client(self, deadline: float | None) -> Iterator[Any]:
        remaining = None if deadline is None else deadline - self._clock()
        if remaining is not None and remaining <= 0:
            raise UploadDeadline("Slack time budget exhausted")
        timeout = self._timeout_s if remaining is None else min(self._timeout_s, remaining)
        # The CLI runs on the POSIX main thread. Socket timeouts alone allow a multi-request
        # files_upload_v2 call (or a trickling response) to outlast the whole capture budget.
        use_alarm = (
            remaining is not None
            and threading.current_thread() is threading.main_thread()
            and not signal.getitimer(signal.ITIMER_REAL)[0]
        )
        if not use_alarm:
            yield self._factory(token=self._token, timeout=timeout)
            return

        def expire(signum: int, frame: Any) -> None:
            raise UploadDeadline("Slack time budget exhausted")

        previous = signal.signal(signal.SIGALRM, expire)
        try:
            signal.setitimer(signal.ITIMER_REAL, remaining)
            yield self._factory(token=self._token, timeout=timeout)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def _retry_after(self, exc: Exception) -> float | None:
        code, status, headers = _api_error(exc)
        if code == "ratelimited" or status == 429:
            try:
                # Header names arrive in whatever case the server or a proxy chose.
                found = [value for name, value in headers.items() if str(name).lower() == "retry-after"]
                delay = float(found[0]) if found else self._retry_wait_s
                return delay if math.isfinite(delay) and delay >= 0 else self._retry_wait_s
            except (AttributeError, TypeError, ValueError):
                return self._retry_wait_s
        return None

    def upload(self, path: Path, comment: str, filename: str, *, deadline: float | None = None) -> None:
        try:
            contents = path.read_bytes()
        except OSError as exc:
            raise UploadFileError(f"cannot read {path}: {exc}") from exc
        if not contents:
            raise UploadFileError(f"empty file: {path}", permanent=True)
        for attempt in range(1, self._retries + 1):
            sent = False
            try:
                with self._client(deadline) as client:
                    client.files_upload_v2(
                        channel=self._channel,
                        file=contents,
                        filename=filename,
                        title=filename,
                        initial_comment=comment,
                    )
                    sent = True
                return
            except UploadDeadline:
                if sent:  # the timer fired after Slack had answered: the upload stands
                    return
                raise
            except Exception as exc:
                code, _, _ = _api_error(exc)
                if code in {"file_type_not_allowed", "file_upload_size_restricted"}:
                    raise UploadFileError(f"file rejected: {describe(exc)}", permanent=True) from exc
                if code in PERMANENT_ERRORS:
                    raise UploadError(f"upload rejected: {describe(exc)}", permanent=True) from exc
                log.warning("upload attempt %d/%d failed: %s", attempt, self._retries, describe(exc))
                retry_after = self._retry_after(exc)
                wait = retry_after if retry_after is not None else self._retry_wait_s * attempt
                if attempt == self._retries or (deadline is not None and self._clock() + wait >= deadline):
                    raise UploadError(
                        f"upload failed after {attempt} attempts: {describe(exc)}", retry_after_s=retry_after
                    ) from exc
                self._sleep(wait)

    def alert(self, text: str, *, deadline: float | None = None) -> bool:
        """Post a short text message. Never raises: alerts must not make a failure worse."""
        try:
            with self._client(deadline) as client:
                client.chat_postMessage(channel=self._channel, text=text)
            return True
        except Exception as exc:
            log.warning("could not post alert: %s", describe(exc))
            return False
