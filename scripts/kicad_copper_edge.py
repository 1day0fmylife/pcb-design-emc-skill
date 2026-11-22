"""Зазор прямых дорожек до замкнутого линейного контура KiCad."""

from collections import Counter
from fractions import Fraction
from itertools import combinations
from math import sqrt

if __package__:
    from .kicad_board import board_evidence, trace_scope_blockers
    from .kicad_drc import enrich_item
else:
    from kicad_board import board_evidence, trace_scope_blockers
    from kicad_drc import enrich_item


RULE_ID = "PCB-GEO-003"


def orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def intersects(first, second):
    a, b = first
    c, d = second
    if any(max(min(a[i], b[i]), min(c[i], d[i])) > min(max(a[i], b[i]), max(c[i], d[i])) for i in (0, 1)):
        return False
    return orientation(a, b, c) * orientation(a, b, d) <= 0 and orientation(c, d, a) * orientation(c, d, b) <= 0


def outline_issues(geometry):
    issues = list(geometry["outline_issues"])
    if issues:
        return issues
    edges = geometry["outline_segments"]
    if len(edges) < 3:
        return [{"reason": "outline_missing_or_incomplete"}]
    for uuid, (a, b) in edges:
        if a == b:
            return [{"reason": "zero_length_outline_edge", "uuid": uuid}]
    endpoints = Counter(point for _, segment in edges for point in segment)
    if any(degree != 2 for degree in endpoints.values()):
        return [{"reason": "outline_open_or_branched"}]

    for (first_uuid, first), (second_uuid, second) in combinations(edges, 2):
        shared = set(first).intersection(second)
        if len(shared) == 1:
            point, = shared
            a, = set(first) - shared
            b, = set(second) - shared
            same_ray = orientation(point, a, b) == 0 and sum(
                (a[i] - point[i]) * (b[i] - point[i]) for i in (0, 1)
            ) > 0
            if not same_ray:
                continue
        if intersects(first, second):
            return [{"reason": "outline_intersection", "edge_uuids": [first_uuid, second_uuid]}]
    return []


def point_segment_squared_distance(point, segment):
    a, b = segment
    dx, dy = b[0] - a[0], b[1] - a[1]
    px, py = point[0] - a[0], point[1] - a[1]
    length_squared = dx * dx + dy * dy
    along = px * dx + py * dy
    if along <= 0:
        return px * px + py * py
    if along >= length_squared:
        return (point[0] - b[0]) ** 2 + (point[1] - b[1]) ** 2
    return Fraction((dx * py - dy * px) ** 2, length_squared)


def segment_squared_distance(first, second):
    if intersects(first, second):
        return 0
    return min(
        *(point_segment_squared_distance(point, second) for point in first),
        *(point_segment_squared_distance(point, first) for point in second),
    )


def inside_outline(point, segments):
    x, y = point
    inside = False
    for _, (a, b) in segments:
        x1, y1 = a
        x2, y2 = b
        if (y1 > y) != (y2 > y) and x1 + Fraction((y - y1) * (x2 - x1), y2 - y1) > x:
            inside = not inside
    return inside


def compare_edge_trace(uuid, board, context, profile, criteria, issues):
    metadata = context["items"][uuid]
    object_id = metadata.get("object_id")
    mode = context.get("mode")
    scope = {"uuid": uuid, "object_id": object_id,
             "evidence_ref": {"artifact": board["source"], "locator": f"uuid:{uuid}"}}
    unverified = scope | {"status": "REVIEW_REQUIRED"}
    item = profile["review_objects"].get(object_id, {})
    if item.get("kind") != "board" or mode not in item["modes"]:
        return unverified | {"reason": "board_object_or_mode_unconfirmed"}
    try:
        binding = enrich_item({"uuid": uuid}, context["items"], board["items"])
    except ValueError as error:
        return unverified | {"reason": "board_context_conflict", "detail": str(error)}
    if binding["board_match"] != "matched":
        return unverified | {"reason": "uuid_not_found"}
    native = binding["board_item"]
    copper = [layer for layer in native["layers"] if layer.endswith(".Cu")]
    layer = copper[0] if len(copper) == 1 else None
    if native["kind"] != "PCB_TRACK" or layer is None or metadata.get("layer", layer) != layer:
        return unverified | {"reason": "unsupported_trace_geometry", "pcb_object_kind": native["kind"]}
    if issues:
        return unverified | {"reason": "outline_unconfirmed"}

    geometry = board["edge_geometry"]
    track = geometry["tracks"].get(uuid)
    if not track or track["width"] <= 0 or track["segment"][0] == track["segment"][1]:
        return unverified | {"reason": "trace_geometry_missing"}
    segment = track["segment"]
    segments = geometry["outline_segments"]
    distance_squared, edge_uuid = min(
        (segment_squared_distance(segment, edge), edge_uuid) for edge_uuid, edge in segments
    )
    scale = geometry["units_per_mm"]
    radius = Fraction(track["width"], 2)
    outside = distance_squared > 0 and not inside_outline(segment[0], segments)
    distance = sqrt(distance_squared)
    gap = max(0, (distance - radius) / scale) if not outside else None
    scope.update(layer=layer, net=native.get("net"), edge_uuid=edge_uuid, pcb_value=gap,
                 unit="mm", centerline_distance_mm=distance / scale, width_mm=track["width"] / scale)
    criterion = criteria.get(object_id)
    if criterion is None:
        return scope | {"status": "REVIEW_REQUIRED", "reason": "edge_criterion_missing"}
    scope.update(criterion_id=criterion["criterion_id"], source=criterion.get("source"),
                 criterion={"min": criterion["limit"]})
    if criterion["status"] != "NOT_EVALUATED":
        return scope | {"status": "REVIEW_REQUIRED", "reason": "edge_criterion_unconfirmed",
                        "missing_data": criterion["missing_data"]}
    if outside:
        return scope | {"status": "FAIL", "reason": "trace_centerline_outside_material"}
    threshold = radius + Fraction(str(criterion["limit"])) * scale
    return scope | {"status": "PASS" if distance_squared >= threshold ** 2 else "FAIL"}


def compare_copper_edge(profile, board, context, plan):
    revision = profile["design_revision"]
    context_data = context or {}
    mode = context_data.get("mode")
    uuids = sorted(context_data.get("items", {}))
    blockers = trace_scope_blockers(board, context, revision)
    criteria = {
        row["object_id"]: row for row in plan["criterion_plan"]
        if row["rule_id"] == RULE_ID and row["metric"] == "copper_to_edge" and row["mode"] == mode
    }
    issues, checks, edge_count = [], [], 0
    if board is not None:
        geometry = board["edge_geometry"]
        issues = outline_issues(geometry)
        edge_count = len(geometry["outline_segments"])
        checks = [compare_edge_trace(uuid, board, context, profile, criteria, issues) for uuid in uuids]
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
    return {
        "report_type": "kicad_copper_edge_comparison", "rule_id": RULE_ID,
        "profile_id": profile["profile_id"], "design_revision": revision, "mode": mode,
        "overall_status": status, "scope_uuids": uuids, "board": board_evidence(board, context),
        "outline_segment_count": edge_count, "outline_issues": issues,
        "blockers": blockers, "status_counts": dict(counts), "checks": checks,
    }
