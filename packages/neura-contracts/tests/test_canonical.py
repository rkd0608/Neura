from __future__ import annotations

import json
import math
import struct
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neura_contracts.canonical import (
    CanonicalizationError,
    canonical_json,
    content_digest,
    format_number,
    is_digest,
)

# RFC 8785 Appendix B: IEEE 754 bit patterns and their canonical serialization.
RFC8785_NUMBERS = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


def _from_bits(bits: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(bits))[0]
    return value


@pytest.mark.parametrize(("bits", "expected"), RFC8785_NUMBERS)
def test_rfc8785_number_vectors(bits: str, expected: str) -> None:
    assert format_number(_from_bits(bits)) == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({"x": value})


def test_keys_sort_by_utf16_code_units() -> None:
    # From RFC 8785 §3.2.3: an emoji (surrogate pair D83D...) sorts before U+FB33.
    keys = ["€", "\r", "דּ", "1", "\U0001f600", "\u0080", "ö"]
    encoded = canonical_json(dict.fromkeys(keys, 0)).decode("utf-8")
    order = list(json.loads(encoded))
    assert order == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]


def test_string_escaping_matches_rfc8785() -> None:
    assert canonical_json('\u000f\n"\\€') == '"\\u000f\\n\\"\\\\€"'.encode()


def test_structure_has_no_whitespace() -> None:
    assert canonical_json({"b": [1, True, None], "a": {"y": 1.5, "x": "s"}}) == (
        b'{"a":{"x":"s","y":1.5},"b":[1,true,null]}'
    )


def test_integers_canonicalize_like_equal_doubles() -> None:
    assert canonical_json(2**53) == b"9007199254740992"
    assert canonical_json(10**21) == b"1e+21"
    assert canonical_json(-7) == b"-7"
    assert canonical_json([1, 1.0]) == b"[1,1]"


def test_inexact_integers_and_unsupported_types_are_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(2**53 + 1)
    with pytest.raises(CanonicalizationError):
        canonical_json(10**400)
    with pytest.raises(CanonicalizationError):
        canonical_json({1: "x"})
    with pytest.raises(CanonicalizationError):
        canonical_json(b"bytes")
    with pytest.raises(CanonicalizationError):
        canonical_json({1.5})


def test_digest_format() -> None:
    digest = content_digest({"a": 1})
    assert is_digest(digest)
    assert digest == content_digest({"a": 1})
    assert not is_digest("sha256:ABC")


json_values: st.SearchStrategy[Any] = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(), children, max_size=5)
    ),
    max_leaves=25,
)


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_number_format_round_trips(value: float) -> None:
    text = format_number(value)
    assert float(text) == value or (value == 0 and float(text) == 0)


@given(json_values)
def test_canonical_output_is_valid_json_and_stable(value: Any) -> None:
    encoded = canonical_json(value)
    # RFC 8785 (I-JSON) consumers read every number as a double.
    decoded = json.loads(encoded, parse_int=float)
    assert canonical_json(decoded) == encoded


@given(st.dictionaries(st.text(), st.integers(min_value=0, max_value=100), max_size=8))
def test_key_insertion_order_does_not_change_digest(mapping: dict[str, int]) -> None:
    reversed_mapping = dict(reversed(list(mapping.items())))
    assert content_digest(mapping) == content_digest(reversed_mapping)
