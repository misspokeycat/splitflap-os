"""Schedules and quiet hours.

A schedule claims the display for a time window; quiet hours blank it
entirely. Both are evaluated on a tick, and the winner is written to the same
state the display loop reads, so neither needs to talk to the loop directly.
"""

import logging
import time
from datetime import datetime

import pytz

from splitflap.mqtt import mqtt_publish_state
from splitflap.plugins import _plugin_registry
from splitflap.settings import settings
from splitflap.state import state
from splitflap.tasks import start_background_task, start_supervised_loop


def _in_time_window(start, end, t):
    """Return True if time string t (HH:MM) is within [start, end). Supports overnight ranges."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # overnight e.g. 22:00–07:00


def _is_quiet_hours():
    """Return True if quiet hours are currently active."""
    if not settings.get('quiet_hours_enabled', False):
        return False
    tz = pytz.timezone(settings.get('timezone', 'US/Eastern'))
    now = datetime.now(tz)
    day = ['mon','tue','wed','thu','fri','sat','sun'][now.weekday()]
    if day not in settings.get('quiet_hours_days', []):
        return False
    t = now.strftime('%H:%M')
    return _in_time_window(settings.get('quiet_hours_start', '22:00'),
                           settings.get('quiet_hours_end', '07:00'), t)


def _schedule_tick():

    quiet = _is_quiet_hours()

    # Quiet hours transition: entering
    if quiet and not state.quiet_hours_active:
        state.quiet_hours_active = True
        state.active_app = None
        state.active_app_playlist = None
        state.stop_event.set()
        mqtt_publish_state()
        logging.info("Quiet hours: display stopped")
        return

    # Quiet hours transition: leaving
    if not quiet and state.quiet_hours_active:
        state.quiet_hours_active = False
        logging.info("Quiet hours ended")
        # Fall through to check schedules

    if quiet:
        return  # stay quiet, don't evaluate schedules

    # Evaluate schedules
    tz = pytz.timezone(settings.get('timezone', 'US/Eastern'))
    now = datetime.now(tz)
    day = ['mon','tue','wed','thu','fri','sat','sun'][now.weekday()]
    t = now.strftime('%H:%M')

    matched = None
    for sched in settings.get('schedules', []):
        if not sched.get('enabled', True):
            continue
        if day not in sched.get('days', []):
            continue
        if _in_time_window(sched.get('start_time', '00:00'), sched.get('end_time', '00:00'), t):
            matched = sched
            break

    new_id = matched['id'] if matched else None
    if new_id == state.active_schedule_id:
        return  # no change

    state.active_schedule_id = new_id
    if matched is None:
        logging.info("Schedule: no active schedule")
        return  # schedule ended — don't force stop, let user's state persist

    action = matched.get('action', {})
    atype = action.get('type', 'off')
    name = matched.get('name', '')

    if atype == 'off':
        state.active_app = None
        state.active_app_playlist = None
        state.stop_event.set()
        mqtt_publish_state()
        logging.info(f"Schedule '{name}': display off")

    elif atype == 'app':
        app_id = action.get('value', '')
        if app_id in _plugin_registry:
            manifest = _plugin_registry[app_id]
            state.active_app = app_id
            state.active_app_playlist = None
            saved = settings.get(f'plugin_{app_id}_loop_delay', '')
            state.loop_delay = float(saved) if saved else float(manifest.get('loop_delay', settings.get('global_loop_delay', 5)))
            state.stop_event.set()
            mqtt_publish_state()
            logging.info(f"Schedule '{name}': started app {app_id}")

    elif atype == 'playlist':
        pl_name = action.get('value', '')
        playlists = settings.get('saved_app_playlists', {})
        if pl_name in playlists:
            pl = playlists[pl_name]
            state.active_app_playlist = pl.get('entries', [])
            state.app_playlist_loop = pl.get('loop', True)
            state.app_playlist_name = pl_name
            state.active_app = None
            state.current_playlist = []
            state.last_sent_page = None
            state.stop_event.set()
            mqtt_publish_state()
            logging.info(f"Schedule '{name}': started playlist '{pl_name}'")


def _schedule_loop():
    while True:
        time.sleep(60)
        _schedule_tick()


start_supervised_loop(_schedule_loop)
start_background_task(_schedule_tick)
