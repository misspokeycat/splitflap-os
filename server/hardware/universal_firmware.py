"""Universal Split-Flap Firmware discovery, provisioning, and diagnostics.

Splitflap OS has one shared RS-485 receive buffer. Any operation that expects a
reply must own the serial lock for its full write/read transaction; otherwise an
idle observer can consume bytes intended for that operation. This manager
follows the same rule as the existing calibration code:

* the passive reader only observes unsolicited traffic while the bus is idle;
* Universal Firmware commands that expect replies write and read under the
  shared serial lock, then feed those bytes through the same parser.

Modules forget who they are. The module ID lives in the same EEPROM as the
calibration, and when a cell goes the module stops answering to its address
and starts advertising for a new one — its place in the display goes blank.
The chip serial cannot be forgotten, so an injected registry (see
splitflap.module_registry) remembers which serial answers to which ID and this
manager hands it back. That runs from the passive reader thread and never from
the parse path: provisioning needs the serial lock for a full write/read
transaction, and the parser is usually being called by something already
holding it.
"""

from collections import deque
import logging
import re
import threading
import time


SERIAL_RE = re.compile(r"^[0-9A-F]{20}$")
ADVERTISEMENT_RE = re.compile(r"^mXadv:([0-9A-Fa-f]{20})$")
# Firmware examples exist with and without the colon after "mXack".
ACK_RE = re.compile(r"^mXack:?([0-9A-Fa-f]{20}):(\d{1,3})$")
VERSION_RE = re.compile(
    r"^m(\d+)v:([^:\s]+)(?::(\d+):([0-9A-Fa-f]{20}))?$"
)
SNAPSHOT_RE = re.compile(
    r"^m(\d+)Q:(\d+):(\d+):(\d+):(\d+):(-?\d+)$"
)
HALL_RE = re.compile(
    r"^m(\d+)T:(\d+):(\d+):(\d+)(?::(\d+))?$"
)
MECHANICAL_RE = re.compile(r"^m(\d+)M:(.*)$")


class UniversalFirmwareError(RuntimeError):
    """A user-facing Universal Firmware operation error."""


def parse_firmware_number(value):
    """Return the numeric part of a firmware version such as ``v29``."""
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def parse_universal_line(line):
    """Parse one RS-485 response line into a normalized event dictionary."""
    line = str(line or "").strip()
    if not line:
        return None

    match = ADVERTISEMENT_RE.match(line)
    if match:
        return {"type": "advertisement", "serial": match.group(1).upper()}

    match = ACK_RE.match(line)
    if match:
        module_id = int(match.group(2))
        if module_id <= 254:
            return {
                "type": "ack",
                "serial": match.group(1).upper(),
                "id": module_id,
            }
        return None

    match = VERSION_RE.match(line)
    if match:
        address = int(match.group(1))
        reported_id = int(match.group(3)) if match.group(3) is not None else address
        serial_number = match.group(4)
        return {
            "type": "version",
            "id": address,
            "reported_id": reported_id,
            "firmware": match.group(2),
            "serial": serial_number.upper() if serial_number else "",
            "universal": bool(serial_number),
        }

    match = SNAPSHOT_RE.match(line)
    if match:
        return {
            "type": "snapshot",
            "id": int(match.group(1)),
            "reset_cause": int(match.group(2)),
            "boot_count": int(match.group(3)),
            "vcc_mv": int(match.group(4)),
            "eeprom_ok": int(match.group(5)) == 1,
            "current_index": int(match.group(6)),
        }

    match = HALL_RE.match(line)
    if match:
        return {
            "type": "hall",
            "id": int(match.group(1)),
            "code": int(match.group(2)),
            "rising_edges": int(match.group(3)),
            "active_samples": int(match.group(4)),
            "falling_edges": int(match.group(5)) if match.group(5) is not None else None,
        }

    match = MECHANICAL_RE.match(line)
    if match:
        values = match.group(2).split(":")
        if len(values) < 6:
            return None
        try:
            event = {
                "type": "mechanical",
                "id": int(match.group(1)),
                "code": int(values[0]),
                "minimum": int(values[1]),
                "maximum": int(values[2]),
                "spread_tenths_percent": int(values[3]),
                "gate_active": int(values[4]),
                "gate_span": int(values[5]),
                "average_magnet_width": None,
                "revolutions": [],
            }
            if len(values) >= 7 and values[6] != "":
                event["average_magnet_width"] = int(values[6])
            if len(values) >= 8 and values[7]:
                event["revolutions"] = [
                    int(value) for value in values[7].split(",") if value
                ]
            return event
        except ValueError:
            return None

    return {"type": "other", "line": line}


class UniversalFirmwareManager:
    """Observe and operate Universal Firmware modules on a shared serial bus."""

    ADVERTISEMENT_TTL_SECONDS = 45
    EVENT_HISTORY_SIZE = 256
    # A module that cannot be written to will advertise forever. Back off
    # between tries and stop after a few, so a dead module costs a handful of
    # transactions rather than one every time it advertises.
    RECOVERY_RETRY_SECONDS = 20.0
    RECOVERY_MAX_ATTEMPTS = 3
    RECOVERY_SWEEP_SECONDS = 1.0
    RECOVERY_PROBE_SECONDS = 1.0
    RECOVERY_ACK_TIMEOUT = 2.0

    def __init__(self, get_serial, serial_lock, get_sim_mode=None, registry=None):
        self._get_serial = get_serial
        self._serial_lock = serial_lock
        self._get_sim_mode = get_sim_mode or (lambda: False)
        self._registry = registry
        self._state_lock = threading.RLock()
        self._condition = threading.Condition(self._state_lock)
        self._rx_lock = threading.Lock()
        self._modules = {}
        self._unprovisioned = {}
        self._events = deque(maxlen=self.EVENT_HISTORY_SIZE)
        self._event_sequence = 0
        self._rx_buffer = b""
        self._serial_identity = None
        self._recovery = {}
        self._scan_deadline = 0.0
        self._last_scan_at = 0.0
        self._last_cleanup_at = 0.0
        self._last_recovery_sweep = 0.0
        self._stop_event = threading.Event()
        self._reader_thread = None

    def start(self):
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="universal-firmware-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def ensure_started(self):
        """Start passive observation on demand."""
        self.start()

    def stop(self):
        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)

    def reset(self):
        """Clear bus-specific state, for example after changing serial ports."""
        with self._rx_lock:
            self._rx_buffer = b""
            with self._condition:
                self._modules.clear()
                self._unprovisioned.clear()
                self._events.clear()
                self._event_sequence = 0
                self._recovery.clear()
                self._serial_identity = None
                self._scan_deadline = 0.0
                self._last_scan_at = 0.0
                self._last_cleanup_at = 0.0
                self._last_recovery_sweep = 0.0
                self._condition.notify_all()

    def _reader_loop(self):
        while not self._stop_event.is_set():
            serial_port = self._get_serial()
            if (
                serial_port is None
                or self._get_sim_mode()
                or getattr(serial_port, "is_open", True) is False
            ):
                time.sleep(0.1)
                continue

            identity = id(serial_port)
            if identity != self._serial_identity:
                self._serial_identity = identity
                with self._rx_lock:
                    self._rx_buffer = b""

            acquired = False
            try:
                acquired = self._serial_lock.acquire(timeout=0.01)
                if not acquired:
                    time.sleep(0.01)
                    continue
                waiting = int(getattr(serial_port, "in_waiting", 0) or 0)
                chunk = serial_port.read(waiting) if waiting > 0 else b""
            except Exception as exc:
                logging.debug("Universal Firmware serial observer: %s", exc)
                chunk = b""
            finally:
                if acquired:
                    self._serial_lock.release()

            if chunk:
                self.feed_bytes(chunk)
            else:
                self._cleanup_if_due()
                self._recover_if_due()
                time.sleep(0.01)

    def feed_bytes(self, chunk):
        """Feed raw serial bytes into the line parser (also useful in tests)."""
        if not chunk:
            return
        with self._rx_lock:
            self._rx_buffer += bytes(chunk)
            if len(self._rx_buffer) > 8192 and b"\n" not in self._rx_buffer:
                self._rx_buffer = b""
                return
            while b"\n" in self._rx_buffer:
                raw_line, self._rx_buffer = self._rx_buffer.split(b"\n", 1)
                line = raw_line.decode("ascii", errors="ignore").strip()
                if line:
                    self.handle_line(line)

    @staticmethod
    def _consume_lines(buffer, chunk):
        """Return ``(remaining_buffer, decoded_lines)`` for a serial chunk."""
        buffer += bytes(chunk or b"")
        if len(buffer) > 8192 and b"\n" not in buffer:
            return b"", []

        lines = []
        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            line = raw_line.decode("ascii", errors="ignore").strip()
            if line:
                lines.append(line)
        return buffer, lines

    def _prune_expired_unprovisioned_locked(self, now):
        expired = [
            serial_number
            for serial_number, item in self._unprovisioned.items()
            if now - item["last_seen"] > self.ADVERTISEMENT_TTL_SECONDS
        ]
        for serial_number in expired:
            self._unprovisioned.pop(serial_number, None)

    def _cleanup_if_due(self):
        now = time.time()
        with self._condition:
            if now - self._last_cleanup_at < 5.0:
                return
            self._last_cleanup_at = now
            self._prune_expired_unprovisioned_locked(now)

    def handle_line(self, line):
        """Record one decoded bus line and update module discovery state."""
        event = parse_universal_line(line)
        if event is None:
            return None

        now = time.time()
        pairing = None
        with self._condition:
            self._prune_expired_unprovisioned_locked(now)
            event = dict(event)
            self._event_sequence += 1
            event["sequence"] = self._event_sequence
            event["received_at"] = now
            event["line"] = str(line).strip()
            self._events.append(event)

            if event["type"] == "advertisement":
                serial_number = event["serial"]
                entry = self._unprovisioned.get(serial_number, {
                    "serial": serial_number,
                    "first_seen": now,
                })
                entry["last_seen"] = now
                self._unprovisioned[serial_number] = entry

            elif event["type"] == "ack":
                serial_number = event["serial"]
                module_id = event["id"]
                self._unprovisioned.pop(serial_number, None)
                module = self._modules.get(module_id, {})
                module.update({
                    "id": module_id,
                    "serial": serial_number,
                    "provisioned": True,
                    "acknowledged": True,
                    "last_seen": now,
                })
                self._modules[module_id] = module
                pairing = (serial_number, module_id, module.get("firmware"))

            elif event["type"] == "version" and event["universal"]:
                module_id = event["reported_id"]
                module = self._modules.get(module_id, {})
                module.update({
                    "id": module_id,
                    "serial": event["serial"],
                    "firmware": event["firmware"],
                    "firmware_number": parse_firmware_number(event["firmware"]),
                    "provisioned": module_id != 255,
                    "acknowledged": module.get("acknowledged", False),
                    "last_seen": now,
                })
                if module_id == 255:
                    serial_number = event["serial"]
                    entry = self._unprovisioned.get(serial_number, {
                        "serial": serial_number,
                        "first_seen": now,
                    })
                    entry["last_seen"] = now
                    self._unprovisioned[serial_number] = entry
                else:
                    self._unprovisioned.pop(event["serial"], None)
                    self._modules[module_id] = module
                    pairing = (event["serial"], module_id, event["firmware"])

            elif event["type"] in ("snapshot", "hall", "mechanical"):
                module = self._modules.get(event["id"])
                if module is not None:
                    module["last_seen"] = now
                    diagnostics = module.setdefault("diagnostics", {})
                    diagnostics[event["type"]] = {
                        key: value for key, value in event.items()
                        if key not in ("line", "sequence")
                    }

            self._condition.notify_all()

        if pairing:
            self._registry_call("remember", *pairing)
        return event

    def _connected(self):
        serial_port = self._get_serial()
        return bool(
            serial_port is not None
            and getattr(serial_port, "is_open", True) is not False
        )

    def _require_live_serial(self):
        if self._get_sim_mode():
            raise UniversalFirmwareError(
                "Switch the display to LIVE mode before using provisioning."
            )
        serial_port = self._get_serial()
        if serial_port is None or getattr(serial_port, "is_open", True) is False:
            raise UniversalFirmwareError("No serial hardware is connected.")
        return serial_port

    @staticmethod
    def _frame(command):
        return command if command.endswith("\n") else command + "\n"

    @staticmethod
    def _read_waiting(serial_port):
        waiting = int(getattr(serial_port, "in_waiting", 0) or 0)
        return serial_port.read(waiting) if waiting > 0 else b""

    def _write_unlocked(self, serial_port, command):
        frame = self._frame(command)
        serial_port.write(frame.encode("ascii"))
        serial_port.flush()

    def _write(self, command):
        self._require_live_serial()
        frame = command if command.endswith("\n") else command + "\n"
        try:
            with self._serial_lock:
                serial_port = self._get_serial()
                if serial_port is None:
                    raise UniversalFirmwareError("The serial connection was closed.")
                serial_port.write(frame.encode("ascii"))
                serial_port.flush()
        except UniversalFirmwareError:
            raise
        except Exception as exc:
            raise UniversalFirmwareError("Serial write failed: {}".format(exc))

    def _transaction(self, command, timeout, predicate=None, drain_before=True):
        """Write a command and collect its responses while owning the bus.

        ``predicate`` is optional. When provided, the transaction returns the
        first parsed event that matches it. Without a predicate, the transaction
        reads until ``timeout`` expires and returns ``None`` after feeding all
        complete response lines through ``handle_line``.
        """
        self._require_live_serial()
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        local_buffer = b""

        try:
            with self._serial_lock:
                serial_port = self._require_live_serial()
                if drain_before and hasattr(serial_port, "reset_input_buffer"):
                    try:
                        serial_port.reset_input_buffer()
                    except Exception:
                        pass

                self._write_unlocked(serial_port, command)

                while True:
                    if self._stop_event.is_set():
                        return None
                    chunk = self._read_waiting(serial_port)
                    if chunk:
                        matched_event = None
                        local_buffer, lines = self._consume_lines(local_buffer, chunk)
                        for line in lines:
                            event = self.handle_line(line)
                            if (
                                matched_event is None
                                and event is not None
                                and predicate
                                and predicate(event)
                            ):
                                matched_event = dict(event)
                        if matched_event is not None:
                            return matched_event

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    time.sleep(min(0.01, remaining))
        except UniversalFirmwareError:
            raise
        except Exception as exc:
            raise UniversalFirmwareError("Serial transaction failed: {}".format(exc))

    def status(self, module_limit=45):
        now = time.time()
        with self._condition:
            modules = []
            for module_id in sorted(self._modules):
                module = dict(self._modules[module_id])
                module["online"] = now - module.get("last_seen", 0) <= 60
                modules.append(module)

            unprovisioned = []
            for serial_number in sorted(self._unprovisioned):
                item = dict(self._unprovisioned[serial_number])
                if now - item["last_seen"] > self.ADVERTISEMENT_TTL_SECONDS:
                    continue
                item["age_seconds"] = max(0, int(now - item["last_seen"]))
                record = self._recovery.get(serial_number)
                if record:
                    item["recovery"] = dict(record)
                unprovisioned.append(item)

        # Registry reads happen with the state lock released, for the same
        # reason the writes do.
        for module in modules:
            recoveries = self._registry_call("recoveries", module.get("serial"))
            if recoveries:
                module["recoveries"] = recoveries
        for item in unprovisioned:
            # A module we recognise is one the server is about to reclaim, and
            # the UI says so rather than offering it as a blank slate.
            item["known_id"] = self._registry_call("known_id", item["serial"])
        auto_reprovision = self._recovery_enabled()

        with self._condition:
            used_ids = {module["id"] for module in modules}
            used_ids |= {item["known_id"] for item in unprovisioned
                         if item.get("known_id") is not None}
            preferred_limit = max(1, min(int(module_limit or 45), 255))
            suggested_id = next(
                (module_id for module_id in range(preferred_limit)
                 if module_id not in used_ids),
                next((module_id for module_id in range(255)
                      if module_id not in used_ids), None),
            )
            return {
                "connected": self._connected(),
                "live": not self._get_sim_mode(),
                "auto_reprovision": auto_reprovision,
                "has_universal": bool(modules),
                "modules": modules,
                "unprovisioned": unprovisioned,
                "suggested_id": suggested_id,
                "scan_in_progress": time.monotonic() < self._scan_deadline,
                "last_scan_at": self._last_scan_at,
            }

    def scan(self, maximum_id):
        maximum_id = max(0, min(int(maximum_id), 254))
        self._require_live_serial()
        timeout = 0.75 + ((maximum_id + 1) * 0.1)
        with self._condition:
            self._last_scan_at = time.time()
            self._scan_deadline = time.monotonic() + timeout

        thread = threading.Thread(
            target=self._scan_worker,
            args=(maximum_id, timeout),
            name="universal-firmware-scan",
            daemon=True,
        )
        thread.start()

    def _scan_worker(self, maximum_id, timeout):
        try:
            self._transaction("m*v0-{}".format(maximum_id), timeout=timeout)
        except UniversalFirmwareError as exc:
            logging.warning("Universal Firmware scan failed: %s", exc)
        finally:
            with self._condition:
                self._scan_deadline = 0.0
                self._condition.notify_all()

    @staticmethod
    def validate_serial(serial_number):
        serial_number = str(serial_number or "").strip().upper()
        if not SERIAL_RE.match(serial_number):
            raise UniversalFirmwareError(
                "Serial number must be exactly 20 hexadecimal characters."
            )
        return serial_number

    @staticmethod
    def validate_id(module_id):
        try:
            module_id = int(module_id)
        except (TypeError, ValueError):
            raise UniversalFirmwareError("Module ID must be a number from 0 to 254.")
        if module_id < 0 or module_id > 254:
            raise UniversalFirmwareError("Module ID must be between 0 and 254.")
        return module_id

    def home_by_serial(self, serial_number):
        serial_number = self.validate_serial(serial_number)
        self._write("mXH{}".format(serial_number))

    def home_module(self, module_id):
        module_id = self.validate_id(module_id)
        self._write("m{}h".format(module_id))

    def provision(self, serial_number, module_id, timeout=2.0):
        serial_number = self.validate_serial(serial_number)
        module_id = self.validate_id(module_id)
        with self._condition:
            existing = self._modules.get(module_id)
            if existing and existing.get("serial") != serial_number:
                raise UniversalFirmwareError(
                    "Module ID {} is already assigned to {}.".format(
                        module_id, existing.get("serial", "another module")
                    )
                )

        acknowledgement = self._transaction(
            "mXI{}:{}".format(serial_number, module_id),
            timeout=timeout,
            predicate=lambda event: (
                event["type"] == "ack"
                and event["serial"] == serial_number
                and event["id"] == module_id
            ),
        )
        if acknowledgement:
            with self._condition:
                self._recovery.pop(serial_number, None)
            # Query after assignment so the inventory gains its firmware version.
            time.sleep(0.05)
            self._transaction(
                "m{}v".format(module_id),
                timeout=1.0,
                predicate=lambda event: (
                    event["type"] == "version"
                    and event.get("reported_id") == module_id
                    and event.get("universal")
                ),
            )
        return acknowledgement is not None

    def deprovision(self, module_id):
        module_id = self.validate_id(module_id)
        self._write("m{}R".format(module_id))
        with self._condition:
            module = self._modules.pop(module_id, None)
            self._recovery.pop((module or {}).get("serial"), None)
        # Someone wants this module unassigned, so the pairing has to go too:
        # otherwise the next advertisement would be answered with the ID they
        # just erased.
        self._registry_call("forget", module_id=module_id)

    def deprovision_all(self):
        self._write("m*R")
        with self._condition:
            self._modules.clear()
            self._recovery.clear()
        self._registry_call("forget_all")

    # ── Identity recovery ────────────────────────────────────

    def _registry_call(self, name, *args, **kwargs):
        """Call the registry, if there is one, without ever failing the bus."""
        if self._registry is None:
            return None
        try:
            return getattr(self._registry, name)(*args, **kwargs)
        except Exception:
            logging.exception("Module registry %s failed", name)
            return None

    def _recovery_enabled(self):
        return bool(self._registry is not None and self._registry_call("enabled"))

    def _recovery_candidates(self, now):
        """Modules advertising for an ID that we already know the answer to."""
        with self._condition:
            advertising = [
                serial_number
                for serial_number, item in sorted(self._unprovisioned.items())
                if now - item["last_seen"] <= self.ADVERTISEMENT_TTL_SECONDS
            ]
            attempted = {
                serial_number: dict(record)
                for serial_number, record in self._recovery.items()
            }

        candidates = []
        for serial_number in advertising:
            record = attempted.get(serial_number)
            if record:
                if record["attempts"] >= self.RECOVERY_MAX_ATTEMPTS:
                    continue
                if now - record["last_attempt_at"] < self.RECOVERY_RETRY_SECONDS:
                    continue
            module_id = self._registry_call("known_id", serial_number)
            if module_id is not None:
                candidates.append((serial_number, module_id))
        return candidates

    def recover_pending(self):
        """Give their IDs back to any modules on the bus that have lost theirs.

        Called from the passive reader thread, which holds no lock between
        reads. Returns one record per module it tried, so a caller driving
        this directly can see what happened.
        """
        # A sweep with no bus to talk to would only spend attempts on
        # failures that say nothing about the modules.
        if not self._recovery_enabled() or self._get_sim_mode() or not self._connected():
            return []
        return [
            self._recover(serial_number, module_id)
            for serial_number, module_id in self._recovery_candidates(time.time())
        ]

    def _record_recovery(self, serial_number, module_id, attempts, status, message=""):
        record = {
            "serial": serial_number,
            "id": module_id,
            "attempts": attempts,
            "status": status,
            "message": message,
            "last_attempt_at": time.time(),
        }
        with self._condition:
            self._recovery[serial_number] = record
        return dict(record)

    def _id_holder(self, module_id):
        """Who answers to this ID right now — a serial, "unknown", or None.

        Our inventory cannot be trusted for this. Modules say nothing unless
        they are asked, so an entry that has not been heard from in an hour
        describes a module that is perfectly fine just as well as one that has
        gone. Handing out an ID is only safe if nobody answers to it, so ask.

        A module on the original firmware answers without a serial, and
        "something is there but I cannot say what" still means taken.
        """
        answer = self._transaction(
            "m{}v".format(module_id),
            timeout=self.RECOVERY_PROBE_SECONDS,
            predicate=lambda event: (
                event["type"] == "version" and event.get("reported_id") == module_id
            ),
        )
        if answer is None:
            return None
        return (answer.get("serial") or "").upper() or "unknown"

    def _recover(self, serial_number, module_id):
        with self._condition:
            attempts = int(self._recovery.get(serial_number, {}).get("attempts", 0)) + 1

        try:
            occupant = self._id_holder(module_id)
            if occupant and occupant != serial_number:
                logging.warning(
                    "%s is asking for an ID and we have it down as module %02d, but "
                    "%s is answering to that ID; leaving it alone",
                    serial_number, module_id, occupant)
                return self._record_recovery(
                    serial_number, module_id, attempts, "conflict",
                    "Module {:02d} is already answering as {}.".format(module_id, occupant))

            if occupant is None:
                # Nobody is there. Anything our inventory still believes about
                # that ID is out of date, and provision() would refuse on the
                # strength of it.
                with self._condition:
                    stale = self._modules.get(module_id)
                    if stale and stale.get("serial") != serial_number:
                        self._modules.pop(module_id, None)

            acknowledged = self.provision(
                serial_number, module_id, timeout=self.RECOVERY_ACK_TIMEOUT)
        except UniversalFirmwareError as exc:
            logging.warning("Could not give module %02d back to %s: %s",
                            module_id, serial_number, exc)
            return self._record_recovery(
                serial_number, module_id, attempts, "failed", str(exc))

        if not acknowledged:
            return self._record_recovery(
                serial_number, module_id, attempts, "unconfirmed",
                "No acknowledgement was received.")

        # Attempts reset on success: a module that forgets again months from
        # now should not be starting from a spent budget.
        record = self._record_recovery(serial_number, module_id, 0, "recovered")
        self._registry_call("on_recovered", serial_number, module_id)
        return record

    def _recover_if_due(self):
        now = time.monotonic()
        with self._condition:
            if now - self._last_recovery_sweep < self.RECOVERY_SWEEP_SECONDS:
                return
            self._last_recovery_sweep = now
        try:
            self.recover_pending()
        except UniversalFirmwareError as exc:
            logging.debug("Universal Firmware recovery deferred: %s", exc)

    def run_diagnostic(self, module_id, kind="snapshot", revolutions=5):
        module_id = self.validate_id(module_id)
        kind = str(kind or "snapshot").lower()
        if kind not in ("snapshot", "hall", "mechanical"):
            raise UniversalFirmwareError("Unknown diagnostic test.")

        with self._condition:
            module = self._modules.get(module_id)
            firmware_number = int((module or {}).get("firmware_number", 0))
            if firmware_number and firmware_number < 26:
                raise UniversalFirmwareError(
                    "Module {} needs Universal Firmware v26 or newer for diagnostics."
                    .format(module_id)
                )

        if kind == "snapshot":
            command = "m{}Q".format(module_id)
            timeout = 3.0
        elif kind == "hall":
            command = "m{}T".format(module_id)
            timeout = 35.0
        else:
            try:
                revolutions = int(revolutions)
            except (TypeError, ValueError):
                revolutions = 5
            revolutions = max(5, min(revolutions, 20))
            command = (
                "m{}M{}".format(module_id, revolutions)
                if firmware_number >= 29
                else "m{}M".format(module_id)
            )
            timeout = 20.0 + (revolutions * 7.0)

        result = self._transaction(
            command,
            timeout=timeout,
            predicate=lambda event: (
                event["type"] == kind
                and event.get("id") == module_id
            ),
        )
        if result is None:
            raise UniversalFirmwareError(
                "Module {} did not answer the {} test.".format(module_id, kind)
            )
        return {
            key: value for key, value in result.items()
            if key not in ("line", "sequence")
        }
