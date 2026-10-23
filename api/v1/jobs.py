import json
import uuid
from http.server import BaseHTTPRequestHandler
from typing import Any

from ._common import (
    Request,
    UnsupportedSolverInput,
    authenticate_client,
    connect,
    json_response,
    query_parameters,
    request_json,
    validate_job_request,
    write_response,
)


def handle_request(request: Any) -> tuple[int, dict[str, str], str]:
    if request.method != "POST":
        return json_response(405, {"error": "method_not_allowed"})
    try:
        payload = request_json(request)
        validate_job_request(payload)
        job_id = str(uuid.uuid4())
        connection = connect()
        try:
            user_id = authenticate_client(request, connection)
            if user_id is None:
                return json_response(401, {"error": "unauthorized"})
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO jobs (id, user_id, request_json, status)
                    VALUES (%s, %s, %s::jsonb, 'queued')
                    """,
                    (job_id, user_id, json.dumps(payload)),
                )
            connection.commit()
        finally:
            connection.close()
        return json_response(202, {"job_id": job_id, "status": "queued"})
    except UnsupportedSolverInput as exc:
        return json_response(
            422,
            {"error": "unsupported_solver_input", "message": str(exc)},
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return json_response(400, {"error": "invalid_request", "message": str(exc)})
    except Exception:
        return json_response(500, {"error": "internal_server_error"})


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        write_response(self, handle_request(Request(self, query_parameters(self))))
