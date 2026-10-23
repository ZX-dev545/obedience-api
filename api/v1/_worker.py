import hmac
import json
import os
from typing import Any

from ._common import connect, json_response, request_json


def _authorized(request: Any) -> bool:
    configured = os.environ.get("WORKER_AUTH_TOKEN")
    supplied = request.headers.get("authorization", "")
    scheme, _, token = supplied.partition(" ")
    return bool(
        configured
        and scheme.lower() == "bearer"
        and hmac.compare_digest(token, configured)
    )


def _worker_id(request: Any) -> str:
    return request.headers.get("x-worker-id", "unknown")[:200]


def claim(request: Any) -> tuple[int, dict[str, str], str]:
    if request.method != "POST":
        return json_response(405, {"error": "method_not_allowed"})
    if not _authorized(request):
        return json_response(401, {"error": "worker_unauthorized"})
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
                    return json_response(204, {})
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
                    (_worker_id(request), job_id),
                )
        return json_response(
            200,
            {"job_id": str(job_id), "payload": payload},
        )
    finally:
        connection.close()


def complete(request: Any, job_id: str) -> tuple[int, dict[str, str], str]:
    return _finish(request, job_id, "completed")


def fail(request: Any, job_id: str) -> tuple[int, dict[str, str], str]:
    return _finish(request, job_id, "failed")


def _finish(
    request: Any, job_id: str, status: str
) -> tuple[int, dict[str, str], str]:
    if request.method != "POST":
        return json_response(405, {"error": "method_not_allowed"})
    if not _authorized(request):
        return json_response(401, {"error": "worker_unauthorized"})
    payload = request_json(request)
    connection = connect()
    try:
        with connection.cursor() as cursor:
            if status == "completed":
                result = payload.get("result")
                if not isinstance(result, dict):
                    return json_response(400, {"error": "result_must_be_object"})
                cursor.execute(
                    """
                    UPDATE jobs
                    SET status = 'completed',
                        result_json = %s::jsonb,
                        error_text = NULL,
                        completed_at = now(),
                        lease_expires_at = NULL
                    WHERE id = %s AND status = 'running'
                    """,
                    (json.dumps(result), job_id),
                )
            else:
                error = payload.get("error")
                if not isinstance(error, str) or not error:
                    return json_response(400, {"error": "error_must_be_non_empty"})
                cursor.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        error_text = %s,
                        completed_at = now(),
                        lease_expires_at = NULL
                    WHERE id = %s AND status = 'running'
                    """,
                    (error[:4000], job_id),
                )
            if cursor.rowcount != 1:
                connection.rollback()
                return json_response(409, {"error": "job_not_running"})
        connection.commit()
    finally:
        connection.close()
    return json_response(200, {"job_id": job_id, "status": status})
