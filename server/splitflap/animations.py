"""Module send orders for transitions and animations.

Every entry maps the grid to a list of module indices in the order they
should be told to move. The shape of the effect is entirely in the ordering
— the display code just walks the list.
"""

import random

from splitflap.grid import get_cols, get_rows


def get_animation_order(style='ltr', rows=None, cols=None):
    """Return a list of the module indices in the requested send order."""
    rows = rows or get_rows()
    cols = cols or get_cols()
    total = rows * cols
    def m(r, c): return r * cols + c

    if style == 'rtl':
        return list(range(total - 1, -1, -1))

    elif style == 'center_out':
        order, seen = [], set()
        center = cols // 2
        for d in range(center + 1):
            for r in range(rows):
                cs = [center] if d == 0 else [center - d, center + d]
                for c in cs:
                    if 0 <= c < cols:
                        idx = m(r, c)
                        if idx not in seen:
                            seen.add(idx); order.append(idx)
        return order

    elif style == 'outside_in':
        return list(reversed(get_animation_order('center_out', rows, cols)))

    elif style == 'spiral':
        vis = [[False] * cols for _ in range(rows)]
        order = []
        top, bottom, left, right = 0, rows - 1, 0, cols - 1
        while top <= bottom and left <= right:
            for c in range(left, right + 1):
                if not vis[top][c]:
                    vis[top][c] = True; order.append(m(top, c))
            for r in range(top + 1, bottom + 1):
                if not vis[r][right]:
                    vis[r][right] = True; order.append(m(r, right))
            if top < bottom:
                for c in range(right - 1, left - 1, -1):
                    if not vis[bottom][c]:
                        vis[bottom][c] = True; order.append(m(bottom, c))
            if left < right:
                for r in range(bottom - 1, top, -1):
                    if not vis[r][left]:
                        vis[r][left] = True; order.append(m(r, left))
            top += 1; bottom -= 1; left += 1; right -= 1
        return order

    elif style == 'diagonal':
        order, seen = [], set()
        for d in range(rows + cols - 1):
            for r in range(rows):
                c = d - r
                if 0 <= c < cols:
                    idx = m(r, c)
                    if idx not in seen:
                        seen.add(idx); order.append(idx)
        return order

    elif style == 'anti_diagonal':
        order, seen = [], set()
        for d in range(rows + cols - 1):
            for r in range(rows):
                c = (cols - 1 - d) + r
                if 0 <= c < cols:
                    idx = m(r, c)
                    if idx not in seen:
                        seen.add(idx); order.append(idx)
        return order

    elif style == 'random':
        return random.sample(range(total), total)

    elif style == 'rain':
        return [m(r, c) for r in range(rows) for c in range(cols)]

    elif style == 'reverse_rain':
        return [m(r, c) for r in range(rows - 1, -1, -1) for c in range(cols)]

    elif style == 'columns':
        return [m(r, c) for c in range(cols) for r in range(rows)]

    elif style == 'columns_rtl':
        return [m(r, c) for c in range(cols - 1, -1, -1) for r in range(rows)]

    elif style == 'alternating':
        order = []
        for c in range(cols):
            for r in range(rows):
                ac = c if r % 2 == 0 else (cols - 1 - c)
                order.append(m(r, ac))
        return order

    return list(range(total))  # default ltr
