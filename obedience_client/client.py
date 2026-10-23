from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ObedienceApiError(RuntimeError):
    """Base error for failed Obedience API requests."""


class ApiError(ObedienceApiError):
    """An HTTP or malformed-response error returned by the API."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"API request failed with HTTP {status_code}: {message}")
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    status: str
    result: dict[str, Any] | None
    error: str | None
    created_at: str | None
    completed_at: str | None


class ObedienceApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def generate_api_key(self) -> str:
        body = self._request("POST", "/api/v1/keys", authenticated=False)
        api_key = body.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise ObedienceApiError("API response did not contain an api_key")
        return api_key

    def submit_job(self, payload: dict[str, Any]) -> str:
        body = self._request("POST", "/api/v1/jobs", payload)
        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ObedienceApiError("API response did not contain a job_id")
        return job_id

    def get_job(self, job_id: str) -> JobStatus:
        if not job_id:
            raise ValueError("job_id must not be empty")
        body = self._request("GET", f"/api/v1/jobs/{job_id}")
        return JobStatus(
            job_id=str(body.get("job_id", job_id)),
            status=self._required_string(body, "status"),
            result=body.get("result"),
            error=body.get("error"),
            created_at=body.get("created_at"),
            completed_at=body.get("completed_at"),
        )

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
    ) -> JobStatus:
        if poll_interval <= 0 or timeout <= 0:
            raise ValueError("poll_interval and timeout must be positive")
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_job(job_id)
            if status.status not in {"queued", "running"}:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Job {job_id} did not finish within {timeout:g}s")
            time.sleep(min(poll_interval, remaining))

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            if not self.api_key:
                raise ValueError("api_key is required for this request")
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise self._api_error(exc.code, raw) from exc
        except URLError as exc:
            raise ObedienceApiError(f"Could not reach the API: {exc.reason}") from exc
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ObedienceApiError("API returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ObedienceApiError("API returned a non-object JSON response")
        return body

    @staticmethod
    def _required_string(body: dict[str, Any], field: str) -> str:
        value = body.get(field)
        if not isinstance(value, str) or not value:
            raise ObedienceApiError(f"API response did not contain a valid {field}")
        return value

    @staticmethod
    def _api_error(status_code: int, raw: str) -> ApiError:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return ApiError(status_code, raw[:500] or "empty response")
        if isinstance(body, dict):
            message = body.get("message") or body.get("error") or "unknown error"
            return ApiError(status_code, str(message), body)
        return ApiError(status_code, "non-object error response", body)
