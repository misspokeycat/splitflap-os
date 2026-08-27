"""Home Assistant integration over MQTT.

Publishes discovery payloads for the display's entities, mirrors state onto
their state topics, and handles the command topics. Every entity is
advertised under one device so Home Assistant groups them.

This is the display talking to Home Assistant. The other MQTT client in this
codebase (gateway_transport) is unrelated: it is a way of reaching the
hardware, not a way of being controlled.
"""

import json
import logging

from splitflap.grid import format_lines, get_cols, get_module_count, get_rows
from splitflap.plugins import _plugin_registry
from splitflap.settings import save_settings, settings
from splitflap.state import state
from splitflap.transport import send_raw

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None
    logging.warning("paho-mqtt not installed — MQTT integration disabled")


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
