"""Представления проверенного инженерного отчёта."""

import re
from collections import Counter
from urllib.parse import quote


SARIF_SCHEMA = "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
SARIF_KINDS = {
    "FAIL": "fail",
    "PASS": "pass",
    "REVIEW_REQUIRED": "review",
    "NOT_EVALUATED": "review",
    "N_A": "notApplicable",
}
SARIF_LEVELS = {"BLOCKER": "error", "ERROR": "error", "WARNING": "warning", "INFO": "note"}
FINDING_FIELDS = {
    "priority": "Приоритет",
    "object_id": "Объект",
    "criterion_id": "Критерий",
    "refdes": "RefDes",
    "net": "Сеть",
    "layer": "Слой",
    "confidence": "Уверенность",
    "verification": "Метод проверки",
    "criterion_source": "Источник из отчёта",
    "remediation": "Рекомендация",
}
WAIVER_FIELDS = {
    "id": "ID", "owner": "владелец", "reason": "причина", "design_revision": "ревизия", "expires_on": "срок",
}
CRITERION_COMPARISONS = {"at_least": "≥ ", "at_most": "≤ ", "within_tolerance": ""}
ASSESSMENTS = {"satisfied": "условие выполнено", "not_satisfied": "условие нарушено"}


def markdown_text(value):
    text = " ".join(str(value).splitlines())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", text)


def criterion_value(criterion):
    if criterion["comparison"] == "engineering_review":
        return criterion.get("requirement") or "TBD"
    limit = criterion["limit"]
    if limit is None:
        return "TBD"
    prefix = CRITERION_COMPARISONS[criterion["comparison"]]
    unit = criterion["unit"]
    if (tolerance := criterion.get("tolerance")) is not None:
        return f"{prefix}{limit} ± {tolerance} {unit}"
    return f"{prefix}{limit} {unit}"


def markdown_finding(finding, rule, criterion):
    lines = [
        f"### {finding['rule_id']} — {markdown_text(rule['title'])}",
        "",
        f"- Статус — `{finding['status']}`",
        f"- Тяжесть — {markdown_text(finding.get('severity', rule['severity']))}",
        f"- Доказательство — {markdown_text(finding['evidence'])}",
    ]
    for key, label in FINDING_FIELDS.items():
        if finding.get(key):
            lines.append(f"- {label} — {markdown_text(finding[key])}")
    if criterion is not None:
        lines.append(f"- Режим — {markdown_text(criterion['mode'])}")
        lines.append(f"- Требование профиля — {markdown_text(criterion_value(criterion))}")
        if source := criterion.get("source"):
            detail = " · ".join(source[key] for key in ("document", "revision", "section"))
            lines.append(f"- Источник профиля — {markdown_text(detail)}")
        if gaps := criterion["missing_data"]:
            lines.append(f"- Недостающие данные — {markdown_text(' '.join(gaps.values()))}")
        if confirmation := criterion.get("confirmation"):
            detail = " · ".join(confirmation.get(key) or "TBD"
                                for key in ("owner", "confirmed_on", "design_revision"))
            lines.append(f"- Подтверждение критерия — {markdown_text(detail)}")
            if reason := confirmation.get("reason"):
                lines.append(f"- Обоснование выбора источника — {markdown_text(reason)}")
    if measurement := finding.get("measurement"):
        lines.append(f"- Измерение — {measurement['value']} {markdown_text(measurement['unit'])}")
    if assessment := finding.get("assessment"):
        lines.append(f"- Оценка — {ASSESSMENTS[assessment]}")
    if reference := finding.get("evidence_ref"):
        lines.append(f"- Запись в артефакте — {markdown_text(reference['artifact'])} · "
                     f"{markdown_text(reference['locator'])}")
    if waiver := finding.get("waiver"):
        detail = " · ".join(f"{label} — {waiver[key]}" for key, label in WAIVER_FIELDS.items())
        lines.append(f"- Исключение — {markdown_text(detail)}")
    return lines


def review_markdown(report, plan, rules, verified):
    counts = Counter(item["status"] for item in report["findings"])
    lines = [
        "# Инженерный отчёт PCB",
        "",
        f"- Профиль — {markdown_text(report['profile_id'])}",
        f"- Ревизия платы — {markdown_text(report['design_revision'])}",
        f"- Итог — `{report['overall_status']}`",
        f"- Замечания — {len(report['findings'])}",
    ]
    if counts:
        lines.append("- По статусам — " + " · ".join(f"`{key}` — {counts[key]}" for key in sorted(counts)))
    if scope := report.get("scope_criteria"):
        lines.append(f"- Охват критериев — {markdown_text(', '.join(scope))}")
    if "waivers" in verified:
        lines.append(f"- Дата проверки срока исключений — {verified['as_of']}")
    if artifacts := report.get("evidence_artifacts"):
        lines.extend(["", "## Артефакты", ""])
        for name, artifact in sorted(artifacts.items()):
            lines.append(f"- {markdown_text(name)} — {markdown_text(artifact['path'])} · SHA-256 {artifact['sha256']}")
    criteria = {row["criterion_id"]: row for row in plan["criterion_plan"] if row["criterion_id"] is not None}
    if report["findings"]:
        lines.extend(["", "## Результаты проверок"])
        for finding in report["findings"]:
            criterion = criteria.get(finding.get("criterion_id"))
            lines.append("")
            lines.extend(markdown_finding(finding, rules[finding["rule_id"]], criterion))
    return "\n".join(lines) + "\n"


def review_sarif(report, plan, rules, verified):
    rule_ids = sorted({finding["rule_id"] for finding in report["findings"]})
    rule_indices = {rule_id: index for index, rule_id in enumerate(rule_ids)}
    descriptors = [
        {
            "id": rule_id,
            "shortDescription": {"text": rules[rule_id]["title"]},
            "defaultConfiguration": {"level": SARIF_LEVELS[rules[rule_id]["severity"]]},
            "properties": {"category": rules[rule_id]["category"], "reference": rules[rule_id]["reference"]},
        }
        for rule_id in rule_ids
    ]
    registered = sorted(report.get("evidence_artifacts", {}).items())
    artifact_indices = {artifact_id: index for index, (artifact_id, _) in enumerate(registered)}
    artifacts = [
        {
            "location": {"uri": quote(artifact["path"], safe="/")},
            "hashes": {"sha-256": artifact["sha256"]},
            "properties": {"evidence_id": artifact_id},
        }
        for artifact_id, artifact in registered
    ]
    criteria = {row["criterion_id"]: row for row in plan["criterion_plan"] if row["criterion_id"] is not None}
    results = []
    for finding in report["findings"]:
        rule_id, status = finding["rule_id"], finding["status"]
        severity = finding.get("severity", rules[rule_id]["severity"])
        properties = {"pcbFinding": finding, "severity": severity}
        if (criterion := criteria.get(finding.get("criterion_id"))) is not None:
            properties["criterion"] = criterion
        result = {
            "ruleId": rule_id, "ruleIndex": rule_indices[rule_id],
            "kind": SARIF_KINDS[status], "level": SARIF_LEVELS[severity] if status == "FAIL" else "none",
            "message": {"text": finding["evidence"]}, "properties": properties,
        }
        reference = finding.get("evidence_ref", {})
        if reference.get("artifact") in artifact_indices:
            result["relatedLocations"] = [{
                "id": 1, "message": {"text": reference["locator"]},
                "physicalLocation": {"artifactLocation": {"index": artifact_indices[reference["artifact"]]}},
            }]
        results.append(result)
    run = {
        "tool": {"driver": {"name": "pcb-design-review", "rules": descriptors}},
        "artifacts": artifacts,
        "results": results,
        "properties": {
            "profile_id": report["profile_id"], "design_revision": report["design_revision"],
            "overall_status": report["overall_status"], "scope_criteria": report.get("scope_criteria", []),
            "validation": verified,
        },
    }
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": [run]}
