from http.server import BaseHTTPRequestHandler

from api.v1._common import Request, json_response, query_parameters, write_response
from api.v1.jobs import handle_request as handle_job_request
from api.v1.keys import handle_request as handle_key_request


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith("/api/v1/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            try:
                query = query_parameters(self)
                query["job_id"] = job_id
                from api.v1._jobs_status import (
                    handle_request as handle_status_request,
                )

                write_response(
                    self,
                    handle_status_request(Request(self, query)),
                )
            except Exception as exc:
                print(f"API entrypoint failed: {type(exc).__name__}: {exc}")
                write_response(
                    self,
                    json_response(500, {"error": "internal_server_error"}),
                )
            return
        if path != "/api":
            self.send_error(404, "Not found")
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/api/v1/keys", "/api/v1/jobs"):
            self.send_error(404, "Not found")
            return
        try:
            request = Request(self, query_parameters(self))
            request_handler = (
                handle_key_request if path == "/api/v1/keys" else handle_job_request
            )
            write_response(self, request_handler(request))
        except Exception as exc:
            print(f"API entrypoint failed: {type(exc).__name__}: {exc}")
            write_response(
                self,
                json_response(500, {"error": "internal_server_error"}),
            )
