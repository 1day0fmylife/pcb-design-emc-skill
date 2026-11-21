"""Критерии по объектам платы и проверка инженерного отчёта."""

import math
from decimal import Decimal


METRICS = {
    "clearance": (("domain_pair",), ("PCB-GEO-002",), "mm"),
    "creepage": (("domain_pair",), ("PCB-GEO-002",), "mm"),
    "copper_gap": (("net_class", "net_class_pair"), ("PCB-GEO-001", "PCB-DFM-001"), "mm"),
    "copper_to_edge": (("board",), ("PCB-GEO-003",), "mm"),
    "trace_width": (("net_class",), ("PCB-DFM-001",), "mm"),
    "trace_width_max": (("net_class",), ("PCB-DFM-001",), "mm"),
    "controlled_impedance": (("port",), ("PCB-HS-001",), "ohm"),
}
PAIR_METRICS = {
    "domain_pair": (("clearance", "PCB-GEO-002"), ("creepage", "PCB-GEO-002")),
    "net_class_pair": (("copper_gap", "PCB-GEO-001"),),
}


def criterion_key(criterion):
    return tuple(criterion[field] for field in ("object_id", "mode", "metric"))


def validate_matrix(objects, criteria, rule_ids):
    pairs = {}
    for object_id, item in objects.items():
        kind = item["kind"]
        if kind not in PAIR_METRICS:
            continue
        pair = (kind, frozenset(item["members"]))
        if pair in pairs:
            label = "доменов" if kind == "domain_pair" else "классов сетей"
            raise ValueError(f"Повтор пары {label} в {object_id} и {pairs[pair]}")
        pairs[pair] = object_id
        if kind == "net_class_pair":
            for member in item["members"]:
                member_object = objects.get(member, {})
                if member_object.get("kind") != "net_class":
                    raise ValueError(f"{object_id} — участник {member} должен быть объявленным объектом net_class")
                unsupported_modes = sorted(set(item["modes"]) - set(member_object["modes"]))
                if unsupported_modes:
                    raise ValueError(f"{object_id} — класс {member} не объявлен в режимах {', '.join(unsupported_modes)}")

    slots = set()
    for criterion_id, criterion in criteria.items():
        object_id, mode, metric = criterion_key(criterion)
        if object_id not in objects:
            raise ValueError(f"{criterion_id} — объект {object_id} отсутствует")
        if mode not in objects[object_id]["modes"]:
            raise ValueError(f"{criterion_id} — режим {mode} не объявлен для {object_id}")
        if criterion["rule_id"] not in rule_ids:
            raise ValueError(f"{criterion_id} — неизвестное правило {criterion['rule_id']}")
        if metric == "qualitative":
            continue
        kinds, rules, unit = METRICS[metric]
        if objects[object_id]["kind"] not in kinds:
            raise ValueError(f"{criterion_id} — метрика {metric} требует объект {' или '.join(kinds)}")
        if criterion["rule_id"] not in rules:
            raise ValueError(f"{criterion_id} — правило {criterion['rule_id']} не связано с метрикой {metric}")
        if criterion["unit"] != unit:
            raise ValueError(f"{criterion_id} — метрика {metric} требует единицу {unit}")
        slot = (object_id, mode, metric)
        if slot in slots:
            raise ValueError(f"{criterion_id} — повтор критерия для {' / '.join(slot)}")
        slots.add(slot)

        limit = criterion["limit"]
        tolerance = criterion.get("tolerance")
        for value in (limit, tolerance):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{criterion_id} — значение должно быть конечным числом")
        if tolerance is not None and limit is not None and tolerance >= limit:
            raise ValueError(f"{criterion_id} — допуск должен быть меньше целевого импеданса")


def criterion_plan(objects, criteria):
    slots = {criterion_key(entry) for entry in criteria.values()}
    entries = [(name, entry) for name, entry in criteria.items()]
    for object_id, item in objects.items():
        for mode in item["modes"]:
            for metric, rule_id in PAIR_METRICS.get(item["kind"], ()):
                if (object_id, mode, metric) not in slots:
                    entries.append((None, {
                        "object_id": object_id, "mode": mode, "metric": metric,
                        "rule_id": rule_id, "unit": "mm", "limit": None,
                    }))

    plan = []
    for criterion_id, entry in entries:
        missing = {}
        metric = entry["metric"]
        comparison = "at_least"
        if metric == "qualitative":
            comparison = "engineering_review"
            if not entry.get("requirement"):
                missing["requirement"] = "Указать проверяемое условие для объекта и режима."
        elif entry["limit"] is None:
            missing["limit"] = "Указать численный критерий для объекта и режима."
        if metric == "controlled_impedance":
            comparison = "within_tolerance"
            if entry.get("tolerance") is None:
                missing["tolerance"] = "Указать абсолютный допуск импеданса в омах."
        elif metric == "trace_width_max":
            comparison = "at_most"
        if not entry.get("source"):
            missing["source"] = "Указать документ, редакцию и раздел с критерием."
        plan.append(entry | {
            "criterion_id": criterion_id,
            "comparison": comparison,
            "missing_data": missing,
            "status": "REVIEW_REQUIRED" if missing else "NOT_EVALUATED",
        })
    return plan


def result_satisfies(criterion, finding, field):
    if criterion["comparison"] == "engineering_review":
        if "assessment" not in finding:
            raise ValueError(f"{field} — для качественного критерия нужна оценка assessment")
        return finding["assessment"] == "satisfied"

    if "measurement" not in finding:
        raise ValueError(f"{field} — для численного критерия нужен результат measurement")
    measurement = finding["measurement"]
    observed = measurement["value"]
    if isinstance(observed, float) and not math.isfinite(observed):
        raise ValueError(f"{field} — результат должен быть конечным числом")
    if measurement["unit"] != criterion["unit"]:
        raise ValueError(f"{field} — единица результата расходится с критерием")
    observed = Decimal(str(observed))
    limit = Decimal(str(criterion["limit"]))
    if criterion["comparison"] == "within_tolerance":
        return abs(observed - limit) <= Decimal(str(criterion["tolerance"]))
    if criterion["comparison"] == "at_most":
        return observed <= limit
    return observed >= limit


def validate_report_findings(profile, report, plan):
    if report["profile_id"] != profile["profile_id"]:
        raise ValueError("ID профиля в отчёте не совпадает с переданным профилем")
    if report["design_revision"] != profile["design_revision"]:
        raise ValueError("Ревизия платы в отчёте не совпадает с профилем")

    criteria = {row["criterion_id"]: row for row in plan["criterion_plan"] if row["criterion_id"] is not None}
    applicability = {row["rule_id"]: row["applicability"] for row in plan["rules"]}
    scope = set(report.get("scope_criteria", ()))
    if scope - criteria.keys():
        raise ValueError(f"Неизвестные критерии в scope_criteria — {', '.join(sorted(scope - criteria.keys()))}")
    confirmed = set()
    for index, finding in enumerate(report["findings"]):
        field = f"findings[{index}]"
        rule_id = finding["rule_id"]
        object_id = finding.get("object_id")
        criterion_id = finding.get("criterion_id")
        if rule_id not in applicability:
            raise ValueError(f"{field} — неизвестное правило {rule_id}")
        if object_id is not None and object_id not in profile["review_objects"]:
            raise ValueError(f"{field} — неизвестный объект {object_id}")
        if criterion_id is None:
            continue
        criterion = criteria.get(criterion_id)
        if criterion is None:
            raise ValueError(f"{field} — неизвестный критерий {criterion_id}")
        if criterion["rule_id"] != rule_id or criterion["object_id"] != object_id:
            raise ValueError(f"{field} — правило или объект расходится с критерием {criterion_id}")
        if scope and criterion_id not in scope:
            raise ValueError(f"{field} — критерий {criterion_id} находится за пределами scope_criteria")
        if finding["status"] not in {"PASS", "FAIL"}:
            continue
        if applicability[rule_id] != "selected" or criterion["missing_data"]:
            raise ValueError(f"{field} — применимость или исходные данные критерия требуют уточнения")

        satisfies = result_satisfies(criterion, finding, field)
        if satisfies != (finding["status"] == "PASS"):
            raise ValueError(f"{field} — статус не соответствует результату и критерию {criterion_id}")
        confirmed.add(criterion_id)

    if report["overall_status"] == "PASS" and scope != confirmed:
        raise ValueError("Для общего PASS требуется результат PASS по всем критериям scope_criteria")
