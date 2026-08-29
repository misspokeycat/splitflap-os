"""The app library: install, uninstall, settings and triggers."""

import json
import logging
import os
import requests
import shutil
import urllib.request
from flask import Blueprint, jsonify, request
from splitflap.settings import APPS_PATH, save_settings, settings
from splitflap.state import state
from splitflap.plugins import is_valid_app_id, _plugin_registry, _plugin_triggers, _registry_cache, get_plugin_app_list, get_plugin_settings_config, load_installed_plugins
from splitflap.mqtt import mqtt_publish_discovery
from splitflap.triggers import _trigger_cooldowns
from splitflap.sports import SPORTS_LEAGUES

bp = Blueprint("apps", __name__)


@bp.route('/app_library')
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


@bp.route('/app_library/install', methods=['POST'])
def app_library_install():
    app_id = request.json.get("id", "").strip()
    if not app_id:
        return jsonify(status="error", message="No app ID"), 400
    # app_id becomes a path below, and the server runs as root with no auth on
    # this API. Anything that is not a plain app id is refused here.
    if not is_valid_app_id(app_id):
        return jsonify(status="error", message="Invalid app ID"), 400
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


@bp.route('/app_library/uninstall', methods=['POST'])
def app_library_uninstall():
    app_id = request.json.get("id", "").strip()
    if not app_id:
        return jsonify(status="error", message="No app ID"), 400
    if not is_valid_app_id(app_id):
        return jsonify(status="error", message="Invalid app ID"), 400
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

@bp.route('/sports_leagues')
def sports_leagues_route():
    """Return league list with current follow settings."""
    leagues = []
    for key, info in SPORTS_LEAGUES.items():
        followed = settings.get(f'sports_{key}', '').strip()
        leagues.append({'key': key, 'name': info['name'], 'path': info['path'],
                        'followed': followed, 'follow_all': followed == '*'})
    return jsonify(leagues=leagues)

@bp.route('/sports_teams/<league_key>')
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

@bp.route('/sports_follow', methods=['POST'])
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



@bp.route('/installed_apps')
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


@bp.route('/triggers', methods=['GET', 'POST'])
def triggers_route():
    if request.method == 'GET':
        trigs = settings.get('triggers', [])
        # Annotate with last_fired info
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
