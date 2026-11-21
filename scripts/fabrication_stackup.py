"""Сверка явно заданного стека KiCad с критериями изготовителя."""

from collections import Counter
from decimal import Decimal

if __package__:
    from .pcb_comparisons import numeric_check, text_check, validate_range
else:
    from pcb_comparisons import numeric_check, text_check, validate_range


RULE_ID = "PCB-DFM-002"
SOURCE_FIELDS = ("manufacturer", "document", "revision", "section")
TOTAL_FIELDS = {"board": "declared_board_thickness_mm", "stack": "declared_stack_thickness_mm"}
LAYER_FIELDS = ("thickness_mm", "epsilon_r", "loss_tangent")
STRUCTURE_ISSUES = {
    "stackup_missing", "ambiguous_stackup", "unnamed_layer", "duplicate_stack_layers",
    "unexpected_sublayer", "stack_layer_order",
}


def validate_fabrication_criteria(fabrication):
    validate_range((fabrication.get("total_thickness") or {}).get("thickness_mm"), "total_thickness")
    names = set()
    for layer in fabrication.get("layers") or ():
        name = layer["name"]
        if name in names:
            raise ValueError(f"Повтор имени слоя в листе изготовителя — {name}")
        names.add(name)
        for index, sublayer in enumerate(layer.get("sublayers") or (), 1):
            for field in LAYER_FIELDS:
                validate_range(sublayer.get(field), f"{name} / {index} / {field}")
            frequency = sublayer.get("spec_frequency_hz")
            if frequency is not None and not Decimal(str(frequency)).is_finite():
                raise ValueError(f"{name} / {index} — частота должна быть конечным числом")


def compare_layers(stackup, criteria):
    layers = {layer["name"]: layer for layer in stackup["layers"]}
    expected = {layer["name"]: layer for layer in criteria}
    checks = []
    for name in layers.keys() - expected.keys():
        if layers[name]["kind"] in {"copper", "dielectric"}:
            checks.append({"kind": "fabrication_layer_missing", "layer": name, "status": "REVIEW_REQUIRED"})
    checks.sort(key=lambda item: item["layer"])

    pcb_order = [name for name in layers if name in expected]
    fabrication_order = [name for name in expected if name in layers]
    if len(pcb_order) > 1:
        checks.append({"kind": "layer_order", "pcb_value": pcb_order, "criterion": fabrication_order,
                       "status": "PASS" if pcb_order == fabrication_order else "FAIL"})

    for name, target in expected.items():
        if name not in layers:
            checks.append({"kind": "pcb_layer_missing", "layer": name, "status": "FAIL"})
            continue
        if target.get("type") is not None:
            checks.append(text_check("type", layers[name].get("type"), target["type"], layer=name))
        target_sublayers = target.get("sublayers") or []
        if not target_sublayers:
            checks.append({"kind": "fabrication_sublayers_missing", "layer": name, "status": "REVIEW_REQUIRED"})
            continue
        actual_sublayers = layers[name]["sublayers"]
        matching_count = len(actual_sublayers) == len(target_sublayers)
        checks.append({"kind": "sublayer_count", "layer": name, "pcb_value": len(actual_sublayers),
                       "criterion": len(target_sublayers), "status": "PASS" if matching_count else "FAIL"})
        if not matching_count:
            continue
        for index, (actual, target_row) in enumerate(zip(actual_sublayers, target_sublayers), 1):
            scope = {"layer": name, "sublayer": index}
            checks.append(numeric_check("thickness_mm", actual.get("thickness_mm"),
                                        target_row.get("thickness_mm"), **scope))
            if target_row.get("material") is not None:
                checks.append(text_check("material", actual.get("material"), target_row["material"], **scope))
            for field in ("epsilon_r", "loss_tangent"):
                if field not in target_row:
                    continue
                observed_frequency = actual.get("spec_frequency")
                expected_frequency = target_row.get("spec_frequency_hz")
                if not observed_frequency or not expected_frequency or observed_frequency != expected_frequency:
                    checks.append({"kind": "dielectric_frequency", "field": field, "status": "REVIEW_REQUIRED",
                                   "pcb_frequency_hz": observed_frequency,
                                   "fabrication_frequency_hz": expected_frequency, **scope})
                else:
                    checks.append(numeric_check(field, actual.get(field), target_row[field], **scope))
    return checks


def compare_fabrication_stackup(board, fabrication, design_revision):
    criteria = fabrication or {}
    source = criteria.get("source") or {}
    stackup = board["stackup"] if board is not None else None
    blockers = []
    checks = []
    if board is None:
        blockers.append({"kind": "missing_input", "field": "board"})
    if fabrication is None:
        blockers.append({"kind": "missing_input", "field": "fabrication"})
    if not (design_revision or "").strip() or not criteria.get("design_revision"):
        blockers.append({"kind": "design_revision_missing"})
    elif design_revision != criteria["design_revision"]:
        blockers.append({"kind": "design_revision_mismatch", "expected": criteria["design_revision"]})
    fields = [field for field in SOURCE_FIELDS if not source.get(field)]
    if fields:
        blockers.append({"kind": "fabrication_source_missing", "fields": fields})
    if board is not None and criteria.get("board_sha256") and criteria["board_sha256"] != board["board_sha256"]:
        blockers.append({"kind": "board_sha256_mismatch", "expected": criteria["board_sha256"]})

    if stackup is not None and fabrication is not None:
        total = criteria.get("total_thickness") or {}
        basis = total.get("basis")
        if basis is None:
            checks.append({"kind": "total_thickness_basis_missing", "status": "REVIEW_REQUIRED"})
        else:
            field = TOTAL_FIELDS[basis]
            checks.append(numeric_check(field, stackup[field], total.get("thickness_mm"), basis=basis))
        if criteria.get("copper_finish") is not None:
            checks.append(text_check("copper_finish", stackup["settings"].get("copper_finish"),
                                     criteria["copper_finish"]))

        structural = [issue for issue in stackup["issues"] if issue["kind"] in STRUCTURE_ISSUES]
        if structural:
            checks.append({"kind": "pcb_stackup_structure", "issues": structural, "status": "REVIEW_REQUIRED"})
        elif not criteria.get("layers"):
            checks.append({"kind": "fabrication_layers_missing", "status": "REVIEW_REQUIRED"})
        else:
            checks.extend(compare_layers(stackup, criteria["layers"]))

    if blockers:
        checks = [item | {"status": "REVIEW_REQUIRED"} for item in checks]
        checks[:0] = [issue | {"status": "REVIEW_REQUIRED"} for issue in blockers]

    counts = Counter(item["status"] for item in checks)
    status = "REVIEW_REQUIRED"
    if counts["FAIL"]:
        status = "FAIL"
    elif counts["PASS"] and not counts["REVIEW_REQUIRED"]:
        status = "PASS"
    return {
        "report_type": "fabrication_stackup_comparison",
        "rule_id": RULE_ID,
        "overall_status": status,
        "design_revision": design_revision,
        "fabrication_source": source,
        "board": {key: board[key] for key in ("source", "board_sha256", "pcbnew_version")} if board is not None else None,
        "status_counts": dict(counts),
        "checks": checks,
    }
