"""Which physical module is which.

Every Universal Firmware module carries a 20-hex chip serial burned into its
microcontroller. Its module ID is not like that: the ID lives in EEPROM
alongside the calibration, and on this hardware EEPROM forgets. A module that
loses its ID stops answering to its address and starts advertising for a new
one, so its place in the display goes blank until someone notices and
re-provisions it by hand.

The serial is the part that cannot be forgotten, so the pairing is kept here —
chip serial to module ID — and handed back to the module when it turns up
asking who it is. settings.json already holds that module's offset,
calibration and per-character tuning under the same ID, so a module that comes
back gets its whole configuration and not just its address.

The registry is a bijection: a module ID belongs to one physical module. When
a serial takes an ID another serial held, the older pairing is dropped —
otherwise two modules would both try to recover into the same slot.
"""

import logging
import time

from splitflap.settings import save_settings, settings


class ModuleRegistry:
    """Serial-to-ID pairings, persisted in settings.json.

    ``restore`` is injected rather than imported: pushing a module's stored
    configuration back onto the bus belongs to splitflap.transport, which is
    what constructs this.
    """

    def __init__(self, restore=None):
        self._restore = restore

    # settings is one dict, mutated in place — but tests clear and refill it,
    # so the store is looked up per call rather than cached on the instance.
    def _known(self):
        return settings.setdefault("module_registry", {})

    @staticmethod
    def _key(serial):
        return str(serial or "").strip().upper()

    @staticmethod
    def _module_id(module_id):
        try:
            module_id = int(module_id)
        except (TypeError, ValueError):
            return None
        return module_id if 0 <= module_id <= 254 else None

    def enabled(self):
        """Whether a known module that lost its ID should be given it back."""
        return bool(settings.get("auto_reprovision", True))

    def known_id(self, serial):
        """The ID this chip last answered to, or None if we have never seen it."""
        entry = self._known().get(self._key(serial))
        return self._module_id((entry or {}).get("id"))

    def recoveries(self, serial):
        """How many times this module has had to be given its ID back."""
        entry = self._known().get(self._key(serial))
        return int((entry or {}).get("recoveries", 0) or 0)

    def remember(self, serial, module_id, firmware=None):
        """Record that this chip answers to this ID. Returns whether it changed.

        Called for every acknowledgement and version reply, so the unchanged
        case — which is nearly all of them — must not touch the disk. Only a
        new pairing is worth a write; last_seen rides along with whatever
        saves settings.json next.
        """
        serial = self._key(serial)
        module_id = self._module_id(module_id)
        if not serial or module_id is None:
            return False

        known = self._known()
        entry = known.setdefault(serial, {"first_seen": time.time(), "recoveries": 0})
        entry["last_seen"] = time.time()

        if entry.get("id") == module_id and (not firmware or entry.get("firmware") == str(firmware)):
            return False

        for other, item in list(known.items()):
            if other != serial and item.get("id") == module_id:
                logging.info("Module %02d is now %s, no longer %s", module_id, serial, other)
                known.pop(other, None)

        if entry.get("id") is not None and entry["id"] != module_id:
            logging.info("Module %s moved from ID %02d to %02d", serial, entry["id"], module_id)
        entry["id"] = module_id
        if firmware:
            entry["firmware"] = str(firmware)
        save_settings(settings)
        return True

    def forget(self, serial=None, module_id=None):
        """Drop a pairing, by serial or by the ID that holds it.

        De-provisioning is deliberate: someone wants that module unassigned.
        Without this the server would hand the ID straight back, and the
        module could not be reassigned at all.
        """
        known = self._known()
        candidates = []
        if serial:
            candidates.append(self._key(serial))
        module_id = self._module_id(module_id)
        if module_id is not None:
            candidates += [s for s, item in known.items() if item.get("id") == module_id]

        dropped = [s for s in candidates if known.pop(s, None) is not None]
        if dropped:
            save_settings(settings)
        return dropped

    def forget_all(self):
        """Drop every pairing, for a bus-wide de-provision."""
        known = self._known()
        dropped = sorted(known)
        if dropped:
            known.clear()
            save_settings(settings)
        return dropped

    def on_recovered(self, serial, module_id):
        """A module got its ID back: count it, and give it its settings back.

        The ID is only the address. A module whose EEPROM dropped its ID has
        almost certainly dropped its offset, calibration and tuning too, so
        what we hold for that ID goes back onto it here.
        """
        serial = self._key(serial)
        module_id = self._module_id(module_id)
        if module_id is None:
            return
        entry = self._known().setdefault(serial, {"first_seen": time.time()})
        entry["id"] = module_id
        entry["last_seen"] = time.time()
        entry["recoveries"] = int(entry.get("recoveries", 0) or 0) + 1
        entry["last_recovery_at"] = time.time()
        save_settings(settings)

        # Worth a warning, not an info: a module that keeps needing this is a
        # module with failing EEPROM, and the count is the evidence.
        logging.warning(
            "Module %02d (%s) had forgotten its ID; restored it (%d time%s so far)",
            module_id, serial, entry["recoveries"], "" if entry["recoveries"] == 1 else "s")

        if self._restore is None:
            return
        try:
            self._restore(module_id)
        except Exception:
            # The ID is back either way, which is the part that unblocks the
            # display; losing the tuning push must not undo that.
            logging.exception("Restoring stored settings to module %02d failed", module_id)
