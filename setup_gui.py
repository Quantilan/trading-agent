#!/usr/bin/env python3
"""
setup_gui.py — Quantilan Agent setup UI launcher.

Usage:
    python setup_gui.py [--port 8080] [--no-browser]
"""

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Ensure project root is on the path
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))


def _open_browser(url: str, delay: float = 1.5):
    """Open browser after a short delay to let the server start."""
    time.sleep(delay)
    webbrowser.open(url)


def main():
    parser = argparse.ArgumentParser(description="Quantilan Agent Setup GUI")
    parser.add_argument("--host",       default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port",       default=8080, type=int, help="Port (default: 8080)")
    parser.add_argument("--no-browser", action="store_true",   help="Don't open browser automatically")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║     Quantilan Agent — Setup GUI          ║")
    print("  ╚══════════════════════════════════════════╝")
    print()
    print(f"  URL : {url}")
    print(f"  Stop: Ctrl+C")
    print()

    if not args.no_browser:
        t = threading.Thread(target=_open_browser, args=(url,), daemon=True)
        t.start()

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn is not installed.")
        print("       Run:  pip install uvicorn")
        sys.exit(1)

    uvicorn.run(
        "gui.app:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        reload=False,
    )


if __name__ == "__main__":
    main()
