"""The wire to the display.

One transport is open at a time: a local pyserial port, or a GatewayTransport
speaking to an ESP32 over MQTT. Both expose the same pyserial-compatible
surface, so nothing downstream knows which is active. With neither available
the server runs in simulation mode and renders to the web UI only.

serial_lock serialises access: the display loop, the tuning routes and the
firmware manager all write to the same port.
"""

import logging
import os
import threading
import time

import serial
import serial.tools.list_ports

from hardware.universal_firmware import UniversalFirmwareManager
from splitflap.module_registry import ModuleRegistry
from splitflap.settings import read_config_file, save_settings, settings
from splitflap.state import state

try:
    from gateway_transport import GatewayTransport, GatewayConnectionError
except ImportError:
    GatewayTransport = None

    class GatewayConnectionError(Exception):
        pass


SERIAL_PORT_DEFAULT = '/dev/ttyUSB0'
BAUD_RATE = 9600

serial_lock = threading.Lock()


def get_serial_port(data=None):
    """Resolve serial port: env var > settings.json > default.

    Pass an already-loaded settings dict as ``data`` to avoid re-reading
    settings.json (see open_connection).
    """
    env_port = os.environ.get("SPLITFLAP_SERIAL_PORT")
    if env_port:
        return env_port
    if data is None:
        data = read_config_file()
    if data.get("serial_port"):
        return data["serial_port"]
    return SERIAL_PORT_DEFAULT

def get_connection_type(data=None):
    """Resolve the active connection type: env var > settings.json > 'serial'.

    Returns either 'serial' (local USB/serial port) or 'gateway' (MQTT
    SplitFlap Gateway). Pass an already-loaded settings dict as ``data`` to
    avoid re-reading settings.json.
    """
    env_type = os.environ.get("SPLITFLAP_CONNECTION_TYPE")
    if env_type:
        return env_type.strip().lower()
    if data is None:
        data = read_config_file()
    ct = data.get("connection_type", "serial")
    return (ct or "serial").strip().lower()

def get_gateway_config(data=None):
    """Pull the gateway MQTT settings from env/settings.json.

    Pass an already-loaded settings dict as ``data`` to avoid re-reading
    settings.json.
    """
    if data is None:
        data = read_config_file()
    return {
        "broker":   os.environ.get("SPLITFLAP_GATEWAY_BROKER",   data.get("gateway_broker", "")),
        "port":     int(os.environ.get("SPLITFLAP_GATEWAY_PORT", data.get("gateway_port", 1883)) or 1883),
        "prefix":   os.environ.get("SPLITFLAP_GATEWAY_PREFIX",   data.get("gateway_prefix", "splitflap")),
        "user":     os.environ.get("SPLITFLAP_GATEWAY_USER",     data.get("gateway_user", "")),
        "password": os.environ.get("SPLITFLAP_GATEWAY_PASSWORD", data.get("gateway_password", "")),
    }


def open_serial(port=None):
    """Open a serial connection. Returns (Serial, port) or (None, port)."""
    port = port or get_serial_port()
    try:
        s = serial.Serial(port, BAUD_RATE, timeout=0.5)
        logging.info(f"Serial connected: {port}")
        return s, port
    except Exception as e:
        logging.error(f"Serial failed on {port}. Simulation Mode. Reason: {e}")
        return None, port

def open_gateway(cfg=None):
    """Open an MQTT gateway connection. Returns (GatewayTransport, label) or (None, label)."""
    cfg = cfg or get_gateway_config()
    label = f"gateway:{cfg.get('broker','')}:{cfg.get('port',1883)}"
    if GatewayTransport is None:
        logging.error("Gateway selected but paho-mqtt/gateway_transport unavailable. Simulation Mode.")
        return None, label
    if not cfg.get("broker"):
        logging.error("Gateway selected but no broker configured. Simulation Mode.")
        return None, label
    try:
        t = GatewayTransport(
            broker=cfg["broker"],
            port=cfg.get("port", 1883),
            prefix=cfg.get("prefix", "splitflap"),
            username=cfg.get("user", ""),
            password=cfg.get("password", ""),
        )
        logging.info(f"Gateway connected: {label} prefix={cfg.get('prefix','splitflap')}")
        return t, label
    except GatewayConnectionError as e:
        logging.error(f"Gateway failed ({label}). Simulation Mode. Reason: {e}")
        return None, label
    except Exception as e:
        logging.error(f"Gateway error ({label}). Simulation Mode. Reason: {e}")
        return None, label

def open_connection():
    """Open the active connection based on the configured connection type.

    Returns (transport_or_None, descriptor_string). The transport exposes a
    pyserial-compatible surface in both modes, so all downstream code is
    agnostic to which one is active.
    """
    # Read settings.json once and thread it through the resolvers below so
    # startup doesn't re-open the file for each setting it needs.
    data = read_config_file()
    if get_connection_type(data) == "gateway":
        return open_gateway(get_gateway_config(data))
    return open_serial(get_serial_port(data))


def send_raw(cmd):
    if not cmd.endswith('\n'):
        cmd += '\n'
    with serial_lock:
        if state.ser and not state.sim_mode:
            state.ser.write(cmd.encode())
            state.ser.flush()
            time.sleep(0.02)

# Unwritten EEPROM reads as 0xFFFF, and this codebase already uses that value
# as its "no tuning stored" sentinel — `w<idx>:65535` is the erase command. So
# a module reporting 65535 is almost always saying "never tuned", not
# "corrupt", and it must never be stored as though it were a real position.
ERASED = 65535


def parse_hardware_data(data):
    """Parse a ``d`` (dump) response into offset, calibration and tuned steps.

    Returns None if it is not a response we recognise. No validation here —
    that is sanitise_module_data's job, so the raw reading stays available for
    comparison against what we have stored.
    """
    parts = data.split(':')
    if len(parts) < 2:
        return None
    try:
        reading = {"offset": int(parts[0]), "calibration": int(parts[1]), "tuned": {}}
    except ValueError:
        return None
    if len(parts) >= 3 and parts[2]:
        for pair in parts[2].split(','):
            if '=' not in pair:
                continue
            index, value = pair.split('=', 1)
            try:
                reading["tuned"][index.strip()] = int(value)
            except ValueError:
                continue
    return reading


def sanitise_module_data(reading, stored_calibration=None):
    """Drop values a module cannot actually have meant.

    A step is a position within one revolution, so anything at or beyond the
    calibration is meaningless, and the erased sentinel means "not set". A
    garbage calibration is the dangerous one: it is written back to the module
    on restore, so a bad reading must not displace a good stored value.

    Returns (clean, rejected) where rejected explains what was dropped.
    """
    rejected = {}
    calibration = reading.get("calibration")
    if calibration is None or calibration <= 0 or calibration == ERASED:
        rejected["calibration"] = calibration
        calibration = stored_calibration
    if not calibration or calibration <= 0:
        return None, rejected          # nothing can be validated without it

    clean = {"calibration": calibration, "tuned": {}}

    offset = reading.get("offset")
    if offset is None or not 0 <= offset < calibration:
        rejected["offset"] = offset
    else:
        clean["offset"] = offset

    for index, step in reading.get("tuned", {}).items():
        if step == ERASED:
            continue               # "not tuned" is not a rejection
        if 0 <= step < calibration:
            clean["tuned"][index] = step
        else:
            rejected.setdefault("tuned", {})[index] = step
    return clean, rejected


def read_hardware_data(mod_id):
    """Ask a module for its stored settings. Returns a reading, or None."""
    if not state.ser:
        return None
    with serial_lock:
        state.ser.reset_input_buffer()
        state.ser.write(f"m{mod_id:02d}d\n".encode())
        state.ser.flush()
        start = time.time()
        buffer = ""
        target = f"m{mod_id:02d}d:"
        while time.time() - start < 5.0:
            if state.ser.in_waiting > 0:
                try:
                    buffer += state.ser.read(state.ser.in_waiting).decode('utf-8', errors='ignore')
                    if target in buffer and '\n' in buffer[buffer.find(target):]:
                        line = buffer[buffer.find(target):].split('\n')[0]
                        return parse_hardware_data(line.split('d:', 1)[1])
                except Exception as e:
                    logging.error(f"Parse error: {e}")
                    return None
            time.sleep(0.05)
    return None


def sync_hardware_data(mod_id):
    """Read a module's stored settings into ours, discarding nonsense."""
    reading = read_hardware_data(mod_id)
    if reading is None:
        return False
    key = str(mod_id)
    stored_calibration = settings['calibrations'].get(key)
    clean, rejected = sanitise_module_data(reading, stored_calibration)
    if clean is None:
        logging.error("Module %s reported an unusable calibration (%s); keeping ours",
                      mod_id, rejected.get("calibration"))
        return False
    if rejected:
        logging.warning("Module %s: ignoring implausible EEPROM values %s", mod_id, rejected)

    settings['calibrations'][key] = clean["calibration"]
    if "offset" in clean:
        settings['offsets'][key] = clean["offset"]
    settings['tuned_chars'][key] = clean["tuned"]
    save_settings(settings)
    return True


def restore_module_settings(mod_id):
    """Push our stored offset, calibration and tuning back onto one module.

    The module's own copy is in EEPROM, so this is what a module gets after
    losing it — and it is one module's worth of exactly what /restore_settings
    does to the whole display.
    """
    mod_id = int(mod_id)
    key = str(mod_id)
    send_raw(f"m{mod_id:02d}o{int(settings['offsets'].get(key, 2832))}")
    send_raw(f"m{mod_id:02d}t{int(settings['calibrations'].get(key, 4096))}")
    send_raw(f"m{mod_id:02d}e")
    for index, step in settings['tuned_chars'].get(key, {}).items():
        step = int(step)
        if step != ERASED:
            send_raw(f"m{mod_id:02d}w{index}:{step}")


def parse_module_config(data):
    """Pull what we can out of an ``A`` command response.

    The documented layout is

        ver:id:serial:offset:steps:autoHome:curIdx:tunedPairs:flapCount:charMap

    but the field count varies between firmware builds — which is why
    flapCount is *found* rather than indexed, by looking for a plausible value
    at either of the two positions it has been seen at. Everything else is
    then read at a fixed offset from it, so a shift in the leading fields
    moves them all together and the parse still lines up.

    Every field is validated and dropped if it does not make sense, because a
    wrong current index is worse than no current index: the display would
    compute rotations from a position the flap is not in.

    Returns a dict of whatever was recognised; always has flap_count and
    char_map when it returns non-empty.
    """
    parts = data.split(b':')

    def candidate(anchor):
        """What the response says if flapCount sits at `anchor`."""
        if anchor >= len(parts) - 1:
            return None
        try:
            flap_count = int(parts[anchor])
        except ValueError:
            return None
        if not 1 <= flap_count <= 64:
            return None
        char_map_bytes = b':'.join(parts[anchor + 1:])
        if not char_map_bytes:
            return None
        return anchor, flap_count, char_map_bytes.decode('cp1252', errors='replace')

    candidates = [c for c in (candidate(a) for a in (8, 9)) if c]
    if not candidates:
        return {}

    # A module's character map describes its flaps, so a reading where the two
    # agree is the real one. Without this check a tunedPairs value that merely
    # looks like a flap count wins the scan, and every field derived from that
    # anchor is then off by one — including the flap position, which the
    # display would rotate from.
    consistent = [c for c in candidates if len(c[2]) == c[1]]
    anchor, flap_count, char_map = (consistent or candidates)[0]
    config = {"flap_count": flap_count, "char_map": char_map}
    if not consistent:
        # Anchor unconfirmed: report what the old parser reported and trust
        # nothing positional beyond it.
        return config

    def field(offset, low, high):
        index = anchor - offset
        if index < 0:
            return None
        try:
            value = int(parts[index])
        except (ValueError, IndexError):
            return None
        return value if low <= value <= high else None

    auto_home = field(3, 0, 1)
    if auto_home is not None:
        config["auto_home"] = bool(auto_home)
    current_index = field(2, 0, flap_count - 1)
    if current_index is not None:
        config["current_index"] = current_index
    return config


def sync_module_config(mod_id):
    """Query module's A command for flap count and character map (Universal Firmware v31+)."""
    if not state.ser:
        return False
    with serial_lock:
        state.ser.reset_input_buffer()
        state.ser.write(f"m{mod_id:02d}A\n".encode())
        state.ser.flush()
        start = time.time()
        buffer = b""
        target = f"m{mod_id:02d}A:".encode('ascii')
        while time.time() - start < 5.0:
            if state.ser.in_waiting > 0:
                try:
                    chunk = state.ser.read(state.ser.in_waiting)
                    buffer += chunk
                    if target in buffer and b'\n' in buffer[buffer.find(target):]:
                        line = buffer[buffer.find(target):].split(b'\n')[0]
                        data = line.split(b'A:', 1)[1]
                        logging.debug("Module %d A response: %r", mod_id, data)
                        config = parse_module_config(data)
                        if config:
                            settings.setdefault("module_configs", {})[str(mod_id)] = {
                                "flap_count": config["flap_count"],
                                "char_map": config["char_map"],
                            }
                            save_settings(settings)
                            # The module reports where its flap actually is, so
                            # take its word for it rather than leaving the
                            # position unknown and rotating the long way round.
                            if "current_index" in config and mod_id < len(state.current_indices):
                                state.current_indices[mod_id] = config["current_index"]
                            if config.get("auto_home") is not None:
                                wanted = bool(settings.get('auto_home', True))
                                if config["auto_home"] != wanted:
                                    logging.warning(
                                        "Module %d reports auto-home %s but the setting is %s; "
                                        "re-applying", mod_id,
                                        "on" if config["auto_home"] else "off",
                                        "on" if wanted else "off")
                                    send_raw(f"m**a{1 if wanted else 0}")
                            return True
                        else:
                            logging.warning(f"Module {mod_id} A response: could not find flap_count in {data.count(b':') + 1} fields")
                except Exception as e:
                    logging.error(f"A command parse error: {e}")
            time.sleep(0.05)
    return False


state.ser, state.serial_port = open_connection()
state.sim_mode = not state.ser

module_registry = ModuleRegistry(restore=restore_module_settings)

universal_firmware = UniversalFirmwareManager(
    get_serial=lambda: state.ser,
    serial_lock=serial_lock,
    get_sim_mode=lambda: state.sim_mode,
    registry=module_registry,
)
