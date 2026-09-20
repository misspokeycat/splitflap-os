"""Persistent settings: defaults, on-disk load/save, and accessors.

``settings`` is a single dict shared by every module. It is loaded once at
import and mutated in place from then on — never rebound — so importing it
by name is safe.
"""

import json
import logging
import os
import tempfile


# Where everything lives, derived from this file rather than the working
# directory so the server runs from wherever it is checked out. One definition:
# these were four separate __file__ walks, and the one in the updater counted
# the wrong number of levels after the package layout changed.
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(SERVER_DIR)

CONFIG_PATH = os.environ.get(
    "SPLITFLAP_CONFIG", os.path.join(SERVER_DIR, "settings.json"))
# The copy the last save displaced. settings.json is every offset,
# calibration and tuned character for the whole display, and it is the one
# thing on this machine that exists nowhere else — not in git, not upstream.
BACKUP_PATH = CONFIG_PATH + ".bak"
APPS_PATH = os.path.join(REPO_DIR, "apps")
VERSION_FILE = os.path.join(REPO_DIR, "VERSION")

DEFAULT_FLAP_CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$&()-+=;q:%\'.,/?*roygbpw"


def _read_json(path):
    """Parse one settings file, or None if it is missing or unreadable."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logging.error("Settings file %s could not be read: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _read_stored_settings():
    """What is on disk, falling back to the copy the last save displaced.

    A truncated settings.json used to read as "no settings", which is not a
    harmless answer: the server would come up on defaults and the next save
    would write those defaults over the real ones. Losing the last change is
    recoverable. Losing the calibration of 45 modules is not.
    """
    stored = _read_json(CONFIG_PATH)
    if stored is not None:
        return stored
    stored = _read_json(BACKUP_PATH)
    if stored is not None:
        logging.warning(
            "%s was unreadable; using the previous copy from %s",
            CONFIG_PATH, BACKUP_PATH)
    return stored


def read_config_file():
    """Read settings.json best-effort, before load_settings() has run."""
    return _read_stored_settings() or {}


# Where the web server listens. The defaults are what this has always done —
# every interface on port 80 — but behind a reverse proxy you want the opposite:
# a high port so the service does not need to own 80, and usually 127.0.0.1 so
# the only way in is through the proxy holding the certificate.
DEFAULT_BIND_HOST = '0.0.0.0'
DEFAULT_BIND_PORT = 80


def _valid_port(value, source):
    """A port the socket can actually take, or the default.

    A bad value must not reach app.run(): the service would fail to start and
    the display would go down with it, on a machine whose only UI is this
    server. Refusing the value and saying so leaves the display reachable.
    """
    try:
        port = int(value)
    except (TypeError, ValueError):
        logging.error("%s is not a number (%r); using %d", source, value, DEFAULT_BIND_PORT)
        return DEFAULT_BIND_PORT
    if not 1 <= port <= 65535:
        logging.error("%s is outside 1-65535 (%r); using %d", source, value, DEFAULT_BIND_PORT)
        return DEFAULT_BIND_PORT
    return port


def get_bind_host(data=None):
    """Resolve the bind address: env var > settings.json > every interface.

    Pass an already-loaded settings dict as ``data`` to avoid re-reading
    settings.json.
    """
    env_host = os.environ.get("SPLITFLAP_HOST")
    if env_host and env_host.strip():
        return env_host.strip()
    if data is None:
        data = read_config_file()
    host = data.get("bind_host")
    return host.strip() if isinstance(host, str) and host.strip() else DEFAULT_BIND_HOST


def get_bind_port(data=None):
    """Resolve the bind port: env var > settings.json > 80.

    Pass an already-loaded settings dict as ``data`` to avoid re-reading
    settings.json.
    """
    env_port = os.environ.get("SPLITFLAP_PORT")
    if env_port and env_port.strip():
        return _valid_port(env_port.strip(), "SPLITFLAP_PORT")
    if data is None:
        data = read_config_file()
    port = data.get("bind_port")
    if port is None or port == "":
        return DEFAULT_BIND_PORT
    return _valid_port(port, "bind_port in settings.json")


def load_settings():
    # Detect system timezone
    sys_tz = 'US/Eastern'
    try:
        import subprocess
        result = subprocess.run(['cat', '/etc/timezone'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            sys_tz = result.stdout.strip()
    except Exception:
        try:
            link = os.readlink('/etc/localtime')
            sys_tz = link.split('zoneinfo/')[-1]
        except Exception:
            pass
    defaults = {
        "offsets":       {str(i): 2832 for i in range(45)},
        "calibrations":  {str(i): 4096 for i in range(45)},
        "tuned_chars":   {str(i): {} for i in range(45)},
        "zip_code":      "",
        "location_lat":  "",
        "location_lon":  "",
        "location_name": "",
        "timezone":      sys_tz,
        "weather_api_key": "",
        "stocks_list":   "",
        "auto_home":     True,
        "countdown_event":   "NEW YEAR",
        "countdown_target":  "2027-01-01T00:00:00",
        "world_clock_zones": "US/Eastern,US/Pacific,Europe/London",
        "anim_style":    "ltr",
        "anim_speed":    "0.4",
        "anim_text":     "SPLIT  FLAP  DISPLAY",
        "currency_symbol": "$",
        "saved_playlists": {},
        "sports_nfl":   "",
        "sports_nba":   "",
        "sports_mlb":   "",
        "sports_nhl":   "",
        "sports_ncaaf": "",
        "sports_ncaab": "",
        "sports_mls":   "",
        "sports_epl":   "",
        "sports_laliga":"",
        "sports_ucl":   "",
        "sports_wnba":  "",
        "sports_pga":   "",
        "sports_ufc":   "",
        "mqtt_enabled":  False,
        "mqtt_broker":   "homeassistant.local",
        "mqtt_port":     1883,
        "mqtt_user":     "",
        "mqtt_password": "",
        "sim_rows": 3,
        "sim_cols": 15,
        # Blank means "the repository this checkout came from" — resolved at
        # request time by updates.app_library_base(). A URL here overrides that.
        "app_library_url": "",
        "notify_enabled": False,
        "notify_display_seconds": 10,
        "notify_sources": {},
        "transition_style": "ltr",
        "transition_speed": 15,
        "schedules": [],
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "quiet_hours_days": ["sun","mon","tue","wed","thu","fri","sat"],
        "serial_port": "",
        "connection_type": "serial",
        "gateway_broker":   "",
        "gateway_port":     1883,
        "gateway_prefix":   "splitflap",
        "gateway_user":     "",
        "gateway_password": "",
        "char_map": DEFAULT_FLAP_CHARS,
        "module_configs": {},
        "module_registry": {},
        "auto_reprovision": True,
        "triggers_enabled": True,
        "triggers": [],
        "installed_apps": [
            "time", "date", "weather", "stocks", "sports", "countdown",
            "world_clock", "iss", "dashboard",
            "anim_rainbow", "anim_sweep", "anim_twinkle", "anim_checker", "anim_matrix",
            "anim_random_spin",
            "word-clock", "moon-phase",
        ],
    }
    stored = _read_stored_settings()
    if stored is not None:
        defaults.update(stored)
        if "tuned_chars" not in defaults:
            defaults["tuned_chars"] = {str(i): {} for i in range(45)}
    return defaults


def _fsync_directory(path):
    """Make the rename durable, not only the bytes it renamed into place.

    Without it a crash can lose the directory entry and bring back the
    previous file. That is still a whole file rather than half of one, so it
    is a lesser failure than the one below — but it is the difference between
    losing the last save and not. Directories cannot be opened this way on
    Windows, where the suite runs.
    """
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def save_settings(data):
    """Replace settings.json atomically, keeping the copy it displaced.

    This used to truncate the live file and write into it. A power cut partway
    through left it empty or half-written — and this display browns out: it is
    full of stepper motors that draw hardest exactly when a write lands, which
    is the same reason the modules lose their EEPROM.

    So the new settings go to a separate file, get forced to the disk rather
    than left in the page cache, and are swapped in with a rename, which is
    atomic. A crash at any point during this leaves either the previous file
    or the new one, never a partial one.
    """
    directory = os.path.dirname(CONFIG_PATH) or '.'
    handle, temp_path = tempfile.mkstemp(
        dir=directory, prefix='.settings-', suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(CONFIG_PATH):
            os.replace(CONFIG_PATH, BACKUP_PATH)
        os.replace(temp_path, CONFIG_PATH)
    except BaseException:
        # Leaving the half-written temp file behind would litter the directory
        # with one per failed save, and the live file is untouched either way.
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    _fsync_directory(directory)

settings = load_settings()


def get_flap_chars():
    """The global character map. Was a module global, reassigned on save."""
    return settings.get("char_map", DEFAULT_FLAP_CHARS)


def get_module_char_map(mod_id):
    cfg = settings.get("module_configs", {}).get(str(mod_id), {})
    return cfg.get("char_map", get_flap_chars())


def get_module_flap_count(mod_id):
    cfg = settings.get("module_configs", {}).get(str(mod_id), {})
    return cfg.get("flap_count", len(get_module_char_map(mod_id)))


def read_version():
    try:
        with open(VERSION_FILE, 'r') as f:
            return f.read().strip()
    except Exception:
        return 'unknown'
