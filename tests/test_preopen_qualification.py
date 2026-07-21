from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.preopen_qualification import _check_automation, _check_legacy_automation_paused


class PreopenQualificationTests(unittest.TestCase):
    @patch("scripts.preopen_qualification._automation_text")
    def test_valid_automation_contract(self, read_text):
        read_text.return_value = '\n'.join((
            'status = "ACTIVE"',
            'rrule = "RRULE:FREQ=DAILY"',
            'prompt = "scheduler-ack READ_ONLY"',
        ))
        self.assertEqual(_check_automation("sample"), (True, []))

    @patch("scripts.preopen_qualification._automation_text")
    def test_count_one_and_missing_ack_fail_closed(self, read_text):
        read_text.return_value = '\n'.join((
            'status = "ACTIVE"',
            'rrule = "RRULE:FREQ=DAILY;COUNT=1"',
            'prompt = "READ_ONLY"',
        ))
        passed, reasons = _check_automation("sample")
        self.assertFalse(passed)
        self.assertIn("DEFECTIVE_COUNT_ONE_RECURRENCE", reasons)
        self.assertIn("ATOMIC_START_ACK_NOT_REQUIRED", reasons)

    @patch("scripts.preopen_qualification._automation_text")
    def test_legacy_automation_must_be_paused(self, read_text):
        read_text.return_value = 'status = "PAUSED"\n'
        self.assertEqual(_check_legacy_automation_paused("sample"), (True, []))

    @patch("scripts.preopen_qualification._automation_text")
    def test_absent_legacy_automation_passes_after_codex_removal(self, read_text):
        read_text.return_value = None
        passed, reasons = _check_legacy_automation_paused("sample")
        self.assertTrue(passed)
        self.assertEqual(["LEGACY_AUTOMATION_ABSENT"], reasons)

    @patch("scripts.preopen_qualification._automation_text")
    def test_active_legacy_automation_still_fails(self, read_text):
        read_text.return_value = 'status = "ACTIVE"\n'
        passed, reasons = _check_legacy_automation_paused("sample")
        self.assertFalse(passed)
        self.assertIn("DUPLICATE_SCHEDULER_NOT_PAUSED", reasons)


if __name__ == "__main__":
    unittest.main()
