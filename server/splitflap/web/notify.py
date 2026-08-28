"""The notification interrupt API."""

import logging
import time
from flask import Blueprint, jsonify, request
from splitflap.settings import settings
from splitflap.notifications import _notify_lock, _notify_queue, push

bp = Blueprint("notify", __name__)


def _notify_auth():
    """Validate Authorization header against notify_sources. Returns source name or None."""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:].strip()
    sources = settings.get('notify_sources', {})
    for source, key in sources.items():
        if key == token:
            return source
    return None


@bp.route('/notify', methods=['POST'])
def notify_push():
    if not settings.get('notify_enabled', False):
        return jsonify(error='Notification interrupts are disabled'), 503
    source = _notify_auth()
    if source is None:
        return jsonify(error='Unauthorized'), 401
    data = request.get_json(force=True, silent=True) or {}
    text = data.get('text', '').strip()
    if not text:
        return jsonify(error='text is required'), 400
    msg = push(
        text,
        source,
        display_seconds=data.get('display_seconds'),
        animation=data.get('animation', 'ltr'),
    )
    if msg is None:
        # push() re-reads notify_enabled, so it can refuse even though the
        # check at the top of this handler passed.
        return jsonify(error='Notification interrupts are disabled'), 503
    logging.info(f"Notify: {source} pushed '{text[:30]}'")
    return jsonify(id=msg['id'], source=source, position=len(_notify_queue)), 201


@bp.route('/notify', methods=['GET'])
def notify_list():
    now = time.time()
    with _notify_lock:
        active = [m for m in _notify_queue if m['expires_at'] > now]
        return jsonify(messages=active, count=len(active))


@bp.route('/notify', methods=['DELETE'])
def notify_clear():
    source = request.args.get('source')
    with _notify_lock:
        if source:
            _notify_queue[:] = [m for m in _notify_queue if m.get('source') != source]
        else:
            _notify_queue.clear()
    return jsonify(ok=True)
