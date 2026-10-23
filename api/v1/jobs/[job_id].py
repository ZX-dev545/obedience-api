from http.server import BaseHTTPRequestHandler

from .._jobs_status import handle_request
from .._common import (
    Request,
    query_parameters,
    write_response,
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = query_parameters(self)
        query["job_id"] = self.path.rstrip("/").rsplit("/", 1)[-1]
        write_response(self, handle_request(Request(self, query)))
