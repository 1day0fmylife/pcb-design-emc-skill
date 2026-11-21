"""Сверка ширины явно указанных дорожек KiCad с критериями профиля."""

from collections import Counter, defaultdict

if __package__:
    from .kicad_board import TRACE_KINDS
    from .kicad_drc import enrich_item
    from .pcb_comparisons import numeric_check, validate_range
else:
    from kicad_board import TRACE_KINDS
    from kicad_drc import enrich_item
    from pcb_comparisons import numeric_check, validate_range


RULE_ID = "PCB-DFM-001"
BOUNDS = {"trace_width": "min", "trace_width_max": "max"}


def compare_bound_trace(uuid, board, context, objects, criteria_by_class):
    context_items = context["items"]
    object_id = context_items[uuid].get("object_id")
    mode = context.get("mode")
    scope = {
        "uuid": uuid, "object_id": object_id,
        "evidence_ref": {"artifact": board["source"], "locator": f"uuid:{uuid}"},
    }
    unverified = scope | {"status": "REVIEW_REQUIRED"}
    if object_id not in objects or objects[object_id]["kind"] != "net_class":
        return [unverified | {"reason": "net_class_binding_missing_or_invalid"}]
    if mode is None or mode not in objects[object_id]["modes"]:
        return [unverified | {"reason": "class_mode_unconfirmed"}]

    try:
        item = enrich_item({"uuid": uuid}, context_items, board["items"])
    except ValueError as error:
        return [unverified | {"reason": "board_context_conflict", "detail": str(error)}]
    if item["board_match"] != "matched":
        return [unverified | {"reason": "uuid_not_found"}]
    trace = item["board_item"]
    if trace["kind"] not in TRACE_KINDS:
        return [unverified | {"reason": "not_a_trace", "pcb_object_kind": trace["kind"]}]

    net, width = trace.get("net"), trace.get("width_mm")
    scope.update(net=net, layers=trace["layers"], pcb_object_kind=trace["kind"])
    copper_layers = [layer for layer in trace["layers"] if layer.endswith(".Cu")]
    context_layer = context_items[uuid].get("layer")
    if not net or len(copper_layers) != 1 or (context_layer and context_layer not in copper_layers):
        return [scope | {"status": "REVIEW_REQUIRED", "reason": "net_or_copper_layer_unconfirmed",
                         "pcb_value": width}]
    criteria = criteria_by_class.get(object_id, ())
    if not criteria:
        return [scope | {"status": "REVIEW_REQUIRED", "reason": "width_criterion_missing", "pcb_value": width}]

    limits = {BOUNDS[row["metric"]]: row["limit"] for row in criteria if row["status"] == "NOT_EVALUATED"}
    try:
        validate_range(limits, object_id)
    except ValueError:
        return [scope | {"status": "REVIEW_REQUIRED", "reason": "conflicting_width_bounds",
                         "criterion_ids": [row["criterion_id"] for row in criteria],
                         "criterion": limits, "pcb_value": width}]

    checks = []
    for row in criteria:
        observed = numeric_check("width_mm", width, {BOUNDS[row["metric"]]: row["limit"]}, **scope,
                                 criterion_id=row["criterion_id"], source=row.get("source"))
        if row["status"] != "NOT_EVALUATED":
            observed.update(status="REVIEW_REQUIRED", reason="width_criterion_unconfirmed",
                            missing_data=row["missing_data"])
        checks.append(observed)
    return checks


def compare_trace_width(profile, board, context, plan):
    context_data = context or {}
    context_items = context_data.get("items", {})
    mode = context_data.get("mode")
    revision = profile["design_revision"]
    blockers = []
    if board is None:
        blockers.append("board_missing")
    if context is None:
        blockers.append("context_missing")
    elif not (revision or "").strip() or context["design_revision"] != revision:
        blockers.append("design_revision_unconfirmed")
    if not mode:
        blockers.append("mode_missing")
    if not context_items:
        blockers.append("trace_scope_empty")
    if board is not None and context is not None:
        source = context["board_source"].replace("\\", "/").rsplit("/", 1)[-1]
        name = board["source"].replace("\\", "/").rsplit("/", 1)[-1]
        if source != name:
            blockers.append("board_source_mismatch")
        if context.get("board_sha256", board["board_sha256"]) != board["board_sha256"]:
            blockers.append("board_sha256_mismatch")

    by_object = defaultdict(list)
    for row in plan["criterion_plan"]:
        if row["rule_id"] == RULE_ID and row["metric"] in BOUNDS and row["mode"] == mode:
            by_object[row["object_id"]].append(row)
    checks = []
    if board is not None:
        for uuid in sorted(context_items):
            checks.extend(compare_bound_trace(uuid, board, context, profile["review_objects"], by_object))
    if blockers:
        checks = [
            row | {"status": "REVIEW_REQUIRED", "reason": "scope_unverified"}
            if row["status"] != "REVIEW_REQUIRED" else row for row in checks
        ]
    counts = Counter(row["status"] for row in checks)
    status = "REVIEW_REQUIRED"
    if counts["FAIL"]:
        status = "FAIL"
    elif counts["PASS"] and not counts["REVIEW_REQUIRED"] and not blockers:
        status = "PASS"
    board_evidence = None
    if board is not None:
        board_evidence = {key: board[key] for key in ("source", "board_sha256", "pcbnew_version")}
        board_evidence["sha256_verified_against_context"] = (
            context_data.get("board_sha256") == board["board_sha256"]
        )
    return {
        "report_type": "kicad_trace_width_comparison",
        "rule_id": RULE_ID,
        "profile_id": profile["profile_id"],
        "design_revision": revision,
        "mode": mode,
        "overall_status": status,
        "scope_uuids": sorted(context_items),
        "board": board_evidence,
        "blockers": blockers,
        "status_counts": dict(counts),
        "checks": checks,
    }
