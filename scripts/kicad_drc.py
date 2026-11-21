"""Импорт наблюдений из готового отчёта KiCad DRC v1."""

import math
from collections import Counter


SECTIONS = ("violations", "unconnected_items", "schematic_parity")
VIOLATION_RULES = {
    "clearance": ("PCB-GEO-001", "copper_gap"),
    "copper_edge_clearance": ("PCB-GEO-003", "copper_to_edge"),
    "copper_outside_board": ("PCB-GEO-003", None),
    "annular_width": ("PCB-GEO-005", None),
    "hole_clearance": ("PCB-GEO-006", None),
    "hole_to_hole": ("PCB-DFM-001", None),
    "holes_co_located": ("PCB-DFM-001", None),
    "drill_out_of_range": ("PCB-DFM-001", None),
    "microvia_drill_out_of_range": ("PCB-DFM-001", None),
    "via_diameter": ("PCB-DFM-001", None),
    "solder_mask_bridge": ("PCB-DFM-003", None),
    "isolated_copper": ("PCB-GND-003", None),
}
WIDTH_METRICS = {"min": "trace_width", "max": "trace_width_max"}


def enrich_item(item, index, board_items):
    uuid = item["uuid"].lower()
    metadata = index.get(uuid, {})
    result = item | {"project_context": metadata}
    if board_items is None:
        return result
    native = board_items.get(uuid)
    if native is None:
        return result | {"board_match": "not_found"}

    derived = {field: native[field] for field in ("refdes", "net") if field in native}
    layers = native["layers"]
    if len(layers) == 1:
        derived["layer"] = layers[0]
    for field in ("refdes", "net"):
        if field in metadata and field in derived and metadata[field] != derived[field]:
            raise ValueError(f"{uuid} — поле {field} в контексте расходится с PCB")
    if layers and "layer" in metadata and metadata["layer"] not in layers:
        raise ValueError(f"{uuid} — слой в контексте расходится с PCB")
    return result | {"project_context": derived | metadata, "board_match": "matched", "board_item": native}


def finding_object(affected, metric, net_class_pairs):
    object_ids = {item["project_context"].get("object_id") for item in affected}
    if len(object_ids) == 1:
        return next(iter(object_ids))
    if metric != "copper_gap" or len(affected) != 2:
        return None
    nets = {item["project_context"].get("net") for item in affected}
    if len(nets) == 2 and None not in nets:
        return net_class_pairs.get(frozenset(object_ids))
    return None


def import_drc(profile, report, context, artifact, known_rules, board=None):
    if context["design_revision"] != profile["design_revision"]:
        raise ValueError("Ревизия контекста KiCad не совпадает с профилем")
    if context["board_source"] != report["source"]:
        raise ValueError("board_source контекста не совпадает с source отчёта KiCad")
    board_items = None
    if board is not None:
        expected_sha = context.get("board_sha256")
        if expected_sha and expected_sha != board["board_sha256"]:
            raise ValueError("SHA-256 переданной платы расходится с board_sha256 из контекста KiCad")
        board_items = board["items"]

    width_bounds = context.get("track_width_bounds", {})
    width_locations = {
        f"/violations/{position}"
        for position, violation in enumerate(report["violations"])
        if violation["type"] == "track_width"
    }
    if width_bounds.keys() - width_locations:
        raise ValueError("track_width_bounds содержит указатель, по которому нет замечания track_width")

    objects = profile["review_objects"]
    net_class_pairs = {
        frozenset(item["members"]): object_id for object_id, item in objects.items()
        if item["kind"] == "net_class_pair"
    }
    index = context.get("items", {})
    for uuid, metadata in index.items():
        object_id = metadata.get("object_id")
        if object_id is None:
            continue
        if object_id not in objects:
            raise ValueError(f"{uuid} — неизвестный объект профиля {object_id}")
        if objects[object_id]["kind"] == "net_class_pair":
            raise ValueError(f"{uuid} — для привязки объекта KiCad укажите ID отдельного класса сетей")

    criteria_by_object = {}
    for criterion_id, criterion in profile["criteria"].items():
        key = (criterion["object_id"], criterion["metric"], criterion["rule_id"])
        if context.get("mode") not in (None, criterion["mode"]):
            continue
        criteria_by_object.setdefault(key, []).append(criterion_id)

    findings = []
    board_matches = Counter()
    for section in SECTIONS:
        for position, violation in enumerate(report[section]):
            affected = [enrich_item(item, index, board_items) for item in violation["items"]]
            if board_items is not None:
                board_matches.update(item["board_match"] for item in affected)
            locations = [item["pos"] for item in affected]
            if "pos" in violation:
                locations.append(violation["pos"])
            if any(
                isinstance(point[key], float) and not math.isfinite(point[key])
                for point in locations for key in ("x", "y")
            ):
                raise ValueError(f"{section}/{position} — координаты должны быть конечными числами")

            excluded = violation.get("excluded", False)
            locator = f"/{section}/{position}"
            rule_id, metric = VIOLATION_RULES.get(violation["type"], (None, None))
            if violation["type"] == "track_width":
                rule_id, metric = "PCB-DFM-001", WIDTH_METRICS.get(width_bounds.get(locator))
            if metric == "copper_to_edge":
                layers = {layer for item in affected for layer in item.get("board_item", {}).get("layers", ())}
                layers.update(item["project_context"].get("layer") for item in affected)
                if "Edge.Cuts" not in layers or "Margin" in layers:
                    metric = None
            if rule_id not in known_rules or section != "violations":
                rule_id = metric = None
            objects_resolved = board is None or all(item["board_match"] == "matched" for item in affected)
            candidate_object = finding_object(affected, metric, net_class_pairs) if (
                rule_id and metric and not excluded and objects_resolved
            ) else None
            candidates = (
                criteria_by_object.get((candidate_object, metric, rule_id), []) if candidate_object else []
            )
            findings.append(violation | {
                "items": affected,
                "section": section,
                "excluded": excluded,
                "status": "REVIEW_REQUIRED",
                "candidate_rule_id": rule_id,
                "candidate_metric": metric,
                "candidate_object_id": candidate_object,
                "candidate_criterion_ids": candidates,
                "evidence_ref": {"artifact": artifact, "locator": locator},
            })

    counts = Counter(
        "excluded" if item["excluded"] else item["severity"] for item in findings
    )
    candidates_by_rule = Counter(item["candidate_rule_id"] for item in findings
                                 if item["candidate_rule_id"] and not item["excluded"])
    result = {
        "report_type": "kicad_drc_import",
        "profile_id": profile["profile_id"],
        "design_revision": profile["design_revision"],
        "overall_status": "REVIEW_REQUIRED" if findings else "NOT_EVALUATED",
        "source": report["source"],
        "kicad_version": report["kicad_version"],
        "date": report["date"],
        "coordinate_units": report["coordinate_units"],
        "mode": context.get("mode"),
        "exit_code": context.get("exit_code"),
        "included_severities": report.get("included_severities"),
        "ignored_checks": report.get("ignored_checks"),
        "section_counts": {section: len(report[section]) for section in SECTIONS},
        "counts": {key: counts[key] for key in ("error", "warning", "excluded")},
        "active_candidates_by_rule": dict(sorted(candidates_by_rule.items())),
        "findings": findings,
    }
    if board is not None:
        result["board"] = {
            "source": board["source"],
            "sha256": board["board_sha256"],
            "sha256_verified_against_context": "board_sha256" in context,
            "pcbnew_version": board["pcbnew_version"],
            "matches": {key: board_matches[key] for key in ("matched", "not_found")},
        }
    return result
