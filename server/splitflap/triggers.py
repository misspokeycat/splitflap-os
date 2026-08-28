"""Trigger runtime.

An app can expose trigger(settings) and have its pages pushed to the display
when some condition it cares about becomes true — a train approaching, a game
starting. Cooldowns and failure counts are kept per trigger so a noisy or
broken one backs off instead of hammering the display.
"""

import logging
import time

from splitflap.notifications import push
from splitflap.plugins import _plugin_registry, _plugin_triggers, get_plugin_pages
from splitflap.settings import settings
from splitflap.state import state
from splitflap.tasks import start_supervised_loop


_trigger_cooldowns = {}  # trigger_id → last_fired timestamp
_trigger_last_check = {}  # trigger_id → last_checked timestamp
_trigger_failures = {}  # trigger_id → consecutive failure count


def _check_triggers():
    if not settings.get('triggers_enabled', True):
        return
    if state.quiet_hours_active:
        return
    now = time.time()
    for trig in settings.get('triggers', []):
        if not trig.get('enabled', True):
            continue
        trig_id = trig.get('id', '')
        app_id = trig.get('app', '')
        trigger_fn = _plugin_triggers.get(app_id)
        if not trigger_fn:
            continue
        manifest = _plugin_registry.get(app_id, {})
        interval = float(manifest.get('trigger_interval', 60))
        cooldown = float(trig.get('cooldown', manifest.get('trigger_cooldown', 300)))
        # Exponential backoff on repeated failures (doubles interval up to 10min cap)
        failures = _trigger_failures.get(trig_id, 0)
        effective_interval = min(interval * (2 ** failures), 600) if failures else interval
        # Check interval
        if now - _trigger_last_check.get(trig_id, 0) < effective_interval:
            continue
        _trigger_last_check[trig_id] = now
        # Check cooldown
        if now - _trigger_cooldowns.get(trig_id, 0) < cooldown:
            continue
        try:
            plugin_settings = dict(settings)
            for s in manifest.get('settings', []):
                if not s.get('global_key'):
                    key = f"plugin_{app_id}_{s['key']}"
                    plugin_settings[s['key']] = settings.get(key, s.get('default', ''))
            conditions = trig.get('conditions', {})
            fired = trigger_fn(plugin_settings, conditions)
            _trigger_failures[trig_id] = 0
        except Exception as e:
            _trigger_failures[trig_id] = failures + 1
            logging.error(f"Trigger {trig_id} ({app_id}) error (fail #{failures+1}): {e}")
            continue
        if fired:
            _trigger_cooldowns[trig_id] = now
            display_seconds = float(trig.get('display_seconds',
                                             manifest.get('trigger_display_seconds', 30)))
            pages = get_plugin_pages(app_id)
            if pages:
                text = pages[0] if isinstance(pages[0], str) else pages[0].get('text', '')
                push(text, f"trigger:{app_id}", display_seconds=display_seconds)
                logging.info(f"Trigger fired: {trig.get('name',trig_id)} ({app_id})")


def _trigger_loop():
    while True:
        time.sleep(10)
        _check_triggers()


start_supervised_loop(_trigger_loop)
