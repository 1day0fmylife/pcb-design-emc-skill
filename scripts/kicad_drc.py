"""Импорт наблюдений из готового отчёта KiCad DRC v1."""

import math
from collections import Counter


SECTIONS = ("violations", "unconnected_items", "schematic_parity")
VIOLATION_RULES = {
    "clearance": ("PCB-GEO-001", "copper_gap"),
}
WIDTH_METRICS = {"min": "trace_width", "max": "trace_width_max"}


def import_drc(profile, report, context, artifact, known_rules):
    if context["design_revision"] != profile["design_revision"]:
        raise ValueError("Ревизия контекста KiCad не совпадает с профилем")
    if context["board_source"] != report["source"]:
        raise ValueError("board_source контекста не совпадает с source отчёта KiCad")

    width_bounds = context.get("track_width_bounds", {})
    width_locations = {
        f"/violations/{position}"
        for position, violation in enumerate(report["violations"])
        if violation["type"] == "track_width"
    }
    if width_bounds.keys() - width_locations:
        raise ValueError("track_width_bounds содержит указатель, по которому нет замечания track_width")

    objects = profile["review_objects"]
    index = context.get("items", {})
    for uuid, metadata in index.items():
        object_id = metadata.get("object_id")
        if object_id is not None and object_id not in objects:
            raise ValueError(f"{uuid} — неизвестный объект профиля {object_id}")

    criteria_by_object = {}
    for criterion_id, criterion in profile["criteria"].items():
        key = (criterion["object_id"], criterion["metric"], criterion["rule_id"])
        if context.get("mode") not in (None, criterion["mode"]):
            continue
        criteria_by_object.setdefault(key, []).append(criterion_id)

    findings = []
    for section in SECTIONS:
        for position, violation in enumerate(report[section]):
            affected = [
                item | {"project_context": index.get(item["uuid"].lower(), {})}
                for item in violation["items"]
            ]
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
            if rule_id not in known_rules or section != "violations":
                rule_id = None
            object_ids = {item["project_context"].get("object_id") for item in affected}
            shared_object = next(iter(object_ids)) if len(object_ids) == 1 else None
            candidates = (
                criteria_by_object.get((shared_object, metric, rule_id), [])
                if rule_id and shared_object and not excluded else []
            )
            findings.append(violation | {
                "items": affected,
                "section": section,
                "excluded": excluded,
                "status": "REVIEW_REQUIRED",
                "candidate_rule_id": rule_id,
                "candidate_criterion_ids": candidates,
                "evidence_ref": {"artifact": artifact, "locator": locator},
            })

    counts = Counter(
        "excluded" if item["excluded"] else item["severity"] for item in findings
    )
    return {
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
        "findings": findings,
    }
