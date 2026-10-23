import hashlib
import os
import secrets
import uuid
from http.server import BaseHTTPRequestHandler
from typing import Any

from ._common import Request, connect, json_response, query_parameters, write_response


def handle_request(request: Any) -> tuple[int, dict[str, str], str]:
    if request.method != "POST":
        return json_response(405, {"error": "method_not_allowed"})

    pepper = os.environ.get("API_KEY_PEPPER")
    if not pepper:
        return json_response(500, {"error": "api_key_generation_not_configured"})

    user_id = uuid.uuid4()
    key_id = uuid.uuid4()
    api_key = secrets.token_hex(32)
    key_digest = hashlib.sha256((pepper + api_key).encode("utf-8")).hexdigest()

    try:
        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO users (id) VALUES (%s)", (user_id,))
                cursor.execute(
                    """
                    INSERT INTO api_keys (id, user_id, key_digest)
                    VALUES (%s, %s, %s)
                    """,
                    (key_id, user_id, key_digest),
                )
            connection.commit()
        finally:
            connection.close()
    except Exception as exc:
        print(f"API key generation failed: {type(exc).__name__}: {exc}")
        return json_response(500, {"error": "api_key_generation_failed"})

    return json_response(201, {"api_key": api_key})


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        write_response(self, handle_request(Request(self, query_parameters(self))))
