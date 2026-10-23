"""Submit an example job to the Obedience API.

Set OBEDIENCE_API_KEY before running this example. Generate a demo key with
--generate-key if the deployment's public key-generation endpoint is enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from obedience_api import ObedienceApiClient, ObedienceApiError


DEMO_PAYLOAD = {
    "operation": "solve",
    "circuit": {
        "columns": ["A", "B", "OUT"],
        "column_kinds": ["input", "input", "output"],
        "rows": [
            ["0", "0", "0"],
            ["0", "1", "0"],
            ["1", "0", "0"],
            ["1", "1", "1"],
        ],
    },
    "standard_parts": {
        "AND": {
            "columns": ["A", "B", "AND_out"],
            "column_kinds": ["input", "input", "output"],
            "rows": [
                ["0", "0", "0"],
                ["1", "0", "0"],
                ["0", "1", "0"],
                ["1", "1", "1"],
            ],
        }
    },
    "imvolution_mode": "no imvolutions",
    "iteration_limit": 1000,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url",
        default=os.environ.get(
            "OBEDIENCE_API_URL", "https://obedience-api.vercel.app"
        ),
        help="API base URL (default: OBEDIENCE_API_URL or the hosted demo)",
    )
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="Generate a new demo key instead of using OBEDIENCE_API_KEY",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between status checks (default: 2)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Maximum seconds to wait for the solver (default: 300)",
    )
    args = parser.parse_args()

    client = ObedienceApiClient(
        args.api_url,
        api_key=os.environ.get("OBEDIENCE_API_KEY"),
    )

    try:
        if args.generate_key:
            api_key = client.generate_api_key()
            client = ObedienceApiClient(
                args.api_url,
                api_key=api_key,
            )
            print("Generated API key (save it now; it will not be shown again):")
            print(api_key)
        elif not client.api_key:
            print(
                "Set OBEDIENCE_API_KEY or rerun with --generate-key.",
                file=sys.stderr,
            )
            return 2

        job_id = client.submit_job(DEMO_PAYLOAD)
        print(f"Submitted job: {job_id}")
        status = client.wait_for_job(
            job_id,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
        print(f"Final status: {status.status}")
        if status.error:
            print(f"Solver error: {status.error}", file=sys.stderr)
            return 1
        print(json.dumps(status.result, indent=2))
        return 0
    except (ObedienceApiError, TimeoutError, ValueError) as error:
        print(f"Request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
