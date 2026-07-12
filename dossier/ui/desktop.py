"""True native desktop window for Dossier.

Runs the Chainlit chat UI headless on a local port and displays it inside a
native OS webview window (pywebview) - a real desktop window, not a browser tab.
On macOS this uses the system WKWebView (no extra runtime); on Windows the Edge
WebView2 runtime; on Linux GTK/Qt WebKit.

    pip install ".[desktop]"     # pywebview + chainlit
    dossier desktop              # opens the window

To ship a literal double-click .app/.exe, wrap this entrypoint with PyInstaller
(see the README); the window logic below is the runtime it launches.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

_WINDOW_TITLE = "Dossier"


def _free_port() -> int:
    """Ask the OS for an unused localhost port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _wait_until_up(url: str, proc: "subprocess.Popen[bytes]", timeout: float = 40.0) -> None:
    """Poll `url` until the Chainlit server answers, or the process dies / times out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Chainlit server exited before it came up")
        try:
            with urllib.request.urlopen(url, timeout=1.0):
                return
        except OSError:
            time.sleep(0.4)
    raise TimeoutError("Chainlit server did not start in time")


def run(*, width: int = 1000, height: int = 780) -> None:
    """Launch Chainlit headless and open it in a native window; tears the server
    down when the window closes. Raises SystemExit with guidance if the
    `desktop` extra isn't installed."""
    chainlit_exe = shutil.which("chainlit")
    if chainlit_exe is None:
        raise SystemExit("Chainlit is not installed. Run: pip install '.[desktop]'")
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("pywebview is not installed. Run: pip install '.[desktop]'") from exc

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    app_path = Path(__file__).resolve().parent / "app.py"
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        [chainlit_exe, "run", str(app_path), "--headless", "--host", "127.0.0.1", "--port", str(port)]
    )
    try:
        _wait_until_up(url, proc)
        webview.create_window(_WINDOW_TITLE, url, width=width, height=height)
        webview.start()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
