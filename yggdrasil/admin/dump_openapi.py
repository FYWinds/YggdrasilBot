"""Print the admin OpenAPI document (input for the web client generator)::

uv run python -m yggdrasil.admin.dump_openapi > web/openapi.json
"""

import json
import sys

from yggdrasil.admin import build_api


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # pyright: ignore[reportAttributeAccessIssue]
    sys.stdout.write(json.dumps(build_api().openapi(), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
