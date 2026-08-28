"""Background task registration.

The server runs its display, schedule, trigger and network loops as daemon
threads started at import. Tests — and any tooling that only wants to inspect
the modules — set SPLITFLAP_NO_BACKGROUND_TASKS=1 to skip them, which also
keeps the hardware unhomed and the broker unconnected.

Two kinds of task:

  start_background_task  one-shot work, run once at boot
  start_supervised_loop  work that must never stop for the life of the process
"""

import logging
import os
import threading
import time

BACKGROUND_TASKS = os.environ.get("SPLITFLAP_NO_BACKGROUND_TASKS") != "1"

RESTART_DELAY = 1.0


def start_background_task(fn):
    """Run fn once, in a daemon thread."""
    if BACKGROUND_TASKS:
        threading.Thread(target=fn, daemon=True).start()


def supervise(fn, delay=RESTART_DELAY):
    """Run fn forever, logging and resuming if it raises.

    These loops each own a subsystem — the display, the schedule, the
    triggers, connectivity — and nothing restarts them. Without this, a single
    unhandled exception ends that subsystem for the lifetime of the process:
    an escape from the display loop freezes the sign on whatever it happened
    to be showing until someone restarts the service.

    fn is expected to loop internally. Escaping is a bug, and the useful
    response is to record it and carry on rather than lose the subsystem.
    """
    def runner():
        while True:
            try:
                fn()
                logging.error("%s returned unexpectedly; restarting", fn.__name__)
            except Exception:
                logging.exception("%s crashed; restarting", fn.__name__)
            time.sleep(delay)
    runner.__name__ = f"supervised_{fn.__name__}"
    return runner


def start_supervised_loop(fn):
    """Run fn in a daemon thread, restarting it if it ever stops."""
    start_background_task(supervise(fn))
