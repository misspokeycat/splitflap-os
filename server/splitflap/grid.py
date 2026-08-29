"""Grid geometry and text layout.

The display is a rows x cols grid of modules addressed as one flat string of
``get_module_count()`` characters. Everything that renders to it goes through
``format_lines``, or ``layout_text`` for the "|"-separated form that callers
outside the server speak.
"""

from splitflap.settings import settings


def get_rows():
    return int(settings.get('sim_rows', 3))


def get_cols():
    return int(settings.get('sim_cols', 15))


def get_module_count():
    return get_rows() * get_cols()


def format_lines(*lines, cols=None):
    cols = cols or get_cols()
    rows = get_rows()
    padded = list(lines) + [''] * (rows - len(lines))
    return ''.join(l.center(cols)[:cols] for l in padded[:rows])


LINE_BREAK = '|'


def layout_text(text, center=True):
    """Lay a "|"-separated string out across the grid.

    This is the form the API and the MQTT text entity both take, because it is
    the one a person can type: "HELLO|WORLD" is two lines. Lines beyond the
    grid height are dropped, and each is centred or left-aligned to fill
    exactly one row.
    """
    lines = text.split(LINE_BREAK)[:get_rows()]
    if center:
        return format_lines(*lines)
    cols, rows = get_cols(), get_rows()
    padded = lines + [''] * (rows - len(lines))
    return ''.join(l.ljust(cols)[:cols] for l in padded[:rows])
