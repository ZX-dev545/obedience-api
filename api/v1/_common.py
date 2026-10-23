import hashlib
import hmac
import json
import os
from typing import Any
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import psycopg


class UnsupportedSolverInput(ValueError):
    """Raised when a request cannot be represented by the private solver."""


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


def _validate_table(value: Any, name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    columns = value.get("columns")
    rows = value.get("rows")
    if not isinstance(columns, list) or not columns:
        raise ValueError(f"{name}.columns must be a non-empty list")
    if any(not isinstance(column, str) or not column.strip() for column in columns):
        raise ValueError(f"{name}.columns must contain non-empty strings")
    if len(set(columns)) != len(columns):
        raise ValueError(f"{name}.columns must be unique")
    if not isinstance(rows, list):
        raise ValueError(f"{name}.rows must be a list")
    width = len(columns)
    if any(
        not isinstance(row, list) or len(row) != width
        for row in rows
    ):
        raise ValueError(f"Every {name} row must match {name}.columns")
    kinds = value.get("column_kinds")
    if kinds is not None:
        if not isinstance(kinds, list) or len(kinds) != width:
            raise ValueError(f"{name}.column_kinds must match {name}.columns")
        if any(kind not in {"input", "output", "derived"} for kind in kinds):
            raise UnsupportedSolverInput(
                f"{name}.column_kinds supports only input, output, and derived"
            )
    for key in ("parameter_domains", "parameter_positions"):
        if key in value and not isinstance(value[key], dict):
            raise ValueError(f"{name}.{key} must be an object")


def validate_job_request(value: dict[str, Any]) -> None:
    supported_fields = {
        "operation",
        "circuit",
        "standard_parts",
        "imvolution_mode",
        "iteration_limit",
    }
    unsupported_fields = sorted(set(value) - supported_fields)
    if unsupported_fields:
        raise UnsupportedSolverInput(
            "Unsupported solver input field(s): " + ", ".join(unsupported_fields)
        )
    if value.get("operation") != "solve":
        raise UnsupportedSolverInput("Only the 'solve' operation is supported")
    _validate_table(value.get("circuit"), "circuit")

    standard_parts = value.get("standard_parts", {})
    if not isinstance(standard_parts, dict):
        raise ValueError("standard_parts must be an object")
    for name, part in standard_parts.items():
        if not isinstance(name, str) or not name:
            raise ValueError("standard_parts keys must be non-empty strings")
        _validate_table(part, f"standard_parts.{name}")

    mode = value.get("imvolution_mode", "free")
    if not isinstance(mode, str) or not mode:
        raise ValueError("imvolution_mode must be a non-empty string")
    iteration_limit = value.get("iteration_limit")
    if iteration_limit is not None and (
        isinstance(iteration_limit, bool)
        or not isinstance(iteration_limit, int)
        or iteration_limit < 1
    ):
        raise ValueError("iteration_limit must be a positive integer")
