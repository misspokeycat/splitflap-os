"""Background loop registration.

The server runs its display, schedule, trigger and network loops as daemon
threads started at import. Tests — and any tooling that only wants to inspect
the modules — set SPLITFLAP_NO_BACKGROUND_TASKS=1 to skip them, which also
keeps the hardware unhomed and the broker unconnected.
"""

import os
import threading

# Importing this module normally starts the display, schedule, trigger and
# network loops, homes the hardware and connects to the broker. Tests (and
# any tooling that just wants to inspect the module) set this to skip them.
BACKGROUND_TASKS = os.environ.get("SPLITFLAP_NO_BACKGROUND_TASKS") != "1"


def start_background_task(fn):
    if BACKGROUND_TASKS:
        threading.Thread(target=fn, daemon=True).start()
