"""Notification interrupts.

Messages POSTed to /notify are queued and shown between playlist pages, then
the loop carries on where it left off. The queue is the handoff between the
request thread and the display loop.
"""

import threading
import time

from splitflap.animations import get_animation_order
from splitflap.display import _rotation_time, send_to_display
from splitflap.grid import format_lines
from splitflap.settings import settings
from splitflap.state import state


_notify_queue = []
_notify_lock  = threading.Lock()


def _pop_notify():
    """Return and remove the oldest non-expired notification, or None."""
    if state.quiet_hours_active:
        return None
    now = time.time()
    with _notify_lock:
        # Prune stale messages (not shown within 5 minutes)
        _notify_queue[:] = [m for m in _notify_queue if m['expires_at'] > now]
        if _notify_queue:
            return _notify_queue.pop(0)
    return None


def _show_notify_message(msg):
    """Display a notification for its display_seconds, then return."""
    text = msg.get('text', '')
    secs = float(msg.get('display_seconds', settings.get('notify_display_seconds', 10)))
    order = get_animation_order(msg.get('animation', 'ltr'))
    max_dist = send_to_display(format_lines(*text.split('|')), order)
    state.last_sent_page = text
    rotation_time = _rotation_time(max_dist)
    for _ in range(int(rotation_time * 10)):
        if state.stop_event.is_set(): return
        time.sleep(0.1)
    for _ in range(int(secs * 10)):
        if state.stop_event.is_set(): return
        time.sleep(0.1)
