from __future__ import annotations

import argparse
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Sequence

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_OPEN_TIMEOUT_SECONDS = 15.0


def frontend_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}/"


def server_url(host: str, port: int) -> str:
    """Base URL the Lightroom plug-in and the bridge jobs must call back to."""

    return frontend_url(host, port).rstrip("/")


def wait_for_http(url: str, timeout_seconds: float, interval_seconds: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(interval_seconds)
    return False


def open_when_ready(url: str, timeout_seconds: float) -> None:
    if wait_for_http(url, timeout_seconds):
        webbrowser.open(url)
    else:
        print(f"Frontend did not become ready in {timeout_seconds:.0f}s: {url}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run AI Photo Agent and open the frontend.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host for the Uvicorn server.")
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=int,
        help="Port for the Uvicorn server.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the server without opening the frontend automatically.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable Uvicorn reload for local development.",
    )
    parser.add_argument(
        "--open-timeout",
        default=DEFAULT_OPEN_TIMEOUT_SECONDS,
        type=float,
        help="Seconds to wait for the frontend before opening the browser.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    url = frontend_url(args.host, args.port)
    # Bridge jobs embed this URL so the Lightroom plug-in can fetch analysis
    # status/preset while the server runs on a custom port. It has to be set
    # before uvicorn imports ``app.asgi`` (which creates the bridge). An explicit
    # environment value stays authoritative.
    if not os.environ.get("AI_PHOTO_AGENT_SERVER_URL"):
        os.environ["AI_PHOTO_AGENT_SERVER_URL"] = server_url(args.host, args.port)
    if not args.no_browser:
        opener = threading.Thread(
            target=open_when_ready,
            args=(url, args.open_timeout),
            daemon=True,
        )
        opener.start()
        print(f"Starting AI Photo Agent. The frontend will open at {url}")
    else:
        print(f"Starting AI Photo Agent at {url}")

    uvicorn.run(
        "app.asgi:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
