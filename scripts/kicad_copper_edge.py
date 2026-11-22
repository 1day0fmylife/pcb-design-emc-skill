"""Зазор прямых дорожек до замкнутого линейного или круглого контура KiCad."""

from collections import Counter
from fractions import Fraction
from itertools import combinations, product
from math import sqrt

if __package__:
    from .kicad_board import board_evidence, trace_scope_blockers
    from .kicad_drc import enrich_item
    from .pcb_segments import intersects, orientation, point_segment_squared_distance, segment_squared_distance
else:
    from kicad_board import board_evidence, trace_scope_blockers
    from kicad_drc import enrich_item
    from pcb_segments import intersects, orientation, point_segment_squared_distance, segment_squared_distance


RULE_ID = "PCB-GEO-003"


def point_distance_squared(point, center):
    return (point[0] - center[0]) ** 2 + (point[1] - center[1]) ** 2


def radial_bounds_squared(segment, center):
    # Distance to a point is continuous, with its maximum at a segment endpoint.
    return point_segment_squared_distance(center, segment), max(
        point_distance_squared(point, center) for point in segment
    )


def outline_issues(geometry):
    issues = list(geometry["outline_issues"])
    if issues:
        return issues
    edges = geometry["outline_segments"]
    circles = geometry.get("outline_circles", ())
    if (edges and len(edges) < 3) or not (edges or circles):
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

    for uuid, (_, radius) in circles:
        if radius <= 0:
            return [{"reason": "zero_radius_outline_circle", "uuid": uuid}]
    for (line_uuid, segment), (circle_uuid, (center, radius)) in product(edges, circles):
        nearest, farthest = radial_bounds_squared(segment, center)
        if nearest <= radius ** 2 <= farthest:
            return [{"reason": "outline_intersection", "edge_uuids": [line_uuid, circle_uuid]}]
    for (first_uuid, (a, r1)), (second_uuid, (b, r2)) in combinations(circles, 2):
        if (r1 - r2) ** 2 <= point_distance_squared(a, b) <= (r1 + r2) ** 2:
            return [{"reason": "outline_intersection", "edge_uuids": [first_uuid, second_uuid]}]
    return []


def inside_outline(point, segments, circles):
    x, y = point
    inside = False
    for _, (a, b) in segments:
        x1, y1 = a
        x2, y2 = b
        if (y1 > y) != (y2 > y) and x1 + Fraction((y - y1) * (x2 - x1), y2 - y1) > x:
            inside = not inside
    for _, (center, radius) in circles:
        if point_distance_squared(point, center) < radius ** 2:
            inside = not inside
    return inside


def circle_distance(segment, center, radius, threshold):
    nearest, farthest = radial_bounds_squared(segment, center)
    radius_squared = radius ** 2
    if nearest > radius_squared:
        # Rationalize the difference so that a small positive gap survives rounding.
        distance = (nearest - radius_squared) / (sqrt(nearest) + radius)
        return distance, nearest >= (radius + threshold) ** 2
    if farthest < radius_squared:
        distance = (radius_squared - farthest) / (radius + sqrt(farthest))
        max_distance_to_center = radius - threshold
        return distance, max_distance_to_center >= 0 and farthest <= max_distance_to_center ** 2
    return 0, False


def measure_outline(segment, segments, circles, threshold):
    if segments:
        squared, uuid = min((segment_squared_distance(segment, edge), uuid) for uuid, edge in segments)
        yield sqrt(squared), uuid, squared >= threshold ** 2
    for uuid, (center, radius) in circles:
        distance, meets_threshold = circle_distance(segment, center, radius, threshold)
        yield distance, uuid, meets_threshold


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
    circles = geometry.get("outline_circles", ())
    scale = geometry["units_per_mm"]
    radius = Fraction(track["width"], 2)
    criterion = criteria.get(object_id)
    threshold = radius
    if criterion is not None and criterion["status"] == "NOT_EVALUATED":
        threshold += Fraction(str(criterion["limit"])) * scale
    distances = list(measure_outline(segment, segments, circles, threshold))
    distance, edge_uuid, _ = min(distances)
    outside = distance > 0 and not inside_outline(segment[0], segments, circles)
    gap = max(0, (distance - radius) / scale) if not outside else None
    scope.update(layer=layer, net=native.get("net"), edge_uuid=edge_uuid, pcb_value=gap,
                 unit="mm", centerline_distance_mm=distance / scale, width_mm=track["width"] / scale)
    circle = dict(circles).get(edge_uuid)
    if circle is not None:
        (x, y), edge_radius = circle
        scope["edge_circle"] = {"center_mm": {"x": x / scale, "y": y / scale}, "radius_mm": edge_radius / scale}
    if criterion is None:
        return scope | {"status": "REVIEW_REQUIRED", "reason": "edge_criterion_missing"}
    scope.update(criterion_id=criterion["criterion_id"], source=criterion.get("source"),
                 criterion={"min": criterion["limit"]})
    if criterion["status"] != "NOT_EVALUATED":
        return scope | {"status": "REVIEW_REQUIRED", "reason": "edge_criterion_unconfirmed",
                        "missing_data": criterion["missing_data"]}
    if outside:
        return scope | {"status": "FAIL", "reason": "trace_centerline_outside_material"}
    return scope | {"status": "PASS" if all(meets_threshold for _, _, meets_threshold in distances) else "FAIL"}


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
    issues, checks = [], []
    edge_count, circle_count = 0, 0
    if board is not None:
        geometry = board["edge_geometry"]
        issues = outline_issues(geometry)
        edge_count = len(geometry["outline_segments"])
        circle_count = len(geometry.get("outline_circles", ()))
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
        "outline_segment_count": edge_count, "outline_circle_count": circle_count, "outline_issues": issues,
        "blockers": blockers, "status_counts": dict(counts), "checks": checks,
    }
