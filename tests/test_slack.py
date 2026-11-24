import pytest
from slack_sdk.errors import SlackApiError

from rpicam_slack.slack import SlackUploader, UploadError


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


def test_upload_passes_the_legacy_arguments(tmp_path):
    image = tmp_path / "a.jpg"
    image.write_bytes(b"x")
    client = FakeClient([])
    uploader(client, []).upload(image, "hello", "cam1_20261124.jpg")
    assert client.uploads == [
        {"channel": "C123", "file": str(image), "filename": "cam1_20261124.jpg",
         "title": "cam1_20261124.jpg", "initial_comment": "hello"}
    ]  # fmt: skip


def test_transient_errors_are_retried_with_backoff(tmp_path):
    client = FakeClient([OSError("reset"), api_error("internal_error", 500)])
    sleeps: list[float] = []
    uploader(client, sleeps).upload(tmp_path / "a.jpg", "c", "f.jpg")
    assert len(client.uploads) == 3
    assert sleeps == [2.0, 4.0]


def test_rate_limits_honour_retry_after(tmp_path):
    client = FakeClient([api_error("ratelimited", 429, {"Retry-After": "7"})])
    sleeps: list[float] = []
    uploader(client, sleeps).upload(tmp_path / "a.jpg", "c", "f.jpg")
    assert sleeps == [7.0]


def test_retry_after_is_capped(tmp_path):
    client = FakeClient([api_error("ratelimited", 429, {"Retry-After": "600"})])
    sleeps: list[float] = []
    uploader(client, sleeps).upload(tmp_path / "a.jpg", "c", "f.jpg")
    assert sleeps == [30.0]


@pytest.mark.parametrize("code", ["invalid_auth", "not_in_channel", "channel_not_found", "missing_scope"])
def test_permanent_errors_are_not_retried(tmp_path, code):
    client = FakeClient([api_error(code)])
    sleeps: list[float] = []
    with pytest.raises(UploadError) as info:
        uploader(client, sleeps).upload(tmp_path / "a.jpg", "c", "f.jpg")
    assert info.value.permanent
    assert len(client.uploads) == 1 and sleeps == []


def test_gives_up_after_the_configured_attempts(tmp_path):
    client = FakeClient([OSError("down")] * 5)
    with pytest.raises(UploadError, match="after 3 attempts") as info:
        uploader(client, []).upload(tmp_path / "a.jpg", "c", "f.jpg")
    assert not info.value.permanent
    assert len(client.uploads) == 3


def test_alert_never_raises():
    ok = FakeClient([])
    assert uploader(ok, []).alert("hello") is True
    assert ok.messages == [{"channel": "C123", "text": "hello"}]
    broken = FakeClient([api_error("invalid_auth")])
    assert uploader(broken, []).alert("hello") is False
