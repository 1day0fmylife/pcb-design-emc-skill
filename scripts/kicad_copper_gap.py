"""Номинальный зазор меди между выбранными парами прямых дорожек KiCad."""

from collections import Counter, defaultdict
from fractions import Fraction
from itertools import combinations, product
from math import sqrt

if __package__:
    from .kicad_board import board_evidence, trace_scope_blockers
    from .kicad_drc import enrich_item
    from .pcb_segments import segment_squared_distance
else:
    from kicad_board import board_evidence, trace_scope_blockers
    from kicad_drc import enrich_item
    from pcb_segments import segment_squared_distance


RULE_ID = "PCB-GEO-001"


def native_trace(uuid, board_items, context_items):
    try:
        binding = enrich_item({"uuid": uuid}, context_items, board_items)
    except ValueError as error:
        return None, {"reason": "board_context_conflict", "detail": str(error)}
    if binding["board_match"] != "matched":
        return None, {"reason": "uuid_not_found"}
    native = binding["board_item"]
    if native["kind"] != "PCB_TRACK":
        return None, {"reason": "unsupported_trace_geometry", "pcb_object_kind": native["kind"]}
    copper_layers = [layer for layer in native["layers"] if layer.endswith(".Cu")]
    if not native.get("net") or len(copper_layers) != 1:
        return None, {"reason": "net_or_copper_layer_unconfirmed"}
    return {
        "uuid": uuid,
        "object_id": context_items.get(uuid, {}).get("object_id"),
        "net": native["net"],
        "layer": copper_layers[0],
    }, None


def select_trace_pairs(bound_traces):
    by_layer = defaultdict(lambda: defaultdict(list))
    for uuid, (trace, problem) in bound_traces.items():
        if problem is None:
            by_layer[trace["layer"]][trace["net"]].append(uuid)
    return sorted(
        tuple(sorted(pair))
        for nets in by_layer.values()
        for first_net, second_net in combinations(nets.values(), 2)
        for pair in product(first_net, second_net)
    )


def compare_trace_pair(uuids, board, bound_traces, mode, objects, pairs_by_members, criteria):
    scope = {
        "trace_uuids": list(uuids), "rule_id": RULE_ID,
        "evidence_ref": {"artifact": board["source"], "locator": f"uuids:{','.join(uuids)}"},
    }
    traces = []
    for uuid in uuids:
        trace, problem = bound_traces[uuid]
        if problem:
            return scope | {"status": "REVIEW_REQUIRED", "affected_uuid": uuid} | problem
        traces.append(trace.copy())
    first, second = traces
    scope["traces"] = traces
    if first["net"] == second["net"]:
        return scope | {"status": "N_A", "reason": "same_net"}
    if first["layer"] != second["layer"]:
        return scope | {"status": "N_A", "reason": "different_copper_layers"}

    geometry = board["trace_geometry"]
    tracks = []
    scale = geometry["units_per_mm"]
    for uuid in uuids:
        track = geometry["tracks"].get(uuid)
        if not track or track["width"] <= 0 or track["segment"][0] == track["segment"][1]:
            return scope | {"status": "REVIEW_REQUIRED", "reason": "trace_geometry_missing", "affected_uuid": uuid}
        tracks.append(track)
    for trace, track in zip(traces, tracks):
        trace["width_mm"] = track["width"] / scale
    first_track, second_track = tracks
    distance_squared = segment_squared_distance(first_track["segment"], second_track["segment"])
    radii = Fraction(first_track["width"] + second_track["width"], 2)
    distance = sqrt(distance_squared)
    scope.update(pcb_value=max(0, (distance - radii) / scale), unit="mm", centerline_distance_mm=distance / scale)

    classes = [objects.get(trace["object_id"], {}) for trace in traces]
    if any(item.get("kind") != "net_class" or mode not in item["modes"] for item in classes):
        return scope | {"status": "REVIEW_REQUIRED", "reason": "net_class_or_mode_unconfirmed"}
    first_class, second_class = first["object_id"], second["object_id"]
    object_id = first_class
    if first_class != second_class:
        object_id = pairs_by_members.get(frozenset((first_class, second_class)))
        if object_id is None or mode not in objects[object_id]["modes"]:
            return scope | {"status": "REVIEW_REQUIRED", "reason": "net_class_pair_or_mode_unconfirmed"}
    scope["object_id"] = object_id
    criterion = criteria.get(object_id)
    if criterion is None:
        return scope | {"status": "REVIEW_REQUIRED", "reason": "gap_criterion_missing"}
    scope.update(rule_id=criterion["rule_id"], criterion_id=criterion["criterion_id"],
                 source=criterion.get("source"), criterion={"min": criterion["limit"]})
    if criterion["status"] != "NOT_EVALUATED":
        return scope | {"status": "REVIEW_REQUIRED", "reason": "gap_criterion_unconfirmed",
                        "missing_data": criterion["missing_data"]}
    # Compare the squared distances exactly. The reported mm value is for display.
    threshold = radii + Fraction(str(criterion["limit"])) * scale
    return scope | {"status": "PASS" if distance_squared >= threshold ** 2 else "FAIL"}


def compare_copper_gap(profile, board, context, plan, *, all_pairs=False):
    context_data = context or {}
    context_items = context_data.get("items", {})
    mode = context_data.get("mode")
    revision = profile["design_revision"]
    if all_pairs and "trace_pairs" in context_data:
        raise ValueError("--all-pairs требует контекст без поля trace_pairs")
    trace_pairs = sorted(tuple(sorted(pair)) for pair in context_data.get("trace_pairs", ()))
    if len(trace_pairs) != len(set(trace_pairs)):
        raise ValueError("trace_pairs — повтор пары UUID дорожек")
    blockers = trace_scope_blockers(board, context, revision)
    if all_pairs and len(context_items) < 2:
        blockers.append("trace_scope_insufficient")
    elif not all_pairs and not trace_pairs:
        blockers.append("trace_pair_scope_empty")

    uuids = sorted(context_items) if all_pairs else sorted({uuid for pair in trace_pairs for uuid in pair})
    bound_traces = {} if board is None else {
        uuid: native_trace(uuid, board["items"], context_items) for uuid in uuids
    }
    unresolved = []
    selection = {}
    if all_pairs:
        trace_pairs = select_trace_pairs(bound_traces)
        unresolved = [
            {"uuid": uuid, "status": "REVIEW_REQUIRED"} | problem
            for uuid, (_, problem) in bound_traces.items() if problem is not None
        ]
        selection = {"scope_mode": "all_pairs", "scope_uuids": uuids, "unresolved_traces": unresolved}

    objects = profile["review_objects"]
    pairs_by_members = {
        frozenset(item["members"]): object_id for object_id, item in objects.items()
        if item["kind"] == "net_class_pair"
    }
    criteria = {
        row["object_id"]: row for row in plan["criterion_plan"]
        if row["metric"] == "copper_gap" and row["mode"] == mode
    }
    checks = [] if board is None else [
        compare_trace_pair(pair, board, bound_traces, mode, objects, pairs_by_members, criteria)
        for pair in trace_pairs
    ]
    if blockers:
        checks = [
            row | {"status": "REVIEW_REQUIRED", "reason": "scope_unverified"}
            if row["status"] != "REVIEW_REQUIRED" else row for row in checks
        ]
    counts = Counter(row["status"] for row in checks)
    status = "REVIEW_REQUIRED"
    if counts["FAIL"]:
        status = "FAIL"
    elif not counts["REVIEW_REQUIRED"] and not blockers and not unresolved:
        status = "PASS" if counts["PASS"] else "N_A"
    return {
        "report_type": "kicad_copper_gap_comparison",
        "profile_id": profile["profile_id"], "design_revision": revision, "mode": mode,
        "overall_status": status, "scope_pairs": [list(pair) for pair in trace_pairs],
        "board": board_evidence(board, context), "blockers": blockers,
        "status_counts": dict(counts), "checks": checks,
    } | selection
