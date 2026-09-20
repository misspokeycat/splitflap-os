"""Pure helpers for fine-tuning hardware commands."""


def build_tuning_adjust_commands(
    module_id,
    char_index,
    step_position,
    calibration_steps,
    flap_count=64,
):
    """Return commands that save and immediately preview a tuned position.

    The preview command intentionally uses firmware ``g`` ("goto absolute
    motor step position"), not character-index navigation. Fine-tuning changes
    the physical step for the currently displayed character; asking firmware to
    go to the same character index again may be ignored because the logical flap
    index has not changed.
    """

    module_id = int(module_id)
    char_index = int(char_index)
    step_position = int(step_position)
    calibration_steps = int(calibration_steps)
    flap_count = int(flap_count)

    if module_id < 0 or module_id > 254:
        raise ValueError("Module ID must be between 0 and 254.")
    if char_index < 0 or char_index >= flap_count:
        raise ValueError(f"Character index must be between 0 and {flap_count - 1}.")
    if calibration_steps <= 0:
        raise ValueError("Calibration steps must be positive.")
    if step_position < 0:
        raise ValueError("Step position cannot be negative.")
    if step_position >= calibration_steps:
        raise ValueError(
            f"Step position must be less than calibration steps ({calibration_steps})."
        )

    return (
        f"m{module_id:02d}w{char_index}:{step_position}",
        f"m{module_id:02d}g{step_position}",
    )


# A flap's tuned position may compensate for real mechanical slop, but only by
# so much. These bound one gap between neighbouring flaps as a fraction of the
# nominal spacing; outside them the sequence is not compensating for anything,
# it has a flap in the wrong place.
STEP_GAP_MIN = 0.4
STEP_GAP_MAX = 1.6


def effective_steps(tuned, calibration, flap_count):
    """Where each flap actually stops: the tuned step, or the plain division.

    The same value /tuning_status reports as "active" and the same one the
    server sends when it holds the positions itself.
    """
    calibration = int(calibration)
    flap_count = int(flap_count)
    steps = []
    for index in range(flap_count):
        stored = (tuned or {}).get(str(index), (tuned or {}).get(index))
        if stored is None:
            steps.append((index * calibration) // flap_count)
        else:
            steps.append(int(stored))
    return steps


def step_sequence_problems(steps, calibration, flap_count=None,
                           gap_min=STEP_GAP_MIN, gap_max=STEP_GAP_MAX):
    """Where a module's positions stop going round the drum in order.

    A reel turns one way. Flap i+1 is one flap further round than flap i, so
    the steps have to climb and come back to the start after exactly one
    revolution. Nothing mechanical moves a flap past its neighbour, so a
    position that breaks the order is not a correction — it is a misread that
    got as far as being written.

    Returns a list of the gaps that are wrong, each naming the flap it starts
    at. An empty list means the sequence is walkable.
    """
    calibration = int(calibration)
    count = len(steps)
    if flap_count is None:
        flap_count = count
    if count < 2 or calibration <= 0:
        return []

    nominal = calibration / float(flap_count)
    low, high = nominal * gap_min, nominal * gap_max
    problems = []
    for index in range(count):
        # Modular, so the step from the last flap back to the first is
        # measured the same way as every other.
        gap = (int(steps[(index + 1) % count]) - int(steps[index])) % calibration
        if gap < low or gap > high:
            problems.append({
                "index": index,
                "next": (index + 1) % count,
                "gap": gap,
                "nominal": round(nominal, 1),
            })
    return problems
