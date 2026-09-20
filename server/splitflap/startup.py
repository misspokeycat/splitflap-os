"""Things that run once, as the server comes up."""

import logging
import time

from splitflap.mqtt import mqtt_setup
from splitflap.settings import settings
from splitflap.tasks import BACKGROUND_TASKS, start_background_task
from splitflap.transport import send_raw, universal_firmware


def apply_auto_home_setting():
    """Push the auto-home flag to the modules.

    The setting is a *module firmware* mode: when it is on, each module homes
    itself as it powers up, with no involvement from this server. The flag
    lives in module RAM, so it is lost on every power cycle and has to be
    re-asserted — which is the whole reason this runs at startup.

    It deliberately does not home anything. A server restart is not a module
    power-up, and homing all 45 modules takes about twelve seconds; doing that
    on every service restart would be both wrong and disruptive.

    Returns whether auto-home is enabled.
    """
    enabled = bool(settings.get('auto_home', True))
    send_raw(f"m**a{1 if enabled else 0}")
    logging.info("Auto-home on power-up: %s", "enabled" if enabled else "disabled")
    return enabled


def _startup_auto_home():
    time.sleep(3)  # let the controller finish booting before we talk to it
    try:
        apply_auto_home_setting()
    except Exception as e:
        logging.error(f"Applying the auto-home setting failed: {e}")


def _startup_module_watch():
    """Start watching the bus for modules that have forgotten who they are.

    The Universal Firmware reader used to start on the first request from the
    calibration page, which is the one page nobody has open when a module
    drops off. A module that loses its ID leaves its place in the display
    blank, so the watch that puts it back has to be running all along.
    """
    time.sleep(3)
    try:
        universal_firmware.ensure_started()
    except Exception as e:
        logging.error(f"Starting the Universal Firmware watcher failed: {e}")


start_background_task(_startup_auto_home)
start_background_task(_startup_module_watch)

# Connected here (not at import time) so the plugin registry and playlist
# globals that the discovery/state publishers read already exist.
if BACKGROUND_TASKS:
    mqtt_setup()
