"""Version reporting and in-place updates."""

import logging
import os
import requests
import threading
import time
from flask import Blueprint, jsonify, request
from splitflap.settings import REPO_DIR, SERVER_DIR, read_version
from splitflap.updates import commits_behind, describe, pull, update_target

bp = Blueprint("system", __name__)


_update_cache = {'checked_at': 0, 'result': None}

@bp.route('/version')
def version_route():
    return jsonify(version=read_version())

def _release_notes(repo):
    """The repository's latest release, for showing a name and a link.

    Decoration only. Releases are cut from one branch, so they cannot say
    whether the branch this checkout is on has moved — that is git's answer,
    below.
    """
    resp = requests.get(
        f'https://api.github.com/repos/{repo}/releases/latest',
        timeout=5, headers={'User-Agent': 'SplitflapOS'})
    resp.raise_for_status()
    data = resp.json()
    # Coerce every field: the cache-hit path returns the stored result without
    # a try block, so a value that cannot be serialised would turn this route
    # into a 500 for the next hour.
    return {
        'latest': str(data.get('tag_name') or '').lstrip('v'),
        'release_name': str(data.get('name') or ''),
        'release_url': str(data.get('html_url') or ''),
    }


@bp.route('/check_update')
def check_update():
    """Report whether the branch this checkout tracks has moved ahead of it."""
    now = time.time()
    force = request.args.get('force') == '1'
    if not force and _update_cache['result'] and (now - _update_cache['checked_at']) < 3600:
        return jsonify(_update_cache['result'])

    current = read_version()
    target = update_target()
    result = {
        'current': current,
        'latest': None,
        'has_update': False,
        'release_name': '',
        'release_url': '',
        'branch': target['branch'] or '',
        'tracking': describe(target),
        'commits_behind': None,
    }
    if target['detached']:
        result['error'] = 'This checkout is not on a branch (detached HEAD).'
        return jsonify(result)

    behind = commits_behind(target)
    result['commits_behind'] = behind

    # No repo means the remote is not on GitHub, so there is no releases API
    # to ask. The git comparison above is the real answer either way.
    if target['repo']:
        try:
            result.update(_release_notes(target['repo']))
        except Exception as e:
            logging.warning(f"Release lookup failed: {e}")

    if behind is None:
        # git could not reach the remote. The release comparison is the only
        # signal left, and it is only meaningful on a released branch — but a
        # wrong "up to date" is worse than a version that turns out to be the
        # one already installed.
        result['has_update'] = bool(result['latest'] and result['latest'] != current)
        if not result['latest']:
            result['error'] = f"Could not reach {describe(target) or 'the remote'}."
            return jsonify(result)
    else:
        result['has_update'] = behind > 0

    response = jsonify(result)          # raises here rather than after caching
    _update_cache['result'] = result
    _update_cache['checked_at'] = now
    return response

@bp.route('/apply_update', methods=['POST'])
def apply_update():
    """Pull the branch this checkout tracks, and restart the service."""
    import subprocess, hashlib
    repo_dir = REPO_DIR
    req_path = os.path.join(SERVER_DIR, 'requirements.txt')

    def _hash_file(path):
        try:
            with open(path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return None

    req_hash_before = _hash_file(req_path)

    try:
        target = update_target()
        if target['detached']:
            return jsonify(
                status='error',
                message='This checkout is not on a branch, so there is nothing to '
                        'update from. Check out a branch first.'), 400

        ok, output = pull(target)
        if not ok:
            return jsonify(status='error', message=output), 500
        logging.info("Updated from %s", describe(target))

        _update_cache['checked_at'] = 0  # invalidate cache

        req_hash_after = _hash_file(req_path)
        venv_exists = os.path.isfile(os.path.join(repo_dir, 'venv', 'bin', 'python'))
        needs_install = req_hash_before != req_hash_after or not venv_exists

        def _restart():
            time.sleep(1)
            try:
                install_script = os.path.join(repo_dir, 'setup', 'install.sh')
                # bash exits non-zero on a missing script rather than raising,
                # which would leave the new code pulled and the old code
                # running with nothing to say so.
                if needs_install and os.path.isfile(install_script):
                    subprocess.run(['bash', install_script], timeout=180, check=True)
                else:
                    if needs_install:
                        logging.error("Install script missing at %s; restarting only",
                                      install_script)
                    subprocess.run(['systemctl', 'restart', 'splitflap.service'],
                                   timeout=10, check=True)
            except Exception:
                logging.exception("Restart after update failed; re-execing")
                os.execv(os.sys.executable, [os.sys.executable] + os.sys.argv)
        threading.Thread(target=_restart, daemon=True).start()
        return jsonify(status='updating', needs_install=needs_install,
                       tracking=describe(target))
    except Exception as e:
        logging.error(f"Update error: {e}")
        return jsonify(status='error', message=str(e)), 500
