from typing import Any

from ._common import authenticate_client, connect, json_response


def handle_request(request: Any) -> tuple[int, dict[str, str], str]:
    if request.method != "GET":
        return json_response(405, {"error": "method_not_allowed"})
    job_id = str(request.query.get("job_id", "")).strip()
    if not job_id:
        return json_response(400, {"error": "missing_job_id"})
    connection = connect()
    try:
        user_id = authenticate_client(request, connection)
        if user_id is None:
            return json_response(401, {"error": "unauthorized"})
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, result_json, error_text, created_at, completed_at
                FROM jobs
                WHERE id = %s AND user_id = %s
                """,
                (job_id, user_id),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if row is None:
        return json_response(404, {"error": "job_not_found"})
    status, result, error, created_at, completed_at = row
    return json_response(
        200,
        {
            "job_id": job_id,
            "status": status,
            "result": result,
            "error": error,
            "created_at": created_at.isoformat() if created_at else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
        },
    )
