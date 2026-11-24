"""Unit tests for form list[T] field extraction helpers.

Tests ``_is_sequence_field``, which says which model fields get each value
of a repeated form key.
"""

from __future__ import annotations

from freeman._kwargs.extractors import _is_sequence_field

# ---------------------------------------------------------------------------
# _is_sequence_field
# ---------------------------------------------------------------------------


class TestIsSequenceField:
    """Tests for _is_sequence_field — identifies list/set/frozenset/tuple fields."""

    def test_list_str(self):
        assert _is_sequence_field(list[str]) is True

    def test_list_int(self):
        assert _is_sequence_field(list[int]) is True

    def test_set_str(self):
        assert _is_sequence_field(set[str]) is True

    def test_frozenset_str(self):
        assert _is_sequence_field(frozenset[str]) is True

    def test_tuple_str_ellipsis(self):
        assert _is_sequence_field(tuple[str, ...]) is True

    def test_optional_list_str(self):
        """Optional[list[T]] should still be recognised after unwrapping."""
        assert _is_sequence_field(list[str] | None) is True

    def test_optional_list_int(self):
        assert _is_sequence_field(list[int] | None) is True

    def test_plain_str_is_not_sequence(self):
        assert _is_sequence_field(str) is False

    def test_plain_int_is_not_sequence(self):
        assert _is_sequence_field(int) is False

    def test_plain_bool_is_not_sequence(self):
        assert _is_sequence_field(bool) is False

    def test_dict_is_not_sequence(self):
        # dict is not in _SEQUENCE_ORIGINS
        assert _is_sequence_field(dict[str, str]) is False

    def test_optional_str_is_not_sequence(self):
        assert _is_sequence_field(str | None) is False

    def test_optional_int_is_not_sequence(self):
        assert _is_sequence_field(int | None) is False
