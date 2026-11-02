"""web/openapi.json, which the dashboard's API client is generated from (``npm run gen`` in web/), is the admin API as
it stands."""

import json
from pathlib import Path

from yggdrasil.admin import build_api

DOCUMENT = Path(__file__).parents[2] / "web" / "openapi.json"


def test_web_openapi_json_is_the_admin_api() -> None:
    current = json.dumps(build_api().openapi(), ensure_ascii=False, indent=2) + "\n"  # as dump_openapi prints it
    assert DOCUMENT.read_text(encoding="utf-8") == current, (
        "web/openapi.json is stale: run `uv run python -m yggdrasil.admin.dump_openapi > web/openapi.json`, "
        "then `npm run gen` in web/"
    )
