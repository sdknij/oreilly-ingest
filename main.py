#!/usr/bin/env python3
"""O'Reilly Downloader - Main Entry Point"""

import argparse
import logging
import sys
from web.server import run_server, validate_startup_dependencies


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    configure_logging()
    logger = logging.getLogger("oreilly_ingest")

    parser = argparse.ArgumentParser(description="O'Reilly Book Downloader")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    args = parser.parse_args()

    try:
        validate_startup_dependencies()
    except (ImportError, RuntimeError) as exc:
        print("ERROR: Unable to start O'Reilly Downloader.", file=sys.stderr)
        print(file=sys.stderr)
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    print("=" * 50)
    print("  O'Reilly Downloader")
    print("=" * 50)
    print(f"\n  Open http://{args.host}:{args.port} in your browser\n")
    print("  Press Ctrl+C to stop\n")
    print("=" * 50)

    try:
        run_server(args.host, args.port)
    except KeyboardInterrupt:
        logger.info("Shutting down O'Reilly Downloader")


if __name__ == "__main__":
    main()
