"""Date and phone read-back formatting helpers."""

from __future__ import annotations

from datetime import datetime

from voice_desk.prompts import spell_digits
from voice_desk.providers.base import Slot


def test_spell_digits_basic():
    assert spell_digits("0134") == "zero, one, three, four"


def test_spell_digits_strips_non_digits():
    assert spell_digits("+1 (555) 0199") == "one, five, five, five, zero, one, nine, nine"


def test_slot_labels():
    slot = Slot(
        id="slot-x",
        start=datetime(2026, 7, 14, 9, 0),
        end=datetime(2026, 7, 14, 10, 0),
    )
    assert slot.day_label == "Tuesday, July 14"
    assert slot.time_label == "9:00 AM"
    assert slot.spoken() == "Tuesday, July 14 at 9:00 AM"


def test_slot_time_label_pm():
    slot = Slot(
        id="slot-y",
        start=datetime(2026, 7, 14, 14, 30),
        end=datetime(2026, 7, 14, 15, 30),
    )
    assert slot.time_label == "2:30 PM"
