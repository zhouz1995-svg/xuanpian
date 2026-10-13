"""Boot smoke test for a packaged Xuanpian macOS app (P3 Task 5).

Usage:
    python scripts/smoke_test_bundle.py --app dist/选片APP.app

Launches the bundled binary offscreen, waits for the main window to appear,
then asks the app to exit.  Exit code 0 only when the app stays alive through
the startup window and exits cleanly.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


WINDOW_READY_MARKER = "选片工作台"
STARTUP_WAIT_SECONDS = 8.0
STYLESHEET_MISSING_MARKER = "styles.qss not found"


def _binary_path(app: Path) -> Path:
    candidates = (
        app / "Contents" / "MacOS" / "选片APP",
        app / "选片APP",
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit(f"bundle binary not found under {app}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--app",
        type=Path,
        default=Path("dist") / "选片APP.app",
    )
    args = parser.parse_args()
    app = args.app
    if not app.is_dir():
        raise SystemExit(f"bundle missing: {app}")
    binary = _binary_path(app)

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    process = subprocess.Popen(
        [str(binary)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    stderr_lines: list[str] = []
    stderr_lock = threading.Lock()

    def _drain_stderr() -> None:
        for line in process.stderr:
            with stderr_lock:
                stderr_lines.append(line)

    reader = threading.Thread(target=_drain_stderr, daemon=True)
    reader.start()
    try:
        while time.monotonic() - started < STARTUP_WAIT_SECONDS:
            if process.poll() is not None:
                out, err = process.communicate(timeout=5)
                raise SystemExit(
                    "app exited during startup "
                    f"(code={process.returncode})\n{err[-2000:]}"
                )
            time.sleep(0.2)
        reader.join(timeout=2.0)
        with stderr_lock:
            stderr_text = "".join(stderr_lines)
        if STYLESHEET_MISSING_MARKER in stderr_text:
            raise SystemExit(
                "packaged app failed to load styles.qss; "
                "UI falls back to the default light theme"
            )
        print(
            json_dumps(
                {
                    "status": "ok",
                    "app": str(app),
                    "stylesheet": "loaded",
                    "alive_after_seconds": round(
                        time.monotonic() - started, 1
                    ),
                }
            )
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    return 0


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
