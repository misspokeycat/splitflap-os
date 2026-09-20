"""Per-module calibration: offsets, character tuning, backup and restore."""

from datetime import datetime
import logging
import time
from flask import Blueprint, jsonify, request
from splitflap.settings import get_flap_chars, get_module_char_map, get_module_flap_count, get_position_source, save_settings, settings
from splitflap.grid import get_cols, get_module_count, get_rows
from splitflap.state import resize_grid, state
from splitflap.web.params import as_int as _as_int, module_id as _module_id
from splitflap.transport import (
    read_hardware_data,
    restore_module_settings,
    sanitise_module_data,
    send_raw,
    serial_lock,
    sync_hardware_data,
    sync_module_config,
)
from splitflap.display import send_to_display
from splitflap.mqtt import mqtt_publish_discovery
from tuning import (build_tuning_adjust_commands, effective_steps,
                    step_sequence_problems)

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
            if _module_id(data.get('id', 0)) is None:
                return jsonify(error="Unknown module"), 400
            delta = _as_int(data.get('delta'), 0)
            if delta is None:
                return jsonify(error="'delta' must be a number"), 400
            new_offset = int(settings['offsets'].get(mod_id, 2832)) + delta
            settings['offsets'][mod_id] = new_offset
            save_settings(settings)
            send_raw(f"m{int(mod_id):02d}o{new_offset}")
            return jsonify(new_offset=new_offset)

        if action == 'home_one':
            module = _module_id(data.get('id', 0))
            if module is None:
                return jsonify(error="Unknown module"), 400
            send_raw(f"m{module:02d}h")
            state.current_indices[module] = 0
            sl = list(state.current_display_string.ljust(get_module_count()))
            sl[module] = ' '
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
    mod_id = _module_id(data.get('id', 0))
    if mod_id is None:
        return jsonify(error="Unknown module"), 400

    if action == 'goto':
        step = _as_int(data.get('step'), 0)
        idx  = _as_int(data.get('index'), 0)
        if step is None or idx is None:
            return jsonify(error="'step' and 'index' must be numbers"), 400
        send_raw(f"m{mod_id:02d}g{step}")
        if 0 <= idx < len(get_flap_chars()):
            state.current_indices[mod_id] = idx
            sl = list(state.current_display_string.ljust(get_module_count()))
            sl[mod_id] = get_flap_chars()[idx]
            state.current_display_string = "".join(sl)

    elif action == 'save':
        idx  = _as_int(data.get('index'), 0)
        step = _as_int(data.get('step'), 0)
        if idx is None or step is None:
            return jsonify(error="'step' and 'index' must be numbers"), 400
        # A tuned step is a position within one revolution. auto_tune already
        # clamps to this; storing anything outside it would write a position
        # the module cannot reach into EEPROM.
        cal = int(settings['calibrations'].get(str(mod_id), 4096))
        if not 0 <= step < cal:
            return jsonify(error=f"Step must be between 0 and {cal - 1}"), 400
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
    mod_id = _module_id(request.json.get('id', 0))
    if mod_id is None:
        return jsonify(error="Unknown module"), 400
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
    # The id is formatted as %02d into a serial frame, so it has to be a
    # number the firmware can actually address.
    new_id = _as_int(request.json.get('id'), 0)
    if new_id is None or not 0 <= new_id <= 99:
        return jsonify(error="Module id must be between 0 and 99"), 400
    send_raw(f"m**i{new_id:02d}")
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

    # Only the modules the caller actually sent. Restoring a whole backup
    # names all of them and still writes all of them; restoring three does
    # not rewrite the EEPROM of the other forty-two, which is what it used
    # to do — at roughly thirty milliseconds a command on a 9600 baud bus.
    touched = set()
    for key in ('offsets', 'calibrations', 'tuned_chars'):
        if key in data:
            settings[key].update(data[key])
            for mod_id in data[key]:
                if _module_id(mod_id) is not None:
                    touched.add(_module_id(mod_id))
    save_settings(settings)

    hw = False
    if state.ser and touched:
        hw = True
        for i in sorted(touched):
            restore_module_settings(i)
            logging.info(f"Restored m{i:02d}")
    return jsonify(status="success", hardware_updated=hw,
                   modules_updated=len(touched))


@bp.route('/apply_tuning', methods=['POST'])
def apply_tuning():
    """Write only the tuned positions that changed, one command each.

    Unlike /restore_settings, which erases a module's tuning and writes all
    of it back to make the module match our settings exactly, the cost here
    is the number of corrections rather than how much tuning the display
    already carries.
    """
    data = request.json or {}
    tuned = data.get('tuned')
    if not isinstance(tuned, dict):
        return jsonify(error="'tuned' must be an object"), 400

    # Validate everything before writing anything: a half-applied correction
    # set is worse than a rejected one, because nothing says which half.
    writes = []
    for raw_id, pairs in tuned.items():
        mod_id = _module_id(raw_id)
        if mod_id is None:
            return jsonify(error=f"Unknown module {raw_id!r}"), 400
        if not isinstance(pairs, dict):
            return jsonify(error=f"Module {mod_id} must map index to step"), 400
        key = str(mod_id)
        cal = int(settings['calibrations'].get(key, 4096))
        flap_count = get_module_flap_count(mod_id)
        for raw_index, raw_step in pairs.items():
            index = _as_int(raw_index, None)
            step = _as_int(raw_step, None)
            if index is None or not 0 <= index < flap_count:
                return jsonify(
                    error=f"Module {mod_id}: index must be between 0 and {flap_count - 1}"), 400
            # A step is a position within one revolution; anything else is a
            # position the module cannot reach.
            if step is None or not 0 <= step < cal:
                return jsonify(
                    error=f"Module {mod_id} index {index}: step must be between 0 and {cal - 1}"), 400
            writes.append((mod_id, index, step))

    # A reel turns one way, so a module's positions have to climb round it in
    # order; nothing mechanical moves a flap past its neighbour. Only problems
    # this write introduces are refused, so a module whose stored sequence is
    # already broken stays fixable.
    by_module = {}
    for mod_id, index, step in writes:
        by_module.setdefault(mod_id, {})[str(index)] = step
    for mod_id, pairs in by_module.items():
        key = str(mod_id)
        cal = int(settings['calibrations'].get(key, 4096))
        flap_count = get_module_flap_count(mod_id)
        stored = settings['tuned_chars'].get(key, {})
        before = {tuple(sorted(p.items())) for p in
                  step_sequence_problems(effective_steps(stored, cal, flap_count), cal)}
        after = step_sequence_problems(
            effective_steps(dict(stored, **pairs), cal, flap_count), cal)
        introduced = [p for p in after if tuple(sorted(p.items())) not in before]
        if introduced:
            first = introduced[0]
            return jsonify(
                error=(f"Module {mod_id}: flap {first['index']} would sit "
                       f"{first['gap']} steps from flap {first['next']}, where the "
                       f"reel spaces them about {first['nominal']}. A flap cannot "
                       f"move past its neighbour, so this is a misread rather than "
                       f"a correction."),
                module=mod_id, problems=introduced), 409

    # With the server holding the positions there is nothing to put on a
    # module: the next page sends the step itself, and writing EEPROM as well
    # would be wear for no benefit on the one store here that loses writes.
    to_hardware = get_position_source() != 'server'
    for mod_id, index, step in writes:
        if to_hardware:
            send_raw(f"m{mod_id:02d}w{index}:{step}")
        settings['tuned_chars'].setdefault(str(mod_id), {})[str(index)] = step
    save_settings(settings)

    return jsonify(status="success", writes=len(writes),
                   modules=len({m for m, _, _ in writes}),
                   position_source=get_position_source(),
                   hardware_updated=to_hardware and bool(state.ser) and bool(writes))

# ── Saved Playlists ──────────────────────────────────────────


@bp.route('/module_audit', methods=['POST'])
def module_audit():
    """Compare what the modules have stored against what we have stored.

    Read-only: nothing is written to the modules or to settings.json. Each
    module is asked for its settings and the reading is reported three ways —
    values that cannot be right, values that disagree with ours, and the rest.

    EEPROM drifts. A write interrupted by a brownout (the motors draw hardest
    exactly when a write lands) leaves a half-written cell, and an unwritten
    one reads as 65535. This is how you find out which modules have a problem
    without changing anything.
    """
    data = request.json or {}
    ids = data.get('ids')
    if ids is None:
        ids = list(range(get_module_count()))
    if not isinstance(ids, list) or not all(_module_id(i) is not None for i in ids):
        return jsonify(error="'ids' must be a list of module ids"), 400
    if not state.ser:
        return jsonify(error="No hardware connected"), 409

    report = []
    for mod_id in (_module_id(i) for i in ids):
        key = str(mod_id)
        reading = read_hardware_data(mod_id)
        if reading is None:
            # Same shape as every other entry: a caller iterating the report
            # should not have to special-case the module that did not answer.
            report.append({"id": mod_id, "status": "no_response",
                           "rejected": {}, "diverged": {}})
            continue
        clean, rejected = sanitise_module_data(
            reading, settings['calibrations'].get(key))

        diverged = {}
        if clean is not None:
            ours_cal = int(settings['calibrations'].get(key, 4096))
            if clean["calibration"] != ours_cal:
                diverged["calibration"] = {"module": clean["calibration"], "ours": ours_cal}
            if "offset" in clean:
                ours_offset = int(settings['offsets'].get(key, 2832))
                if clean["offset"] != ours_offset:
                    diverged["offset"] = {"module": clean["offset"], "ours": ours_offset}
            ours_tuned = {str(k): int(v) for k, v in
                          settings['tuned_chars'].get(key, {}).items()}
            if clean["tuned"] != ours_tuned:
                diverged["tuned"] = {"module": clean["tuned"], "ours": ours_tuned}

        report.append({
            "id": mod_id,
            "status": "unusable" if clean is None else ("diverged" if diverged
                                                        else ("suspect" if rejected else "ok")),
            "rejected": rejected,
            "diverged": diverged,
        })

    counts = {}
    for entry in report:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return jsonify(modules=report, summary=counts)
