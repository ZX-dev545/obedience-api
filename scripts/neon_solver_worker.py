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

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from api.v1._common import connect


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
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def load_solver(solver_path: str) -> Any:
    resolved_path = str(Path(solver_path).resolve())
    solver_directory = str(Path(resolved_path).parent)
    if solver_directory not in sys.path:
        sys.path.insert(0, solver_directory)
    module_name = Path(resolved_path).stem
    solver = importlib.import_module(module_name)
    solve_payload = getattr(solver, "solve_payload", None)
    if not callable(solve_payload):
        raise RuntimeError(
            f"{resolved_path} must expose a callable solve_payload(payload)"
        )
    return solve_payload


def claim_job(worker_id: str) -> tuple[str, dict[str, Any]] | None:
    connection = connect()
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, request_json
                    FROM jobs
                    WHERE status = 'queued'
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                job_id, payload = row
                cursor.execute(
                    """
                    UPDATE jobs
                    SET status = 'running',
                        worker_id = %s,
                        claimed_at = now(),
                        lease_expires_at = now() + interval '10 minutes'
                    WHERE id = %s
                    """,
                    (worker_id, job_id),
                )
        return str(job_id), payload
    finally:
        connection.close()


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                SET status = 'completed',
                    result_json = %s::jsonb,
                    error_text = NULL,
                    completed_at = now(),
                    lease_expires_at = NULL
                WHERE id = %s
                """,
                (json.dumps(result), job_id),
            )
        connection.commit()
    finally:
        connection.close()


def fail_job(job_id: str, error: str) -> None:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error_text = %s,
                    completed_at = now(),
                    lease_expires_at = NULL
                WHERE id = %s
                """,
                (error[:4000], job_id),
            )
        connection.commit()
    finally:
        connection.close()


def process_once(solve_payload: Any, worker_id: str) -> bool:
    claimed = claim_job(worker_id)
    if claimed is None:
        return False
    job_id, payload = claimed
    print(f"Claimed job {job_id}")
    try:
        result = solve_payload(payload)
        if not isinstance(result, dict):
            raise TypeError("solve_payload must return a JSON object")
        json.dumps(result)
        complete_job(job_id, result)
        print(f"Completed job {job_id}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"Failed job {job_id}: {error}", file=sys.stderr)
        fail_job(job_id, error)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Neon solver worker.")
    parser.add_argument(
        "--solver-path",
        default=str(Path(__file__).resolve().parents[2] / "solver_worker.py"),
        help="Path to the private solver_worker.py module",
    )
    parser.add_argument("--once", action="store_true", help="Process at most one job")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--worker-id", default=f"local-{uuid.uuid4().hex[:12]}")
    parser.add_argument(
        "--env-file",
        default=str(REPOSITORY_ROOT / ".env.local"),
        help="Environment file to load before connecting to Neon",
    )
    args = parser.parse_args()
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")

    load_env_file(args.env_file)
    solve_payload = load_solver(args.solver_path)
    print(f"Worker {args.worker_id} started")
    while True:
        processed = process_once(solve_payload, args.worker_id)
        if args.once:
            return
        if not processed:
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
