"""The app plugin system.

An installed app is a directory under apps/ with a manifest.json. "channel"
apps ship static pages in data.json; "functional" apps ship an app.py
exposing fetch(settings, format_lines, get_rows, get_cols) and optionally
trigger(). The fetch contract is passed in rather than imported, which is why
apps stay independent of the server's internals.

SECURITY NOTE: functional plugins execute arbitrary Python code. Only install
apps from trusted sources. Plugins run with the same permissions as the Flask
app. There is no sandboxing.
"""

import importlib.util
import json
import logging
import os
import time

from splitflap.grid import format_lines, get_cols, get_rows
from splitflap.settings import APPS_PATH, settings
from splitflap.state import state


_plugin_registry = {}
_plugin_modules = {}
_plugin_triggers = {}
_plugin_data = {}
_plugin_caches = {}
_registry_cache = {'data': None, 'fetched_at': 0}


def load_installed_plugins():
    _plugin_registry.clear()
    _plugin_modules.clear()
    _plugin_data.clear()
    _plugin_triggers.clear()
    if not os.path.isdir(APPS_PATH):
        return
    enabled = settings.get('installed_apps', [])
    for app_id in os.listdir(APPS_PATH):
        if app_id not in enabled:
            continue
        app_dir = os.path.join(APPS_PATH, app_id)
        manifest_path = os.path.join(app_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding='utf-8') as f:
                manifest = json.load(f)
            manifest["id"] = app_id
            _plugin_registry[app_id] = manifest
            if manifest.get("type") == "channel":
                _load_channel_data(app_id, app_dir)
            elif manifest.get("type") == "functional":
                _load_functional_module(app_id, app_dir)
            logging.info(f"Plugin loaded: {app_id} ({manifest.get('type')})")
        except Exception as e:
            logging.error(f"Failed to load plugin {app_id}: {e}")


def _load_channel_data(app_id, app_dir):
    data_path = os.path.join(app_dir, "data.json")
    if not os.path.isfile(data_path):
        return
    try:
        with open(data_path, "r", encoding='utf-8') as f:
            data = json.load(f)
        pages = []
        for page in data.get("pages", []):
            if isinstance(page, str):
                pages.append(page)
            elif isinstance(page, dict) and "lines" in page:
                pages.append(format_lines(*page["lines"]))
        _plugin_data[app_id] = pages
    except Exception as e:
        logging.error(f"Plugin {app_id}: error loading data.json: {e}")


def _load_functional_module(app_id, app_dir):
    module_path = os.path.join(app_dir, "app.py")
    if not os.path.isfile(module_path):
        return
    try:
        spec = importlib.util.spec_from_file_location(f"plugin_{app_id}", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "fetch") and callable(mod.fetch):
            _plugin_modules[app_id] = mod
        else:
            logging.error(f"Plugin {app_id}: app.py has no fetch() function")
        if hasattr(mod, "trigger") and callable(mod.trigger):
            _plugin_triggers[app_id] = mod.trigger
            logging.info(f"Plugin {app_id}: trigger() loaded")
    except Exception as e:
        logging.error(f"Plugin {app_id}: error importing app.py: {e}")


def resolve_app_id(app_id):
    """Strip the legacy "plugin_" prefix some callers still send."""
    if app_id and app_id.startswith('plugin_'):
        return app_id[7:]
    return app_id


def loop_delay_for(app_id):
    """How long to hold each page of an app, in seconds.

    User setting first, then the app's manifest, then the global default.
    Animations ignore all of that and run at the animation speed, floored so
    a zero can never spin the display loop with no delay.

    One implementation because the web UI and the MQTT app selector had
    drifted: starting the same app from Home Assistant used a hardcoded 5s
    where the web UI used global_loop_delay.
    """
    global_default = float(settings.get('global_loop_delay', 5))
    manifest = _plugin_registry.get(resolve_app_id(app_id))
    if manifest is None:
        return global_default
    if manifest.get('animation'):
        return max(0.1, float(settings.get('anim_speed', '0.4')))
    saved = settings.get(f'plugin_{resolve_app_id(app_id)}_loop_delay', '')
    if saved:
        return float(saved)
    return float(manifest.get('loop_delay', global_default))


def get_plugin_pages(app_id):
    manifest = _plugin_registry.get(app_id)
    if not manifest:
        return [format_lines("PLUGIN ERROR", app_id.upper()[:get_cols()], "NOT FOUND")]
    app_type = manifest.get("type")
    refresh_interval = manifest.get("refresh_interval", 300)
    # Allow a plugin setting named 'polling_rate' to override the cache interval
    _poll = settings.get(f"plugin_{app_id}_polling_rate")
    if _poll:
        try:
            refresh_interval = max(10, int(float(_poll)))
        except (ValueError, TypeError):
            pass

    if app_type == "channel":
        pages = _plugin_data.get(app_id, [])
        return pages or [format_lines(manifest.get("name", app_id).upper()[:get_cols()], "NO DATA", "")]

    elif app_type == "functional":
        mod = _plugin_modules.get(app_id)
        if not mod:
            return [format_lines("PLUGIN ERROR", app_id.upper()[:get_cols()], "NOT LOADED")]
        now = time.time()
        cached = _plugin_caches.get(app_id)
        if cached and (now - cached["fetched_at"]) < refresh_interval:
            return cached["pages"]
        try:
            plugin_settings = dict(settings)  # full settings for built-in apps
            for s in manifest.get("settings", []):
                if s.get('global_key'):
                    # global_key settings use the key as-is, already in settings
                    pass
                else:
                    key = f"plugin_{app_id}_{s['key']}"
                    plugin_settings[s["key"]] = settings.get(key, s.get("default", ""))
            pages = mod.fetch(plugin_settings, format_lines, get_rows, get_cols)
            if not isinstance(pages, list):
                pages = [str(pages)]
            _plugin_caches[app_id] = {"pages": pages, "fetched_at": now}
            return pages
        except Exception as e:
            logging.error(f"Plugin {app_id} fetch error: {e}")
            cached_pages = _plugin_caches.get(app_id, {}).get("pages")
            if cached_pages:
                return cached_pages
            # Show OFFLINE for network errors, generic error otherwise
            err_str = str(e).lower()
            if not state.is_online or 'timeout' in err_str or 'connection' in err_str or 'network' in err_str:
                return [format_lines(manifest.get("name", app_id).upper()[:get_cols()], "OFFLINE", "")]
            return [format_lines("APP ERROR", app_id.upper()[:get_cols()], str(e)[:get_cols()])]

    return [format_lines("PLUGIN ERROR", "UNKNOWN TYPE", "")]


def get_plugin_app_list():
    entries = []
    for app_id, manifest in _plugin_registry.items():
        entry = {
            "key": f"plugin_{app_id}",
            "icon": manifest.get("icon", "🧩"),
            "name": manifest.get("name", app_id),
            "desc": manifest.get("description", "")[:30],
            "plugin": True,
            "plugin_id": app_id,
        }
        if "min_rows" in manifest:
            entry["min_rows"] = manifest["min_rows"]
        if "min_cols" in manifest:
            entry["min_cols"] = manifest["min_cols"]
        entries.append(entry)
    entries.sort(key=lambda a: a['name'].lower())
    return entries


_SETTING_PASSTHROUGH_KEYS = (
    "size",
    "ph",
    "min",
    "max",
    "step",
    "stepper",
    "searchUrl",
    "resultKey",
    "maxItems",
    "compute",
    "compute_config",
    "disabled_when",
    "variant",
    "title",
    "text",
    "items",
    "icon",
    "linkText",
    "linkHref",
)


def _resolve_manifest_setting_key(app_id, raw_key, *, global_key=False):
    if global_key:
        return raw_key
    return f"plugin_{app_id}_{raw_key}"


def _build_resolved_settings_lookup(app_id, settings):
    return {
        setting["key"]: _resolve_manifest_setting_key(
            app_id,
            setting["key"],
            global_key=setting.get("global_key", False),
        )
        for setting in settings
        if setting.get("key")
    }


def _normalize_inline_toggle(app_id, inline_toggle):
    inline = dict(inline_toggle)
    inline_key = inline.get("key")
    if inline_key:
        inline["key"] = _resolve_manifest_setting_key(
            app_id,
            inline_key,
            global_key=inline.get("global_key", False),
        )
    return inline


def _normalize_sync_values(sync_values, map_related_key):
    return {
        source_value: {
            map_related_key(target_key): target_value
            for target_key, target_value in target_map.items()
        }
        for source_value, target_map in sync_values.items()
    }


def _build_plugin_setting_field(app_id, setting, resolved_keys):
    raw_key = setting["key"]
    key = resolved_keys[raw_key]
    field_type = setting.get("type", "text")

    def map_related_key(raw_related_key):
        if raw_related_key in resolved_keys:
            return resolved_keys[raw_related_key]
        return _resolve_manifest_setting_key(app_id, raw_related_key)

    field = {
        "key": key,
        "label": setting.get("label", "" if field_type == "notice" else raw_key),
        "type": field_type,
        "ph": setting.get("default", ""),
    }

    if "options" in setting:
        field["opts"] = setting["options"]

    for pass_key in _SETTING_PASSTHROUGH_KEYS:
        if pass_key in setting:
            field[pass_key] = setting[pass_key]

    if "inline_toggle" in setting:
        field["inline_toggle"] = _normalize_inline_toggle(app_id, setting["inline_toggle"])

    if "sync_values" in setting:
        field["sync_values"] = _normalize_sync_values(setting["sync_values"], map_related_key)

    if "sync_parent" in setting:
        field["sync_parent"] = map_related_key(setting["sync_parent"])

    if "sync_parent_custom_value" in setting:
        field["sync_parent_custom_value"] = setting["sync_parent_custom_value"]

    if "visible_when" in setting:
        field["visible_when"] = {
            map_related_key(k): v
            for k, v in setting["visible_when"].items()
        }

    if "disabled_when" in setting:
        field["disabled_when"] = {
            map_related_key(k): v
            for k, v in setting["disabled_when"].items()
        }

    if "watches" in setting:
        field["watches"] = [map_related_key(k) for k in setting["watches"]]

    return field


def get_plugin_settings_config():
    configs = {}
    for app_id, manifest in _plugin_registry.items():
        manifest_settings = [s for s in manifest.get("settings", []) if s.get("key")]
        resolved_keys = _build_resolved_settings_lookup(app_id, manifest_settings)
        fields = [
            _build_plugin_setting_field(app_id, setting, resolved_keys)
            for setting in manifest_settings
        ]

        configs[f"plugin_{app_id}"] = {
            "title": f"{manifest.get('icon', '🧩')} {manifest.get('name', app_id)}",
            "fields": fields,
        }
    return configs


os.makedirs(APPS_PATH, exist_ok=True)
load_installed_plugins()
