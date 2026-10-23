# Obedience API

This repository contains only the public request broker for the future
Obedience SaaS. It stores validated JSON jobs in Neon and returns their status
and results through Vercel functions.

The private solver is intentionally not part of this repository. Do not copy
`pathways.py`, solver fixtures, debug dumps, or private worker code here.

## Setup

1. Create a private GitHub repository and publish this folder.
2. Import the repository into Vercel.
3. Add a Neon integration or set `DATABASE_URL` in Vercel.
4. Set `API_KEY_PEPPER` and `WORKER_AUTH_TOKEN` as Vercel environment variables.
5. Run `migrations/001_initial_schema.sql` against Neon.
6. Insert a user and an API-key digest using the same digest algorithm in
   `api/v1/_common.py`.

The Python files in `api/` use Vercel's file-based
`BaseHTTPRequestHandler` interface. The `pyproject.toml` entrypoint points to
the importable jobs handler; the dynamic `[job_id].py` filename is a Vercel
route and is not imported as a Python module.

The current API supports:

```text
POST /api/v1/jobs
GET  /api/v1/jobs/{job_id}
POST /api/v1/keys
```

The home page provides a public demo button for generating an API key. The
plaintext key is returned only once; the database stores only its digest. This
endpoint is intentionally public for the demo and should be protected with
authentication or a rate limit before production use.

Use an API key as a bearer token:

```text
Authorization: Bearer <api-key>
```

The worker claim/update endpoints are deliberately not exposed yet. The next
step is to add a small authenticated worker protocol, then adapt the private
`solver_worker.py` to use it.

## Request shape

```json
{
  "target": {
    "columns": ["A", "OUT"],
    "rows": [["0", "123"]]
  },
  "prescribed_graph": {
    "nodes": [],
    "edges": []
  },
  "options": {
    "imvolution_mode": "free",
    "iteration_limit": 1000
  }
}
```

The API stores the request as opaque JSON. It does not import or execute
anything supplied by the client.

## Local validation

Install the dependencies with `pip install -r requirements.txt`, then run the
test suite once tests are added. Vercel supplies the request object and calls
the exported `handler`/`app` function.
