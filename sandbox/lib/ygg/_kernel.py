"""Kernel entry point (`python -P -m ygg._kernel -f <connection file>`, started by `ygg py run`).

An ipykernel with `ygg` preloaded into the user namespace and an idle watchdog: after IDLE_EXIT seconds without
a cell it closes the browser and exits, so an abandoned workspace stops holding a kernel and Chromium.
"""

import os
import sys
import threading
import time

IDLE_EXIT = 20 * 60
PRELOAD = [
    "import ygg",
    "from ygg import show, publish, unpublish, start, wait_port, browser, shot, snapshot",
]


def _watchdog() -> None:
    import ygg
    from ygg import _browser

    while True:
        time.sleep(30)
        if time.monotonic() - ygg.last_activity() > IDLE_EXIT:
            _browser.close_from_thread(timeout=10.0)
            os._exit(0)


def main() -> None:
    from ipykernel.kernelapp import IPKernelApp

    import ygg

    class KernelApp(IPKernelApp):
        def log_connection_info(self) -> None:
            # The "Ctrl-C will not work / --existing" banner is for humans at a terminal; kernel.log keeps only
            # real diagnostics (its tail is reported when the kernel dies).
            pass

    app = KernelApp.instance()
    app.exec_lines = PRELOAD
    app.initialize(sys.argv[1:])
    # Started with -P so the workspace could not shadow the kernel's own imports; user code imports from the
    # working directory like in any notebook.
    if "" not in sys.path:
        sys.path.insert(0, "")
    shell = app.shell
    if shell is not None:
        shell.events.register("post_execute", ygg.touch)
    threading.Thread(target=_watchdog, name="ygg-idle-watchdog", daemon=True).start()
    app.start()


if __name__ == "__main__":
    main()
