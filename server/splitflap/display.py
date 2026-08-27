"""Rendering text onto the modules.

Every path here normalises the text, maps it through the module character
maps and walks the modules in some order, updating state.current_indices as
it goes so the next send knows how far each flap has to travel.

The four senders differ only in that ordering and timing:
  send_to_display       one pass in a given module order (the transitions)
  send_to_display_sync  staggered starts so every module lands together
  send_to_display_slot  spin to random characters, then lock in left to right
  _send_with_effect     picks between them from per-page/per-app/global config
"""

import logging
import random
import time
import unicodedata

from splitflap.animations import get_animation_order
from splitflap.grid import get_module_count
from splitflap.mqtt import mqtt_publish_state
from splitflap.settings import (
    get_flap_chars,
    get_module_char_map,
    get_module_flap_count,
    settings,
)
from splitflap.state import state
from splitflap.transport import serial_lock


def _rotation_time(max_dist):
    """Estimate seconds for the slowest module to finish rotating max_dist positions."""
    n = get_module_count()
    min_flap_count = min(get_module_flap_count(i) for i in range(n)) if n else 64
    return max_dist * (4.0 / min_flap_count)

COLOR_MAP = {
    '\U0001f7e5': 'r', '\U0001f7e7': 'o', '\U0001f7e8': 'y', '\U0001f7e9': 'g',
    '\U0001f7e6': 'b', '\U0001f7ea': 'p', '\u2b1c': 'w', '\u2b1b': ' ',
}

def send_to_display_sync(text):
    """Send modules staggered so all arrive at their target character simultaneously."""
    if not text:
        return 0
    clean_text = unicodedata.normalize('NFC', text.upper())
    for emoji, char in COLOR_MAP.items():
        clean_text = clean_text.replace(emoji, char)
    if '"' not in get_flap_chars():
        clean_text = clean_text.replace('"', 'q')
    n = get_module_count()
    clean_text = clean_text.ljust(n)[:n]
    logging.info(f"DISPLAY (sync): {clean_text}")

    dists = []
    for i in range(n):
        char = clean_text[i]
        flap_count = get_module_flap_count(i)
        char_map = get_module_char_map(i)
        target_idx = char_map.find(char)
        if target_idx == -1:
            target_idx = 0
        # Treat -1 (pre-home unknown) as position 0 so sync stagger works on first run
        current = 0 if state.current_indices[i] == -1 else state.current_indices[i]
        dist = (target_idx - current) % flap_count
        dists.append((i, char, target_idx, dist, flap_count))

    max_dist = max(d[3] for d in dists) if dists else 0
    dists_sorted = sorted(dists, key=lambda x: -x[3])

    t0 = time.time()
    with serial_lock:
        for i, char, target_idx, dist, flap_count in dists_sorted:
            delay_before = (max_dist - dist) * (4.0 / flap_count)
            elapsed = time.time() - t0
            remaining = delay_before - elapsed
            if remaining > 0:
                time.sleep(remaining)
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{char}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
            state.current_indices[i] = target_idx

    state.current_display_string = clean_text
    state.is_homed = True
    mqtt_publish_state()
    return max_dist


def send_to_display_slot(text, effect_speed=80):
    """Slot machine: all modules spin to random chars, then lock in L→R."""
    if not text:
        return 0
    clean_text = unicodedata.normalize('NFC', text.upper())
    for emoji, char in COLOR_MAP.items():
        clean_text = clean_text.replace(emoji, char)
    if '"' not in get_flap_chars():
        clean_text = clean_text.replace('"', 'q')
    n = get_module_count()
    clean_text = clean_text.ljust(n)[:n]
    logging.info(f"DISPLAY (slot): {clean_text}")

    # Phase 1: all modules spin to random intermediate chars simultaneously
    # Ensure spin char differs from target so the lock-in is always visible
    spin_chars = []
    for i in range(n):
        char_map = get_module_char_map(i)
        target_idx = char_map.find(clean_text[i])
        if target_idx == -1: target_idx = 0
        candidates = [c for c in char_map[1:len(char_map)-4] if char_map.find(c) != target_idx]
        spin_chars.append(random.choice(candidates) if candidates else char_map[1])
    with serial_lock:
        for i in range(n):
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{spin_chars[i]}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
            char_map = get_module_char_map(i)
            idx = char_map.find(spin_chars[i])
            state.current_indices[i] = idx if idx != -1 else 0

    time.sleep(1.5)

    # Phase 2: lock in final chars L→R
    max_dist = 0
    with serial_lock:
        for i in range(n):
            char = clean_text[i]
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{char}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
                time.sleep(effect_speed / 1000.0)
            char_map = get_module_char_map(i)
            flap_count = get_module_flap_count(i)
            target_idx = char_map.find(char)
            if target_idx == -1:
                target_idx = 0
            dist = (target_idx - state.current_indices[i]) % flap_count
            if dist > max_dist:
                max_dist = dist
            state.current_indices[i] = target_idx

    state.current_display_string = clean_text
    state.is_homed = True
    mqtt_publish_state()
    return max_dist


def _send_with_effect(page_text, page_style, page_speed, is_anim, app_id=None):
    """Dispatch a page send using the active transition style (per-page > per-app > global)."""
    if is_anim:
        return send_to_display(page_text, get_animation_order(page_style or 'ltr'), raw=True, step_delay_ms=page_speed)
    # Priority: per-page > per-app > global
    style = page_style or \
            (settings.get(f'plugin_{app_id}_transition_style') if app_id else None) or \
            settings.get('transition_style', 'ltr')
    app_speed = settings.get(f'plugin_{app_id}_transition_speed') if app_id else None
    speed = page_speed if page_speed is not None else \
            (int(app_speed) if app_speed else int(settings.get('transition_speed', 15)))
    state.last_transition_style = style
    state.last_transition_speed = speed
    if style == 'sync':
        return send_to_display_sync(page_text)
    if style == 'slot':
        return send_to_display_slot(page_text, effect_speed=speed)
    return send_to_display(page_text, get_animation_order(style), step_delay_ms=speed)


def send_to_display(text, order=None, raw=False, step_delay_ms=15):
    if not text:
        return 0

    # For normal text: uppercase first (emojis are unaffected by upper()),
    # then replace emojis with color codes. Animation pages pass raw=True to
    # skip uppercasing so their color codes (r o y g b p w) stay lowercase.
    if not raw:
        clean_text = unicodedata.normalize('NFC', text.upper())
    else:
        clean_text = text
    for emoji, char in COLOR_MAP.items():
        clean_text = clean_text.replace(emoji, char)
    # Apply currency symbol alias: user's currency char → $ (the physical flap position)
    currency = settings.get('currency_symbol', '$').strip()
    if currency and currency != '$':
        clean_text = clean_text.replace(currency.upper(), '$')
    # The physical " flap is addressed as 'q' in the default firmware character map.
    # Only apply this substitution if " is not in the active char map.
    if '"' not in get_flap_chars():
        clean_text = clean_text.replace('"', 'q')
    n = get_module_count()
    clean_text = clean_text.ljust(n)[:n]
    logging.info(f"DISPLAY: {clean_text}")

    if order is None:
        order = list(range(n))

    # Update sim state immediately so the browser reflects the new text without waiting
    # for the serial loop to complete (fixes sim lag on hardware transitions)
    state.current_display_string = clean_text
    state.is_homed = True
    mqtt_publish_state()

    max_dist = 0
    with serial_lock:
        for i in order:
            if i >= len(clean_text):
                continue
            char = clean_text[i]
            if state.ser and not state.sim_mode:
                state.ser.write(f"m{i:02d}-{char}\n".encode('cp1252', errors='replace'))
                state.ser.flush()
                time.sleep(step_delay_ms / 1000.0)

            target_idx = get_module_char_map(i).find(char)
            if target_idx == -1:
                target_idx = 0
            flap_count = get_module_flap_count(i)
            dist = 128 if state.current_indices[i] == -1 else (target_idx - state.current_indices[i]) % flap_count
            if dist > max_dist:
                max_dist = dist
            state.current_indices[i] = target_idx

    return max_dist
