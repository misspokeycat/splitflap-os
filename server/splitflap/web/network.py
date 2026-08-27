"""WiFi and broker configuration."""

import logging
import os
from flask import Blueprint, jsonify, request
from splitflap.settings import save_settings, settings
from splitflap.state import state
from splitflap.mqtt import mqtt_reconnect

bp = Blueprint("network", __name__)


@bp.route('/mqtt_reconnect', methods=['POST'])
def mqtt_reconnect_route():
    """Reconnect MQTT with current settings."""
    mqtt_reconnect()
    return jsonify(status="reconnecting")


@bp.route('/network_status')
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

@bp.route('/network_config', methods=['POST'])
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

@bp.route('/wifi_scan')
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

@bp.route('/wifi_connect', methods=['POST'])
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
