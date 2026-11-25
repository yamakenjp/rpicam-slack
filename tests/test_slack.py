import time

import pytest
from slack_sdk.errors import SlackApiError

from rpicam_slack.slack import SlackUploader, UploadDeadline, UploadError, UploadFileError


class FakeResponse(dict):
    def __init__(self, error: str, status: int = 200, headers: dict | None = None) -> None:
        super().__init__(error=error)
        self.status_code = status
        self.headers = headers or {}


class FakeClient:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.uploads: list[dict] = []
        self.messages: list[dict] = []

    def _next(self) -> None:
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome

    def files_upload_v2(self, **kwargs) -> None:
        self.uploads.append(kwargs)
        self._next()

    def chat_postMessage(self, **kwargs) -> None:
        self.messages.append(kwargs)
        self._next()


def api_error(code: str, status: int = 200, headers: dict | None = None) -> SlackApiError:
    return SlackApiError(code, FakeResponse(code, status, headers))


def uploader(client: FakeClient, sleeps: list[float], **kwargs) -> SlackUploader:
    return SlackUploader(
        "xoxb-token", "C123", retries=3, retry_wait_s=2.0,
        client_factory=lambda **_: client, sleep=sleeps.append, **kwargs,
    )  # fmt: skip


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    return path


def test_upload_passes_the_file_contents_and_metadata(image):
    client = FakeClient([])
    uploader(client, []).upload(image, "hello", "cam1_20261124.jpg")
    assert client.uploads == [
        {"channel": "C123", "file": b"x", "filename": "cam1_20261124.jpg",
         "title": "cam1_20261124.jpg", "initial_comment": "hello"}
    ]  # fmt: skip


def test_transient_errors_are_retried_with_backoff(image):
    client = FakeClient([OSError("reset"), api_error("internal_error", 500)])
    sleeps: list[float] = []
    uploader(client, sleeps).upload(image, "c", "f.jpg")
    assert len(client.uploads) == 3
    assert sleeps == [2.0, 4.0]


def test_rate_limits_honour_retry_after(image):
    client = FakeClient([api_error("ratelimited", 429, {"Retry-After": "7"})])
    sleeps: list[float] = []
    uploader(client, sleeps).upload(image, "c", "f.jpg")
    assert sleeps == [7.0]


def test_long_retry_after_is_never_shortened(image):
    client = FakeClient([api_error("ratelimited", 429, {"Retry-After": "600"})])
    sleeps: list[float] = []
    uploader(client, sleeps).upload(image, "c", "f.jpg")
    assert sleeps == [600.0]


def test_rate_limit_longer_than_budget_returns_delay_without_retrying(image):
    client = FakeClient([api_error("ratelimited", 429, {"Retry-After": "600"})])
    sleeps = []
    with pytest.raises(UploadError) as info:
        uploader(client, sleeps, clock=lambda: 0).upload(image, "c", "f.jpg", deadline=10)
    assert info.value.retry_after_s == 600
    assert len(client.uploads) == 1 and sleeps == []


def test_in_flight_sdk_operation_is_interrupted_at_the_deadline(image):
    class StuckClient(FakeClient):
        def files_upload_v2(self, **kwargs):
            self.uploads.append(kwargs)
            time.sleep(2)
            pytest.fail("the in-flight SDK call outlasted its deadline")

    client = StuckClient([])
    with pytest.raises(UploadDeadline):
        uploader(client, []).upload(image, "c", "f.jpg", deadline=time.monotonic() + 0.2)
    assert len(client.uploads) == 1


def test_unreadable_file_never_reaches_slack(tmp_path):
    client = FakeClient([])
    with pytest.raises(UploadFileError):
        uploader(client, []).upload(tmp_path / "missing.jpg", "c", "f.jpg")
    assert client.uploads == []


@pytest.mark.parametrize("code", ["invalid_auth", "not_in_channel", "channel_not_found", "missing_scope"])
def test_permanent_errors_are_not_retried(image, code):
    client = FakeClient([api_error(code)])
    sleeps: list[float] = []
    with pytest.raises(UploadError) as info:
        uploader(client, sleeps).upload(image, "c", "f.jpg")
    assert info.value.permanent
    assert len(client.uploads) == 1 and sleeps == []


def test_gives_up_after_the_configured_attempts(image):
    client = FakeClient([OSError("down")] * 5)
    with pytest.raises(UploadError, match="after 3 attempts") as info:
        uploader(client, []).upload(image, "c", "f.jpg")
    assert not info.value.permanent
    assert len(client.uploads) == 3


def test_alert_never_raises():
    ok = FakeClient([])
    assert uploader(ok, []).alert("hello") is True
    assert ok.messages == [{"channel": "C123", "text": "hello"}]
    broken = FakeClient([api_error("invalid_auth")])
    assert uploader(broken, []).alert("hello") is False
