"""Shared runtime state.

Everything here was a module global in app.py, mutated with ``global`` from a
dozen different functions and read from several threads. Module globals do not
survive being split across files — ``from x import active_app`` binds a value,
so a later rebinding in x is invisible to the importer. Holding them as
attributes on one object keeps every module looking at the same thing:

    from splitflap.state import state
    state.active_app = None

Nothing here is defensive about threading, which matches the behaviour these
globals already had: the display loop, the scheduler, the trigger loop and the
request handlers all touch this state, coordinated by ``stop_event`` rather
than by locks.
"""

import threading

from splitflap.grid import get_module_count


class RuntimeState:
    def __init__(self):
        # ── Connection ──────────────────────────────────────────────
        self.ser = None                     # pyserial handle or GatewayTransport
        self.serial_port = None             # label of what ser is attached to
        self.sim_mode = True                # no hardware: render to the web UI only

        # ── Display ─────────────────────────────────────────────────
        self.is_homed = False
        self.current_indices = []           # flap index per module, -1 = unknown
        self.current_display_string = ""
        self.last_transition_style = 'ltr'
        self.last_transition_speed = 15

        # ── What is currently playing ───────────────────────────────
        self.active_app = None
        self.active_app_playlist = None
        self.app_playlist_loop = True
        self.app_playlist_name = None
        self.current_playlist = []
        self.last_sent_page = None
        self.loop_delay = 5
        self.stop_event = threading.Event()  # set to interrupt the display loop

        # ── Scheduler ───────────────────────────────────────────────
        self.active_schedule_id = None
        self.quiet_hours_active = False

        # ── Network ─────────────────────────────────────────────────
        self.network_mode = 'unknown'
        self.is_online = False

        # ── MQTT ────────────────────────────────────────────────────
        self.mqtt_client = None
        self.mqtt_last_text = ""


state = RuntimeState()


def resize_grid():
    """Reset the display buffers to match the configured grid size."""
    n = get_module_count()
    state.current_indices = [-1] * n
    state.current_display_string = " " * n


resize_grid()
