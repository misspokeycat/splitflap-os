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


def step_sequence_problems(steps, calibration):
    """Where a module's positions stop going round the drum in order.

    A reel turns one way, so the steps climb and pass zero exactly once per
    revolution: read round the cycle there is exactly one place where the
    next step is not greater than this one. That is the seam, and it can fall
    anywhere. A second such place is a flap put behind the one before it or
    ahead of the one after it, which nothing mechanical does.

    Order, not distance. How far apart two flaps sit says only that the
    spacing is uneven, and uneven spacing is also what a half-finished
    correction looks like — positions are corrected in ascending order, so a
    flap being written always sits against an uncorrected neighbour.

    Returns the descents when there is more than one, each naming the flap it
    starts at; which is the seam and which the fault cannot be told from the
    steps alone, so both are reported. An empty list means the sequence walks.
    """
    calibration = int(calibration)
    count = len(steps)
    if count < 2 or calibration <= 0:
        return []

    descents = []
    for index in range(count):
        following = (index + 1) % count
        step, next_step = int(steps[index]), int(steps[following])
        # Not-greater, so two flaps on the same step counts: they cannot both
        # be in the window at once either.
        if next_step <= step:
            descents.append({
                "index": index,
                "next": following,
                "step": step,
                "next_step": next_step,
            })
    return descents if len(descents) > 1 else []


def repair_step_sequence(tuned, calibration, flap_count, prefer=()):
    """Give up the tuned positions that cannot be where they claim to be.

    There is nothing to preserve in a flap that is out of order: dropping its
    tuned value falls the flap back to the plain division of the reel, which
    is where the module started and is always in order.

    Which flap in a broken sequence is the fault is not decidable from the
    steps alone, so it is decided by trial. An untuned flap holds the plain
    division and cannot be the odd one out, so the candidates are the tuned
    ones, and the fault is whichever settles the sequence when dropped. Where
    several would, `prefer` breaks the tie — a caller writing a flap is
    saying that one is the new arrival. Where none does, they all go.

    Returns the tuning that walks, and the flaps that had to be given up.
    """
    calibration = int(calibration)
    flap_count = int(flap_count)
    prefer = {int(i) for i in prefer}
    repaired = {str(k): int(v) for k, v in (tuned or {}).items()}
    reset = []

    def walks(positions):
        return not step_sequence_problems(
            effective_steps(positions, calibration, flap_count), calibration)

    while not walks(repaired):
        problems = step_sequence_problems(
            effective_steps(repaired, calibration, flap_count), calibration)
        involved = {p["index"] for p in problems} | {p["next"] for p in problems}
        candidates = sorted(i for i in involved if str(i) in repaired)
        if not candidates:
            break            # nothing tuned to give up; the reel itself disagrees
        alone = [i for i in candidates
                 if walks({k: v for k, v in repaired.items() if k != str(i)})]
        drop = [next((i for i in alone if i in prefer), alone[0])] if alone else candidates
        for index in drop:
            repaired.pop(str(index))
            reset.append(index)

    return repaired, sorted(reset)
