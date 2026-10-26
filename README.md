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
uses a worker-side `solution_cursor` to resume the same solver generator on the
next click. The cursor is held in worker memory, so restarting the worker
starts a new solution sequence.
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

To generate a demo key, open the deployed API's home page (for example,
`https://obedience-api.vercel.app`) and click **Generate API key**. Copy and
save the returned value immediately: it is displayed only once. The same
operation is available through `client.generate_api_key()`, or by sending an
unauthenticated `POST` request to `/api/v1/keys`.

The key is a client credential. Send it as a bearer token when submitting jobs
or checking their status; do not commit it to source control or share it. It is
different from `WORKER_AUTH_TOKEN`, which is used only by solver workers.

The reusable Python client does not accept a Vercel protection bypass. Deploy
the public API without Vercel Deployment Protection, or expose a public
production domain for API consumers. Vercel protection bypasses are for
internal testing only and should not be distributed to library users.

## API-backed pathway UI

The repository contains a Tkinter UI example at
`examples/pathways_api.py`. From the `obedience-api` directory, install the
project package and its dependencies into the Python environment you intend to
use:

```powershell
python -m pip install -e .
python -m pip install regex
```

The UI example also uses the private project's shared UI/regex modules
(`regex_cells.py` and `regex_rewrite.py`). Run it from the main project root
with both project directories on `PYTHONPATH`:

```powershell
# Run these commands from the main project root (the directory containing
# pathways_prod.py, regex_cells.py, and the obedience-api folder).
$env:PYTHONPATH = "$((Get-Location).Path);$((Get-Location).Path)\obedience-api"
$env:OBEDIENCE_API_URL = "https://obedience-api.vercel.app"
$env:OBEDIENCE_API_KEY = "PASTE_YOUR_API_KEY_HERE"
python .\obedience-api\examples\pathways_api.py
```

Replace the example URL if you deployed the API elsewhere. The `OBEDIENCE_API_KEY`
is the one-time key generated on the API home page, not the worker token. These
environment variables apply to the current PowerShell window; launch the UI
from that same window. Avoid putting the key directly in a script or committing
it to a `.env` file.

The API stores submitted jobs as `queued`; a solver worker must be running to
process them. Start the API worker in a **second PowerShell window**, from the
main project root:

```powershell
$env:OBEDIENCE_API_URL = "https://obedience-api.vercel.app"
python .\obedience-api\scripts\solver_api_worker.py `
  --solver-path .\solver_worker.py `
  --env-file .\obedience-api\.env.local
```

That environment file must define `WORKER_AUTH_TOKEN` matching the deployed
API's worker token. If the deployment uses Vercel Deployment Protection, it
may also need `VERCEL_AUTOMATION_BYPASS_SECRET` in that file. Do not paste or
commit either secret. The worker token is not the API key used by the UI.
Keep this worker process running while using the UI. Each Solve click requests
one candidate; use **Resume solver** to request the next one.

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
