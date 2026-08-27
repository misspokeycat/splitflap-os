"""Grid geometry and text layout.

The display is a rows x cols grid of modules addressed as one flat string of
``get_module_count()`` characters. Everything that renders to it goes through
``format_lines``.
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
