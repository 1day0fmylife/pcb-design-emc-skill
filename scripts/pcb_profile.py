#!/usr/bin/env python3
"""Локальный конфигуратор PCB-профиля."""
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
from pathlib import Path

try:
    import yaml
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
except ModuleNotFoundError as error:
    if __name__ == "__main__":
        sys.exit(f"Требуется окружение Python с PyYAML 6 и jsonschema 4 ({error.name}).")
    raise

if __package__:
    from .fabrication_stackup import compare_fabrication_stackup, validate_fabrication_criteria
    from .kicad_board import read_board_inventory
    from .kicad_checks import run_kicad_checks
    from .kicad_drc import import_drc
    from .kicad_netlist import compare_netlist, read_kicad_netlist
    from .kicad_probe import probe_kicad
    from .kicad_trace_width import compare_trace_width
    from .mechanical_holes import RULE_IDS as MECHANICAL_RULE_IDS
    from .mechanical_holes import compare_mechanical_holes, validate_mechanical_criteria
    from .pcb_criteria import criterion_plan, parse_calendar_date, validate_matrix, validate_report_findings
    from .pcb_evidence import verify_evidence_artifacts
else:
    from fabrication_stackup import compare_fabrication_stackup, validate_fabrication_criteria
    from kicad_board import read_board_inventory
    from kicad_checks import run_kicad_checks
    from kicad_drc import import_drc
    from kicad_netlist import compare_netlist, read_kicad_netlist
    from kicad_probe import probe_kicad
    from kicad_trace_width import compare_trace_width
    from mechanical_holes import RULE_IDS as MECHANICAL_RULE_IDS
    from mechanical_holes import compare_mechanical_holes, validate_mechanical_criteria
    from pcb_criteria import criterion_plan, parse_calendar_date, validate_matrix, validate_report_findings
    from pcb_evidence import verify_evidence_artifacts


ROOT = Path(__file__).resolve().parents[1]
PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
PROFILE_FORMATS = (".yaml", ".yml", ".json")
REPORT_FORMATS = {".md": "markdown", ".markdown": "markdown", ".json": "json"}
SEVERITIES = ("BLOCKER", "ERROR", "WARNING", "INFO")
DOMAINS = {
    "electrical_safety": "Электробезопасность",
    "si_pi": "Функциональность, SI и PI",
    "emc_emission": "Эмиссия EMC",
    "emc_immunity": "Устойчивость EMC",
    "fabrication": "Изготовление и сборка",
    "mechanical": "Механика",
    "thermal": "Тепловой режим",
    "testability": "Тестируемость",
}
VERIFICATION = {
    "geometry": "Геометрия платы",
    "calculation": "Расчёт или модель",
    "review": "Инженерное ревью",
    "test": "Проверка по согласованной программе испытаний",
}


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise ValueError(f"Ожидается строковый ключ, получен {key!r}")
        if key in result:
            raise ValueError(f"Повторяющийся ключ {key!r}")
        result[key] = value
    return result


class UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        return unique_mapping(self.construct_pairs(node, deep=deep))


def reject_json_constant(value):
    raise ValueError(f"Недопустимое значение JSON {value}")


def load_document(path, *, content=None, strict_json=False):
    try:
        if content is None:
            content = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            return json.loads(content, object_pairs_hook=unique_mapping,
                              parse_constant=reject_json_constant if strict_json else None)
        return yaml.load(content, Loader=UniqueSafeLoader)
    except (OSError, ValueError, yaml.YAMLError, RecursionError) as error:
        raise ValueError(f"{path} — {error}") from error


def validate_document(document, validator, name):
    error = next(validator.iter_errors(document), None)
    if error:
        field = ".".join(map(str, error.absolute_path)) or "<root>"
        raise ValueError(f"{name} ({field}) — {error.message}")


def repository_file(root, reference):
    path = (root / reference).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Недоступная ссылка каталога {reference!r}")
    return path


class ProfileReview:
    def __init__(self, root=ROOT):
        self.root = root
        self.profiles = root / "profiles"
        self.validators = {}
        for name in ("profile", "catalog", "profile-checks", "report", "kicad-drc", "kicad-erc", "kicad-context",
                     "fabrication-stackup", "mechanical-holes"):
            schema_path = root / "schemas" / f"{name}.schema.json"
            schema = load_document(schema_path)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                raise ValueError(f"{schema_path} — {error.message}") from error
            self.validators[name] = Draft202012Validator(schema)
        self.constraint_names = self.validators["profile"].schema["properties"]["constraints"]["properties"]

        self.catalog = self.load_validated(root / "rules" / "catalog.yaml", "catalog")
        self.rules = {}
        for rule in self.catalog["rules"]:
            rule_id = rule["id"]
            if rule_id in self.rules:
                raise ValueError(f"Повторяющийся ID правила {rule_id}")
            repository_file(root, rule["reference"])
            self.rules[rule_id] = rule

        self.policy = self.load_validated(root / "rules" / "profile-checks.yaml", "profile-checks")
        self.features = self.policy["features"]
        self.input_labels = self.policy["inputs"]
        self.rule_details = self.compile_policy()

    def load_validated(self, path, schema):
        document = load_document(path)
        validate_document(document, self.validators[schema], path)
        return document

    def compile_policy(self):
        families = self.policy["families"]
        if set(families) != {rule["category"] for rule in self.rules.values()}:
            raise ValueError("Семейства правил и политика профилей расходятся")
        if any("ids" in family for family in families.values()):
            raise ValueError("Списки ID допустимы только в checks")

        for name, feature in self.features.items():
            self.check_names(feature.get("implies", ()), self.features, name)
            if "reference" in feature:
                repository_file(self.root, feature["reference"])
        for detail in [*families.values(), *self.policy["checks"]]:
            self.check_names(detail.get("inputs", ()), self.input_labels, "inputs")
            self.check_names(detail.get("constraints", ()), self.constraint_names, "constraints")
            for branch in detail.get("when", ()):
                self.check_names(branch, self.features, "when")

        details = {}
        for check in self.policy["checks"]:
            for rule_id in check["ids"]:
                if rule_id in details:
                    raise ValueError(f"Повторяющаяся настройка правила {rule_id}")
                self.check_names([rule_id], self.rules, "checks")
                details[rule_id] = {key: value for key, value in check.items() if key != "ids"}
        compiled = {}
        for rule_id, rule in self.rules.items():
            family = families[rule["category"]]
            check = details.get(rule_id, {})
            inputs = list(dict.fromkeys([*family["inputs"], *check.get("inputs", ())]))
            compiled[rule_id] = family | check | {"inputs": inputs}
        return compiled

    @staticmethod
    def check_names(names, allowed, context):
        unknown = sorted(set(names) - allowed.keys())
        if unknown:
            raise ValueError(f"{context} — неизвестные ключи {', '.join(unknown)}")

    def find_named_profile(self, profile_id, local):
        if not PROFILE_ID.fullmatch(profile_id):
            raise ValueError(f"Некорректный ID профиля {profile_id!r}")
        for folder in dict.fromkeys((local.resolve(), self.profiles.resolve())):
            paths = [folder / f"{profile_id}{ext}" for ext in PROFILE_FORMATS]
            matches = [path for path in paths if path.is_file()]
            if len(matches) > 1:
                raise ValueError(f"Неоднозначный профиль {profile_id!r} в {folder}")
            if matches:
                return matches[0]
        raise ValueError(f"Профиль {profile_id!r} отсутствует в {local} и {self.profiles}")

    def profile_path(self, argument):
        path = Path(argument)
        if PROFILE_ID.fullmatch(argument):
            return self.find_named_profile(argument, self.profiles)
        if path.suffix.lower() not in PROFILE_FORMATS:
            raise ValueError(f"Ожидается ID встроенного профиля или путь к YAML/JSON ({argument})")
        return path

    def load_profile_chain(self, path):
        chain = []
        visited = set()
        expected_id = None
        while path is not None:
            real_path = path.resolve()
            if real_path in visited:
                raise ValueError(f"Цикл наследования профилей через {path}")
            visited.add(real_path)
            profile = self.load_validated(path, "profile")
            name = profile["profile_id"]
            if not PROFILE_ID.fullmatch(name):
                raise ValueError(f"{path} — некорректный profile_id {name!r}")
            if expected_id is not None and expected_id != name:
                raise ValueError(f"{path} — ожидался profile_id {expected_id!r}, найден {name!r}")
            chain.append(profile)
            expected_id = profile["inherits"]
            path = self.find_named_profile(expected_id, path.parent) if expected_id is not None else None
        return list(reversed(chain))

    def resolve_profile(self, chain):
        leaf = chain[-1]
        objects = leaf.get("review_objects", {})
        criteria = leaf.get("criteria", {})
        validate_matrix(objects, criteria, self.rules)
        profile = {
            "feature_tags": set(),
            "absent_feature_tags": set(),
            "constraints": dict.fromkeys(self.constraint_names),
            "constraint_sources": {},
            "inputs": {},
            "notes": [],
            "review_objects": objects,
            "criteria": criteria,
        }
        for layer in chain:
            present = set(layer["feature_tags"])
            absent = set(layer.get("absent_feature_tags", ()))
            if present & absent:
                raise ValueError(f"{layer['profile_id']} — противоречивые признаки {', '.join(sorted(present & absent))}")
            profile["feature_tags"] -= absent
            profile["feature_tags"] |= present
            profile["absent_feature_tags"] -= present
            profile["absent_feature_tags"] |= absent

            sources = layer.get("constraint_sources", {})
            for field, value in layer["constraints"].items():
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError(f"{layer['profile_id']} — {field} должно быть конечным числом")
                profile["constraints"][field] = value
                profile["constraint_sources"].pop(field, None)
            for field, source in sources.items():
                if layer["constraints"].get(field) is None:
                    raise ValueError(f"{layer['profile_id']} — источник {field} требует значения в этом профиле")
                if not all(value.strip() for value in source.values()):
                    raise ValueError(f"{layer['profile_id']} — источник {field} содержит пустые поля")
                profile["constraint_sources"][field] = source
            for field, value in layer.get("inputs", {}).items():
                if value is not None and not value.strip():
                    raise ValueError(f"{layer['profile_id']} — пустое значение inputs.{field}, для TBD укажите null")
                profile["inputs"][field] = value
            profile["notes"].extend(layer.get("notes", ()))

        if any(item["kind"] == "domain_pair" for item in objects.values()):
            profile["feature_tags"].add("isolation-barrier")
        pending = list(profile["feature_tags"])
        while pending:
            implied = set(self.features.get(pending.pop(), {}).get("implies", ()))
            pending.extend(implied - profile["feature_tags"])
            profile["feature_tags"] |= implied
        conflict = profile["feature_tags"] & profile["absent_feature_tags"]
        if conflict:
            raise ValueError(f"Указанные признаки подразумевают исключённые признаки {', '.join(sorted(conflict))}")
        if {"two-layer", "four-layer"} <= profile["feature_tags"]:
            raise ValueError("Признаки two-layer и four-layer несовместимы")

        profile.update(
            profile_id=leaf["profile_id"],
            description=leaf["description"],
            design_revision=leaf.get("design_revision"),
            approved=leaf["approved"],
            inheritance=[layer["profile_id"] for layer in chain],
        )
        return profile

    def profile(self, path):
        return self.resolve_profile(self.load_profile_chain(path))

    def required_data(self, profile, detail):
        missing = {}
        for key in detail["inputs"]:
            if profile["inputs"].get(key) is None:
                missing[f"inputs.{key}"] = self.input_labels[key]
        for key in detail.get("constraints", ()):
            if profile["constraints"][key] is None:
                missing[f"constraints.{key}"] = "Указать проектное значение с единицей из имени поля и документом-источником."
            elif key not in profile["constraint_sources"]:
                missing[f"constraint_sources.{key}"] = "Указать документ, редакцию и область применения значения."
        return missing

    @staticmethod
    def applicability(profile, branches):
        present = profile["feature_tags"]
        absent = profile["absent_feature_tags"]
        if not branches:
            return "selected", "Базовый пункт скрининга. Уточнить применимость к конкретным объектам платы."
        for branch in branches:
            if present.issuperset(branch):
                return "selected", "Подтверждено в профиле: " + ", ".join(branch)
        open_branches = [branch for branch in branches if absent.isdisjoint(branch)]
        if open_branches:
            missing = [", ".join(tag for tag in branch if tag not in present) for branch in open_branches]
            return "unknown", "Уточнить признаки: " + " или ".join(missing)
        excluded = [", ".join(tag for tag in branch if tag in absent) for branch in branches]
        return "excluded", "Профиль исключает каждую ветвь: " + " / ".join(excluded)

    def analyze(self, profile):
        results = []
        input_gaps = defaultdict(list)
        criteria = criterion_plan(profile["review_objects"], profile["criteria"], profile["design_revision"])
        by_rule = defaultdict(list)
        for row in criteria:
            by_rule[row["rule_id"]].append(row)
        for rule_id, rule in self.rules.items():
            detail = self.rule_details[rule_id]
            branches = detail.get("when", [])
            applicability, basis = self.applicability(profile, branches)
            gaps = self.required_data(profile, detail) if applicability == "selected" else {}
            rows = by_rule[rule_id]
            for row in rows:
                row["applicability"] = applicability
                if applicability != "selected":
                    row["status"] = "N_A" if applicability == "excluded" else "REVIEW_REQUIRED"
            if rule_id == "PCB-GEO-002" and applicability == "selected" and not any(
                row["metric"] in {"clearance", "creepage"} for row in rows
            ):
                gaps["review_objects.domain_pair"] = "Указать пары доменов и режимы для отдельных критериев clearance и creepage."
            if applicability == "excluded":
                status = "N_A"
            elif applicability == "unknown" or gaps or any(row["missing_data"] for row in rows):
                status = "REVIEW_REQUIRED"
            else:
                status = "NOT_EVALUATED"
            for field in gaps:
                input_gaps[field].append(rule_id)
            results.append({
                "rule_id": rule_id,
                "category": rule["category"],
                "domain": detail["domain"],
                "severity": rule["severity"],
                "status": status,
                "applicability": applicability,
                "applicability_basis": basis,
                "when": branches,
                "title": rule["title"],
                "reason": detail["reason"],
                "verification_method": rule["verification"],
                "required_evidence": detail["evidence"],
                "remediation": detail.get("correction", rule["title"]),
                "reference": rule["reference"],
                "criteria_source": rule["criteria_source"],
                "required_inputs": detail["inputs"],
                "criterion_ids": [row["criterion_id"] for row in rows if row["criterion_id"] is not None],
                "constraints_used": {
                    key: {
                        "value": profile["constraints"][key],
                        "source": profile["constraint_sources"].get(key),
                    }
                    for key in detail.get("constraints", ())
                },
                "missing_data": gaps,
            })

        security_applicability, security_basis = self.applicability(profile, [["information-security"]])
        security_scope = {
            "applicability": security_applicability,
            "applicability_basis": security_basis,
            "status": {"selected": "REVIEW_REQUIRED", "unknown": "NOT_EVALUATED", "excluded": "N_A"}[security_applicability],
            "input": profile["inputs"].get("information_security_scope"),
            "required_evidence": self.input_labels["information_security_scope"],
            "reference": "references/interference-verification.md",
        }
        if security_applicability == "selected" and security_scope["input"] is None:
            input_gaps["inputs.information_security_scope"] = []
        revision_missing = not (profile["design_revision"] or "").strip()
        feature_guidance = [
            {"feature": tag, "guidance": self.features[tag]["guidance"], "reference": self.features[tag]["reference"]}
            for tag in sorted(profile["feature_tags"] & self.features.keys())
            if "guidance" in self.features[tag]
        ]
        return {
            "report_type": "profile_review_plan",
            "profile": profile | {
                "feature_tags": sorted(profile["feature_tags"]),
                "absent_feature_tags": sorted(profile["absent_feature_tags"]),
            },
            "catalog_version": self.catalog["version"],
            "catalog_status": self.catalog["status"],
            "policy_version": self.policy["version"],
            "overall_status": (
                "REVIEW_REQUIRED"
                if revision_missing or security_scope["status"] == "REVIEW_REQUIRED"
                or any(rule["status"] == "REVIEW_REQUIRED" for rule in results)
                else "NOT_EVALUATED"
            ),
            "status_counts": dict(Counter(rule["status"] for rule in results)),
            "applicability_counts": dict(Counter(rule["applicability"] for rule in results)),
            "design_revision_missing": revision_missing,
            "unmapped_feature_tags": sorted(
                (profile["feature_tags"] | profile["absent_feature_tags"]) - self.features.keys()
            ),
            "feature_guidance": feature_guidance,
            "missing_data": dict(sorted(input_gaps.items())),
            "criterion_plan": criteria,
            "rules": results,
            "information_security": security_scope,
        }

    def validate_report(self, profile, path, artifact_root=None, *, as_of=None):
        if path.suffix.lower() != ".json":
            raise ValueError("Инженерный отчёт должен быть файлом .json")
        report = self.load_validated(path, "report")
        as_of = as_of or date.today()
        waiver_count = validate_report_findings(profile, report, self.analyze(profile), as_of=as_of)
        result = {}
        if "evidence_artifacts" in report:
            result.update(verify_evidence_artifacts(report, artifact_root or path.parent, load_document))
        elif artifact_root is not None:
            raise ValueError("Для --artifact-root в отчёте требуется evidence_artifacts")
        if waiver_count:
            result.update(waivers=waiver_count, as_of=as_of.isoformat())
        return result or None

    def import_drc(self, profile, report_path, context_path, board_path=None):
        if report_path.suffix.lower() != ".json":
            raise ValueError("Вход KiCad DRC должен быть отчётом .json")
        report = self.load_validated(report_path, "kicad-drc")
        context = self.load_validated(context_path, "kicad-context")
        board = None
        if board_path is not None:
            source_name = report["source"].replace("\\", "/").rsplit("/", 1)[-1]
            if board_path.name != source_name:
                raise ValueError("Имя переданной платы расходится с source отчёта KiCad")
            board = read_board_inventory(board_path)
        result = import_drc(profile, report, context, str(report_path), self.rules, board)
        return result | {"context_artifact": str(context_path)}

    def load_criteria_artifact(self, path, schema):
        if path is None:
            return None, None
        if path.suffix.lower() not in PROFILE_FORMATS:
            raise ValueError(f"{path} — файл критериев должен быть в формате YAML или JSON")
        data = path.read_bytes()
        document = load_document(path, content=data.decode("utf-8"))
        validate_document(document, self.validators[schema], path)
        return document, {"source": str(path), "sha256": sha256(data).hexdigest()}

    def compare_stackup(self, board_path, fabrication_path, design_revision):
        fabrication, artifact = self.load_criteria_artifact(fabrication_path, "fabrication-stackup")
        if fabrication is not None:
            validate_fabrication_criteria(fabrication)
        board = read_board_inventory(board_path, stackup_only=True) if board_path is not None else None
        result = compare_fabrication_stackup(board, fabrication, design_revision)
        return result | {"fabrication_artifact": artifact}

    def compare_mounting(self, board_path, mechanical_path, design_revision):
        mechanical, artifact = self.load_criteria_artifact(mechanical_path, "mechanical-holes")
        if mechanical is not None:
            validate_mechanical_criteria(mechanical)
        board = read_board_inventory(board_path) if board_path is not None else None
        result = compare_mechanical_holes(board, mechanical, design_revision)
        return result | {"mechanical_artifact": artifact}

    def compare_trace_width(self, profile, board_path, context_path):
        context, artifact = self.load_criteria_artifact(context_path, "kicad-context")
        board = read_board_inventory(board_path) if board_path is not None else None
        result = compare_trace_width(profile, board, context, self.analyze(profile))
        return result | {"context_artifact": artifact}

    def validate(self, extra_paths):
        paths = self.built_in_paths()
        identifiers = set()
        for path in paths:
            profile = self.profile(path)
            profile_id = profile["profile_id"]
            if path.stem != profile_id or profile_id in identifiers:
                raise ValueError(f"{path} — имя встроенного файла и profile_id должны совпадать и быть уникальными")
            identifiers.add(profile_id)
        for path in extra_paths:
            self.profile(self.profile_path(path))
        for path in sorted((self.root / "examples").glob("review-result*.json")):
            report = self.load_validated(path, "report")
            self.check_names((item["rule_id"] for item in report["findings"]), self.rules, str(path))
        for path in sorted((self.root / "examples").glob("fabrication-stackup*.yaml")):
            validate_fabrication_criteria(self.load_validated(path, "fabrication-stackup"))
        self.check_names(MECHANICAL_RULE_IDS, self.rules, "compare-mounting")
        for path in sorted((self.root / "examples").glob("mechanical-holes*.yaml")):
            validate_mechanical_criteria(self.load_validated(path, "mechanical-holes"))
        return len(paths)

    def built_in_paths(self):
        return sorted(
            path for path in self.profiles.iterdir()
            if path.is_file() and path.suffix in PROFILE_FORMATS
        )


def markdown(report):
    profile = report["profile"]
    revision = "TBD" if report["design_revision_missing"] else profile["design_revision"]
    lines = [
        f"# План ревью PCB · {profile['profile_id']}",
        "",
        profile["description"],
        "",
        f"- Статус плана — `{report['overall_status']}`",
        f"- Ревизия платы — {revision}",
        f"- Каталог — {report['catalog_version']} ({report['catalog_status']})",
        f"- Политика отбора — {report['policy_version']}",
        f"- Наследование — {' → '.join(profile['inheritance'])}",
        f"- Признаки — {', '.join(profile['feature_tags']) or 'TBD'}",
        f"- Явно исключённые признаки — {', '.join(profile['absent_feature_tags']) or 'нет'}",
        "",
        "Результат основан на декларациях профиля. Проверка схемы, платы, расчётов и испытаний выполняется отдельно.",
        "",
        "## Ограничения из профиля",
        "",
        "| Поле | Значение | Документ, редакция и область применения |",
        "|---|---|---|",
    ]
    for key, value in profile["constraints"].items():
        source = profile["constraint_sources"].get(key)
        details = " · ".join(source[name] for name in ("document", "revision", "scope")) if source else "TBD"
        safe_details = " ".join(details.splitlines()).replace("|", "&#124;")
        lines.append(f"| `{key}` | {value if value is not None else 'TBD'} | {safe_details} |")

    if report["unmapped_feature_tags"]:
        lines.extend(["", "Признаки без привязки к политике — " + ", ".join(report["unmapped_feature_tags"]) + "."])
    if profile["notes"]:
        lines.extend(["", "## Заметки профиля", "", *(f"- {note}" for note in profile["notes"])])
    if report["feature_guidance"]:
        lines.extend(["", "## Особенности профиля", ""])
        for item in report["feature_guidance"]:
            lines.append(f"- `{item['feature']}` — {item['guidance']} `{item['reference']}`")

    if report["criterion_plan"]:
        lines.extend(["", "## Критерии по объектам и режимам", ""])
        for row in report["criterion_plan"]:
            name = row["criterion_id"] or row["metric"]
            value = row.get("requirement") or "TBD"
            if row.get("limit") is not None:
                prefix = {"at_least": "≥ ", "at_most": "≤ "}.get(row["comparison"], "")
                value = f"{prefix}{row['limit']} {row['unit']}"
            if row.get("tolerance") is not None:
                value += f" ± {row['tolerance']} {row['unit']}"
            source = row.get("source")
            detail = " · ".join(source[key] for key in ("document", "revision", "section")) if source else ""
            gaps = " ".join(row["missing_data"].values())
            lines.append(
                f"- `{row['object_id']}` / `{row['mode']}` / `{name}` · "
                f"`{row['rule_id']}` · `{row['status']}` · {value}. "
                f"{' '.join(part for part in (detail, gaps) if part)}".rstrip()
            )
            if confirmation := row.get("confirmation"):
                details = [confirmation.get(field) or "TBD" for field in ("owner", "confirmed_on", "design_revision")]
                if confirmation.get("reason"):
                    details.append(confirmation["reason"])
                lines.append(f"  - Подтверждение — {' · '.join(details)}")
            for alternative in row.get("alternative_sources", ()):
                source = alternative["source"]
                detail = " · ".join(source[key] for key in ("document", "revision", "section"))
                value = f"{alternative['limit']} {row['unit']}"
                if "tolerance" in alternative:
                    value += f" ± {alternative['tolerance']} {row['unit']}"
                lines.append(f"  - Другой источник — {detail} · {value}")
            if row.get("source_conflict") == "resolved":
                lines.append("  - Расхождение источников рассмотрено при подтверждении критерия.")

    plans = [rule for rule in report["rules"] if rule["applicability"] == "selected"]
    gap_labels = {key: label for rule in plans for key, label in rule["missing_data"].items()}
    security = report["information_security"]
    if "inputs.information_security_scope" in report["missing_data"]:
        gap_labels["inputs.information_security_scope"] = security["required_evidence"]
    lines.extend(["", "## Недостающие данные", ""])
    if report["design_revision_missing"]:
        lines.append("- `design_revision` — указать проверяемую ревизию платы.")
    for key, rules in report["missing_data"].items():
        affected = f" (правил — {len(rules)})" if rules else ""
        lines.append(f"- `{key}` — {gap_labels[key]}{affected}")
    if not report["design_revision_missing"] and not report["missing_data"]:
        lines.append("Для выбранных пунктов указаны запрашиваемые данные.")

    lines.extend(["", "## Выбранные пункты ревью", ""])
    status_counts = report["status_counts"]
    counts = " · ".join(f"{status} — {count}" for status, count in status_counts.items())
    lines.extend([f"Правил в каталоге — {len(report['rules'])}. {counts}.", ""])
    for domain, label in DOMAINS.items():
        group = [rule for rule in plans if rule["domain"] == domain]
        if not group:
            continue
        lines.extend([f"### {label}", ""])
        for rule in sorted(group, key=lambda item: SEVERITIES.index(item["severity"])):
            action = rule["title"]
            if rule["remediation"] != action:
                action += " " + rule["remediation"]
            lines.append(
                f"- `{rule['rule_id']}` · {rule['severity']} · {rule['status']} — {action} "
                f"{rule['reason']} **Проверка** — {VERIFICATION[rule['verification_method']]}. "
                f"{rule['required_evidence']} {rule['applicability_basis']} `{rule['reference']}`"
            )

    for state, heading in (
        ("unknown", "Применимость требует уточнения"),
        ("excluded", "Исключено по признакам профиля"),
    ):
        grouped = defaultdict(list)
        for rule in report["rules"]:
            if rule["applicability"] == state:
                grouped[rule["applicability_basis"]].append(rule["rule_id"])
        if grouped:
            lines.extend(["", f"## {heading}", ""])
            for basis, identifiers in grouped.items():
                lines.append(f"- {basis} — " + ", ".join(f"`{name}`" for name in identifiers) + ".")

    lines.extend([
        "",
        "## Информационная безопасность и ПЭМИН",
        "",
        f"`{security['status']}`. {security['applicability_basis']}. "
        f"Для отдельной оценки — {security['required_evidence']}. `{security['reference']}`",
    ])
    return "\n".join(lines) + "\n"


def emit(content, output):
    if output is None:
        print(content, end="")
    else:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(content)


def as_json(document):
    return json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def initialize(review, args):
    if not PROFILE_ID.fullmatch(args.profile_id):
        raise ValueError(f"Некорректный ID профиля {args.profile_id!r}")
    if args.output is not None and args.output.suffix.lower() not in PROFILE_FORMATS:
        raise ValueError("Для профиля укажите файл .yaml, .yml или .json")
    parent = review.find_named_profile(args.base, args.output.parent if args.output else review.profiles)
    chain = review.load_profile_chain(parent)
    if any(layer["profile_id"] == args.profile_id for layer in chain):
        raise ValueError("ID нового профиля должен отличаться от ID его предков")
    review.check_names(args.feature + args.absent_feature, review.features, "init")
    document = {
        "profile_id": args.profile_id,
        "description": args.description,
        "inherits": args.base,
        "approved": False,
        "design_revision": None,
        "feature_tags": args.feature,
        "absent_feature_tags": args.absent_feature,
        "constraints": {},
        "constraint_sources": {},
        "review_objects": {},
        "criteria": {},
        "inputs": {},
    }
    validate_document(document, review.validators["profile"], args.profile_id)
    plan = review.analyze(review.resolve_profile([*chain, document]))
    document["constraints"] = {
        key: None for key, value in plan["profile"]["constraints"].items() if value is None
    }
    document["inputs"] = {
        key.removeprefix("inputs."): None
        for key in plan["missing_data"]
        if key.startswith("inputs.")
    }
    content = (
        as_json(document) if args.output and args.output.suffix.lower() == ".json"
        else yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    )
    emit(content, args.output)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Показать стартовые профили")
    listing.add_argument("--fields", action="store_true", help="Показать известные признаки и исходные данные")
    init = commands.add_parser("init", help="Создать редактируемый профиль")
    init.add_argument("profile_id")
    init.add_argument("--from", dest="base", required=True, help="ID базового профиля")
    init.add_argument("--description", required=True)
    init.add_argument("--feature", action="append", default=[], help="Присутствующий признак, можно повторять")
    init.add_argument("--absent-feature", action="append", default=[], help="Исключённый признак, можно повторять")
    init.add_argument("--output", type=Path, help="Новый файл YAML/JSON, по умолчанию stdout")
    analysis = commands.add_parser("analyze", help="Сформировать план ревью по профилю")
    analysis.add_argument("profile", help="Встроенный ID либо путь к YAML/JSON")
    analysis.add_argument("--format", choices=("markdown", "json"), help="Формат по расширению файла или Markdown")
    analysis.add_argument("--output", type=Path, help="Новый файл отчёта, по умолчанию stdout")
    validation = commands.add_parser("validate", help="Проверить схемы, каталог, профили и примеры отчётов")
    validation.add_argument("profiles", nargs="*", help="Дополнительные ID или пути проектов")
    result = commands.add_parser("validate-report", help="Сверить инженерный отчёт с профилем и критериями")
    result.add_argument("profile", help="ID либо путь к профилю")
    result.add_argument("report", type=Path, help="Инженерный отчёт JSON")
    result.add_argument("--artifact-root", type=Path, help="Каталог артефактов, по умолчанию каталог отчёта")
    result.add_argument("--as-of", help="Дата проверки срока исключений YYYY-MM-DD, по умолчанию сегодня")
    drc = commands.add_parser("import-drc", help="Импортировать готовый JSON-отчёт KiCad DRC")
    drc.add_argument("profile", help="ID либо путь к профилю")
    drc.add_argument("report", type=Path, help="Отчёт KiCad DRC JSON")
    drc.add_argument("--context", type=Path, required=True, help="Ревизия платы и контекст объектов YAML/JSON")
    drc.add_argument("--board", type=Path, help="Плата .kicad_pcb для привязки замечаний по UUID")
    drc.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    inventory = commands.add_parser("inventory-board", help="Прочитать объекты платы KiCad по UUID")
    inventory.add_argument("board", type=Path, help="Плата .kicad_pcb")
    inventory.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    stackup = commands.add_parser("inventory-stackup", help="Прочитать физический стек, записанный в KiCad PCB")
    stackup.add_argument("board", type=Path, help="Плата .kicad_pcb")
    stackup.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    fabrication = commands.add_parser("compare-stackup", help="Сверить стек KiCad с листом изготовителя")
    fabrication.add_argument("--board", type=Path, help="Плата .kicad_pcb")
    fabrication.add_argument("--fabrication", type=Path, help="Критерии изготовителя в YAML или JSON")
    fabrication.add_argument("--design-revision", help="Ревизия платы, к которой относится лист")
    fabrication.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    mounting = commands.add_parser("compare-mounting", help="Сверить монтажные отверстия KiCad с чертежом")
    mounting.add_argument("--board", type=Path, help="Плата .kicad_pcb")
    mounting.add_argument("--mechanical", type=Path, help="Критерии чертежа в YAML или JSON")
    mounting.add_argument("--design-revision", help="Ревизия платы, к которой относится чертёж")
    mounting.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    width = commands.add_parser("compare-trace-width", help="Сверить ширину дорожек по UUID с критериями профиля")
    width.add_argument("profile", help="ID либо путь к профилю")
    width.add_argument("--board", type=Path, help="Плата .kicad_pcb")
    width.add_argument("--context", type=Path, help="Ревизия, режим и UUID дорожек в YAML или JSON")
    width.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    checks = commands.add_parser("run-checks", help="Запустить ERC и DRC на временной копии проекта")
    checks.add_argument("--schematic", type=Path, help="Корневой файл .kicad_sch")
    checks.add_argument("--board", type=Path, help="Файл .kicad_pcb")
    checks.add_argument("--project-dir", type=Path, help="Общий корень проекта и локальных зависимостей")
    checks.add_argument("--design-revision", help="Ревизия входных файлов")
    checks.add_argument("--output-dir", type=Path, required=True, help="Новый каталог для отчётов")
    checks.add_argument("--kicad-cli", default="kicad-cli", help="Имя или путь к установленному KiCad CLI")
    checks.add_argument("--timeout", type=int, default=120, help="Лимит секунд на каждый вызов KiCad")
    checks.add_argument("--refill-zones", action="store_true", help="Пересчитать заливку в памяти при DRC")
    probe = commands.add_parser("probe-kicad", help="Проверить доступность команд KiCad CLI и Python API")
    probe.add_argument("--kicad-cli", default="kicad-cli", help="Имя или путь к установленному KiCad CLI")
    probe.add_argument("--timeout", type=int, default=10, help="Лимит секунд на каждый запрос к KiCad CLI")
    probe.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    parity = commands.add_parser("compare-netlist", help="Сверить выводы схемы и PCB по XML-нетлисту")
    parity.add_argument("--board", type=Path, help="Плата .kicad_pcb")
    parity.add_argument("--netlist", type=Path, help="XML-нетлист из KiCad")
    parity.add_argument("--design-revision", help="Общая ревизия схемы и платы")
    parity.add_argument("--output", type=Path, help="Новый файл JSON, по умолчанию stdout")
    return parser


def main():
    parser = arguments()
    args = parser.parse_args()
    try:
        review = ProfileReview()
        if args.command == "list":
            for path in review.built_in_paths():
                profile = review.profile(path)
                print(f"{profile['profile_id']} — {profile['description']}")
            if args.fields:
                print("\nПризнаки")
                for key, details in review.features.items():
                    print(f"  {key} — {details['label']}")
                print("\nИсходные данные")
                for key, label in review.input_labels.items():
                    print(f"  {key} — {label}")
        elif args.command == "init":
            initialize(review, args)
        elif args.command == "analyze":
            output_format = args.format or "markdown"
            if args.output:
                inferred = REPORT_FORMATS.get(args.output.suffix.lower())
                if not inferred or (args.format and args.format != inferred):
                    raise ValueError("Используйте расширение .md, .markdown или .json по формату отчёта")
                output_format = inferred
            report = review.analyze(review.profile(review.profile_path(args.profile)))
            emit(as_json(report) if output_format == "json" else markdown(report), args.output)
        elif args.command == "validate":
            count = review.validate(args.profiles)
            print(f"Проверены схемы, {len(review.rules)} ID правил, ссылки каталога и примеры отчётов при наличии.")
            print(f"Встроенные профили — {count}. Дополнительные профили — {len(args.profiles)}.")
        elif args.command == "validate-report":
            profile = review.profile(review.profile_path(args.profile))
            as_of = parse_calendar_date(args.as_of) if args.as_of is not None else None
            verified = review.validate_report(profile, args.report, args.artifact_root, as_of=as_of) or {}
            print("Отчёт согласован со схемой, профилем и заявленными результатами.")
            if "artifacts" in verified:
                print(f"Проверено артефактов по SHA-256 — {verified['artifacts']}. "
                      f"Проверено указателей JSON — {verified['references']}.")
            if "waivers" in verified:
                print(f"Проверено исключений — {verified['waivers']}. Дата проверки — {verified['as_of']}.")
        elif args.command == "import-drc":
            if args.output and args.output.suffix.lower() != ".json":
                raise ValueError("Для импорта DRC укажите выходной файл .json")
            profile = review.profile(review.profile_path(args.profile))
            emit(as_json(review.import_drc(profile, args.report, args.context, args.board)), args.output)
        elif args.command in {"inventory-board", "inventory-stackup"}:
            if args.output and args.output.suffix.lower() != ".json":
                raise ValueError("Для инвентаря укажите выходной файл .json")
            emit(as_json(read_board_inventory(args.board, stackup_only=args.command == "inventory-stackup")),
                 args.output)
        elif args.command == "compare-netlist":
            if args.output and args.output.suffix.lower() != ".json":
                raise ValueError("Для сверки нетлиста укажите выходной файл .json")
            netlist = read_kicad_netlist(args.netlist) if args.netlist is not None else None
            board = read_board_inventory(args.board) if args.board is not None else None
            emit(as_json(compare_netlist(board, netlist, args.design_revision)), args.output)
        elif args.command == "compare-stackup":
            if args.output and args.output.suffix.lower() != ".json":
                raise ValueError("Для сверки стека укажите выходной файл .json")
            emit(as_json(review.compare_stackup(args.board, args.fabrication, args.design_revision)), args.output)
        elif args.command == "compare-mounting":
            if args.output and args.output.suffix.lower() != ".json":
                raise ValueError("Для сверки отверстий укажите выходной файл .json")
            emit(as_json(review.compare_mounting(args.board, args.mechanical, args.design_revision)), args.output)
        elif args.command == "compare-trace-width":
            if args.output and args.output.suffix.lower() != ".json":
                raise ValueError("Для сверки ширины дорожек укажите выходной файл .json")
            profile = review.profile(review.profile_path(args.profile))
            emit(as_json(review.compare_trace_width(profile, args.board, args.context)), args.output)
        elif args.command == "probe-kicad":
            if args.timeout <= 0:
                raise ValueError("--timeout должен быть положительным")
            if args.output and args.output.suffix.lower() != ".json":
                raise ValueError("Для диагностики KiCad укажите выходной файл .json")
            emit(as_json(probe_kicad(args.kicad_cli, args.timeout)), args.output)
        elif args.command == "run-checks":
            if args.timeout <= 0:
                raise ValueError("--timeout должен быть положительным")
            if args.refill_zones and args.board is None:
                raise ValueError("--refill-zones требует --board")
            reports = run_kicad_checks(
                {"erc": args.schematic, "drc": args.board}, args.project_dir, args.output_dir,
                args.design_revision, args.kicad_cli, args.timeout, args.refill_zones, review.load_validated,
            )
            summary = args.output_dir / "summary.json"
            emit(as_json(reports), summary)
            print(f"{reports['overall_status']} — {summary}")
    except (ValueError, OSError) as error:
        parser.exit(2, f"Ошибка — {error}\n")


if __name__ == "__main__":
    main()
