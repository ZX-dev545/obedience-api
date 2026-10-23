from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def load_env_file(filename: str) -> None:
    path = Path(filename)
    if not path.exists():
        return
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not name.strip():
            raise ValueError(f"Invalid environment entry on line {line_number}")
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def load_solver(solver_path: str) -> Any:
    path = Path(solver_path).resolve()
    sys.path.insert(0, str(path.parent))
    solver = importlib.import_module(path.stem)
    solve_payload = getattr(solver, "solve_payload", None)
    if not callable(solve_payload):
        raise RuntimeError(f"{path} must expose solve_payload(payload)")
    return solve_payload


def call_api(base_url: str, token: str, worker_id: str, path: str,
             bypass_secret: str | None = None, method: str = "POST",
             body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Worker-Id": worker_id,
    }
    if bypass_secret:
        headers["x-vercel-protection-bypass"] = bypass_secret
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status == 204:
                return None
            return json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach worker API: {exc.reason}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the solver through the API.")
    parser.add_argument("--api-url", default=os.environ.get("OBEDIENCE_API_URL"))
    parser.add_argument("--worker-token", default=os.environ.get("WORKER_AUTH_TOKEN"))
    parser.add_argument("--solver-path", required=True)
    parser.add_argument("--env-file", default=".env.local")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--worker-id", default=f"local-{uuid.uuid4().hex[:12]}")
    args = parser.parse_args()
    load_env_file(args.env_file)
    api_url = args.api_url or os.environ.get("OBEDIENCE_API_URL")
    token = args.worker_token or os.environ.get("WORKER_AUTH_TOKEN")
    bypass_secret = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
    if not api_url or not token:
        parser.error("OBEDIENCE_API_URL and WORKER_AUTH_TOKEN are required")
    solve_payload = load_solver(args.solver_path)
    while True:
        job = call_api(
            api_url,
            token,
            args.worker_id,
            "/api/v1/worker/claim",
            bypass_secret,
        )
        if job is None:
            if args.once:
                return
            time.sleep(args.poll_interval)
            continue
        job_id = job["job_id"]
        try:
            result = solve_payload(job["payload"])
            if not isinstance(result, dict):
                raise TypeError("solve_payload must return a JSON object")
            json.dumps(result)
            call_api(
                api_url,
                token,
                args.worker_id,
                f"/api/v1/worker/jobs/{job_id}/complete",
                bypass_secret,
                body={"result": result},
            )
            print(f"Completed job {job_id}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            call_api(
                api_url,
                token,
                args.worker_id,
                f"/api/v1/worker/jobs/{job_id}/fail",
                bypass_secret,
                body={"error": error},
            )
            print(f"Failed job {job_id}: {error}", file=sys.stderr)
        if args.once:
            return


if __name__ == "__main__":
    main()
