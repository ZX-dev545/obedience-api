import json
from unittest.mock import patch
from urllib.error import HTTPError
from io import BytesIO

import pytest

from obedience_client import ApiError, JobStatus, ObedienceApiClient


class FakeResponse:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body


def test_submit_and_get_job() -> None:
    client = ObedienceApiClient("https://example.test", "demo-key")
    responses = [
        {"job_id": "job-1", "status": "queued"},
        {
            "job_id": "job-1",
            "status": "completed",
            "result": {"solutions": []},
            "error": None,
            "created_at": None,
            "completed_at": None,
        },
    ]
    with patch("obedience_client.client.urlopen") as open_url:
        open_url.side_effect = [FakeResponse(response) for response in responses]
        assert client.submit_job({"operation": "solve"}) == "job-1"
        status = client.get_job("job-1")
    assert isinstance(status, JobStatus)
    assert status.status == "completed"


def test_api_error_contains_structured_body() -> None:
    client = ObedienceApiClient("https://example.test", "demo-key")
    error = HTTPError(
        "https://example.test/api/v1/jobs",
        422,
        "unprocessable",
        {},
        BytesIO(b'{"error":"unsupported_solver_input","message":"bad field"}'),
    )
    with patch("obedience_client.client.urlopen", side_effect=error):
        with pytest.raises(ApiError) as raised:
            client.submit_job({})
    assert raised.value.status_code == 422
    assert raised.value.body["error"] == "unsupported_solver_input"


def test_wait_for_job_returns_terminal_status() -> None:
    client = ObedienceApiClient("https://example.test", "demo-key")
    with patch.object(
        client,
        "get_job",
        side_effect=[
            JobStatus("job-1", "queued", None, None, None, None),
            JobStatus("job-1", "completed", {"solutions": []}, None, None, None),
        ],
    ), patch("obedience_client.client.time.sleep"):
        status = client.wait_for_job("job-1", poll_interval=1, timeout=5)
    assert status.status == "completed"
