"""Per-module calibration: offsets, character tuning, backup and restore."""

from datetime import datetime
import logging
import time
from flask import Blueprint, jsonify, request
from splitflap.settings import get_flap_chars, get_module_char_map, get_module_flap_count, save_settings, settings
from splitflap.grid import get_cols, get_module_count, get_rows
from splitflap.state import resize_grid, state
from splitflap.transport import send_raw, serial_lock, sync_hardware_data, sync_module_config
from splitflap.display import send_to_display
from splitflap.mqtt import mqtt_publish_discovery
from tuning import build_tuning_adjust_commands

bp = Blueprint("tuning", __name__)


@bp.route('/settings', methods=['GET', 'POST'])
def handle_settings():
    if request.method == 'POST':
        data   = request.json
        action = data.get('action')
        mod_id = str(data.get('id', '0'))

        if action == 'save_global':
            # Validate char_map before applying
            if 'char_map' in data and len(data['char_map']) != 64:
                return jsonify(error="Character map must be exactly 64 characters"), 400
            # Save any key except internal/protected ones
            protected = {'action', 'id', 'offsets', 'calibrations', 'tuned_chars', 'installed_apps', 'saved_playlists', 'saved_app_playlists'}
            for k, v in data.items():
                if k not in protected:
                    settings[k] = v
            if 'sim_rows' in data or 'sim_cols' in data:
                resize_grid()
                mqtt_publish_discovery()
            save_settings(settings)
            return jsonify(status="Saved")

        if action == 'adjust':
            delta      = int(data.get('delta', 0))
            new_offset = int(settings['offsets'].get(mod_id, 2832)) + delta
            settings['offsets'][mod_id] = new_offset
            save_settings(settings)
            send_raw(f"m{int(mod_id):02d}o{new_offset}")
            return jsonify(new_offset=new_offset)

        if action == 'home_one':
            send_raw(f"m{int(mod_id):02d}h")
            state.current_indices[int(mod_id)] = 0
            sl = list(state.current_display_string.ljust(get_module_count()))
            sl[int(mod_id)] = ' '
            state.current_display_string = "".join(sl)
            return jsonify(status="Homing")

        if action == 'calibrate':
            with serial_lock:
                if state.ser:
                    state.ser.reset_input_buffer()
                    state.ser.write(f"m{int(mod_id):02d}c\n".encode())
                    state.ser.flush()
                    start_wait = time.time()
                    buffer = ""
                    target = f"m{int(mod_id):02d}:"
                    while (time.time() - start_wait) < 45.0:
                        if state.ser.in_waiting > 0:
                            chunk = state.ser.read(state.ser.in_waiting).decode('utf-8', errors='ignore')
                            buffer += chunk
                            if target in buffer and '\n' in buffer[buffer.find(target):]:
                                valid_part = buffer[buffer.find(target):].split('\n')[0]
                                try:
                                    val = int(valid_part.split(target)[1])
                                    settings['calibrations'][mod_id] = val
                                    save_settings(settings)
                                    state.ser.write(f"m{int(mod_id):02d}t{val}\n".encode())
                                    state.ser.flush()
                                    return jsonify(status="success", steps=val)
                                except:
                                    pass
                        time.sleep(0.1)
                    return jsonify(status="error", message="Timeout"), 500
    return jsonify(settings)

@bp.route('/custom_tune', methods=['POST'])
def custom_tune():
    data   = request.json
    action = data.get('action')
    mod_id = int(data.get('id', 0))

    if action == 'goto':
        step = int(data.get('step', 0))
        idx  = int(data.get('index', 0))
        send_raw(f"m{mod_id:02d}g{step}")
        if 0 <= idx < len(get_flap_chars()):
            state.current_indices[mod_id] = idx
            sl = list(state.current_display_string.ljust(get_module_count()))
            sl[mod_id] = get_flap_chars()[idx]
            state.current_display_string = "".join(sl)

    elif action == 'save':
        idx  = int(data.get('index', 0))
        step = int(data.get('step', 0))
        send_raw(f"m{mod_id:02d}w{idx}:{step}")
        settings['tuned_chars'].setdefault(str(mod_id), {})[str(idx)] = step
        save_settings(settings)

    elif action == 'erase':
        idx = str(data.get('index', ''))
        if idx:
            send_raw(f"m{mod_id:02d}w{idx}:65535")
            settings['tuned_chars'].setdefault(str(mod_id), {}).pop(idx, None)
        else:
            send_raw(f"m{mod_id:02d}e")
            settings['tuned_chars'][str(mod_id)] = {}
        save_settings(settings)

    return jsonify(status="Success")

@bp.route('/sync_module', methods=['POST'])
def sync_module():
    mod_id  = int(request.json.get('id', 0))
    success = sync_hardware_data(mod_id)
    sync_module_config(mod_id)
    return jsonify(status="success" if success else "failed", settings=settings)

@bp.route('/sync_all', methods=['POST'])
def sync_all():
    for i in range(get_module_count()):
        sync_hardware_data(i)
        sync_module_config(i)
    return jsonify(status="success", settings=settings)

@bp.route('/assign_id', methods=['POST'])
def assign_id():
    send_raw(f"m**i{int(request.json.get('id', 0)):02d}")
    return jsonify(status="ID Assigned")

@bp.route('/toggle_autohome', methods=['POST'])
def toggle_autohome():
    enabled = request.json.get('enabled', True)
    settings['auto_home'] = enabled
    save_settings(settings)
    send_raw(f"m**a{1 if enabled else 0}")
    return jsonify(status="Auto-home updated")



@bp.route('/auto_tune', methods=['POST'])
def auto_tune_route():
    data   = request.json
    action = data.get('action')

    if action == 'home':
        send_raw("m**h")
        state.is_homed = True
        state.current_indices = [0] * get_module_count()
        state.current_display_string = " " * get_module_count()
        return jsonify(status="ok")

    elif action == 'goto_char':
        char_idx = int(data.get('char_index', 0))
        n = get_module_count()
        # Build per-module string: each module gets the char at char_idx in its own map
        chars = []
        for i in range(n):
            char_map = get_module_char_map(i)
            if 0 <= char_idx < len(char_map):
                chars.append(char_map[char_idx])
            else:
                chars.append(char_map[0])
        text = ''.join(chars)
        send_to_display(text, raw=True)
        flap_chars = get_flap_chars()
        return jsonify(status="ok", char=flap_chars[char_idx] if char_idx < len(flap_chars) else ' ', index=char_idx)

    elif action == 'adjust':
        modules   = data.get('modules', [])
        char_idx  = int(data.get('char_index', 0))
        delta     = int(data.get('delta', 0))
        adjusted  = []

        for mod_id in modules:
            mod_str = str(mod_id)
            cal     = int(settings['calibrations'].get(mod_str, 4096))
            flap_count = get_module_flap_count(mod_id)
            expected = (char_idx * cal) // flap_count

            # Current value: tuned if available, else expected
            tuned_val = settings['tuned_chars'].get(mod_str, {}).get(str(char_idx))
            base = int(tuned_val) if tuned_val is not None else expected
            new_val = base + delta

            # Clamp to valid range
            if new_val < 0:
                new_val = 0
            if new_val >= cal:
                new_val = cal - 1

            # Update settings
            if mod_str not in settings['tuned_chars']:
                settings['tuned_chars'][mod_str] = {}
            settings['tuned_chars'][mod_str][str(char_idx)] = new_val

            # Save the tuned position, then move there immediately so the user
            # can see the compensation. The preview uses firmware `g`, the
            # absolute motor-step GOTO command also used by Custom Tune's Test
            # Position flow; re-sending the same flap index could be ignored
            # because firmware still considers that character index active.
            save_command, preview_command = build_tuning_adjust_commands(
                mod_id,
                char_idx,
                new_val,
                cal,
                flap_count,
            )
            send_raw(save_command)
            send_raw(preview_command)

            adjusted.append({
                'module': mod_id,
                'old': base,
                'new': new_val,
                'previewed': True,
            })

        save_settings(settings)
        return jsonify(status="ok", adjusted=adjusted)

    elif action == 'get_positions':
        char_idx = int(data.get('char_index', 0))
        positions = {}
        for i in range(get_module_count()):
            mod_str  = str(i)
            cal      = int(settings['calibrations'].get(mod_str, 4096))
            flap_count = get_module_flap_count(i)
            expected = (char_idx * cal) // flap_count
            tuned    = settings['tuned_chars'].get(mod_str, {}).get(str(char_idx))
            positions[mod_str] = {
                'expected': expected,
                'tuned':    int(tuned) if tuned is not None else None,
                'active':   int(tuned) if tuned is not None else expected,
            }
        return jsonify(positions=positions)

    return jsonify(status="error", message="Unknown action"), 400


@bp.route('/tuning_status')
def tuning_status():
    char_idx = int(request.args.get('char_index', 0))
    if char_idx < 0 or char_idx >= len(get_flap_chars()):
        return jsonify(status="error", message="Invalid char_index"), 400
    positions = {}
    for i in range(get_module_count()):
        mod_str = str(i)
        cal = int(settings['calibrations'].get(mod_str, 4096))
        flap_count = get_module_flap_count(i)
        expected = (char_idx * cal) // flap_count
        tuned = settings['tuned_chars'].get(mod_str, {}).get(str(char_idx))
        positions[mod_str] = {
            'expected': expected,
            'tuned': int(tuned) if tuned is not None else None,
            'active': int(tuned) if tuned is not None else expected,
        }
    return jsonify(
        char_index=char_idx,
        char=get_flap_chars()[char_idx],
        flap_chars=get_flap_chars(),
        grid={'rows': get_rows(), 'cols': get_cols(), 'total': get_module_count()},
        positions=positions,
    )


# ── Backup / Restore ─────────────────────────────────────────

@bp.route('/backup_settings')
def backup_settings():
    return jsonify({
        'version':      1,
        'created':      datetime.now().isoformat(),
        'offsets':      settings['offsets'],
        'calibrations': settings['calibrations'],
        'tuned_chars':  settings['tuned_chars'],
    })

@bp.route('/restore_settings', methods=['POST'])
def restore_settings():
    data = request.json
    if not data:
        return jsonify(status="error", message="No data"), 400
    for key in ('offsets', 'calibrations', 'tuned_chars'):
        if key in data and not isinstance(data[key], dict):
            return jsonify(status="error",
                           message=f"'{key}' must be an object"), 400
    if 'offsets'      in data: settings['offsets'].update(data['offsets'])
    if 'calibrations' in data: settings['calibrations'].update(data['calibrations'])
    if 'tuned_chars'  in data: settings['tuned_chars'].update(data['tuned_chars'])
    save_settings(settings)
    hw = False
    if state.ser:
        hw = True
        for i in range(get_module_count()):
            s = str(i)
            send_raw(f"m{i:02d}o{int(settings['offsets'].get(s, 2832))}")
            send_raw(f"m{i:02d}t{int(settings['calibrations'].get(s, 4096))}")
            send_raw(f"m{i:02d}e")
            for idx, step in settings['tuned_chars'].get(s, {}).items():
                sv = int(step)
                if sv != 65535:
                    send_raw(f"m{i:02d}w{idx}:{sv}")
            logging.info(f"Restored m{i:02d}")
    return jsonify(status="success", hardware_updated=hw,
                   modules_updated=get_module_count())

# ── Saved Playlists ──────────────────────────────────────────
