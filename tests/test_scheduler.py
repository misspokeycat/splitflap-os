"""Day-scoped time windows.

Quiet hours and schedules both claim the display for a window on chosen days.
The interesting case is a window that crosses midnight: "Mondays, 22:00-07:00"
has to mean Monday 22:00 through Tuesday 07:00, not Monday 22:00 to midnight
and then again Monday 00:00-07:00.
"""

import unittest
from datetime import datetime
from unittest import mock

import pytz

from support import SplitflapTestCase

from splitflap import scheduler
from splitflap.settings import settings

TZ = pytz.timezone('US/Eastern')

# 2026-08-24 is a Monday.
MON = 24
ALL_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def at(day, hour, minute=0):
    return TZ.localize(datetime(2026, 8, day, hour, minute))


class QuietHoursTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        settings.update({
            'quiet_hours_enabled': True,
            'timezone': 'US/Eastern',
        })

    def quiet_at(self, when, days, start='22:00', end='07:00'):
        settings.update({'quiet_hours_days': days,
                         'quiet_hours_start': start,
                         'quiet_hours_end': end})
        with mock.patch.object(scheduler, 'datetime') as clock:
            clock.now.return_value = when
            return scheduler._is_quiet_hours()

    # ── overnight window, one day selected ──────────────────────────
    def test_starts_on_the_selected_evening(self):
        self.assertTrue(self.quiet_at(at(MON, 23), ['mon']))

    def test_continues_past_midnight_into_the_next_day(self):
        self.assertTrue(self.quiet_at(at(MON + 1, 1), ['mon']))

    def test_continues_until_the_end_time(self):
        self.assertTrue(self.quiet_at(at(MON + 1, 6, 59), ['mon']))

    def test_ends_at_the_end_time(self):
        self.assertFalse(self.quiet_at(at(MON + 1, 7), ['mon']))

    def test_does_not_start_early(self):
        self.assertFalse(self.quiet_at(at(MON, 21, 59), ['mon']))

    def test_the_next_evening_is_not_selected(self):
        # Tuesday evening belongs to a Tuesday window, which was not chosen.
        self.assertFalse(self.quiet_at(at(MON + 1, 23), ['mon']))

    def test_an_unselected_night_stays_awake(self):
        self.assertFalse(self.quiet_at(at(MON + 2, 1), ['mon']))

    # ── the common configurations must not change ───────────────────
    def test_every_day_selected_is_quiet_all_night(self):
        for when in (at(MON, 23), at(MON + 1, 1), at(MON + 1, 6, 59)):
            with self.subTest(when=when.isoformat()):
                self.assertTrue(self.quiet_at(when, ALL_DAYS))

    def test_every_day_selected_is_awake_in_the_afternoon(self):
        self.assertFalse(self.quiet_at(at(MON, 15), ALL_DAYS))

    def test_a_same_day_window_is_unaffected(self):
        self.assertTrue(self.quiet_at(at(MON, 13), ['mon'], start='12:00', end='14:00'))
        self.assertFalse(self.quiet_at(at(MON, 15), ['mon'], start='12:00', end='14:00'))
        self.assertFalse(self.quiet_at(at(MON + 1, 13), ['mon'], start='12:00', end='14:00'))

    def test_disabled_quiet_hours_are_never_active(self):
        settings['quiet_hours_enabled'] = False
        self.assertFalse(self.quiet_at(at(MON, 23), ALL_DAYS))

    def test_no_days_selected_is_never_active(self):
        self.assertFalse(self.quiet_at(at(MON, 23), []))


class ScheduleWindowTests(SplitflapTestCase):
    """Schedules use the same windows, so an overnight schedule has to behave
    the same way."""

    def setUp(self):
        super().setUp()
        settings.update({'timezone': 'US/Eastern', 'quiet_hours_enabled': False})

    def active_at(self, when, days, start='22:00', end='02:00'):
        settings['schedules'] = [{
            'id': 'overnight', 'enabled': True, 'days': days,
            'start_time': start, 'end_time': end,
            'action': {'type': 'off'}, 'name': 'overnight',
        }]
        with mock.patch.object(scheduler, 'datetime') as clock:
            clock.now.return_value = when
            scheduler.state.active_schedule_id = None
            scheduler._schedule_tick()
            return scheduler.state.active_schedule_id

    def test_an_overnight_schedule_holds_past_midnight(self):
        self.assertEqual(self.active_at(at(MON + 1, 1), ['mon']), 'overnight')

    def test_an_overnight_schedule_starts_on_its_own_evening(self):
        self.assertEqual(self.active_at(at(MON, 23), ['mon']), 'overnight')

    def test_an_overnight_schedule_ends_on_time(self):
        self.assertIsNone(self.active_at(at(MON + 1, 2, 1), ['mon']))

    def test_a_disabled_schedule_never_matches(self):
        settings['schedules'] = [{
            'id': 'x', 'enabled': False, 'days': ALL_DAYS,
            'start_time': '00:00', 'end_time': '23:59', 'action': {'type': 'off'},
        }]
        with mock.patch.object(scheduler, 'datetime') as clock:
            clock.now.return_value = at(MON, 12)
            scheduler.state.active_schedule_id = None
            scheduler._schedule_tick()
        self.assertIsNone(scheduler.state.active_schedule_id)


class TimeWindowTests(unittest.TestCase):
    """The underlying comparison, independent of days."""

    def test_a_same_day_window(self):
        self.assertTrue(scheduler._in_time_window('09:00', '17:00', '12:00'))
        self.assertFalse(scheduler._in_time_window('09:00', '17:00', '08:59'))
        self.assertFalse(scheduler._in_time_window('09:00', '17:00', '17:00'))

    def test_an_overnight_window(self):
        self.assertTrue(scheduler._in_time_window('22:00', '07:00', '23:30'))
        self.assertTrue(scheduler._in_time_window('22:00', '07:00', '03:00'))
        self.assertFalse(scheduler._in_time_window('22:00', '07:00', '12:00'))

    def test_an_empty_window_matches_nothing(self):
        self.assertFalse(scheduler._in_time_window('12:00', '12:00', '12:00'))


if __name__ == "__main__":
    unittest.main()
