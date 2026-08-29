"""Version reporting and in-place updates."""

import logging
import os
import requests
import threading
import time
from flask import Blueprint, jsonify, request
from splitflap.settings import REPO_DIR, SERVER_DIR, read_version

bp = Blueprint("system", __name__)


_update_cache = {'checked_at': 0, 'result': None}

@bp.route('/version')
def version_route():
    return jsonify(version=read_version())

@bp.route('/check_update')
def check_update():
    now = time.time()
    force = request.args.get('force') == '1'
    if not force and _update_cache['result'] and (now - _update_cache['checked_at']) < 3600:
        return jsonify(_update_cache['result'])
    try:
        repo_url = 'https://api.github.com/repos/csader/splitflap-os/releases/latest'
        resp = requests.get(repo_url, timeout=5, headers={'User-Agent': 'SplitflapOS'})
        resp.raise_for_status()
        data = resp.json()
        current = read_version()
        # Coerce every field: the cache-hit path below returns the stored
        # result without a try block, so a value that cannot be serialised
        # would turn this route into a 500 for the next hour.
        latest = str(data.get('tag_name') or '').lstrip('v')
        result = {
            'current': current,
            'latest': latest,
            'has_update': bool(latest and latest != current),
            'release_name': str(data.get('name') or ''),
            'release_url': str(data.get('html_url') or ''),
        }
        response = jsonify(result)      # raises here rather than after caching
        _update_cache['result'] = result
        _update_cache['checked_at'] = now
        return response
    except Exception as e:
        logging.error(f"Update check error: {e}")
        return jsonify({'current': read_version(), 'latest': None, 'has_update': False, 'error': str(e)})

@bp.route('/apply_update', methods=['POST'])
def apply_update():
    """Pull latest from main and restart the service."""
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
        # Run git as the repo directory owner, not root
        stat = os.stat(repo_dir)
        repo_uid = stat.st_uid
        import pwd
        repo_user = pwd.getpwuid(repo_uid).pw_name

        # Ensure git trusts this directory (fixes safe.directory errors)
        subprocess.run(
            ['git', 'config', '--global', '--add', 'safe.directory', os.path.realpath(repo_dir)],
            timeout=5, capture_output=True
        )

        # Reset any local changes that would block the pull
        subprocess.run(
            ['sudo', '-u', repo_user, 'git', 'reset', '--hard', 'HEAD'],
            cwd=repo_dir, timeout=30, capture_output=True
        )

        result = subprocess.run(
            ['sudo', '-u', repo_user, 'git', 'pull', 'origin', 'main'],
            cwd=repo_dir, timeout=60, capture_output=True, text=True
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or 'git pull failed'
            logging.error(f"Update git pull failed: {error_msg}")
            return jsonify(status='error', message=error_msg), 500

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
        return jsonify(status='updating', needs_install=needs_install)
    except Exception as e:
        logging.error(f"Update error: {e}")
        return jsonify(status='error', message=str(e)), 500
