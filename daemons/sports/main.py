"""Command-line entry point for the sports reservation daemon."""

from __future__ import annotations

import argparse

from .app import create_app
from .daemon import SportsReservationDaemon


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sports reservation daemon.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to listen on (default: 127.0.0.1)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=5003, help="Port to listen on (default: 5003)"
    )
    args = parser.parse_args()

    daemon = SportsReservationDaemon()
    daemon.start()

    app = create_app(daemon)
    dashboard_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    print(f"Starting Sports Reservation Daemon on {args.host}:{args.port}")
    print(f"Dashboard: http://{dashboard_host}:{args.port}/")
    print("Auth mode: credentials")
    app.run(debug=False, host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
