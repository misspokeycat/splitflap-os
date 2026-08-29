"""Parsing request fields.

Request bodies reach the display buffers and the serial port, so a field that
is the wrong type or out of range has to be turned away at the edge rather
than raising somewhere further in. Each of these returns None for "not usable"
so the caller can answer 400.

The failure that motivated them: `request.json.get("id", "")` returns None
when the key is present and null — the default only applies when the key is
absent — so the .strip() a line later raised, and int(None) did the same
elsewhere. A dict slips past `or ""` for the same reason: it is truthy.
"""

from splitflap.grid import get_module_count


def as_int(raw, default=0):
    """An int from a request field, or None if it is not one."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def as_str(raw, default=""):
    """A stripped string from a request field, or None if it is not one."""
    if raw is None:
        return default
    if not isinstance(raw, str):
        return None
    return raw.strip()


def module_id(raw):
    """An addressable module id, or None.

    Module ids index the display buffers and are formatted into serial
    frames, so one outside the grid is not a harmless no-op — it used to
    raise IndexError, after already putting a frame like "m999h" on the wire.
    """
    parsed = as_int(raw, None)
    if parsed is None or not 0 <= parsed < get_module_count():
        return None
    return parsed
