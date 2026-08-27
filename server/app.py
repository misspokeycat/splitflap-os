import serial
import serial.tools.list_ports
import time
import threading
import json
import os
import logging
import random
import requests
import pytz
import unicodedata
import yfinance as yf
import importlib.util
import urllib.request
import shutil
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from splitflap.settings import (
    APPS_PATH,
    CONFIG_PATH,
    DEFAULT_FLAP_CHARS,
    get_flap_chars,
    get_module_char_map,
    get_module_flap_count,
    read_config_file,
    read_version,
    save_settings,
    settings,
)
from splitflap.state import resize_grid, state
from splitflap.tasks import BACKGROUND_TASKS, start_background_task
from splitflap.transport import (
    BAUD_RATE,
    SERIAL_PORT_DEFAULT,
    get_connection_type,
    get_gateway_config,
    get_serial_port,
    open_gateway,
    open_serial,
    send_raw,
    serial_lock,
    sync_hardware_data,
    sync_module_config,
    universal_firmware,
)
from splitflap.plugins import (
    _plugin_caches,
    _plugin_data,
    _plugin_modules,
    _plugin_registry,
    _plugin_triggers,
    get_plugin_app_list,
    get_plugin_pages,
    get_plugin_settings_config,
    load_installed_plugins,
)
from splitflap.animations import get_animation_order
from splitflap.sports import SPORTS_LEAGUES
from splitflap.grid import format_lines, get_cols, get_module_count, get_rows
from tuning import build_tuning_adjust_commands
from hardware.universal_firmware import (
    UniversalFirmwareError,
    UniversalFirmwareManager,
)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None
    logging.warning("paho-mqtt not installed — MQTT integration disabled")


app = Flask(__name__)

# ============================================================
#  SETTINGS
# ============================================================

# ============================================================
#  GRID HELPERS
# ============================================================



# ============================================================
#  MQTT INTEGRATION
# ============================================================

MQTT_TOPIC_PREFIX = "splitflap"
MQTT_AVAIL_TOPIC = f"{MQTT_TOPIC_PREFIX}/availability"
MQTT_TEXT_CMD     = f"{MQTT_TOPIC_PREFIX}/text/set"
MQTT_TEXT_STATE   = f"{MQTT_TOPIC_PREFIX}/text/state"
MQTT_MODE_CMD     = f"{MQTT_TOPIC_PREFIX}/app/set"
MQTT_MODE_STATE   = f"{MQTT_TOPIC_PREFIX}/app/state"
MQTT_STATUS_STATE = f"{MQTT_TOPIC_PREFIX}/status/state"
MQTT_CENTER_CMD   = f"{MQTT_TOPIC_PREFIX}/center/set"
MQTT_CENTER_STATE = f"{MQTT_TOPIC_PREFIX}/center/state"
MQTT_PLAYLIST_CMD = f"{MQTT_TOPIC_PREFIX}/playlist/set"
MQTT_PLAYLIST_STATE = f"{MQTT_TOPIC_PREFIX}/playlist/state"

MQTT_DEVICE = {
    "identifiers": ["splitflap_display"],
    "name": "Split-Flap Display",
    "manufacturer": "Adam G Makes",
    "model": "SplitFlap 45-Module",
}

def _get_mqtt_app_options():
    """Build dynamic app list from plugin registry."""
    return ["off"] + sorted(_plugin_registry.keys())


def _get_mqtt_playlist_options():
    """Build playlist list from saved playlists."""
    return ["off"] + sorted(settings.get('saved_app_playlists', {}).keys())


def _mqtt_text_max():
    """Longest payload the text entity can accept.

    One character per module, plus the '|' separators that sit between
    lines (rows - 1 of them). Home Assistant caps text entities at 255.
    """
    return min(255, get_module_count() + max(0, get_rows() - 1))


def _mqtt_format_text(payload):
    """Lay a '|'-delimited payload out across the grid.

    Honours the Center Text switch: centred when on, left-aligned when off.
    Extra lines beyond the grid height are dropped.
    """
    lines = payload.split('|')[:get_rows()]
    if settings.get('mqtt_center', True):
        return format_lines(*lines)
    cols, rows = get_cols(), get_rows()
    padded = lines + [''] * (rows - len(lines))
    return ''.join(l.ljust(cols)[:cols] for l in padded[:rows])


def mqtt_publish_state():
    """Publish current display state and active mode to MQTT."""
    if not state.mqtt_client or not state.mqtt_client.is_connected():
        return
    state.mqtt_client.publish(MQTT_STATUS_STATE, state.current_display_string, retain=True)
    state.mqtt_client.publish(MQTT_TEXT_STATE, state.mqtt_last_text, retain=True)
    state.mqtt_client.publish(MQTT_MODE_STATE, state.active_app or "off", retain=True)
    center_state = "ON" if settings.get('mqtt_center', True) else "OFF"
    state.mqtt_client.publish(MQTT_CENTER_STATE, center_state, retain=True)
    state.mqtt_client.publish(MQTT_PLAYLIST_STATE, state.app_playlist_name or "off", retain=True)


def mqtt_publish_discovery():
    """Publish Home Assistant MQTT discovery config for all entities."""
    if not state.mqtt_client:
        return
    avail = {"topic": MQTT_AVAIL_TOPIC, "payload_available": "online", "payload_not_available": "offline"}

    configs = [
        ("text", "splitflap_text", {
            "unique_id": "splitflap_text",
            "name": "Display Text (use | for line breaks)",
            "command_topic": MQTT_TEXT_CMD,
            "state_topic": MQTT_TEXT_STATE,
            "min": 0,
            "max": _mqtt_text_max(),
            "mode": "text",
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("select", "splitflap_app", {
            "unique_id": "splitflap_app",
            "name": "Active App",
            "command_topic": MQTT_MODE_CMD,
            "state_topic": MQTT_MODE_STATE,
            "options": _get_mqtt_app_options(),
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("select", "splitflap_playlist", {
            "unique_id": "splitflap_playlist",
            "name": "Playlist",
            "command_topic": MQTT_PLAYLIST_CMD,
            "state_topic": MQTT_PLAYLIST_STATE,
            "options": _get_mqtt_playlist_options(),
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("switch", "splitflap_center", {
            "unique_id": "splitflap_center",
            "name": "Center Text",
            "command_topic": MQTT_CENTER_CMD,
            "state_topic": MQTT_CENTER_STATE,
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("sensor", "splitflap_status", {
            "unique_id": "splitflap_status",
            "name": "Display Status",
            "state_topic": MQTT_STATUS_STATE,
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("sensor", "splitflap_network_mode", {
            "unique_id": "splitflap_network_mode",
            "name": "Network Mode",
            "state_topic": f"{MQTT_TOPIC_PREFIX}/network/mode",
            "icon": "mdi:wifi",
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("sensor", "splitflap_network_ssid", {
            "unique_id": "splitflap_network_ssid",
            "name": "WiFi SSID",
            "state_topic": f"{MQTT_TOPIC_PREFIX}/network/ssid",
            "icon": "mdi:wifi",
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("sensor", "splitflap_network_ip", {
            "unique_id": "splitflap_network_ip",
            "name": "IP Address",
            "state_topic": f"{MQTT_TOPIC_PREFIX}/network/ip",
            "icon": "mdi:ip-network",
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("binary_sensor", "splitflap_online", {
            "unique_id": "splitflap_online",
            "name": "Internet Connected",
            "state_topic": f"{MQTT_TOPIC_PREFIX}/network/online",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "connectivity",
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
        ("button", "splitflap_home", {
            "unique_id": "splitflap_home",
            "name": "Home All",
            "command_topic": f"{MQTT_TOPIC_PREFIX}/home/set",
            "icon": "mdi:home-import-outline",
            "availability": avail,
            "device": MQTT_DEVICE,
        }),
    ]
    for component, object_id, payload in configs:
        topic = f"homeassistant/{component}/{object_id}/config"
        state.mqtt_client.publish(topic, json.dumps(payload), retain=True)
    # Remove deprecated entities
    state.mqtt_client.publish("homeassistant/select/splitflap_mode/config", "", retain=True)
    logging.info("MQTT discovery payloads published")


def _mqtt_on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0 or (hasattr(rc, 'is_failure') and not rc.is_failure):
        logging.info("MQTT connected to broker")
        client.subscribe(MQTT_TEXT_CMD)
        client.subscribe(MQTT_MODE_CMD)
        client.subscribe(MQTT_CENTER_CMD)
        client.subscribe(MQTT_PLAYLIST_CMD)
        client.subscribe(f"{MQTT_TOPIC_PREFIX}/home/set")
        client.subscribe("homeassistant/status")
        client.publish(MQTT_AVAIL_TOPIC, "online", retain=True)
        mqtt_publish_discovery()
        mqtt_publish_state()
    else:
        logging.error(f"MQTT connection failed with code {rc}")


def _mqtt_on_message(client, userdata, msg):
    payload = msg.payload.decode('utf-8', errors='ignore').strip()

    if msg.topic == "homeassistant/status" and payload == "online":
        mqtt_publish_discovery()
        mqtt_publish_state()
        return

    if msg.topic == MQTT_TEXT_CMD:
        state.active_app = None
        state.active_app_playlist = None
        state.mqtt_last_text = payload
        state.current_playlist = [_mqtt_format_text(payload)]
        state.last_sent_page = None
        state.stop_event.set()
        mqtt_publish_state()

    elif msg.topic == MQTT_CENTER_CMD:
        settings['mqtt_center'] = (payload.upper() == 'ON')
        save_settings(settings)
        mqtt_publish_state()

    elif msg.topic == MQTT_MODE_CMD:
        if payload == "off":
            state.active_app = None
            state.active_app_playlist = None
            state.stop_event.set()
        elif payload in _plugin_registry:
            state.active_app = payload
            state.active_app_playlist = None
            manifest = _plugin_registry[payload]
            if manifest.get('animation'):
                state.loop_delay = max(0.1, float(settings.get('anim_speed', '0.4')))
            else:
                saved = settings.get(f'plugin_{payload}_loop_delay', '')
                state.loop_delay = float(saved) if saved else float(manifest.get('loop_delay', 5))
            state.stop_event.set()
        mqtt_publish_state()

    elif msg.topic == MQTT_PLAYLIST_CMD:
        if payload == "off":
            state.active_app_playlist = None
            state.active_app = None
            state.stop_event.set()
        else:
            playlists = settings.get('saved_app_playlists', {})
            if payload in playlists:
                pl = playlists[payload]
                state.active_app_playlist = pl.get('entries', [])
                state.app_playlist_loop = pl.get('loop', True)
                state.app_playlist_name = payload
                state.active_app = None
                state.current_playlist = []
                state.last_sent_page = None
                state.stop_event.set()
        mqtt_publish_state()

    elif msg.topic == f"{MQTT_TOPIC_PREFIX}/home/set":
        send_raw("m**h")
        state.active_app = None
        state.active_app_playlist = None
        state.is_homed = True
        state.current_indices = [0] * get_module_count()
        state.current_display_string = " " * get_module_count()
        mqtt_publish_state()


def mqtt_setup():
    """Initialize MQTT client and connect to broker. Fails gracefully."""
    if not mqtt or not settings.get('mqtt_enabled', False):
        logging.info("MQTT disabled or paho-mqtt not installed")
        return
    try:
        state.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="splitflap_display")
        user = settings.get('mqtt_user', '').strip()
        pw = settings.get('mqtt_password', '').strip()
        if user:
            state.mqtt_client.username_pw_set(user, pw)
        state.mqtt_client.will_set(MQTT_AVAIL_TOPIC, "offline", qos=1, retain=True)
        state.mqtt_client.on_connect = _mqtt_on_connect
        state.mqtt_client.on_message = _mqtt_on_message
        broker = settings.get('mqtt_broker', 'homeassistant.local')
        port = int(settings.get('mqtt_port', 1883))
        state.mqtt_client.connect_async(broker, port)
        state.mqtt_client.loop_start()
        logging.info(f"MQTT connecting to {broker}:{port}")
    except Exception as e:
        state.mqtt_client = None
        logging.error(f"MQTT setup failed: {e}")


def mqtt_reconnect():
    """Disconnect and reconnect MQTT with current settings."""
    if state.mqtt_client:
        try:
            state.mqtt_client.loop_stop()
            state.mqtt_client.disconnect()
        except Exception:
            pass
        state.mqtt_client = None
    mqtt_setup()


@app.route('/serial_ports', methods=['GET'])
def list_serial_ports():
    """List available serial ports on the system."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device": p.device,
            "description": p.description,
            "hwid": p.hwid
        })
    return jsonify(ports=ports, current=state.serial_port)


@app.route('/serial_port', methods=['POST'])
def set_serial_port():
    """Change the active serial port and persist to settings.

    Applying a serial port also switches the active connection type back to
    'serial' (in case the gateway was previously selected).
    """
    data = request.json
    new_port = data.get('port', '').strip()
    if not new_port:
        return jsonify(status="error", message="No port specified"), 400

    with serial_lock:
        if state.ser:
            try:
                state.ser.close()
            except Exception:
                pass
        state.ser, SERIAL_PORT = open_serial(new_port)
        state.sim_mode = not state.ser
        universal_firmware.reset()

    settings['serial_port'] = new_port
    settings['connection_type'] = 'serial'
    save_settings(settings)
    return jsonify(status="success", port=state.serial_port, sim_mode=state.sim_mode)


@app.route('/connection', methods=['GET', 'POST'])
def connection_config():
    """Get or set the active hardware connection (serial vs MQTT gateway).

    POST body (gateway):
        {"type":"gateway","broker":"...","port":1883,"prefix":"splitflap",
         "user":"","password":""}
    POST body (serial):
        {"type":"serial","port":"/dev/ttyUSB0"}   # port optional
    """
    if request.method == 'GET':
        return jsonify(
            type=settings.get('connection_type', 'serial'),
            serial_port=settings.get('serial_port', ''),
            gateway_broker=settings.get('gateway_broker', ''),
            gateway_port=settings.get('gateway_port', 1883),
            gateway_prefix=settings.get('gateway_prefix', 'splitflap'),
            gateway_user=settings.get('gateway_user', ''),
            # Password intentionally not echoed back in full for safety;
            # report whether one is set instead.
            gateway_password_set=bool(settings.get('gateway_password', '')),
            sim_mode=state.sim_mode,
            connected=state.ser is not None,
            descriptor=state.serial_port,
        )

    data = request.json or {}
    conn_type = (data.get('type') or 'serial').strip().lower()

    if conn_type not in ('serial', 'gateway'):
        return jsonify(
            status="error",
            message=f"Unknown connection type '{conn_type}' "
                    "(expected 'serial' or 'gateway')",
        ), 400

    if conn_type == 'gateway':
        broker = (data.get('broker') or '').strip()
        if not broker:
            return jsonify(status="error", message="Broker address is required"), 400
        prefix = (data.get('prefix') or 'splitflap').strip() or 'splitflap'
        try:
            gw_port = int(data.get('port', 1883) or 1883)
        except (TypeError, ValueError):
            return jsonify(status="error", message="Invalid port"), 400
        user = (data.get('user') or '').strip()
        # Preserve the existing password if the field is left blank on resubmit.
        password = data.get('password', None)
        if password is None or password == '':
            password = settings.get('gateway_password', '')

        # Persist before (re)connecting so open_gateway picks up fresh values.
        settings['connection_type'] = 'gateway'
        settings['gateway_broker'] = broker
        settings['gateway_port'] = gw_port
        settings['gateway_prefix'] = prefix
        settings['gateway_user'] = user
        settings['gateway_password'] = password
        save_settings(settings)

        with serial_lock:
            if state.ser:
                try:
                    state.ser.close()
                except Exception:
                    pass
            state.ser, SERIAL_PORT = open_gateway({
                "broker": broker, "port": gw_port, "prefix": prefix,
                "user": user, "password": password,
            })
            state.sim_mode = not state.ser
            universal_firmware.reset()
        return jsonify(
            status="success",
            type="gateway",
            descriptor=state.serial_port,
            sim_mode=state.sim_mode,
            message="Gateway connected" if not state.sim_mode
                    else "Saved, but could not reach gateway — simulation mode",
        )

    # Fall back to serial.
    settings['connection_type'] = 'serial'
    save_settings(settings)
    with serial_lock:
        if state.ser:
            try:
                state.ser.close()
            except Exception:
                pass
        port = (data.get('port') or settings.get('serial_port') or '').strip() or None
        state.ser, SERIAL_PORT = open_serial(port)
        state.sim_mode = not state.ser
        universal_firmware.reset()
    return jsonify(
        status="success",
        type="serial",
        descriptor=state.serial_port,
        sim_mode=state.sim_mode,
        message="Serial connected" if not state.sim_mode
                else "Saved, but could not open serial — simulation mode",
    )


def _universal_error_response(exc, status=400):
    return jsonify(status="error", message=str(exc)), status


def _ensure_universal_firmware_started():
    universal_firmware.ensure_started()
    return universal_firmware


@app.route('/universal/status')
def universal_status():
    """Return Universal Firmware modules and recent unprovisioned adverts."""
    manager = _ensure_universal_firmware_started()
    return jsonify(manager.status(get_module_count()))


@app.route('/universal/scan', methods=['POST'])
def universal_scan():
    """Discover provisioned Universal Firmware modules in the configured grid."""
    manager = _ensure_universal_firmware_started()
    try:
        manager.scan(get_module_count() - 1)
        return jsonify(status="scanning")
    except UniversalFirmwareError as exc:
        return _universal_error_response(exc)


@app.route('/universal/home', methods=['POST'])
def universal_home():
    """Home a provisioned module by ID or an unprovisioned module by serial."""
    manager = _ensure_universal_firmware_started()
    data = request.get_json(silent=True) or {}
    try:
        if data.get('serial'):
            manager.home_by_serial(data['serial'])
        elif data.get('id') is not None:
            manager.home_module(data['id'])
        else:
            return jsonify(status="error", message="serial or id is required"), 400
        return jsonify(status="sent")
    except UniversalFirmwareError as exc:
        return _universal_error_response(exc)


@app.route('/universal/provision', methods=['POST'])
def universal_provision():
    """Assign an ID by chip serial and wait for the firmware acknowledgement."""
    manager = _ensure_universal_firmware_started()
    data = request.get_json(silent=True) or {}
    try:
        acknowledged = manager.provision(
            data.get('serial'),
            data.get('id'),
        )
        response = {
            "status": "success" if acknowledged else "unconfirmed",
            "acknowledged": acknowledged,
            "message": (
                "Module acknowledged its new ID."
                if acknowledged
                else "No acknowledgement was received. The assignment may still have succeeded."
            ),
        }
        return jsonify(response), 200 if acknowledged else 202
    except UniversalFirmwareError as exc:
        return _universal_error_response(exc)


@app.route('/universal/deprovision', methods=['POST'])
def universal_deprovision():
    """Reset one module's ID, or every module when ``all`` is explicitly true."""
    manager = _ensure_universal_firmware_started()
    data = request.get_json(silent=True) or {}
    try:
        if data.get('all') is True:
            manager.deprovision_all()
            return jsonify(status="sent", message="All modules are returning to provisioning mode.")
        manager.deprovision(data.get('id'))
        return jsonify(status="sent", message="Module is returning to provisioning mode.")
    except UniversalFirmwareError as exc:
        return _universal_error_response(exc)


@app.route('/universal/diagnose', methods=['POST'])
def universal_diagnose():
    """Run a Universal Firmware Q, T, or M diagnostic transaction."""
    manager = _ensure_universal_firmware_started()
    data = request.get_json(silent=True) or {}
    try:
        result = manager.run_diagnostic(
            data.get('id'),
            kind=data.get('kind', 'snapshot'),
            revolutions=data.get('revolutions', 5),
        )
        return jsonify(status="success", result=result)
    except UniversalFirmwareError as exc:
        return _universal_error_response(exc, 504)


# ============================================================
#  DISPLAY
# ============================================================

def _rotation_time(max_dist):
    """Estimate seconds for the slowest module to finish rotating max_dist positions."""
    n = get_module_count()
    min_flap_count = min(get_module_flap_count(i) for i in range(n)) if n else 64
    return max_dist * (4.0 / min_flap_count)

COLOR_MAP = {
    '\U0001f7e5': 'r', '\U0001f7e7': 'o', '\U0001f7e8': 'y', '\U0001f7e9': 'g',
    '\U0001f7e6': 'b', '\U0001f7ea': 'p', '\u2b1c': 'w', '\u2b1b': ' ',
}

def send_to_display_sync(text):
    """Send modules staggered so all arrive at their target character simultaneously."""
    if not text:
        return 0
    clean_text = unicodedata.normalize('NFC', text.upper())
    for emoji, char in COLOR_MAP.items():
        clean_text = clean_text.replace(emoji, char)
    if '"' not in get_flap_chars():
        clean_text = clean_text.replace('"', 'q')
    n = get_module_count()
    clean_text = clean_text.ljust(n)[:n]
    logging.info(f"DISPLAY (sync): {clean_text}")

    dists = []
    for i in range(n):
        char = clean_text[i]
        flap_count = get_module_flap_count(i)
        char_map = get_module_char_map(i)
        target_idx = char_map.find(char)
        if target_idx == -1:
            target_idx = 0
        # Treat -1 (pre-home unknown) as position 0 so sync stagger works on first run
        current = 0 if state.current_indices[i] == -1 else state.current_indices[i]
        dist = (target_idx - current) % flap_count
        dists.append((i, char, target_idx, dist, flap_count))

    max_dist = max(d[3] for d in dists) if dists else 0
    dists_sorted = sorted(dists, key=lambda x: -x[3])

    t0 = time.time()
    with serial_lock:
        for i, char, target_idx, dist, flap_count in dists_sorted:
            delay_before = (max_dist - dist) * (4.0 / flap_count)
            elapsed = time.time() - t0
            remaining = delay_before - elapsed
            if remaining > 0:
                time.sleep(remaining)
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{char}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
            state.current_indices[i] = target_idx

    state.current_display_string = clean_text
    state.is_homed = True
    mqtt_publish_state()
    return max_dist


def send_to_display_slot(text, effect_speed=80):
    """Slot machine: all modules spin to random chars, then lock in L→R."""
    if not text:
        return 0
    clean_text = unicodedata.normalize('NFC', text.upper())
    for emoji, char in COLOR_MAP.items():
        clean_text = clean_text.replace(emoji, char)
    if '"' not in get_flap_chars():
        clean_text = clean_text.replace('"', 'q')
    n = get_module_count()
    clean_text = clean_text.ljust(n)[:n]
    logging.info(f"DISPLAY (slot): {clean_text}")

    # Phase 1: all modules spin to random intermediate chars simultaneously
    # Ensure spin char differs from target so the lock-in is always visible
    spin_chars = []
    for i in range(n):
        char_map = get_module_char_map(i)
        target_idx = char_map.find(clean_text[i])
        if target_idx == -1: target_idx = 0
        candidates = [c for c in char_map[1:len(char_map)-4] if char_map.find(c) != target_idx]
        spin_chars.append(random.choice(candidates) if candidates else char_map[1])
    with serial_lock:
        for i in range(n):
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{spin_chars[i]}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
            char_map = get_module_char_map(i)
            idx = char_map.find(spin_chars[i])
            state.current_indices[i] = idx if idx != -1 else 0

    time.sleep(1.5)

    # Phase 2: lock in final chars L→R
    max_dist = 0
    with serial_lock:
        for i in range(n):
            char = clean_text[i]
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{char}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
                time.sleep(effect_speed / 1000.0)
            char_map = get_module_char_map(i)
            flap_count = get_module_flap_count(i)
            target_idx = char_map.find(char)
            if target_idx == -1:
                target_idx = 0
            dist = (target_idx - state.current_indices[i]) % flap_count
            if dist > max_dist:
                max_dist = dist
            state.current_indices[i] = target_idx

    state.current_display_string = clean_text
    state.is_homed = True
    mqtt_publish_state()
    return max_dist


def _send_with_effect(page_text, page_style, page_speed, is_anim, app_id=None):
    """Dispatch a page send using the active transition style (per-page > per-app > global)."""
    if is_anim:
        return send_to_display(page_text, get_animation_order(page_style or 'ltr'), raw=True, step_delay_ms=page_speed)
    # Priority: per-page > per-app > global
    style = page_style or \
            (settings.get(f'plugin_{app_id}_transition_style') if app_id else None) or \
            settings.get('transition_style', 'ltr')
    app_speed = settings.get(f'plugin_{app_id}_transition_speed') if app_id else None
    speed = page_speed if page_speed is not None else \
            (int(app_speed) if app_speed else int(settings.get('transition_speed', 15)))
    state.last_transition_style = style
    state.last_transition_speed = speed
    if style == 'sync':
        return send_to_display_sync(page_text)
    if style == 'slot':
        return send_to_display_slot(page_text, effect_speed=speed)
    return send_to_display(page_text, get_animation_order(style), step_delay_ms=speed)


def send_to_display(text, order=None, raw=False, step_delay_ms=15):
    if not text:
        return 0

    # For normal text: uppercase first (emojis are unaffected by upper()),
    # then replace emojis with color codes. Animation pages pass raw=True to
    # skip uppercasing so their color codes (r o y g b p w) stay lowercase.
    if not raw:
        clean_text = unicodedata.normalize('NFC', text.upper())
    else:
        clean_text = text
    for emoji, char in COLOR_MAP.items():
        clean_text = clean_text.replace(emoji, char)
    # Apply currency symbol alias: user's currency char → $ (the physical flap position)
    currency = settings.get('currency_symbol', '$').strip()
    if currency and currency != '$':
        clean_text = clean_text.replace(currency.upper(), '$')
    # The physical " flap is addressed as 'q' in the default firmware character map.
    # Only apply this substitution if " is not in the active char map.
    if '"' not in get_flap_chars():
        clean_text = clean_text.replace('"', 'q')
    n = get_module_count()
    clean_text = clean_text.ljust(n)[:n]
    logging.info(f"DISPLAY: {clean_text}")

    if order is None:
        order = list(range(n))

    # Update sim state immediately so the browser reflects the new text without waiting
    # for the serial loop to complete (fixes sim lag on hardware transitions)
    state.current_display_string = clean_text
    state.is_homed = True
    mqtt_publish_state()

    max_dist = 0
    with serial_lock:
        for i in order:
            if i >= len(clean_text):
                continue
            char = clean_text[i]
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{char}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
                time.sleep(step_delay_ms / 1000.0)

            target_idx = get_module_char_map(i).find(char)
            if target_idx == -1:
                target_idx = 0
            flap_count = get_module_flap_count(i)
            dist = 128 if state.current_indices[i] == -1 else (target_idx - state.current_indices[i]) % flap_count
            if dist > max_dist:
                max_dist = dist
            state.current_indices[i] = target_idx

    return max_dist



# ============================================================
#  PLAYLIST LOOP
# ============================================================


# ── Notification Interrupts ────────────────────────────────
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


def _run_app_playlist():
    """Execute one pass through the app playlist entries."""

    entries = state.active_app_playlist
    if not entries:
        state.active_app_playlist = None
        return

    while True:
        for entry in entries:
            if state.stop_event.is_set():
                state.stop_event.clear()
                return

            etype = entry.get('type', 'app')
            duration = float(entry.get('duration', 30))

            if etype == 'compose':
                # Send composed text to display
                text = entry.get('text', '')
                style = entry.get('style', 'ltr')
                speed = int(entry.get('speed', 15))
                order = get_animation_order(style)
                max_dist = send_to_display(text, order, step_delay_ms=speed)
                state.last_sent_page = text
                # Wait for rotation + duration
                rotation_time = _rotation_time(max_dist)
                for _ in range(int(rotation_time * 10)):
                    if state.stop_event.is_set():
                        state.stop_event.clear()
                        return
                    time.sleep(0.1)
                for _ in range(int(duration * 10)):
                    if state.stop_event.is_set():
                        state.stop_event.clear()
                        return
                    time.sleep(0.1)

            elif etype == 'app':
                app_key = entry.get('app', '')
                if not app_key:
                    continue
                # Temporarily set active_app so existing fetch logic works
                state.active_app = app_key
                deadline = time.time() + duration

                while time.time() < deadline:
                    if state.stop_event.is_set():
                        state.active_app = None
                        state.stop_event.clear()
                        return

                    display_pages = _get_pages_for_app(app_key)
                    if not display_pages:
                        time.sleep(1)
                        continue

                    is_anim = app_key.startswith('anim_') or \
                              (app_key in _plugin_registry and _plugin_registry[app_key].get('animation'))

                    # Determine per-page delay
                    reg = app_key[7:] if app_key.startswith('plugin_') else app_key
                    if is_anim:
                        eff_delay = max(0.1, float(settings.get('anim_speed', '0.4')))
                    elif reg in _plugin_registry:
                        saved = settings.get(f'plugin_{reg}_loop_delay', '')
                        default = float(_plugin_registry[reg].get('loop_delay', settings.get('global_loop_delay', 5)))
                        eff_delay = float(saved) if saved else default
                    else:
                        eff_delay = float(settings.get('global_loop_delay', 5))

                    active_order = None
                    if is_anim:
                        active_order = get_animation_order(settings.get('anim_style', 'ltr'))

                    for page in display_pages:
                        if state.stop_event.is_set() or time.time() >= deadline:
                            break
                        page_text = page.get('text', '') if isinstance(page, dict) else page
                        page_style = page.get('style') if isinstance(page, dict) else None
                        page_speed = int(page.get('speed', 15)) if isinstance(page, dict) else 15
                        page_delay = float(page.get('delay', eff_delay)) if isinstance(page, dict) else eff_delay

                        anim_style_ap = settings.get('anim_style','ltr') if is_anim else None
                        eff_style_ap = (anim_style_ap or page_style or
                                        (settings.get(f'plugin_{reg}_transition_style') if reg else None) or
                                        settings.get('transition_style', 'ltr'))
                        state.last_transition_style = eff_style_ap
                        state.last_transition_speed = page_speed if page_speed is not None else int(settings.get('transition_speed', 15))
                        if is_anim or page_text != state.last_sent_page:
                            max_dist = _send_with_effect(page_text, page_style if not is_anim else anim_style_ap, page_speed, is_anim, app_id=reg)
                            state.last_sent_page = page_text

                        rotation_time = _rotation_time(max_dist)
                        for _ in range(int(rotation_time * 10)):
                            if state.stop_event.is_set() or time.time() >= deadline: break
                            time.sleep(0.1)
                        for _ in range(int(page_delay * 10)):
                            if state.stop_event.is_set() or time.time() >= deadline: break
                            time.sleep(0.1)

                state.active_app = None

        # After all entries
        if not state.app_playlist_loop:
            state.active_app_playlist = None
            return
        # Otherwise loop continues


def _get_pages_for_app(app_key):
    """Fetch display pages for an app via the plugin system."""
    if app_key in _plugin_registry:
        return get_plugin_pages(app_key)
    # Try with plugin_ prefix stripped
    if app_key.startswith('plugin_'):
        return get_plugin_pages(app_key[7:])
    return []


# ============================================================
#  SCHEDULER + QUIET HOURS
# ============================================================



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


start_background_task(_schedule_loop)
start_background_task(_schedule_tick)

def playlist_loop():

    while True:
        now = time.time()
        display_pages = []
        active_order  = None   # custom module send order for this cycle

        # ── App playlist mode ─────────────────────────────
        if state.active_app_playlist is not None:
            _run_app_playlist()
            continue

        # ── No active app — use compose playlist ──────────
        if state.active_app is None:
            display_pages = state.current_playlist

        # ── Plugin-based apps ─────────────────────────────
        elif state.active_app in _plugin_registry:
            manifest = _plugin_registry[state.active_app]
            display_pages = get_plugin_pages(state.active_app)
            if manifest.get('animation'):
                active_order = get_animation_order(settings.get('anim_style', 'ltr'))

        elif state.active_app.startswith('plugin_') and state.active_app[7:] in _plugin_registry:
            plugin_id = state.active_app[7:]
            manifest = _plugin_registry[plugin_id]
            display_pages = get_plugin_pages(plugin_id)
            if manifest.get('animation'):
                active_order = get_animation_order(settings.get('anim_style', 'ltr'))

        else:
            display_pages = state.current_playlist

        if not display_pages:
            time.sleep(1)
            continue

        # Resolve plugin_ prefix for registry lookups
        reg_key = state.active_app[7:] if (state.active_app and state.active_app.startswith('plugin_')) else state.active_app

        is_anim = (reg_key is not None and reg_key.startswith('anim_')) or \
                  (reg_key in _plugin_registry and _plugin_registry[reg_key].get('animation'))

        # Effective per-page delay
        if is_anim:
            eff_delay = max(0.1, float(settings.get('anim_speed', '0.4')))
            if reg_key in _plugin_registry:
                eff_delay = max(0.1, float(_plugin_registry[reg_key].get('loop_delay', eff_delay)))
        elif reg_key in _plugin_registry:
            saved = settings.get(f'plugin_{reg_key}_loop_delay', '')
            manifest = _plugin_registry[reg_key]
            default = float(manifest.get('loop_delay', settings.get('global_loop_delay', 5)))
            eff_delay = float(saved) if saved else default
        else:
            eff_delay = float(settings.get('global_loop_delay', state.loop_delay))

        for page in display_pages:
            if state.stop_event.is_set():
                break

            # Resolve per-page settings — rich playlist objects vs. plain strings
            if isinstance(page, dict):
                page_text  = page.get('text', '')
                page_delay = float(page.get('delay', eff_delay))
                page_style = page.get('style')
                page_speed = int(page.get('speed', 15))
            else:
                page_text  = page
                page_delay = eff_delay
                page_style = None
                page_speed = 15

            max_dist = 0
            # Animations always resend each frame; other apps skip unchanged pages
            anim_style = settings.get('anim_style', 'ltr') if is_anim else None
            eff_style = anim_style or page_style or \
                        (settings.get(f'plugin_{reg_key}_transition_style') if reg_key else None) or \
                        settings.get('transition_style', 'ltr')
            eff_speed = page_speed if page_speed is not None else int(settings.get('transition_speed', 15))
            state.last_transition_style = eff_style
            state.last_transition_speed = eff_speed
            if is_anim or page_text != state.last_sent_page:
                max_dist = _send_with_effect(page_text, anim_style or page_style, page_speed, is_anim, app_id=reg_key)
                state.last_sent_page = page_text

            # Skip rotation wait if manifest opts out (e.g. continuous random spin)
            skip_rot = reg_key in _plugin_registry and _plugin_registry[reg_key].get('skip_rotation_wait')
            if not skip_rot:
                rotation_time = _rotation_time(max_dist)
                for _ in range(int(rotation_time * 10)):
                    if state.stop_event.is_set(): break
                    time.sleep(0.1)

            for _ in range(int(page_delay * 10)):
                if state.stop_event.is_set(): break
                time.sleep(0.1)

            # Check for notification interrupts between pages
            if settings.get('notify_enabled', False):
                msg = _pop_notify()
                if msg:
                    _show_notify_message(msg)

        if state.stop_event.is_set():
            state.stop_event.clear()


start_background_task(playlist_loop)


# ============================================================
#  STARTUP TASKS
# ============================================================

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


# ============================================================
#  TRIGGER RUNTIME
# ============================================================

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
                msg = {
                    'id': f"trig_{trig_id}_{int(now*1000)}",
                    'text': text,
                    'source': f"trigger:{app_id}",
                    'display_seconds': display_seconds,
                    'animation': 'ltr',
                    'created_at': now,
                    'expires_at': now + 300,
                }
                with _notify_lock:
                    _notify_queue.append(msg)
                logging.info(f"Trigger fired: {trig.get('name',trig_id)} ({app_id})")


def _trigger_loop():
    while True:
        time.sleep(10)
        _check_triggers()


start_background_task(_trigger_loop)


# ============================================================
#  FLASK ROUTES
# ============================================================

@app.route('/')
def index():
    version = read_version()
    return render_template('index.html', version=version)

@app.route('/current_state')
def current_state():
    return jsonify(is_homed=state.is_homed, state=state.current_display_string, active_app=state.active_app,
                   active_app_playlist=state.active_app_playlist is not None,
                   app_playlist_name=state.app_playlist_name,
                   rows=get_rows(), cols=get_cols(), sim_mode=state.sim_mode, hardware_connected=state.ser is not None,
                   transition_style=state.last_transition_style,
                   transition_speed=state.last_transition_speed)

@app.route('/grid_config')
def grid_config():
    return jsonify(rows=get_rows(), cols=get_cols(), total=get_module_count(), sim_mode=state.sim_mode)

@app.route('/toggle_sim', methods=['POST'])
def toggle_sim():
    state.sim_mode = request.json.get('enabled', True)
    return jsonify(sim_mode=state.sim_mode)

@app.route('/settings', methods=['GET', 'POST'])
def handle_settings():
    if request.method == 'POST':
        data   = request.json
        action = data.get('action')
        mod_id = str(data.get('id', '0'))

        if action == 'save_global':
            # Validate char_map before applying
            if 'char_map' in data and len(data['char_map']) != 64:
                return jsonify(error="Character map must be exactly 64 characters"), 400
            # Save any key except internal/protected ones
            protected = {'action', 'id', 'offsets', 'calibrations', 'tuned_chars', 'installed_apps', 'saved_playlists', 'saved_app_playlists'}
            for k, v in data.items():
                if k not in protected:
                    settings[k] = v
            if 'sim_rows' in data or 'sim_cols' in data:
                resize_grid()
                mqtt_publish_discovery()
            save_settings(settings)
            return jsonify(status="Saved")

        if action == 'adjust':
            delta      = int(data.get('delta', 0))
            new_offset = int(settings['offsets'].get(mod_id, 2832)) + delta
            settings['offsets'][mod_id] = new_offset
            save_settings(settings)
            send_raw(f"m{int(mod_id):02d}o{new_offset}")
            return jsonify(new_offset=new_offset)

        if action == 'home_one':
            send_raw(f"m{int(mod_id):02d}h")
            state.current_indices[int(mod_id)] = 0
            sl = list(state.current_display_string.ljust(get_module_count()))
            sl[int(mod_id)] = ' '
            state.current_display_string = "".join(sl)
            return jsonify(status="Homing")

        if action == 'calibrate':
            with serial_lock:
                if state.ser:
                    state.ser.reset_input_buffer()
                    state.ser.write(f"m{int(mod_id):02d}c\n".encode())
                    state.ser.flush()
                    start_wait = time.time()
                    buffer = ""
                    target = f"m{int(mod_id):02d}:"
                    while (time.time() - start_wait) < 45.0:
                        if state.ser.in_waiting > 0:
                            chunk = state.ser.read(state.ser.in_waiting).decode('utf-8', errors='ignore')
                            buffer += chunk
                            if target in buffer and '\n' in buffer[buffer.find(target):]:
                                valid_part = buffer[buffer.find(target):].split('\n')[0]
                                try:
                                    val = int(valid_part.split(target)[1])
                                    settings['calibrations'][mod_id] = val
                                    save_settings(settings)
                                    state.ser.write(f"m{int(mod_id):02d}t{val}\n".encode())
                                    state.ser.flush()
                                    return jsonify(status="success", steps=val)
                                except:
                                    pass
                        time.sleep(0.1)
                    return jsonify(status="error", message="Timeout"), 500
    return jsonify(settings)

@app.route('/custom_tune', methods=['POST'])
def custom_tune():
    data   = request.json
    action = data.get('action')
    mod_id = int(data.get('id', 0))

    if action == 'goto':
        step = int(data.get('step', 0))
        idx  = int(data.get('index', 0))
        send_raw(f"m{mod_id:02d}g{step}")
        if 0 <= idx < len(get_flap_chars()):
            state.current_indices[mod_id] = idx
            sl = list(state.current_display_string.ljust(get_module_count()))
            sl[mod_id] = get_flap_chars()[idx]
            state.current_display_string = "".join(sl)

    elif action == 'save':
        idx  = int(data.get('index', 0))
        step = int(data.get('step', 0))
        send_raw(f"m{mod_id:02d}w{idx}:{step}")
        settings['tuned_chars'][str(mod_id)][str(idx)] = step
        save_settings(settings)

    elif action == 'erase':
        idx = str(data.get('index', ''))
        if idx:
            send_raw(f"m{mod_id:02d}w{idx}:65535")
            settings['tuned_chars'][str(mod_id)].pop(idx, None)
        else:
            send_raw(f"m{mod_id:02d}e")
            settings['tuned_chars'][str(mod_id)] = {}
        save_settings(settings)

    return jsonify(status="Success")

@app.route('/sync_module', methods=['POST'])
def sync_module():
    mod_id  = int(request.json.get('id', 0))
    success = sync_hardware_data(mod_id)
    sync_module_config(mod_id)
    return jsonify(status="success" if success else "failed", settings=settings)

@app.route('/sync_all', methods=['POST'])
def sync_all():
    for i in range(get_module_count()):
        sync_hardware_data(i)
        sync_module_config(i)
    return jsonify(status="success", settings=settings)

@app.route('/assign_id', methods=['POST'])
def assign_id():
    send_raw(f"m**i{int(request.json.get('id', 0)):02d}")
    return jsonify(status="ID Assigned")

@app.route('/toggle_autohome', methods=['POST'])
def toggle_autohome():
    enabled = request.json.get('enabled', True)
    settings['auto_home'] = enabled
    save_settings(settings)
    send_raw(f"m**a{1 if enabled else 0}")
    return jsonify(status="Auto-home updated")

@app.route('/update_playlist', methods=['POST'])
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

@app.route('/run_app', methods=['POST'])
def run_app():
    state.active_app_playlist = None
    state.active_app   = request.json.get('app')

    # Resolve plugin_ prefix
    registry_key = state.active_app[7:] if state.active_app and state.active_app.startswith('plugin_') else state.active_app

    # Use loop_delay from user settings, then manifest, then global default
    if registry_key in _plugin_registry:
        manifest = _plugin_registry[registry_key]
        if manifest.get('animation'):
            state.loop_delay = max(0.1, float(settings.get('anim_speed', '0.4')))
        else:
            saved = settings.get(f'plugin_{registry_key}_loop_delay', '')
            default = float(manifest.get('loop_delay', settings.get('global_loop_delay', 5)))
            state.loop_delay = float(saved) if saved else default
    else:
        state.loop_delay = float(settings.get('global_loop_delay', 5))

    state.stop_event.set()
    mqtt_publish_state()
    return jsonify(status=f"App {state.active_app} started")

@app.route('/stop_app', methods=['POST'])
def stop_app():
    state.active_app = None
    state.active_app_playlist = None
    state.stop_event.set()
    mqtt_publish_state()
    return jsonify(status="stopped")

@app.route('/home_all')
def home_all():
    send_raw("m**h")
    state.is_homed = True
    state.current_indices = [0] * get_module_count()
    state.current_display_string = " " * get_module_count()
    return jsonify(status="Homing All")


# ============================================================
#  AUTO FINE-TUNE
# ============================================================

@app.route('/auto_tune', methods=['POST'])
def auto_tune_route():
    data   = request.json
    action = data.get('action')

    if action == 'home':
        send_raw("m**h")
        state.is_homed = True
        state.current_indices = [0] * get_module_count()
        state.current_display_string = " " * get_module_count()
        return jsonify(status="ok")

    elif action == 'goto_char':
        char_idx = int(data.get('char_index', 0))
        n = get_module_count()
        # Build per-module string: each module gets the char at char_idx in its own map
        chars = []
        for i in range(n):
            char_map = get_module_char_map(i)
            if 0 <= char_idx < len(char_map):
                chars.append(char_map[char_idx])
            else:
                chars.append(char_map[0])
        text = ''.join(chars)
        send_to_display(text, raw=True)
        flap_chars = get_flap_chars()
        return jsonify(status="ok", char=flap_chars[char_idx] if char_idx < len(flap_chars) else ' ', index=char_idx)

    elif action == 'adjust':
        modules   = data.get('modules', [])
        char_idx  = int(data.get('char_index', 0))
        delta     = int(data.get('delta', 0))
        adjusted  = []

        for mod_id in modules:
            mod_str = str(mod_id)
            cal     = int(settings['calibrations'].get(mod_str, 4096))
            flap_count = get_module_flap_count(mod_id)
            expected = (char_idx * cal) // flap_count

            # Current value: tuned if available, else expected
            tuned_val = settings['tuned_chars'].get(mod_str, {}).get(str(char_idx))
            base = int(tuned_val) if tuned_val is not None else expected
            new_val = base + delta

            # Clamp to valid range
            if new_val < 0:
                new_val = 0
            if new_val >= cal:
                new_val = cal - 1

            # Update settings
            if mod_str not in settings['tuned_chars']:
                settings['tuned_chars'][mod_str] = {}
            settings['tuned_chars'][mod_str][str(char_idx)] = new_val

            # Save the tuned position, then move there immediately so the user
            # can see the compensation. The preview uses firmware `g`, the
            # absolute motor-step GOTO command also used by Custom Tune's Test
            # Position flow; re-sending the same flap index could be ignored
            # because firmware still considers that character index active.
            save_command, preview_command = build_tuning_adjust_commands(
                mod_id,
                char_idx,
                new_val,
                cal,
                flap_count,
            )
            send_raw(save_command)
            send_raw(preview_command)

            adjusted.append({
                'module': mod_id,
                'old': base,
                'new': new_val,
                'previewed': True,
            })

        save_settings(settings)
        return jsonify(status="ok", adjusted=adjusted)

    elif action == 'get_positions':
        char_idx = int(data.get('char_index', 0))
        positions = {}
        for i in range(get_module_count()):
            mod_str  = str(i)
            cal      = int(settings['calibrations'].get(mod_str, 4096))
            flap_count = get_module_flap_count(i)
            expected = (char_idx * cal) // flap_count
            tuned    = settings['tuned_chars'].get(mod_str, {}).get(str(char_idx))
            positions[mod_str] = {
                'expected': expected,
                'tuned':    int(tuned) if tuned is not None else None,
                'active':   int(tuned) if tuned is not None else expected,
            }
        return jsonify(positions=positions)

    return jsonify(status="error", message="Unknown action"), 400


@app.route('/tuning_status')
def tuning_status():
    char_idx = int(request.args.get('char_index', 0))
    if char_idx < 0 or char_idx >= len(get_flap_chars()):
        return jsonify(status="error", message="Invalid char_index"), 400
    positions = {}
    for i in range(get_module_count()):
        mod_str = str(i)
        cal = int(settings['calibrations'].get(mod_str, 4096))
        flap_count = get_module_flap_count(i)
        expected = (char_idx * cal) // flap_count
        tuned = settings['tuned_chars'].get(mod_str, {}).get(str(char_idx))
        positions[mod_str] = {
            'expected': expected,
            'tuned': int(tuned) if tuned is not None else None,
            'active': int(tuned) if tuned is not None else expected,
        }
    return jsonify(
        char_index=char_idx,
        char=get_flap_chars()[char_idx],
        flap_chars=get_flap_chars(),
        grid={'rows': get_rows(), 'cols': get_cols(), 'total': get_module_count()},
        positions=positions,
    )


# ── Backup / Restore ─────────────────────────────────────────

@app.route('/backup_settings')
def backup_settings():
    return jsonify({
        'version':      1,
        'created':      datetime.now().isoformat(),
        'offsets':      settings['offsets'],
        'calibrations': settings['calibrations'],
        'tuned_chars':  settings['tuned_chars'],
    })

@app.route('/restore_settings', methods=['POST'])
def restore_settings():
    data = request.json
    if not data:
        return jsonify(status="error", message="No data"), 400
    if 'offsets'      in data: settings['offsets'].update(data['offsets'])
    if 'calibrations' in data: settings['calibrations'].update(data['calibrations'])
    if 'tuned_chars'  in data: settings['tuned_chars'].update(data['tuned_chars'])
    save_settings(settings)
    hw = False
    if state.ser:
        hw = True
        for i in range(get_module_count()):
            s = str(i)
            send_raw(f"m{i:02d}o{int(settings['offsets'].get(s, 2832))}")
            send_raw(f"m{i:02d}t{int(settings['calibrations'].get(s, 4096))}")
            send_raw(f"m{i:02d}e")
            for idx, step in settings['tuned_chars'].get(s, {}).items():
                sv = int(step)
                if sv != 65535:
                    send_raw(f"m{i:02d}w{idx}:{sv}")
            logging.info(f"Restored m{i:02d}")
    return jsonify(status="success", hardware_updated=hw, modules_updated=45)

# ── Saved Playlists ──────────────────────────────────────────

@app.route('/playlists', methods=['GET', 'POST'])
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

@app.route('/playlists/<path:name>', methods=['DELETE'])
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

@app.route('/schedules', methods=['GET', 'POST'])
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


@app.route('/schedule_tick', methods=['POST'])
def schedule_tick_route():
    """Force an immediate schedule evaluation (e.g. after saving schedules)."""
    state.active_schedule_id = None  # reset so current window re-fires
    threading.Thread(target=_schedule_tick, daemon=True).start()
    return jsonify(status="ok")


# ============================================================
#  APP PLAYLISTS
# ============================================================

@app.route('/run_app_playlist', methods=['POST'])
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

@app.route('/app_playlists', methods=['GET', 'POST'])
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

@app.route('/app_playlists/<path:name>', methods=['DELETE'])
def delete_app_playlist(name):
    plists = settings.get('saved_app_playlists', {})
    if name in plists:
        del plists[name]
        settings['saved_app_playlists'] = plists
        save_settings(settings)
    mqtt_publish_discovery()
    return jsonify(status="deleted")


# ============================================================
#  APP LIBRARY API
# ============================================================

@app.route('/app_library')
def app_library():
    """List all apps in the apps/ directory with installed status."""
    apps = []
    if os.path.isdir(APPS_PATH):
        for app_id in os.listdir(APPS_PATH):
            manifest_path = os.path.join(APPS_PATH, app_id, 'manifest.json')
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, encoding='utf-8') as f:
                    m = json.load(f)
                m['id'] = app_id
                m['installed'] = app_id in _plugin_registry
                apps.append(m)
            except Exception:
                pass
    apps.sort(key=lambda a: a.get('name', '').lower())
    return jsonify({"version": 1, "apps": apps})


@app.route('/app_library/install', methods=['POST'])
def app_library_install():
    app_id = request.json.get("id", "").strip()
    if not app_id:
        return jsonify(status="error", message="No app ID"), 400
    if app_id in _plugin_registry:
        return jsonify(status="error", message="Already installed"), 409

    app_dir = os.path.join(APPS_PATH, app_id)
    if not os.path.isdir(app_dir):
        # Download from remote if not local
        base_url = settings.get('app_library_url', 'https://raw.githubusercontent.com/csader/splitflap-os/main/apps')
        try:
            os.makedirs(app_dir, exist_ok=True)
            manifest_url = f"{base_url}/{app_id}/manifest.json"
            req = urllib.request.Request(manifest_url, headers={"User-Agent": "SplitFlap/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                manifest_bytes = resp.read()
            with open(os.path.join(app_dir, "manifest.json"), "wb") as f:
                f.write(manifest_bytes)

            manifest = json.loads(manifest_bytes.decode())
            app_type = manifest.get("type", "channel")

            if app_type == "channel":
                data_url = f"{base_url}/{app_id}/data.json"
                req = urllib.request.Request(data_url, headers={"User-Agent": "SplitFlap/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    with open(os.path.join(app_dir, "data.json"), "wb") as f:
                        f.write(resp.read())
            elif app_type == "functional":
                code_url = f"{base_url}/{app_id}/app.py"
                req = urllib.request.Request(code_url, headers={"User-Agent": "SplitFlap/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    with open(os.path.join(app_dir, "app.py"), "wb") as f:
                        f.write(resp.read())
        except Exception as e:
            if os.path.isdir(app_dir):
                shutil.rmtree(app_dir, ignore_errors=True)
            logging.error(f"Install error for {app_id}: {e}")
            return jsonify(status="error", message=str(e)), 500

    # Add to installed_apps list
    installed = settings.get('installed_apps', [])
    if app_id not in installed:
        installed.append(app_id)
        settings['installed_apps'] = installed
        save_settings(settings)

    load_installed_plugins()
    _registry_cache['fetched_at'] = 0
    mqtt_publish_discovery()
    return jsonify(status="success", id=app_id)


@app.route('/app_library/uninstall', methods=['POST'])
def app_library_uninstall():
    app_id = request.json.get("id", "").strip()
    if not app_id:
        return jsonify(status="error", message="No app ID"), 400
    if state.active_app in (app_id, f"plugin_{app_id}"):
        state.active_app = None
        state.stop_event.set()
    # Remove from installed_apps list (keep files)
    installed = settings.get('installed_apps', [])
    if app_id in installed:
        installed.remove(app_id)
        settings['installed_apps'] = installed
        save_settings(settings)
    load_installed_plugins()
    _registry_cache['fetched_at'] = 0
    mqtt_publish_discovery()
    return jsonify(status="success", id=app_id)


_teams_cache = {}

@app.route('/sports_leagues')
def sports_leagues_route():
    """Return league list with current follow settings."""
    leagues = []
    for key, info in SPORTS_LEAGUES.items():
        followed = settings.get(f'sports_{key}', '').strip()
        leagues.append({'key': key, 'name': info['name'], 'path': info['path'],
                        'followed': followed, 'follow_all': followed == '*'})
    return jsonify(leagues=leagues)

@app.route('/sports_teams/<league_key>')
def sports_teams_route(league_key):
    """Search teams for a league. Use ?q= for search, otherwise return all (cached)."""
    info = SPORTS_LEAGUES.get(league_key)
    if not info:
        return jsonify(teams=[], error='Unknown league'), 404
    if league_key in ('pga', 'ufc'):
        return jsonify(teams=[], no_teams=True)
    query = request.args.get('q', '').strip().lower()
    # WSOC has no teams endpoint — reuse MSOC list (same schools)
    fetch_key = 'msoc' if league_key == 'wsoc' else league_key
    fetch_path = SPORTS_LEAGUES[fetch_key]['path']
    # Fetch and cache full list
    if league_key not in _teams_cache:
        try:
            all_teams = []
            for page in range(1, 4):
                url = f"https://site.api.espn.com/apis/site/v2/sports/{fetch_path}/teams?limit=200&page={page}"
                data = requests.get(url, timeout=8).json()
                batch = data.get('sports', [{}])[0].get('leagues', [{}])[0].get('teams', [])
                if not batch:
                    break
                for entry in batch:
                    t = entry.get('team', entry)
                    all_teams.append({'abbr': t.get('abbreviation', '?'), 'name': t.get('displayName', '?'),
                                      'short': t.get('shortDisplayName', t.get('displayName', '?'))})
            all_teams.sort(key=lambda t: t['name'])
            # Deduplicate by abbreviation
            seen = set()
            all_teams = [t for t in all_teams if t['abbr'] not in seen and not seen.add(t['abbr'])]
            _teams_cache[league_key] = all_teams
        except Exception as e:
            return jsonify(teams=[], error=str(e)), 502
    teams = _teams_cache[league_key]
    if query:
        teams = [t for t in teams if query in t['name'].lower() or query in t['abbr'].lower()]
    return jsonify(teams=teams)

@app.route('/sports_follow', methods=['POST'])
def sports_follow():
    """Save followed teams for a league."""
    data = request.json
    league = data.get('league', '')
    teams = data.get('teams', '')  # comma-sep abbreviations or '*'
    if league not in SPORTS_LEAGUES:
        return jsonify(status='error', message='Unknown league'), 400
    settings[f'sports_{league}'] = teams
    save_settings(settings)
    return jsonify(status='success')

# ============================================================
#  SEARCH ENDPOINTS (timezone, stocks, crypto, location)
# ============================================================

@app.route('/location_search')
def location_search_route():
    """Search for locations globally via Nominatim."""
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify(results=[])
    try:
        url = f'https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=6&addressdetails=1'
        data = requests.get(url, timeout=5, headers={'User-Agent': 'SplitFlapOS/1.0'}).json()
        results = []
        for r in data:
            name = r.get('display_name', query)
            addr = r.get('address', {})
            short_name = (addr.get('city') or addr.get('town') or addr.get('village')
                          or addr.get('municipality') or name.split(',')[0].strip())
            results.append({
                'lat': r['lat'],
                'lon': r['lon'],
                'name': name,
                'short_name': short_name,
                'value': f"{r['lat']},{r['lon']}|{query}",
                'label': name,
            })
        return jsonify(results=results)
    except Exception as e:
        logging.error(f"Location search error: {e}")
        return jsonify(results=[], error=str(e)), 502

@app.route('/location_timezone')
def location_timezone_route():
    """Get timezone for a lat/lon via Open-Meteo."""
    lat = request.args.get('lat', '')
    lon = request.args.get('lon', '')
    if not lat or not lon:
        return jsonify(timezone='')
    try:
        data = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={'latitude': lat, 'longitude': lon, 'forecast_days': 1, 'current': 'temperature_2m'},
            timeout=5
        ).json()
        return jsonify(timezone=data.get('timezone', ''))
    except Exception:
        return jsonify(timezone='')

@app.route('/timezones')
def timezones_route():
    """Search timezones with common ones first."""
    query = request.args.get('q', '').strip().lower()
    common = ['US/Eastern','US/Central','US/Mountain','US/Pacific','US/Hawaii',
              'Europe/London','Europe/Paris','Europe/Berlin','Asia/Tokyo','Asia/Shanghai',
              'Australia/Sydney','Pacific/Auckland','America/Chicago','America/Denver',
              'America/Los_Angeles','America/New_York','America/Toronto','America/Sao_Paulo']
    all_zones = pytz.common_timezones
    results = []
    seen = set()
    def add_zone(tz):
        if tz in seen: return
        seen.add(tz)
        try:
            offset = datetime.now(pytz.timezone(tz)).strftime('%z')
            label = f"{tz} (UTC{offset[:3]}:{offset[3:]})"
        except Exception:
            label = tz
        results.append({'value': tz, 'label': label})
    if not query:
        for tz in common: add_zone(tz)
    else:
        for tz in common:
            if query in tz.lower(): add_zone(tz)
        for tz in all_zones:
            if query in tz.lower(): add_zone(tz)
    return jsonify(zones=results[:20])

@app.route('/stocks_search')
def stocks_search_route():
    """Search stock tickers via Yahoo Finance autocomplete."""
    query = request.args.get('q', '').strip()
    if len(query) < 1:
        return jsonify(tickers=[])
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0&enableFuzzyQuery=false&quotesQueryId=tss_match_phrase_query"
        data = requests.get(url, timeout=5, headers={'User-Agent':'Mozilla/5.0'}).json()
        tickers = []
        for q in data.get('quotes', []):
            sym = q.get('symbol', '')
            name = q.get('shortname') or q.get('longname') or ''
            if sym:
                tickers.append({'value': sym, 'label': f"{sym} — {name}" if name else sym})
        return jsonify(tickers=tickers)
    except Exception as e:
        logging.error(f"Stock search error: {e}")
        return jsonify(tickers=[], error=str(e)), 502

_crypto_cache = []

@app.route('/crypto_search')
def crypto_search_route():
    """Search CoinGecko coins (cached)."""
    global _crypto_cache
    query = request.args.get('q', '').strip().lower()
    if len(query) < 1:
        return jsonify(coins=[])
    if not _crypto_cache:
        try:
            data = requests.get('https://api.coingecko.com/api/v3/coins/list', timeout=10).json()
            _crypto_cache = [{'id': c['id'], 'symbol': c['symbol'].upper(), 'name': c['name']} for c in data]
        except Exception as e:
            logging.error(f"CoinGecko fetch error: {e}")
            return jsonify(coins=[], error=str(e)), 502
    results = []
    for c in _crypto_cache:
        if query in c['name'].lower() or query in c['symbol'].lower() or query in c['id']:
            results.append({'value': c['id'], 'label': f"{c['name']} ({c['symbol']})",
                            '_exact': c['id']==query or c['name'].lower()==query or c['symbol'].lower()==query})
            if len(results) >= 30: break
    results.sort(key=lambda r: (not r['_exact'], r['label'].lower()))
    for r in results: del r['_exact']
    return jsonify(coins=results[:12])

# ============================================================
#  NETWORK STATUS
# ============================================================


def _check_network():
    """Detect network mode and internet connectivity."""
    # Check mode file written by network-check.sh
    try:
        with open('/tmp/splitflap-network-mode', 'r') as f:
            state.network_mode = f.read().strip()
    except FileNotFoundError:
        state.network_mode = 'wifi'  # assume normal if no file (dev mode)

    # Check internet connectivity
    try:
        requests.get('https://httpbin.org/status/200', timeout=3)
        state.is_online = True
    except Exception:
        state.is_online = False

    # Publish network state via MQTT
    _mqtt_publish_network()

def check_online():
    """Return cached online status. Refreshed periodically."""
    return state.is_online


def _mqtt_publish_network():
    """Publish network status to MQTT."""
    if not state.mqtt_client or not state.mqtt_client.is_connected():
        return
    import subprocess
    ip = '?'
    ssid = '?'
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
        ip = result.stdout.strip().split()[0] if result.stdout.strip() else '?'
    except Exception:
        pass
    try:
        result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True, timeout=2)
        ssid = result.stdout.strip() or ('SplitflapOS' if state.network_mode == 'hotspot' else '?')
    except Exception:
        if state.network_mode == 'hotspot':
            ssid = settings.get('hotspot_ssid', 'SplitflapOS')
    state.mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/network/mode", state.network_mode, retain=True)
    state.mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/network/ssid", ssid, retain=True)
    state.mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/network/ip", ip, retain=True)
    state.mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/network/online", "ON" if state.is_online else "OFF", retain=True)


# Initial check on startup
start_background_task(_check_network)

# Periodic connectivity check every 60s
def _periodic_network_check():
    while True:
        time.sleep(60)
        _check_network()

start_background_task(_periodic_network_check)


@app.route('/mqtt_reconnect', methods=['POST'])
def mqtt_reconnect_route():
    """Reconnect MQTT with current settings."""
    mqtt_reconnect()
    return jsonify(status="reconnecting")


@app.route('/network_status')
def network_status():
    """Return current network mode and connectivity."""
    import subprocess
    ip = '?'
    ssid = '?'
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
        ip = result.stdout.strip().split()[0] if result.stdout.strip() else '?'
    except Exception:
        pass
    try:
        result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True, timeout=2)
        ssid = result.stdout.strip() or ('SplitflapOS' if state.network_mode == 'hotspot' else '?')
    except Exception:
        if state.network_mode == 'hotspot':
            ssid = settings.get('hotspot_ssid', 'SplitflapOS')
    return jsonify(mode=state.network_mode, online=state.is_online, ip=ip, ssid=ssid)

@app.route('/network_config', methods=['POST'])
def network_config():
    """Save hotspot configuration."""
    data = request.json
    if data.get('hotspot_ssid'):
        settings['hotspot_ssid'] = data['hotspot_ssid']
    if data.get('hotspot_password'):
        settings['hotspot_password'] = data['hotspot_password']
    save_settings(settings)
    # Update systemd service environment
    service_path = '/etc/systemd/system/splitflap-network.service'
    if os.path.isfile(service_path):
        try:
            with open(service_path, 'r') as f:
                lines = f.readlines()
            with open(service_path, 'w') as f:
                for line in lines:
                    if line.startswith('Environment=SPLITFLAP_HOTSPOT_SSID='):
                        f.write(f"Environment=SPLITFLAP_HOTSPOT_SSID={settings['hotspot_ssid']}\n")
                    elif line.startswith('Environment=SPLITFLAP_HOTSPOT_PASS='):
                        f.write(f"Environment=SPLITFLAP_HOTSPOT_PASS={settings['hotspot_password']}\n")
                    else:
                        f.write(line)
            os.system('systemctl daemon-reload')
        except Exception as e:
            logging.error(f"Failed to update network service: {e}")
    return jsonify(status="saved")

@app.route('/wifi_scan')
def wifi_scan():
    """Scan for available WiFi networks."""
    import subprocess
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list', '--rescan', 'yes'],
            capture_output=True, text=True, timeout=15)
        networks = []
        seen = set()
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':')
            ssid = parts[0] if parts else ''
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            signal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            security = parts[2] if len(parts) > 2 else ''
            networks.append({'ssid': ssid, 'signal': signal, 'security': security})
        networks.sort(key=lambda n: -n['signal'])
        return jsonify(networks=networks)
    except Exception as e:
        logging.error(f"WiFi scan error: {e}")
        return jsonify(networks=[], error=str(e))

@app.route('/wifi_connect', methods=['POST'])
def wifi_connect():
    """Connect to a WiFi network via NetworkManager."""
    import subprocess
    data = request.json
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    if not ssid:
        return jsonify(status="error", message="SSID required"), 400
    try:
        # Remove existing connection with same name if any
        subprocess.run(['nmcli', 'con', 'delete', ssid], capture_output=True, timeout=5)
        # Connect
        cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]
        if password:
            cmd += ['password', password]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return jsonify(status="success", message=f"Connected to {ssid}")
        else:
            return jsonify(status="error", message=result.stderr.strip() or "Connection failed"), 400
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500


# ============================================================
#  NOTIFICATION INTERRUPTS
# ============================================================

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


@app.route('/notify', methods=['POST'])
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
    display_seconds = float(data.get('display_seconds', settings.get('notify_display_seconds', 10)))
    now = time.time()
    msg = {
        'id': f"msg_{int(now * 1000)}",
        'text': text,
        'source': source,
        'display_seconds': display_seconds,
        'animation': data.get('animation', 'ltr'),
        'created_at': now,
        'expires_at': now + 300,  # expire if not shown within 5 minutes
    }
    with _notify_lock:
        _notify_queue.append(msg)
    logging.info(f"Notify: {source} pushed '{text[:30]}'")
    return jsonify(id=msg['id'], source=source, position=len(_notify_queue)), 201


@app.route('/notify', methods=['GET'])
def notify_list():
    now = time.time()
    with _notify_lock:
        active = [m for m in _notify_queue if m['expires_at'] > now]
        return jsonify(messages=active, count=len(active))


@app.route('/notify', methods=['DELETE'])
def notify_clear():
    source = request.args.get('source')
    with _notify_lock:
        if source:
            _notify_queue[:] = [m for m in _notify_queue if m.get('source') != source]
        else:
            _notify_queue.clear()
    return jsonify(ok=True)


@app.route('/installed_apps')
def installed_apps():
    # Include trigger capability info
    apps = get_plugin_app_list()
    for a in apps:
        app_id = a.get('plugin_id', '')
        manifest = _plugin_registry.get(app_id, {})
        a['has_trigger'] = app_id in _plugin_triggers
        a['trigger_conditions'] = manifest.get('trigger_conditions', [])
    return jsonify(
        apps=apps,
        settings_config=get_plugin_settings_config(),
    )


@app.route('/triggers', methods=['GET', 'POST'])
def triggers_route():
    if request.method == 'GET':
        trigs = settings.get('triggers', [])
        # Annotate with last_fired info
        now = time.time()
        result = []
        for t in trigs:
            entry = dict(t)
            last = _trigger_cooldowns.get(t.get('id', ''))
            entry['last_fired'] = last
            result.append(entry)
        return jsonify(triggers=result,
                       triggers_enabled=settings.get('triggers_enabled', True))
    data = request.json
    if 'triggers' in data:
        settings['triggers'] = data['triggers']
    if 'triggers_enabled' in data:
        settings['triggers_enabled'] = bool(data['triggers_enabled'])
    save_settings(settings)
    return jsonify(status="saved")


# ============================================================
#  UPDATE CHECK
# ============================================================

_update_cache = {'checked_at': 0, 'result': None}

@app.route('/version')
def version_route():
    return jsonify(version=read_version())

@app.route('/check_update')
def check_update():
    now = time.time()
    force = request.args.get('force') == '1'
    if not force and _update_cache['result'] and (now - _update_cache['checked_at']) < 3600:
        return jsonify(_update_cache['result'])
    try:
        repo_url = 'https://api.github.com/repos/csader/splitflap-os/releases/latest'
        resp = requests.get(repo_url, timeout=5, headers={'User-Agent': 'SplitflapOS'})
        resp.raise_for_status()
        data = resp.json()
        latest = data.get('tag_name', '').lstrip('v')
        current = read_version()
        has_update = latest and latest != current
        result = {
            'current': current,
            'latest': latest,
            'has_update': has_update,
            'release_name': data.get('name', ''),
            'release_url': data.get('html_url', ''),
        }
        _update_cache['result'] = result
        _update_cache['checked_at'] = now
        return jsonify(result)
    except Exception as e:
        logging.error(f"Update check error: {e}")
        return jsonify({'current': read_version(), 'latest': None, 'has_update': False, 'error': str(e)})

@app.route('/apply_update', methods=['POST'])
def apply_update():
    """Pull latest from main and restart the service."""
    import subprocess, hashlib
    repo_dir = os.path.join(os.path.dirname(__file__), '..')
    req_path = os.path.join(repo_dir, 'server', 'requirements.txt')

    def _hash_file(path):
        try:
            with open(path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return None

    req_hash_before = _hash_file(req_path)

    try:
        # Run git as the repo directory owner, not root
        stat = os.stat(repo_dir)
        repo_uid = stat.st_uid
        import pwd
        repo_user = pwd.getpwuid(repo_uid).pw_name

        # Ensure git trusts this directory (fixes safe.directory errors)
        subprocess.run(
            ['git', 'config', '--global', '--add', 'safe.directory', os.path.realpath(repo_dir)],
            timeout=5, capture_output=True
        )

        # Reset any local changes that would block the pull
        subprocess.run(
            ['sudo', '-u', repo_user, 'git', 'reset', '--hard', 'HEAD'],
            cwd=repo_dir, timeout=30, capture_output=True
        )

        result = subprocess.run(
            ['sudo', '-u', repo_user, 'git', 'pull', 'origin', 'main'],
            cwd=repo_dir, timeout=60, capture_output=True, text=True
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or 'git pull failed'
            logging.error(f"Update git pull failed: {error_msg}")
            return jsonify(status='error', message=error_msg), 500

        _update_cache['checked_at'] = 0  # invalidate cache

        req_hash_after = _hash_file(req_path)
        venv_exists = os.path.isfile(os.path.join(repo_dir, 'venv', 'bin', 'python'))
        needs_install = req_hash_before != req_hash_after or not venv_exists

        def _restart():
            time.sleep(1)
            try:
                if needs_install:
                    install_script = os.path.join(repo_dir, 'setup', 'install.sh')
                    subprocess.run(['bash', install_script], timeout=120)
                else:
                    subprocess.run(['systemctl', 'restart', 'splitflap.service'], timeout=10)
            except Exception:
                os.execv(os.sys.executable, [os.sys.executable] + os.sys.argv)
        threading.Thread(target=_restart, daemon=True).start()
        return jsonify(status='updating', needs_install=needs_install)
    except Exception as e:
        logging.error(f"Update error: {e}")
        return jsonify(status='error', message=str(e)), 500


if __name__ == '__main__':
    logging.info("Web UI running on 0.0.0.0:80")
    app.run(host='0.0.0.0', port=80)
