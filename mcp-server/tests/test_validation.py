"""Tests for src.lib.validation — tool-boundary input coercion."""

from src.lib.validation import clamp_int


class TestClampInt:
    def test_in_range_value_is_returned(self):
        assert clamp_int(5, default=10, minimum=1, maximum=50) == 5

    def test_below_minimum_is_clamped(self):
        assert clamp_int(-5, default=10, minimum=1, maximum=50) == 1

    def test_above_maximum_is_clamped(self):
        assert clamp_int(100000, default=10, minimum=1, maximum=50) == 50

    def test_string_numeric_value_is_parsed(self):
        assert clamp_int("7", default=10, minimum=1, maximum=50) == 7

    def test_non_numeric_string_falls_back_to_default(self):
        assert clamp_int("ten", default=10, minimum=1, maximum=50) == 10

    def test_none_falls_back_to_default(self):
        assert clamp_int(None, default=10, minimum=1, maximum=50) == 10

    def test_default_is_also_clamped(self):
        # A caller-supplied default above max still returns max rather
        # than leaking an out-of-range value.
        assert clamp_int("not-a-number", default=999, minimum=1, maximum=50) == 50

    def test_float_is_rejected_to_default(self):
        # int("1.5") raises ValueError — we fall back to default rather
        # than silently truncating, to make the LLM-input failure mode
        # explicit.
        assert clamp_int("1.5", default=10, minimum=1, maximum=50) == 10

    def test_zero_minimum_accepts_zero(self):
        assert clamp_int(0, default=5, minimum=0, maximum=100) == 0
