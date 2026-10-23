import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "api_common", ROOT / "api" / "v1" / "_common.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_valid_job_request() -> None:
    module.validate_job_request({
        "operation": "solve",
        "circuit": {"columns": ["A", "OUT"], "rows": [["0", "1"]]},
        "standard_parts": {},
        "imvolution_mode": "free",
        "iteration_limit": 1000,
    })


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "solve", "circuit": {"columns": ["A"], "rows": [["0", "extra"]]}},
        {"operation": "solve", "circuit": {"columns": ["A"], "rows": "not rows"}},
    ],
)
def test_invalid_job_request(payload: dict) -> None:
    with pytest.raises(ValueError):
        module.validate_job_request(payload)


def test_unsupported_solver_fields_are_rejected() -> None:
    with pytest.raises(module.UnsupportedSolverInput, match="prescribed_graph"):
        module.validate_job_request({
            "operation": "solve",
            "circuit": {"columns": ["A"], "rows": [["0"]]},
            "prescribed_graph": {},
        })
