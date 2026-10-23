# Obedience API

This repository contains only the public request broker for the future
Obedience SaaS. It stores validated JSON jobs in Neon and returns their status
and results through Vercel functions.

The private solver is intentionally not part of this repository. Do not copy
`pathways.py`, solver fixtures, debug dumps, or private worker code here.

## Setup

1. Create a private GitHub repository and publish this folder.
2. Import the repository into Vercel.
3. Add a Neon integration or set `NAPIQ_DATABASE_URL`/`DATABASE_URL` in Vercel.
4. Set `API_KEY_PEPPER` and `WORKER_AUTH_TOKEN` as Vercel environment variables.
5. Run `migrations/001_initial_schema.sql` against Neon.
6. Insert a user and an API-key digest using the same digest algorithm in
   `api/v1/_common.py`.

The Python files in `api/` use Vercel's file-based
`BaseHTTPRequestHandler` interface. `pyproject.toml` explicitly points Vercel
to the importable `api.index:handler` entrypoint, while the dynamic
`[job_id].py` filename remains a Vercel route and is not imported as a Python
module.

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

The worker protocol is authenticated with `WORKER_AUTH_TOKEN`:

```text
POST /api/v1/worker/claim
POST /api/v1/worker/jobs/{job_id}/complete
POST /api/v1/worker/jobs/{job_id}/fail
```

## Request shape

```json
{
  "operation": "solve",
  "circuit": {
    "columns": ["A", "B", "OUT"],
    "column_kinds": ["input", "input", "output"],
    "rows": [
      ["0", "0", "0"],
      ["0", "1", "0"],
      ["1", "0", "0"],
      ["1", "1", "1"]
    ]
  },
  "standard_parts": {
    "AND": {
      "columns": ["A", "B", "AND_out"],
      "column_kinds": ["input", "input", "output"],
      "rows": [
        ["0", "0", "0"],
        ["1", "0", "0"],
        ["0", "1", "0"],
        ["1", "1", "1"]
      ]
    }
  },
  "imvolution_mode": "no imvolutions",
  "iteration_limit": 1000,
  "solution_offset": 0,
  "solution_limit": 1
}
```

The request shape matches the private worker's `solve_payload` contract.
`solution_offset` skips earlier candidates and `solution_limit` caps the
number returned. The API-backed UI requests one candidate per Solve click and
advances the offset for the next click.
Unsupported fields such as `target`, `prescribed_graph`, and `options` are
rejected with `422 unsupported_solver_input` rather than being queued.

## Python client

The repository includes a dependency-free Python client in
`obedience_client`. Copy the `obedience_client` package into another Python
project, or install this repository in editable mode while developing:

```powershell
python -m pip install -e .
```

Example:

```python
from obedience_api import ObedienceApiClient

client = ObedienceApiClient(
    "https://obedience-api.vercel.app",
    api_key="YOUR_API_KEY",
)
job_id = client.submit_job({
    "operation": "solve",
    "circuit": {
        "columns": ["A", "B", "OUT"],
        "column_kinds": ["input", "input", "output"],
        "rows": [["0", "0", "0"], ["0", "1", "0"],
                 ["1", "0", "0"], ["1", "1", "1"]],
    },
    "standard_parts": {},
    "imvolution_mode": "no imvolutions",
    "iteration_limit": 1000,
})
status = client.wait_for_job(job_id)
print(status.status, status.result, status.error)
```

To generate a demo key, use `client.generate_api_key()` and save the returned
value immediately; the API displays it only once.

Runnable example:

```powershell
$env:OBEDIENCE_API_KEY = "YOUR_API_KEY"
python .\examples\use_api.py
```

The example submits the AND-gate payload above and waits for the local solver
worker to return a result. To generate a new demo key through the public
endpoint instead, run:

```powershell
python .\examples\use_api.py --generate-key
```

The reusable Python client does not accept a Vercel protection bypass. Deploy
the public API without Vercel Deployment Protection, or expose a public
production domain for API consumers. Vercel protection bypasses are for
internal testing only and should not be distributed to library users.

## API-backed pathway UI

The repository also contains a standalone Tkinter UI clone:

```powershell
$env:OBEDIENCE_API_KEY = "YOUR_API_KEY"
python .\examples\pathways_api.py
```

It imports the public client and non-sensitive data models from
`obedience_api.py`. It does not import the private
`pathways.py`, `solver_worker.py`, or solver implementation. Each Solve click
requests one candidate solution.

## Demo job client

After generating an API key, submit the sample job and poll its status with
PowerShell:

```powershell
.\scripts\submit-demo-job.ps1 `
  -Domain "https://YOUR-VERCEL-DOMAIN.vercel.app" `
  -ApiKey "YOUR_API_KEY"
```

If Vercel Deployment Protection remains enabled, do not commit the bypass
secret. Save it in the ignored file
`scripts/.vercel-bypass-secret` (one line, with no quotes), or pass it with
`-VercelBypassSecret`:

```powershell
Set-Content `
  -Path ".\scripts\.vercel-bypass-secret" `
  -Value "YOUR_VERCEL_BYPASS_SECRET" `
  -NoNewline

.\scripts\submit-demo-job.ps1 `
  -Domain "https://YOUR-VERCEL-DOMAIN.vercel.app" `
  -ApiKey "YOUR_API_KEY"
```

The script does not store the API key. Jobs remain `queued` until a worker is
connected to process them.

## Local solver workers

The private solver remains outside this repository. The local worker adapter
expects the sibling `solver_worker.py` by default and calls its
`solve_payload(payload)` function. If `.env.local` exists (for example after
`vercel env pull .env.local --environment=production`), the worker loads it
automatically. Otherwise set `NAPIQ_DATABASE_URL` (or `DATABASE_URL`) in the
shell where the worker runs.

```powershell
python .\scripts\neon_solver_worker.py --once
```

Use a different environment file with `--env-file`. Existing environment
variables take precedence over values in the file.

Run continuously while testing:

```powershell
python .\scripts\neon_solver_worker.py
```

To use a different solver location:

```powershell
python .\scripts\neon_solver_worker.py `
  --solver-path "C:\path\to\solver_worker.py"
```

The direct adapter claims jobs with PostgreSQL row locking, marks them
`running`, calls the private solver, and records either the JSON result as
`completed` or the exception as `failed`. Use `--once` first so failures are
easy to inspect. It requires the Neon URL locally.

For the REST architecture, use `solver_api_worker.py` instead. It calls the
deployed API and does not need `NAPIQ_DATABASE_URL`. The API URL defaults to
`https://obedience-api.vercel.app`; `WORKER_AUTH_TOKEN` is loaded from the
repository's `.env.local` by default:

```powershell
python .\scripts\solver_api_worker.py `
  --api-url "https://obedience-api.vercel.app" `
  --solver-path "C:\path\to\solver_worker.py" `
  --env-file ".\.env.local" `
  --once
```

## Local validation

Install the dependencies with `pip install -r requirements.txt`, then run the
test suite once tests are added. Vercel supplies the request object and calls
the exported `handler`/`app` function.
