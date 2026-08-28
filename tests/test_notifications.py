"""The notification interrupt queue."""

import time
import unittest

from support import SplitflapTestCase, app

from splitflap import notifications
from splitflap.notifications import MAX_QUEUED, _notify_queue, _pop_notify, push
from splitflap.settings import settings


class PushTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        _notify_queue.clear()
        self.addCleanup(_notify_queue.clear)
        settings['notify_enabled'] = True

    def test_a_pushed_message_can_be_popped(self):
        push("HELLO", "test")
        self.assertEqual(_pop_notify()['text'], "HELLO")

    def test_messages_pop_oldest_first(self):
        push("ONE", "test")
        push("TWO", "test")
        self.assertEqual([_pop_notify()['text'], _pop_notify()['text']], ["ONE", "TWO"])

    def test_nothing_is_queued_while_interrupts_are_disabled(self):
        # Triggers used to enqueue regardless, and nothing drains the queue
        # when interrupts are off, so it grew for the life of the process.
        settings['notify_enabled'] = False
        self.assertIsNone(push("HELLO", "trigger:demo"))
        self.assertEqual(_notify_queue, [])

    def test_expired_messages_are_pruned_on_push(self):
        push("STALE", "test")
        _notify_queue[0]['expires_at'] = time.time() - 1
        push("FRESH", "test")
        self.assertEqual([m['text'] for m in _notify_queue], ["FRESH"])

    def test_expired_messages_are_never_shown(self):
        push("STALE", "test")
        _notify_queue[0]['expires_at'] = time.time() - 1
        self.assertIsNone(_pop_notify())

    def test_the_queue_is_bounded(self):
        for i in range(MAX_QUEUED + 25):
            push(f"MSG{i}", "test")
        self.assertEqual(len(_notify_queue), MAX_QUEUED)
        # the newest survive, the oldest are dropped
        self.assertEqual(_notify_queue[-1]['text'], f"MSG{MAX_QUEUED + 24}")

    def test_display_seconds_defaults_to_the_setting(self):
        settings['notify_display_seconds'] = 42
        self.assertEqual(push("HELLO", "test")['display_seconds'], 42.0)

    def test_quiet_hours_hold_messages_back(self):
        push("HELLO", "test")
        from splitflap.state import state
        state.quiet_hours_active = True
        self.assertIsNone(_pop_notify())
        self.assertEqual(len(_notify_queue), 1, "message should be held, not dropped")


class NotifyRouteTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        _notify_queue.clear()
        self.addCleanup(_notify_queue.clear)
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        settings['notify_enabled'] = True
        settings['notify_sources'] = {"tester": "secret-token"}

    def post(self, payload, token="secret-token"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.http.post("/notify", json=payload, headers=headers)

    def test_a_valid_push_is_queued(self):
        response = self.post({"text": "HELLO"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(_notify_queue[0]['text'], "HELLO")

    def test_an_unknown_token_is_rejected(self):
        self.assertEqual(self.post({"text": "HI"}, token="wrong").status_code, 401)
        self.assertEqual(_notify_queue, [])

    def test_a_missing_token_is_rejected(self):
        self.assertEqual(self.post({"text": "HI"}, token=None).status_code, 401)

    def test_empty_text_is_rejected(self):
        self.assertEqual(self.post({"text": "  "}).status_code, 400)

    def test_the_api_is_closed_when_interrupts_are_disabled(self):
        settings['notify_enabled'] = False
        self.assertEqual(self.post({"text": "HI"}).status_code, 503)

    def test_clearing_by_source_leaves_others(self):
        push("A", "one")
        push("B", "two")
        self.http.delete("/notify?source=one")
        self.assertEqual([m['source'] for m in _notify_queue], ["two"])


if __name__ == "__main__":
    unittest.main()
