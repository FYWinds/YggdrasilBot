"""``python -m yggdrasil.devtools.seed``: seed or reset the local console's demo data."""

import argparse
import asyncio
import sys

import yggdrasil.devtools.seed as package
from yggdrasil.config import APP_CONFIG
from yggdrasil.devtools.seed.data import GROUPS


async def _main(only_reset: bool) -> None:
    if APP_CONFIG.environment == "prod":
        raise SystemExit("refusing to seed with YGG_ENV=prod")
    if only_reset:
        await package.reset()
        sys.stdout.write("removed the seed data\n")
    else:
        await package.seed()
        sys.stdout.write(f"seeded groups {', '.join(map(str, GROUPS))}: chat, stickers, memory, invocations, jobs\n")
    try:
        from yggdrasil import runtime

        await runtime.publish_change("seed")  # a running console reloads its group list
    except Exception:  # nothing is listening
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=package.__doc__.split("\n\n")[0] if package.__doc__ else None)
    parser.add_argument("--reset", action="store_true", help="only remove the seed data")
    asyncio.run(_main(parser.parse_args().reset))


if __name__ == "__main__":
    main()
