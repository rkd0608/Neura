"""Canonical JSON (RFC 8785, JSON Canonicalization Scheme) and content digests.

Contract identities are digests of canonical bytes, so they must be reproducible in any language.
RFC 8785 fixes key ordering (UTF-16 code units), string escaping and number formatting (the
ECMAScript Number-to-String algorithm), which plain ``json.dumps`` does not.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

DIGEST_ALGORITHM = "sha256"
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_DIGEST_RE = re.compile(DIGEST_PATTERN)


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented in canonical JSON."""


def canonical_json(value: Any) -> bytes:
    """Serialize a JSON-compatible value to RFC 8785 canonical bytes."""
    parts: list[str] = []
    _write(value, parts)
    return "".join(parts).encode("utf-8")


def content_digest(value: Any) -> str:
    """Return ``sha256:<hex>`` over the canonical JSON form of ``value``."""
    return f"{DIGEST_ALGORITHM}:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def is_digest(value: str) -> bool:
    return _DIGEST_RE.match(value) is not None


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        # RFC 8785 numbers are IEEE 754 doubles: an integer is accepted only if a double holds it
        # exactly, and it is formatted exactly like the equal float so both canonicalize the same.
        try:
            as_float = float(value)
        except OverflowError:
            as_float = math.inf
        if not math.isfinite(as_float) or int(as_float) != value:
            raise CanonicalizationError(f"integer {value} is not exactly representable as a double")
        out.append(format_number(as_float))
    elif isinstance(value, float):
        out.append(format_number(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, Mapping):
        _write_object(value, out)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise CanonicalizationError(f"unsupported type for canonical JSON: {type(value).__name__}")


def _write_object(value: Mapping[Any, Any], out: list[str]) -> None:
    for key in value:
        if not isinstance(key, str):
            raise CanonicalizationError(f"object keys must be strings, got {type(key).__name__}")
    out.append("{")
    for index, key in enumerate(sorted(value, key=_utf16_sort_key)):
        if index:
            out.append(",")
        out.append(_string(key))
        out.append(":")
        _write(value[key], out)
    out.append("}")


def _utf16_sort_key(key: str) -> bytes:
    return key.encode("utf-16-be", errors="surrogatepass")


def _string(value: str) -> str:
    # json.dumps with ensure_ascii=False escapes exactly the characters RFC 8785 requires
    # (quote, backslash, U+0000-U+001F) using the same short forms and lowercase hex.
    return json.dumps(value, ensure_ascii=False)


def format_number(value: float) -> str:
    """Format a float with the ECMAScript Number::toString algorithm (RFC 8785 §3.2.2.3)."""
    if not math.isfinite(value):
        raise CanonicalizationError("NaN and infinity are not valid JSON numbers")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() yields the shortest digit string that round-trips, as ECMAScript requires.
    digits_tuple, exponent = _shortest_digits(abs(value))
    digits = "".join(str(d) for d in digits_tuple)
    k = len(digits)
    n = exponent + k  # value = 0.digits * 10**n
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = f"{digits[:n]}.{digits[n:]}"
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        e_sign = "+" if e >= 0 else "-"
        mantissa = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
        body = f"{mantissa}e{e_sign}{abs(e)}"
    return sign + body


def _shortest_digits(value: float) -> tuple[tuple[int, ...], int]:
    sign, digits, exponent = Decimal(repr(value)).as_tuple()
    assert sign == 0
    assert isinstance(exponent, int)
    trimmed = list(digits)
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
        exponent += 1
    while len(trimmed) > 1 and trimmed[0] == 0:
        trimmed.pop(0)
    return tuple(trimmed), exponent
