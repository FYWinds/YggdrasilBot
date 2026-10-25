"""``python -m yggdrasil.browser``: start Xvfb (unless a display exists) and serve the browser API.

Environment: ``YGG_BROWSER_PORT`` (8080), ``YGG_BROWSER_CHROME`` (``/usr/bin/google-chrome``),
``YGG_BROWSER_PROFILE`` (``/tmp/chrome-profile``, the warm patchright profile), ``YGG_BROWSER_MAX_PAGES`` (4).
"""

import os
import shutil
import subprocess

import uvicorn

from yggdrasil.browser.service import NodriverEngine, PatchrightEngine, Verdicts, create_app

_DISPLAY = ":99"


def _start_xvfb() -> subprocess.Popen[bytes] | None:
    if os.environ.get("DISPLAY") or shutil.which("Xvfb") is None:
        return None
    proc = subprocess.Popen(["Xvfb", _DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"])
    os.environ["DISPLAY"] = _DISPLAY
    return proc


def main() -> None:
    xvfb = _start_xvfb()
    verdicts = Verdicts()
    engines = {
        "patchright": PatchrightEngine(
            verdicts,
            os.environ.get("YGG_BROWSER_PROFILE", "/tmp/chrome-profile"),
            max_pages=int(os.environ.get("YGG_BROWSER_MAX_PAGES", "4")),
        ),
        "nodriver": NodriverEngine(verdicts, os.environ.get("YGG_BROWSER_CHROME", "/usr/bin/google-chrome")),
    }
    try:
        uvicorn.run(create_app(engines), host="0.0.0.0", port=int(os.environ.get("YGG_BROWSER_PORT", "8080")))
    finally:
        if xvfb is not None:
            xvfb.terminate()


if __name__ == "__main__":
    main()
