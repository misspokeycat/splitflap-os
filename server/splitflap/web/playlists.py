"""Saved page playlists, app playlists and schedules."""

import threading
from flask import Blueprint, jsonify, request
from splitflap.settings import save_settings, settings
from splitflap.state import state
from splitflap.mqtt import mqtt_publish_discovery, mqtt_publish_state
from splitflap.scheduler import _schedule_tick

bp = Blueprint("playlists", __name__)


@bp.route('/playlists', methods=['GET', 'POST'])
def playlists():
    if request.method == 'GET':
        return jsonify(settings.get('saved_playlists', {}))
    data = request.json
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify(status="error", message="Name required"), 400
    if 'saved_playlists' not in settings:
        settings['saved_playlists'] = {}
    settings['saved_playlists'][name] = {
        'pages': data.get('pages', []),
        'delay': data.get('delay', 5),
    }
    save_settings(settings)
    return jsonify(status="saved", name=name)

@bp.route('/playlists/<path:name>', methods=['DELETE'])
def delete_playlist(name):
    plists = settings.get('saved_playlists', {})
    if name in plists:
        del plists[name]
        settings['saved_playlists'] = plists
        save_settings(settings)
    return jsonify(status="deleted")


# ============================================================
#  SCHEDULES + QUIET HOURS
# ============================================================

@bp.route('/schedules', methods=['GET', 'POST'])
def schedules_route():
    if request.method == 'GET':
        return jsonify(schedules=settings.get('schedules', []),
                       quiet_hours_enabled=settings.get('quiet_hours_enabled', False),
                       quiet_hours_start=settings.get('quiet_hours_start', '22:00'),
                       quiet_hours_end=settings.get('quiet_hours_end', '07:00'),
                       quiet_hours_days=settings.get('quiet_hours_days', ['mon','tue','wed','thu','fri','sat','sun']))
    data = request.json
    if 'schedules' in data:
        settings['schedules'] = data['schedules']
    if 'quiet_hours_enabled' in data:
        settings['quiet_hours_enabled'] = bool(data['quiet_hours_enabled'])
    if 'quiet_hours_start' in data:
        settings['quiet_hours_start'] = data['quiet_hours_start']
    if 'quiet_hours_end' in data:
        settings['quiet_hours_end'] = data['quiet_hours_end']
    if 'quiet_hours_days' in data:
        settings['quiet_hours_days'] = data['quiet_hours_days']
    save_settings(settings)
    return jsonify(status="saved")


@bp.route('/schedule_tick', methods=['POST'])
def schedule_tick_route():
    """Force an immediate schedule evaluation (e.g. after saving schedules)."""
    state.active_schedule_id = None  # reset so current window re-fires
    threading.Thread(target=_schedule_tick, daemon=True).start()
    return jsonify(status="ok")


# ============================================================
#  APP PLAYLISTS
# ============================================================

@bp.route('/run_app_playlist', methods=['POST'])
def run_app_playlist():
    data = request.json
    state.active_app_playlist = data.get('entries', [])
    state.app_playlist_loop = data.get('loop', True)
    state.app_playlist_name = data.get('name', None)
    state.active_app = None
    state.current_playlist = []
    state.last_sent_page = None
    state.stop_event.set()
    mqtt_publish_state()
    return jsonify(status="App playlist started")

@bp.route('/app_playlists', methods=['GET', 'POST'])
def app_playlists():
    if request.method == 'GET':
        return jsonify(settings.get('saved_app_playlists', {}))
    data = request.json
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify(status="error", message="Name required"), 400
    if 'saved_app_playlists' not in settings:
        settings['saved_app_playlists'] = {}
    settings['saved_app_playlists'][name] = {
        'entries': data.get('entries', []),
        'loop': data.get('loop', True),
    }
    save_settings(settings)
    mqtt_publish_discovery()
    return jsonify(status="saved", name=name)

@bp.route('/app_playlists/<path:name>', methods=['DELETE'])
def delete_app_playlist(name):
    plists = settings.get('saved_app_playlists', {})
    if name in plists:
        del plists[name]
        settings['saved_app_playlists'] = plists
        save_settings(settings)
    mqtt_publish_discovery()
    return jsonify(status="deleted")
