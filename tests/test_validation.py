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
    module.validate_job_request(
        {"target": {"columns": ["A"], "rows": [["0"]]}, "options": {}}
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"target": {"columns": ["A"], "rows": [["0", "extra"]]}},
        {"target": {"columns": ["A"], "rows": "not rows"}},
    ],
)
def test_invalid_job_request(payload: dict) -> None:
    with pytest.raises(ValueError):
        module.validate_job_request(payload)

