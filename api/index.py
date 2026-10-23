from http.server import BaseHTTPRequestHandler

from .v1._common import Request, query_parameters, write_response
from .v1.keys import handle_request as handle_key_request


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
        request = Request(self, query_parameters(self))
        write_response(self, handle_key_request(request))
