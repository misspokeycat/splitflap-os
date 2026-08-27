"""Things that run once, as the server comes up."""

import logging
import time

from splitflap.grid import get_module_count
from splitflap.mqtt import mqtt_publish_state, mqtt_setup
from splitflap.settings import settings
from splitflap.state import state
from splitflap.tasks import BACKGROUND_TASKS, start_background_task
from splitflap.transport import send_raw


def apply_auto_home_on_boot():
    """Honour the Auto-Home on Boot setting.

    Re-asserts the firmware flag either way — it lives in module RAM and is
    lost on power cycle — then homes when enabled. Returns whether it homed.
    """
    enabled = bool(settings.get('auto_home', True))
    send_raw(f"m**a{1 if enabled else 0}")
    if not enabled:
        return False
    send_raw("m**h")
    state.is_homed = True
    state.current_indices = [0] * get_module_count()
    state.current_display_string = " " * get_module_count()
    mqtt_publish_state()
    logging.info("Auto-home on boot: homed all modules")
    return True


def _startup_auto_home():
    time.sleep(3)  # let the controller finish booting before we talk to it
    try:
        apply_auto_home_on_boot()
    except Exception as e:
        logging.error(f"Auto-home on boot failed: {e}")


start_background_task(_startup_auto_home)

# Connected here (not at import time) so the plugin registry and playlist
# globals that the discovery/state publishers read already exist.
if BACKGROUND_TASKS:
    mqtt_setup()
