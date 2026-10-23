from http.server import BaseHTTPRequestHandler

from api.v1._common import Request, json_response, query_parameters, write_response
from api.v1.keys import handle_request as handle_key_request


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0].rstrip("/") != "/api/v1/keys":
            self.send_error(404, "Not found")
            return
        try:
            request = Request(self, query_parameters(self))
            write_response(self, handle_key_request(request))
        except Exception as exc:
            print(f"API entrypoint failed: {type(exc).__name__}: {exc}")
            write_response(
                self,
                json_response(500, {"error": "internal_server_error"}),
            )
