"""Persistent settings: defaults, on-disk load/save, and accessors.

``settings`` is a single dict shared by every module. It is loaded once at
import and mutated in place from then on — never rebound — so importing it
by name is safe.
"""

import json
import logging
import os


CONFIG_PATH = os.environ.get(
    "SPLITFLAP_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json"),
)

APPS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "apps")
VERSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "VERSION")

DEFAULT_FLAP_CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$&()-+=;q:%\'.,/?*roygbpw"


def read_config_file():
    """Read settings.json best-effort, before load_settings() has run."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


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
        "zip_code":      "02118",
        "location_lat":  "",
        "location_lon":  "",
        "location_name": "",
        "timezone":      sys_tz,
        "weather_api_key": "",
        "mbta_stop":     "",
        "mbta_route":    "",
        "stocks_list":   "",
        "yt_channel_id": "",
        "yt_api_key":    "",
        "yt_video_id":   "",
        "auto_home":     True,
        "countdown_event":   "NEW YEAR",
        "countdown_target":  "2027-01-01T00:00:00",
        "world_clock_zones": "US/Eastern,US/Pacific,Europe/London",
        "crypto_list":   "bitcoin,ethereum,solana",
        "anim_style":    "ltr",
        "anim_speed":    "0.4",
        "anim_text":     "SPLIT  FLAP  DISPLAY",
        "currency_symbol": "$",
        "saved_playlists": {},
        "livestream_interval": "25",
        "livestream_comments": "",
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
        "app_library_url": "https://raw.githubusercontent.com/csader/splitflap-os/main/apps",
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
        "triggers_enabled": True,
        "triggers": [],
        "installed_apps": [
            "time", "date", "weather", "stocks", "sports", "countdown",
            "world_clock", "crypto", "iss", "metro", "youtube", "yt_comments",
            "dashboard", "demo", "livestream",
            "anim_rainbow", "anim_sweep", "anim_twinkle", "anim_checker", "anim_matrix",
            "anim_random_spin",
            "word-clock", "moon-phase", "star-wars-quotes",
        ],
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                defaults.update(data)
                if "tuned_chars" not in defaults:
                    defaults["tuned_chars"] = {str(i): {} for i in range(45)}
                return defaults
        except:
            pass
    return defaults

def save_settings(data):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

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
