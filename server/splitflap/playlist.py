"""The display loop.

One daemon thread owns the display. It walks whatever is currently selected —
a manual page list, a single app, or an app playlist — sending each page,
waiting out the rotation, then holding for the page's dwell time. Selection
changes anywhere else set state.stop_event, which breaks the loop out of its
waits so the change is picked up immediately rather than at the end of the
current page.
"""

import time

from splitflap.animations import get_animation_order
from splitflap.display import _rotation_time, _send_with_effect, send_to_display
from splitflap.notifications import _pop_notify, _show_notify_message
from splitflap.plugins import _plugin_registry, get_plugin_pages
from splitflap.settings import settings
from splitflap.state import state
from splitflap.tasks import start_background_task


def _run_app_playlist():
    """Execute one pass through the app playlist entries."""

    entries = state.active_app_playlist
    if not entries:
        state.active_app_playlist = None
        return

    while True:
        for entry in entries:
            if state.stop_event.is_set():
                state.stop_event.clear()
                return

            etype = entry.get('type', 'app')
            duration = float(entry.get('duration', 30))

            if etype == 'compose':
                # Send composed text to display
                text = entry.get('text', '')
                style = entry.get('style', 'ltr')
                speed = int(entry.get('speed', 15))
                order = get_animation_order(style)
                max_dist = send_to_display(text, order, step_delay_ms=speed)
                state.last_sent_page = text
                # Wait for rotation + duration
                rotation_time = _rotation_time(max_dist)
                for _ in range(int(rotation_time * 10)):
                    if state.stop_event.is_set():
                        state.stop_event.clear()
                        return
                    time.sleep(0.1)
                for _ in range(int(duration * 10)):
                    if state.stop_event.is_set():
                        state.stop_event.clear()
                        return
                    time.sleep(0.1)

            elif etype == 'app':
                app_key = entry.get('app', '')
                if not app_key:
                    continue
                # Temporarily set active_app so existing fetch logic works
                state.active_app = app_key
                deadline = time.time() + duration

                while time.time() < deadline:
                    if state.stop_event.is_set():
                        state.active_app = None
                        state.stop_event.clear()
                        return

                    display_pages = _get_pages_for_app(app_key)
                    if not display_pages:
                        time.sleep(1)
                        continue

                    is_anim = app_key.startswith('anim_') or \
                              (app_key in _plugin_registry and _plugin_registry[app_key].get('animation'))

                    # Determine per-page delay
                    reg = app_key[7:] if app_key.startswith('plugin_') else app_key
                    if is_anim:
                        eff_delay = max(0.1, float(settings.get('anim_speed', '0.4')))
                    elif reg in _plugin_registry:
                        saved = settings.get(f'plugin_{reg}_loop_delay', '')
                        default = float(_plugin_registry[reg].get('loop_delay', settings.get('global_loop_delay', 5)))
                        eff_delay = float(saved) if saved else default
                    else:
                        eff_delay = float(settings.get('global_loop_delay', 5))

                    active_order = None
                    if is_anim:
                        active_order = get_animation_order(settings.get('anim_style', 'ltr'))

                    for page in display_pages:
                        if state.stop_event.is_set() or time.time() >= deadline:
                            break
                        page_text = page.get('text', '') if isinstance(page, dict) else page
                        page_style = page.get('style') if isinstance(page, dict) else None
                        page_speed = int(page.get('speed', 15)) if isinstance(page, dict) else 15
                        page_delay = float(page.get('delay', eff_delay)) if isinstance(page, dict) else eff_delay

                        max_dist = 0
                        anim_style_ap = settings.get('anim_style','ltr') if is_anim else None
                        eff_style_ap = (anim_style_ap or page_style or
                                        (settings.get(f'plugin_{reg}_transition_style') if reg else None) or
                                        settings.get('transition_style', 'ltr'))
                        state.last_transition_style = eff_style_ap
                        state.last_transition_speed = page_speed if page_speed is not None else int(settings.get('transition_speed', 15))
                        if is_anim or page_text != state.last_sent_page:
                            max_dist = _send_with_effect(page_text, page_style if not is_anim else anim_style_ap, page_speed, is_anim, app_id=reg)
                            state.last_sent_page = page_text

                        rotation_time = _rotation_time(max_dist)
                        for _ in range(int(rotation_time * 10)):
                            if state.stop_event.is_set() or time.time() >= deadline: break
                            time.sleep(0.1)
                        for _ in range(int(page_delay * 10)):
                            if state.stop_event.is_set() or time.time() >= deadline: break
                            time.sleep(0.1)

                state.active_app = None

        # After all entries
        if not state.app_playlist_loop:
            state.active_app_playlist = None
            return
        # Otherwise loop continues


def _get_pages_for_app(app_key):
    """Fetch display pages for an app via the plugin system."""
    if app_key in _plugin_registry:
        return get_plugin_pages(app_key)
    # Try with plugin_ prefix stripped
    if app_key.startswith('plugin_'):
        return get_plugin_pages(app_key[7:])
    return []


def playlist_loop():

    while True:
        now = time.time()
        display_pages = []
        active_order  = None   # custom module send order for this cycle

        # ── App playlist mode ─────────────────────────────
        if state.active_app_playlist is not None:
            _run_app_playlist()
            continue

        # ── No active app — use compose playlist ──────────
        if state.active_app is None:
            display_pages = state.current_playlist

        # ── Plugin-based apps ─────────────────────────────
        elif state.active_app in _plugin_registry:
            manifest = _plugin_registry[state.active_app]
            display_pages = get_plugin_pages(state.active_app)
            if manifest.get('animation'):
                active_order = get_animation_order(settings.get('anim_style', 'ltr'))

        elif state.active_app.startswith('plugin_') and state.active_app[7:] in _plugin_registry:
            plugin_id = state.active_app[7:]
            manifest = _plugin_registry[plugin_id]
            display_pages = get_plugin_pages(plugin_id)
            if manifest.get('animation'):
                active_order = get_animation_order(settings.get('anim_style', 'ltr'))

        else:
            display_pages = state.current_playlist

        if not display_pages:
            time.sleep(1)
            continue

        # Resolve plugin_ prefix for registry lookups
        reg_key = state.active_app[7:] if (state.active_app and state.active_app.startswith('plugin_')) else state.active_app

        is_anim = (reg_key is not None and reg_key.startswith('anim_')) or \
                  (reg_key in _plugin_registry and _plugin_registry[reg_key].get('animation'))

        # Effective per-page delay
        if is_anim:
            eff_delay = max(0.1, float(settings.get('anim_speed', '0.4')))
            if reg_key in _plugin_registry:
                eff_delay = max(0.1, float(_plugin_registry[reg_key].get('loop_delay', eff_delay)))
        elif reg_key in _plugin_registry:
            saved = settings.get(f'plugin_{reg_key}_loop_delay', '')
            manifest = _plugin_registry[reg_key]
            default = float(manifest.get('loop_delay', settings.get('global_loop_delay', 5)))
            eff_delay = float(saved) if saved else default
        else:
            eff_delay = float(settings.get('global_loop_delay', state.loop_delay))

        for page in display_pages:
            if state.stop_event.is_set():
                break

            # Resolve per-page settings — rich playlist objects vs. plain strings
            if isinstance(page, dict):
                page_text  = page.get('text', '')
                page_delay = float(page.get('delay', eff_delay))
                page_style = page.get('style')
                page_speed = int(page.get('speed', 15))
            else:
                page_text  = page
                page_delay = eff_delay
                page_style = None
                page_speed = 15

            max_dist = 0
            # Animations always resend each frame; other apps skip unchanged pages
            anim_style = settings.get('anim_style', 'ltr') if is_anim else None
            eff_style = anim_style or page_style or \
                        (settings.get(f'plugin_{reg_key}_transition_style') if reg_key else None) or \
                        settings.get('transition_style', 'ltr')
            eff_speed = page_speed if page_speed is not None else int(settings.get('transition_speed', 15))
            state.last_transition_style = eff_style
            state.last_transition_speed = eff_speed
            if is_anim or page_text != state.last_sent_page:
                max_dist = _send_with_effect(page_text, anim_style or page_style, page_speed, is_anim, app_id=reg_key)
                state.last_sent_page = page_text

            # Skip rotation wait if manifest opts out (e.g. continuous random spin)
            skip_rot = reg_key in _plugin_registry and _plugin_registry[reg_key].get('skip_rotation_wait')
            if not skip_rot:
                rotation_time = _rotation_time(max_dist)
                for _ in range(int(rotation_time * 10)):
                    if state.stop_event.is_set(): break
                    time.sleep(0.1)

            for _ in range(int(page_delay * 10)):
                if state.stop_event.is_set(): break
                time.sleep(0.1)

            # Check for notification interrupts between pages
            if settings.get('notify_enabled', False):
                msg = _pop_notify()
                if msg:
                    _show_notify_message(msg)

        if state.stop_event.is_set():
            state.stop_event.clear()


start_background_task(playlist_loop)
