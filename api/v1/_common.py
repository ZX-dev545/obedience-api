import hashlib
import hmac
import json
import os
from typing import Any
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import psycopg


def database_url() -> str:
    value = os.environ.get("NAPIQ_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("NAPIQ_DATABASE_URL or DATABASE_URL is not configured")
    return value


def connect() -> psycopg.Connection[Any]:
    return psycopg.connect(database_url())


def json_response(
    status: int, body: dict[str, Any]
) -> tuple[int, dict[str, str], str]:
    return (
        status,
        {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        json.dumps(body, separators=(",", ":")),
    )


def write_response(
    response: BaseHTTPRequestHandler, result: tuple[int, dict[str, str], str]
) -> None:
    status, headers, body = result
    encoded_body = body.encode("utf-8")
    response.send_response(status)
    for name, value in headers.items():
        response.send_header(name, value)
    response.send_header("Content-Length", str(len(encoded_body)))
    response.end_headers()
    response.wfile.write(encoded_body)


class Request:
    def __init__(self, request: BaseHTTPRequestHandler, query: dict[str, str]):
        self.method = request.command
        self.headers = request.headers
        length = int(request.headers.get("Content-Length", "0"))
        self.body = request.rfile.read(length)
        self.query = query


def query_parameters(request: BaseHTTPRequestHandler) -> dict[str, str]:
    parsed = parse_qs(urlparse(request.path).query)
    return {key: values[-1] for key, values in parsed.items() if values}


def request_json(request: Any) -> dict[str, Any]:
    raw = request.body
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def bearer_token(request: Any) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def api_key_digest(key: str) -> str:
    pepper = os.environ.get("API_KEY_PEPPER", "")
    return hashlib.sha256((pepper + key).encode("utf-8")).hexdigest()


def authenticate_client(request: Any, connection: psycopg.Connection[Any]) -> str | None:
    token = bearer_token(request)
    if not token:
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_id
            FROM api_keys
            WHERE key_digest = %s AND revoked_at IS NULL
            """,
            (api_key_digest(token),),
        )
        row = cursor.fetchone()
    return str(row[0]) if row else None


def validate_job_request(value: dict[str, Any]) -> None:
    target = value.get("target")
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    for key in ("columns", "rows"):
        if not isinstance(target.get(key), list):
            raise ValueError(f"target.{key} must be a list")
    if not target["columns"]:
        raise ValueError("target.columns must not be empty")
    if any(not isinstance(column, str) or not column for column in target["columns"]):
        raise ValueError("target.columns must contain non-empty strings")
    width = len(target["columns"])
    if any(not isinstance(row, list) or len(row) != width for row in target["rows"]):
        raise ValueError("Every target row must match target.columns")
    options = value.get("options", {})
    if options is not None and not isinstance(options, dict):
        raise ValueError("options must be an object")
