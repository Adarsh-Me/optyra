"""Entry point: `python -m optyra`."""

import asyncio
import sys

from optyra.app import run


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("optyra: interrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
