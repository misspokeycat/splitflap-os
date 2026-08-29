"""Display state and playback control."""

from flask import Blueprint, jsonify, render_template, request
from splitflap.settings import read_version
from splitflap.grid import get_cols, get_module_count, get_rows
from splitflap.state import state
from splitflap.transport import send_raw
from splitflap.plugins import loop_delay_for
from splitflap.mqtt import mqtt_publish_state

bp = Blueprint("display", __name__)


@bp.route('/')
def index():
    version = read_version()
    return render_template('index.html', version=version)

@bp.route('/current_state')
def current_state():
    return jsonify(is_homed=state.is_homed, state=state.current_display_string, active_app=state.active_app,
                   active_app_playlist=state.active_app_playlist is not None,
                   app_playlist_name=state.app_playlist_name,
                   rows=get_rows(), cols=get_cols(), sim_mode=state.sim_mode, hardware_connected=state.ser is not None,
                   transition_style=state.last_transition_style,
                   transition_speed=state.last_transition_speed)

@bp.route('/grid_config')
def grid_config():
    return jsonify(rows=get_rows(), cols=get_cols(), total=get_module_count(), sim_mode=state.sim_mode)

@bp.route('/toggle_sim', methods=['POST'])
def toggle_sim():
    state.sim_mode = request.json.get('enabled', True)
    return jsonify(sim_mode=state.sim_mode)



@bp.route('/update_playlist', methods=['POST'])
def update_playlist():
    data             = request.json
    state.current_playlist = data.get('pages', [])
    state.loop_delay       = data.get('delay', 5)
    state.last_sent_page   = None
    state.active_app       = None
    state.active_app_playlist = None
    state.stop_event.set()
    mqtt_publish_state()
    return jsonify(status="success")

@bp.route('/run_app', methods=['POST'])
def run_app():
    requested = request.json.get('app')
    if requested is not None and not isinstance(requested, str):
        return jsonify(error="'app' must be a string"), 400
    state.active_app_playlist = None
    state.active_app = requested

    state.loop_delay = loop_delay_for(state.active_app)

    state.stop_event.set()
    mqtt_publish_state()
    return jsonify(status=f"App {state.active_app} started")

@bp.route('/stop_app', methods=['POST'])
def stop_app():
    state.active_app = None
    state.active_app_playlist = None
    state.stop_event.set()
    mqtt_publish_state()
    return jsonify(status="stopped")

@bp.route('/home_all')
def home_all():
    send_raw("m**h")
    state.is_homed = True
    state.current_indices = [0] * get_module_count()
    state.current_display_string = " " * get_module_count()
    return jsonify(status="Homing All")
