"""Canonical JSON and sha256 for summary comparisons (Amendment P3)."""
import datetime
import decimal
import hashlib
import json


def _default(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return str(o)
    raise TypeError(f'not JSON serialisable: {type(o).__name__}')


def canonical_json(obj) -> str:
    """Sorted keys, ISO dates, Decimals as strings, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, default=_default, separators=(',', ':'), ensure_ascii=False)


def to_jsonable(obj):
    """Round-trip through canonical JSON so the value can be stored in a JSONField."""
    return json.loads(canonical_json(obj))


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode('utf-8')).hexdigest()
