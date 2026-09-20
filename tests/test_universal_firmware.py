import pathlib
import sys
import threading
import time
import unittest


SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))

from hardware.universal_firmware import (  # noqa: E402
    UniversalFirmwareManager,
    parse_universal_line,
)


SERIAL_NUMBER = "A3F24C0018E7D29B3F01"
OTHER_SERIAL = "B10055FFA3C2918D7E44"


class FakeSerial:
    def __init__(self):
        self.is_open = True
        self.writes = []
        self.write_hook = None
        self.read_count = 0
        self._rx = bytearray()
        self._lock = threading.Lock()

    def write(self, payload):
        self.writes.append(payload)
        if self.write_hook:
            self.write_hook(payload, self)
        return len(payload)

    def flush(self):
        pass

    def queue_read(self, payload):
        if isinstance(payload, str):
            payload = payload.encode("ascii")
        with self._lock:
            self._rx.extend(payload)

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._rx)

    def read(self, size=1):
        with self._lock:
            self.read_count += 1
            chunk = bytes(self._rx[:size])
            del self._rx[:size]
            return chunk

    def reset_input_buffer(self):
        with self._lock:
            self._rx.clear()


class FakeRegistry:
    """The pairings the manager asks about, without settings.json."""

    def __init__(self, pairings=None, enabled=True):
        self.pairings = dict(pairings or {})
        self._enabled = enabled
        self.recovered = []
        self.forgotten = []
        self.counts = {}

    def enabled(self):
        return self._enabled

    def known_id(self, serial):
        return self.pairings.get(serial)

    def recoveries(self, serial):
        return self.counts.get(serial, 0)

    def remember(self, serial, module_id, firmware=None):
        changed = self.pairings.get(serial) != module_id
        for other, held in list(self.pairings.items()):
            if other != serial and held == module_id:
                self.pairings.pop(other)
        self.pairings[serial] = module_id
        return changed

    def forget(self, serial=None, module_id=None):
        dropped = [s for s, held in list(self.pairings.items())
                   if s == serial or (module_id is not None and held == module_id)]
        for s in dropped:
            self.pairings.pop(s, None)
        self.forgotten += dropped
        return dropped

    def forget_all(self):
        dropped = sorted(self.pairings)
        self.pairings.clear()
        self.forgotten += dropped
        return dropped

    def on_recovered(self, serial, module_id):
        self.recovered.append((serial, module_id))
        self.counts[serial] = self.counts.get(serial, 0) + 1


class ProtocolParserTests(unittest.TestCase):
    def test_parses_advertisement_and_both_ack_formats(self):
        self.assertEqual(
            parse_universal_line("mXadv:" + SERIAL_NUMBER),
            {"type": "advertisement", "serial": SERIAL_NUMBER},
        )
        expected = {"type": "ack", "serial": SERIAL_NUMBER, "id": 38}
        self.assertEqual(
            parse_universal_line("mXack:" + SERIAL_NUMBER + ":38"),
            expected,
        )
        self.assertEqual(
            parse_universal_line("mXack" + SERIAL_NUMBER + ":38"),
            expected,
        )

    def test_parses_current_version_and_diagnostic_frames(self):
        version = parse_universal_line(
            "m38v:29:38:" + SERIAL_NUMBER
        )
        self.assertTrue(version["universal"])
        self.assertEqual(version["reported_id"], 38)
        self.assertEqual(version["firmware"], "29")

        snapshot = parse_universal_line("m38Q:8:12:4930:1:-1")
        self.assertEqual(snapshot["reset_cause"], 8)
        self.assertEqual(snapshot["current_index"], -1)

        hall = parse_universal_line("m38T:0:1:168:1")
        self.assertEqual(hall["falling_edges"], 1)

        mechanical = parse_universal_line(
            "m38M:0:4095:4097:1:168:4500:167:4096,4095,4097,4096,4096"
        )
        self.assertEqual(mechanical["average_magnet_width"], 167)
        self.assertEqual(mechanical["revolutions"], [4096, 4095, 4097, 4096, 4096])


class ManagerCommandTests(unittest.TestCase):
    def setUp(self):
        self.serial = FakeSerial()
        self.manager = UniversalFirmwareManager(
            get_serial=lambda: self.serial,
            serial_lock=threading.Lock(),
            get_sim_mode=lambda: False,
        )

    def tearDown(self):
        self.manager.stop()

    def wait_for_writes(self, count, timeout=1.0):
        deadline = time.time() + timeout
        while len(self.serial.writes) < count and time.time() < deadline:
            time.sleep(0.005)
        self.assertGreaterEqual(len(self.serial.writes), count)

    def test_commands_match_provision_py_examples(self):
        self.manager.home_by_serial(SERIAL_NUMBER)
        self.manager.home_module(38)
        self.manager.deprovision(38)
        self.manager.deprovision_all()
        self.manager.scan(0)
        self.wait_for_writes(5)

        self.assertEqual(
            self.serial.writes[:5],
            [
                ("mXH" + SERIAL_NUMBER + "\n").encode("ascii"),
                b"m38h\n",
                b"m38R\n",
                b"m*R\n",
                b"m*v0-0\n",
            ],
        )

    def test_fragmented_serial_reads_are_reassembled(self):
        self.manager.feed_bytes(b"mXadv:A3F24C0018E7")
        self.manager.feed_bytes(b"D29B3F01\nm4v:29:4:B10055")
        self.manager.feed_bytes(b"FFA3C2918D7E44\n")

        status = self.manager.status(module_limit=45)
        self.assertEqual(
            [item["serial"] for item in status["unprovisioned"]],
            [SERIAL_NUMBER],
        )
        self.assertEqual(status["modules"][0]["id"], 4)
        self.assertEqual(status["modules"][0]["firmware_number"], 29)

    def test_provision_waits_for_ack_and_queries_version(self):
        def respond(payload, serial_port):
            if payload == ("mXI" + SERIAL_NUMBER + ":7\n").encode("ascii"):
                serial_port.queue_read("mXack" + SERIAL_NUMBER + ":7\n")
            elif payload == b"m7v\n":
                serial_port.queue_read("m7v:29:7:" + SERIAL_NUMBER + "\n")

        self.serial.write_hook = respond
        acknowledged = self.manager.provision(SERIAL_NUMBER, 7, timeout=0.5)

        self.assertTrue(acknowledged)
        self.assertEqual(
            self.serial.writes,
            [
                ("mXI" + SERIAL_NUMBER + ":7\n").encode("ascii"),
                b"m7v\n",
            ],
        )
        status = self.manager.status(module_limit=45)
        self.assertEqual(status["modules"][0]["id"], 7)
        self.assertEqual(status["modules"][0]["firmware_number"], 29)

    def test_mechanical_count_is_only_sent_to_v29_or_newer(self):
        self.manager.handle_line("m3v:28:3:" + SERIAL_NUMBER)

        def answer_mechanical(payload, serial_port):
            if payload in (b"m3M\n", b"m3M8\n"):
                serial_port.queue_read("m3M:0:4096:4096:0:168:4500\n")

        self.serial.write_hook = answer_mechanical
        result = self.manager.run_diagnostic(3, "mechanical", revolutions=8)
        self.assertEqual(self.serial.writes[-1], b"m3M\n")
        self.assertEqual(result["type"], "mechanical")

        self.serial.writes.clear()
        self.manager.handle_line("m3v:29:3:" + SERIAL_NUMBER)

        self.manager.run_diagnostic(3, "mechanical", revolutions=8)
        self.assertEqual(self.serial.writes[-1], b"m3M8\n")

    def test_transaction_owns_the_serial_lock_until_response_is_read(self):
        lock = threading.Lock()
        self.manager = UniversalFirmwareManager(
            get_serial=lambda: self.serial,
            serial_lock=lock,
            get_sim_mode=lambda: False,
        )

        def respond(payload, serial_port):
            self.assertTrue(lock.locked())
            serial_port.queue_read("m4Q:0:2:3312:1:12\n")

        self.serial.write_hook = respond
        result = self.manager.run_diagnostic(4, "snapshot")

        self.assertEqual(result["type"], "snapshot")
        self.assertEqual(result["vcc_mv"], 3312)
        self.assertGreater(self.serial.read_count, 0)

    def test_passive_reader_backs_off_while_another_transaction_owns_lock(self):
        lock = threading.Lock()
        self.manager = UniversalFirmwareManager(
            get_serial=lambda: self.serial,
            serial_lock=lock,
            get_sim_mode=lambda: False,
        )
        self.serial.queue_read("mXadv:" + SERIAL_NUMBER + "\n")

        lock.acquire()
        try:
            self.manager.ensure_started()
            time.sleep(0.05)
            self.assertEqual(self.serial.read_count, 0)
        finally:
            lock.release()

        deadline = time.time() + 1
        while self.serial.read_count == 0 and time.time() < deadline:
            time.sleep(0.01)

        self.assertGreater(self.serial.read_count, 0)
        self.assertEqual(
            [item["serial"] for item in self.manager.status()["unprovisioned"]],
            [SERIAL_NUMBER],
        )

    def test_status_filters_expired_advertisements_without_mutating_state(self):
        self.manager.handle_line("mXadv:" + SERIAL_NUMBER)
        with self.manager._condition:
            self.manager._unprovisioned[SERIAL_NUMBER]["last_seen"] -= (
                self.manager.ADVERTISEMENT_TTL_SECONDS + 1
            )

        status = self.manager.status(module_limit=45)

        self.assertEqual(status["unprovisioned"], [])
        with self.manager._condition:
            self.assertIn(SERIAL_NUMBER, self.manager._unprovisioned)



class IdentityRecoveryTests(unittest.TestCase):
    """A module that loses its ID must get it back without anyone noticing.

    EEPROM on this hardware forgets. The ID lives there with the calibration,
    and a module that drops it stops answering to its address and starts
    advertising — leaving its place in the display blank until someone opens
    the calibration page and assigns it again by hand. The chip serial is the
    one thing it cannot forget, so that is what the pairing hangs off.
    """

    def setUp(self):
        self.serial = FakeSerial()
        self.registry = FakeRegistry({SERIAL_NUMBER: 7})
        self.manager = self.build()

    def build(self, registry=None):
        manager = UniversalFirmwareManager(
            get_serial=lambda: self.serial,
            serial_lock=threading.Lock(),
            get_sim_mode=lambda: False,
            registry=registry if registry is not None else self.registry,
        )
        # The real timeouts are seconds of waiting on a module that will never
        # answer, which is time the suite should not spend.
        manager.RECOVERY_PROBE_SECONDS = 0.05
        manager.RECOVERY_ACK_TIMEOUT = 0.05
        return manager

    def tearDown(self):
        self.manager.stop()

    def answer(self, holder=None, ack=True, module_id=7):
        """Serve the two transactions a recovery makes.

        ``holder`` is the serial already answering to the ID, if any. Without
        one the probe goes unanswered, which is what a vacant ID looks like.
        """
        probe = "m{}v\n".format(module_id).encode("ascii")

        def respond(payload, serial_port):
            if payload == probe and holder:
                serial_port.queue_read(
                    "m{}v:29:{}:{}\n".format(module_id, module_id, holder))
            elif payload.startswith(b"mXI") and ack:
                serial_port.queue_read(
                    "mXack{}:{}\n".format(payload[3:23].decode(), module_id))
        self.serial.write_hook = respond

    def advertise(self, serial=SERIAL_NUMBER):
        self.manager.handle_line("mXadv:" + serial)

    def commands(self):
        return [payload.decode("ascii").strip() for payload in self.serial.writes]

    def test_a_module_that_forgot_its_id_is_given_it_back(self):
        self.answer()
        self.advertise()

        result, = self.manager.recover_pending()

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["id"], 7)
        # Ask who holds the ID, assign it, then read the version back.
        self.assertEqual(self.commands(), ["m7v", "mXI" + SERIAL_NUMBER + ":7", "m7v"])
        self.assertEqual(self.registry.recovered, [(SERIAL_NUMBER, 7)])

        status = self.manager.status()
        self.assertEqual(status["unprovisioned"], [])
        self.assertEqual(status["modules"][0]["id"], 7)

    def test_a_module_nobody_has_seen_before_is_left_for_a_person(self):
        self.answer()
        self.advertise(OTHER_SERIAL)

        self.assertEqual(self.manager.recover_pending(), [])
        self.assertEqual(self.commands(), [])
        self.assertIsNone(self.manager.status()["unprovisioned"][0]["known_id"])

    def test_an_id_another_module_answers_to_is_not_taken_from_it(self):
        # Two modules on one ID is the failure this path could introduce, so
        # the bus is asked who holds it rather than the inventory.
        self.answer(holder=OTHER_SERIAL)
        self.advertise()

        result, = self.manager.recover_pending()

        self.assertEqual(result["status"], "conflict")
        self.assertIn(OTHER_SERIAL, result["message"])
        self.assertEqual(self.commands(), ["m7v"])
        self.assertEqual(self.registry.recovered, [])

    def test_the_module_actually_holding_the_id_corrects_the_pairing(self):
        # The probe's answer goes through the parser like any other line, so
        # learning who really holds the ID fixes what we believe — and the
        # module that was wrongly claiming it stops being a candidate at all.
        self.answer(holder=OTHER_SERIAL)
        self.advertise()
        self.manager.recover_pending()

        self.assertEqual(self.registry.pairings, {OTHER_SERIAL: 7})
        self.assertIsNone(self.registry.known_id(SERIAL_NUMBER))

    def test_a_module_on_the_original_firmware_still_counts_as_holding_it(self):
        # It answers without a serial. "Something is there but I cannot say
        # what" is not permission to give the ID to someone else.
        def respond(payload, serial_port):
            if payload == b"m7v\n":
                serial_port.queue_read("m7v:12\n")
        self.serial.write_hook = respond
        self.advertise()

        result, = self.manager.recover_pending()

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.registry.recovered, [])

    def test_a_stale_inventory_entry_does_not_block_a_vacant_id(self):
        # Modules say nothing unless asked, so an entry outlives the module it
        # describes. If nobody answers the probe, the ID is free.
        self.manager.handle_line("m7v:29:7:" + OTHER_SERIAL)
        self.registry.pairings[SERIAL_NUMBER] = 7
        self.answer()
        self.advertise()

        result, = self.manager.recover_pending()

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(self.manager.status()["modules"][0]["serial"], SERIAL_NUMBER)

    def test_a_module_that_will_not_take_its_id_back_is_not_retried_forever(self):
        # Bad EEPROM can fail the write as easily as it failed to hold the
        # value. Retrying every advertisement would mean a write every 15
        # seconds for as long as the module stays plugged in.
        self.answer(ack=False)
        self.advertise()

        for _ in range(self.manager.RECOVERY_MAX_ATTEMPTS + 2):
            self.manager.recover_pending()
            with self.manager._condition:
                record = self.manager._recovery.get(SERIAL_NUMBER)
                if record:      # skip the backoff, not the attempt limit
                    record["last_attempt_at"] -= self.manager.RECOVERY_RETRY_SECONDS

        record = self.manager._recovery[SERIAL_NUMBER]
        self.assertEqual(record["attempts"], self.manager.RECOVERY_MAX_ATTEMPTS)
        self.assertEqual(record["status"], "unconfirmed")

    def test_a_failed_attempt_is_not_repeated_immediately(self):
        self.answer(ack=False)
        self.advertise()
        self.manager.recover_pending()
        before = len(self.serial.writes)

        self.assertEqual(self.manager.recover_pending(), [])
        self.assertEqual(len(self.serial.writes), before)

    def test_a_successful_recovery_spends_none_of_the_retry_budget(self):
        # A module that forgets again next year should not find the budget
        # from this year's incident already spent.
        self.answer()
        self.advertise()
        self.manager.recover_pending()

        self.assertEqual(self.manager._recovery[SERIAL_NUMBER]["attempts"], 0)

    def test_de_provisioning_a_module_forgets_it(self):
        # Erasing an ID is deliberate. Handing it straight back would make a
        # module impossible to reassign.
        self.manager.deprovision(7)

        self.assertEqual(self.registry.forgotten, [SERIAL_NUMBER])
        self.advertise()
        self.assertEqual(self.manager.recover_pending(), [])

    def test_de_provisioning_everything_forgets_everything(self):
        self.registry.pairings[OTHER_SERIAL] = 8
        self.manager.deprovision_all()

        self.assertEqual(self.registry.pairings, {})

    def test_recovery_can_be_switched_off(self):
        self.registry._enabled = False
        self.answer()
        self.advertise()

        self.assertEqual(self.manager.recover_pending(), [])
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.manager.status()["auto_reprovision"])

    def test_nothing_is_attempted_without_a_bus_to_attempt_it_on(self):
        # A failure that only means "no hardware" must not spend an attempt.
        manager = UniversalFirmwareManager(
            get_serial=lambda: None,
            serial_lock=threading.Lock(),
            get_sim_mode=lambda: False,
            registry=self.registry,
        )
        manager.handle_line("mXadv:" + SERIAL_NUMBER)

        self.assertEqual(manager.recover_pending(), [])
        self.assertEqual(manager._recovery, {})

    def test_bus_traffic_records_which_chip_answers_to_which_id(self):
        self.manager.handle_line("m4v:29:4:" + OTHER_SERIAL)
        self.manager.handle_line("mXack" + SERIAL_NUMBER + ":9")

        self.assertEqual(self.registry.pairings[OTHER_SERIAL], 4)
        self.assertEqual(self.registry.pairings[SERIAL_NUMBER], 9)

    def test_status_says_which_module_an_advertisement_belongs_to(self):
        self.advertise()
        self.advertise(OTHER_SERIAL)

        status = self.manager.status(module_limit=45)
        known = {item["serial"]: item["known_id"] for item in status["unprovisioned"]}

        self.assertEqual(known, {SERIAL_NUMBER: 7, OTHER_SERIAL: None})
        # 7 is spoken for, so it is not offered for the module that is not.
        self.assertNotEqual(status["suggested_id"], 7)
        self.assertTrue(status["auto_reprovision"])

    def test_status_counts_how_often_a_module_has_needed_this(self):
        # A module that keeps needing this has failing EEPROM, and the count
        # is what tells its owner to replace it.
        self.answer()
        self.advertise()
        self.manager.recover_pending()

        module, = self.manager.status()["modules"]
        self.assertEqual(module["recoveries"], 1)

    def test_a_broken_registry_cannot_take_the_bus_down(self):
        class Exploding:
            def __getattr__(self, name):
                def boom(*args, **kwargs):
                    raise RuntimeError("settings.json is unreadable")
                return boom

        manager = self.build(registry=Exploding())
        with self.assertLogs(level="ERROR"):
            manager.handle_line("mXack" + SERIAL_NUMBER + ":7")
        self.assertEqual(manager.status()["modules"][0]["id"], 7)
        manager.stop()


if __name__ == "__main__":
    unittest.main()
