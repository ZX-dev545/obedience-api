from http.server import BaseHTTPRequestHandler

from api.v1._common import Request, json_response, query_parameters, write_response
from api.v1.jobs import handle_request as handle_job_request
from api.v1.keys import handle_request as handle_key_request
from api.v1._worker import claim, complete, fail


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
        if path == "/api":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path != "/api":
            self.send_error(404, "Not found")
            return

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        worker_prefix = "/api/v1/worker/jobs/"
        if path not in ("/api/v1/keys", "/api/v1/jobs", "/api/v1/worker/claim") and not (
            path.startswith(worker_prefix)
            and path.endswith(("/complete", "/fail"))
        ):
            self.send_error(404, "Not found")
            return
        try:
            request = Request(self, query_parameters(self))
            if path == "/api/v1/keys":
                result = handle_key_request(request)
            elif path == "/api/v1/jobs":
                result = handle_job_request(request)
            elif path == "/api/v1/worker/claim":
                result = claim(request)
            else:
                parts = path.removeprefix(worker_prefix).rsplit("/", 1)
                result = complete(request, parts[0]) if parts[1] == "complete" else fail(request, parts[0])
            write_response(self, result)
        except Exception as exc:
            print(f"API entrypoint failed: {type(exc).__name__}: {exc}")
            write_response(
                self,
                json_response(500, {"error": "internal_server_error"}),
            )
