"""Serial port, gateway connection and Universal Firmware provisioning."""

import serial
from flask import Blueprint, jsonify, request
from splitflap.settings import save_settings, settings
from splitflap.grid import get_module_count
from splitflap.state import state
from splitflap.web.params import as_str
from splitflap.transport import open_gateway, open_serial, serial_lock, universal_firmware
from hardware.universal_firmware import UniversalFirmwareError

bp = Blueprint("hardware", __name__)


@bp.route('/serial_ports', methods=['GET'])
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


@bp.route('/serial_port', methods=['POST'])
def set_serial_port():
    """Change the active serial port and persist to settings.

    Applying a serial port also switches the active connection type back to
    'serial' (in case the gateway was previously selected).
    """
    data = request.json
    new_port = as_str(data.get('port'))
    if not new_port:
        return jsonify(status="error", message="No port given"), 400
    if not new_port:
        return jsonify(status="error", message="No port specified"), 400

    with serial_lock:
        if state.ser:
            try:
                state.ser.close()
            except Exception:
                pass
        state.ser, state.serial_port = open_serial(new_port)
        state.sim_mode = not state.ser
        universal_firmware.reset()

    settings['serial_port'] = new_port
    settings['connection_type'] = 'serial'
    save_settings(settings)
    return jsonify(status="success", port=state.serial_port, sim_mode=state.sim_mode)


@bp.route('/connection', methods=['GET', 'POST'])
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
            state.ser, state.serial_port = open_gateway({
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
        state.ser, state.serial_port = open_serial(port)
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


@bp.route('/universal/status')
def universal_status():
    """Return Universal Firmware modules and recent unprovisioned adverts."""
    manager = _ensure_universal_firmware_started()
    return jsonify(manager.status(get_module_count()))


@bp.route('/universal/scan', methods=['POST'])
def universal_scan():
    """Discover provisioned Universal Firmware modules in the configured grid."""
    manager = _ensure_universal_firmware_started()
    try:
        manager.scan(get_module_count() - 1)
        return jsonify(status="scanning")
    except UniversalFirmwareError as exc:
        return _universal_error_response(exc)


@bp.route('/universal/home', methods=['POST'])
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


@bp.route('/universal/provision', methods=['POST'])
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


@bp.route('/universal/deprovision', methods=['POST'])
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


@bp.route('/universal/diagnose', methods=['POST'])
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
