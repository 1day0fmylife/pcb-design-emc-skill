"""Сверка объявленных монтажных отверстий KiCad с чертежом."""

from collections import Counter
from decimal import Decimal

if __package__:
    from .kicad_board import NIL_UUID
    from .pcb_comparisons import numeric_check, text_check, validate_range
else:
    from kicad_board import NIL_UUID
    from pcb_comparisons import numeric_check, text_check, validate_range


GEOMETRY_RULE = "PCB-MECH-001"
PLATING_RULE = "PCB-MECH-004"
RULE_IDS = (GEOMETRY_RULE, PLATING_RULE)
COORDINATE_SYSTEM = "kicad_pcb_absolute_xy_mm"
SOURCE_FIELDS = ("document", "revision", "section")
POSITION_FIELDS = {"x_mm": "x", "y_mm": "y"}
SIZE_FIELDS = {"circle": ("diameter_mm",), "oblong": ("width_mm", "length_mm")}


def validate_mechanical_criteria(mechanical):
    seen_uuids = set()
    for name, target in (mechanical.get("holes") or {}).items():
        uuid = target.get("pad_uuid")
        if uuid is not None:
            uuid = uuid.lower()
            if uuid == NIL_UUID or uuid in seen_uuids:
                raise ValueError(f"{name} — нулевой или повторяющийся UUID отверстия {uuid}")
            seen_uuids.add(uuid)
        for field in (*POSITION_FIELDS, *SIZE_FIELDS["circle"], *SIZE_FIELDS["oblong"]):
            validate_range(target.get(field), f"{name} / {field}")
        axis = target.get("axis_angle_deg") or {}
        if any(value is not None and not Decimal(str(value)).is_finite() for value in axis.values()):
            raise ValueError(f"{name} / axis_angle_deg — углы должны быть конечными числами")

        shape = target.get("shape")
        if shape == "circle" and any(target.get(field) is not None for field in (*SIZE_FIELDS["oblong"], "axis_angle_deg")):
            raise ValueError(f"{name} — круглое отверстие задаётся диаметром")
        if shape == "oblong" and target.get("diameter_mm") is not None:
            raise ValueError(f"{name} — продолговатое отверстие задаётся шириной и длиной")


def compare_axis(actual, criterion, **scope):
    axis = criterion or {}
    nominal, tolerance = axis.get("nominal"), axis.get("tolerance")
    row = {
        "kind": "angle", "field": "axis_angle_deg", "pcb_value": actual, "criterion": axis, **scope,
    }
    if nominal is None or tolerance is None:
        return row | {"status": "REVIEW_REQUIRED", "reason": "criterion_missing"}
    if actual is None:
        return row | {"status": "REVIEW_REQUIRED", "reason": "pcb_value_missing"}
    difference = abs(Decimal(str(actual)) - Decimal(str(nominal)))
    deviation = min(difference, 180 - difference)
    return row | {
        "deviation_deg": float(deviation),
        "status": "PASS" if deviation <= Decimal(str(tolerance)) else "FAIL",
    }


def compare_hole(board, name, target, coordinates_confirmed):
    uuid = target.get("pad_uuid")
    scope = {"hole_id": name, "pad_uuid": uuid.lower() if uuid else None, "rule_id": GEOMETRY_RULE}
    if uuid is None:
        return [scope | {"kind": "pad_uuid_missing", "status": "REVIEW_REQUIRED"}]
    item = board["items"].get(scope["pad_uuid"])
    if item is None:
        incomplete = bool(board["skipped_without_uuid"].get("PAD"))
        return [scope | {"kind": "pad_uuid_not_found", "status": "REVIEW_REQUIRED" if incomplete else "FAIL"}]
    hole = item.get("hole")
    scope["refdes"] = item.get("refdes")
    if hole is None:
        return [scope | {"kind": "pad_hole_missing", "pcb_object_kind": item["kind"], "status": "FAIL"}]

    plating = scope | {"rule_id": PLATING_RULE}
    shape = text_check("shape", hole["shape"], target.get("shape"), **scope)
    checks = [text_check("kind", hole["kind"], target.get("kind"), **plating), shape]
    center = hole.get("center_mm") or {}
    position_checks = [
        numeric_check(field, center.get(axis), target.get(field), **scope)
        for field, axis in POSITION_FIELDS.items()
    ]
    if shape["status"] == "PASS":
        width, length = sorted((hole["x_mm"], hole["y_mm"]))
        if hole["shape"] == "circle":
            diameter = width if width == length else None
            checks.append(numeric_check("diameter_mm", diameter, target.get("diameter_mm"), **scope))
        else:
            checks.extend([
                numeric_check("width_mm", width, target.get("width_mm"), **scope),
                numeric_check("length_mm", length, target.get("length_mm"), **scope),
            ])
            position_checks.append(compare_axis(hole.get("axis_angle_deg"), target.get("axis_angle_deg"), **scope))
    if not coordinates_confirmed:
        position_checks = [
            row | {"status": "REVIEW_REQUIRED", "reason": "coordinate_system_missing"}
            for row in position_checks
        ]
        for row in position_checks:
            row.pop("deviation_deg", None)
    return checks + position_checks


def compare_mechanical_holes(board, mechanical, design_revision):
    criteria = mechanical or {}
    source = criteria.get("source") or {}
    blockers = []
    if board is None:
        blockers.append({"kind": "missing_input", "field": "board"})
    if mechanical is None:
        blockers.append({"kind": "missing_input", "field": "mechanical"})
    if not (design_revision or "").strip() or not criteria.get("design_revision"):
        blockers.append({"kind": "design_revision_missing"})
    elif design_revision != criteria["design_revision"]:
        blockers.append({"kind": "design_revision_mismatch", "expected": criteria["design_revision"]})
    missing = [field for field in SOURCE_FIELDS if not source.get(field)]
    if missing:
        blockers.append({"kind": "mechanical_source_missing", "fields": missing})
    if board is not None and criteria.get("board_sha256") and criteria["board_sha256"] != board["board_sha256"]:
        blockers.append({"kind": "board_sha256_mismatch", "expected": criteria["board_sha256"]})

    checks = []
    coordinates = criteria.get("coordinate_system")
    holes = criteria.get("holes") or {}
    if not holes:
        checks.append({"kind": "mechanical_holes_missing", "rule_id": GEOMETRY_RULE, "status": "REVIEW_REQUIRED"})
    if board is not None:
        for name, target in holes.items():
            checks.extend(compare_hole(board, name, target, coordinates == COORDINATE_SYSTEM))
    if blockers:
        checks = [
            row | {"status": "REVIEW_REQUIRED", "reason": "scope_unverified"}
            if row["status"] != "REVIEW_REQUIRED" else row
            for row in checks
        ]
        checks[:0] = [row | {"status": "REVIEW_REQUIRED"} for row in blockers]

    counts = Counter(row["status"] for row in checks)
    status = "REVIEW_REQUIRED"
    if counts["FAIL"]:
        status = "FAIL"
    elif counts["PASS"] and not counts["REVIEW_REQUIRED"]:
        status = "PASS"
    return {
        "report_type": "kicad_mounting_comparison",
        "overall_status": status,
        "design_revision": design_revision,
        "mechanical_source": source,
        "coordinate_system": coordinates,
        "board": {key: board[key] for key in ("source", "board_sha256", "pcbnew_version")} if board is not None else None,
        "status_counts": dict(counts),
        "checks": checks,
    }
