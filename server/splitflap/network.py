"""Connectivity probing.

The Pi may be on WiFi, on its own fallback hotspot, or on a network with no
route out. Apps that fetch remote data check state.is_online so they can show
OFFLINE rather than an error, and the result is mirrored to Home Assistant.
"""

import time

import requests
from splitflap.settings import settings
from splitflap.state import state
from splitflap.mqtt import MQTT_TOPIC_PREFIX
from splitflap.tasks import start_background_task, start_supervised_loop


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

start_supervised_loop(_periodic_network_check)
