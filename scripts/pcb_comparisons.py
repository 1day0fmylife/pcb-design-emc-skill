"""Примитивы для сверки данных платы с явно заданными критериями."""

from decimal import Decimal


def validate_range(bounds, label):
    lower, upper = ((bounds or {}).get(key) for key in ("min", "max"))
    numbers = [Decimal(str(value)) for value in (lower, upper) if value is not None]
    if any(not value.is_finite() for value in numbers):
        raise ValueError(f"{label} — границы должны быть конечными числами")
    if len(numbers) == 2 and numbers[0] > numbers[1]:
        raise ValueError(f"{label} — min превышает max")


def numeric_check(field, actual, bounds, **scope):
    bounds = {key: value for key, value in (bounds or {}).items() if value is not None}
    row = {"kind": "numeric", "field": field, "pcb_value": actual, "criterion": bounds, **scope}
    if not bounds:
        return row | {"status": "REVIEW_REQUIRED", "reason": "criterion_missing"}
    if actual is None:
        return row | {"status": "REVIEW_REQUIRED", "reason": "pcb_value_missing"}
    observed = Decimal(str(actual))
    within = ("min" not in bounds or observed >= Decimal(str(bounds["min"]))) and (
        "max" not in bounds or observed <= Decimal(str(bounds["max"]))
    )
    return row | {"status": "PASS" if within else "FAIL"}


def text_check(field, actual, expected, **scope):
    row = {"kind": "text", "field": field, "pcb_value": actual, "criterion": expected, **scope}
    if expected is None:
        return row | {"status": "REVIEW_REQUIRED", "reason": "criterion_missing"}
    if actual is None or actual.strip().lower() in {"", "none", "not specified"}:
        return row | {"status": "REVIEW_REQUIRED", "reason": "pcb_value_missing"}
    return row | {"status": "PASS" if actual == expected else "FAIL"}
