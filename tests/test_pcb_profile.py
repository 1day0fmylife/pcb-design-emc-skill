import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.pcb_profile import ProfileReview, ROOT, load_document, markdown


def profile(profile_id, **overrides):
    return {
        "profile_id": profile_id,
        "description": "Учебный профиль",
        "inherits": None,
        "approved": False,
        "feature_tags": [],
        "constraints": {},
    } | overrides


class ProfileReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()

    def evaluate(self, *layers):
        report = self.review.analyze(self.review.resolve_profile(layers))
        return report, {rule["rule_id"]: rule for rule in report["rules"]}

    def test_templates_route_relay_power_and_high_speed_independently(self):
        for name, chosen, unknown in [
            ("relay-controller", "PCB-RELAY-001", "PCB-RELAY-006"),
            ("isolated-power", "PCB-GEO-002", "PCB-RELAY-001"),
            ("industrial-24v", "PCB-PWR-005", "PCB-IMM-002"),
            ("fpga-10g", "PCB-HS-001", "PCB-RELAY-001"),
        ]:
            with self.subTest(name=name):
                layers = self.review.load_profile_chain(ROOT / "profiles" / f"{name}.yaml")
                report, rules = self.evaluate(*layers)
                self.assertEqual(rules[chosen]["applicability"], "selected")
                self.assertEqual(rules[unknown]["applicability"], "unknown")
                self.assertEqual(rules[unknown]["status"], "REVIEW_REQUIRED")
                self.assertTrue(set(report["status_counts"]) <= {"REVIEW_REQUIRED", "N_A", "NOT_EVALUATED"})
        self.assertEqual(self.review.validate([]), 6)
        for name, layout in [("generic-2layer", "two-layer"), ("generic-4layer", "four-layer")]:
            resolved = self.review.profile(self.review.profile_path(name))
            report = self.review.analyze(resolved)
            self.assertEqual([entry["feature"] for entry in report["feature_guidance"]], [layout])

    def test_safety_alternatives_require_explicit_absence_to_exclude(self):
        relay = profile("relay", feature_tags=["relay", "low-voltage"])
        _, rules = self.evaluate(relay)
        self.assertEqual(rules["PCB-RELAY-001"]["applicability"], "selected")
        self.assertEqual(rules["PCB-RELAY-006"]["applicability"], "unknown")
        _, rules = self.evaluate(relay | {"absent_feature_tags": ["hazardous-voltage"]})
        self.assertEqual(rules["PCB-RELAY-006"]["status"], "N_A")
        self.assertEqual(rules["PCB-GEO-002"]["status"], "REVIEW_REQUIRED")
        _, rules = self.evaluate(relay | {"absent_feature_tags": ["hazardous-voltage", "isolation-barrier"]})
        self.assertEqual(rules["PCB-GEO-002"]["status"], "N_A")

        _, rules = self.evaluate(relay | {"feature_tags": ["relay", "mains"]})
        self.assertEqual(rules["PCB-RELAY-006"]["domain"], "electrical_safety")
        self.assertIn("inputs.insulation_matrix", rules["PCB-RELAY-006"]["missing_data"])

    def test_inputs_with_sources_still_require_actual_verification(self):
        field = "min_copper_gap_mm"
        source = {"document": "FIXTURE", "revision": "C", "scope": "Учебная пара сетей"}
        parent = profile(
            "parent",
            inputs={"pcb": "fixture", "fabrication": "fixture"},
            constraints={field: 0.2},
            constraint_sources={field: source},
            approved=True,
        )
        report, rules = self.evaluate(parent, profile("child"))
        self.assertFalse(report["profile"]["approved"])
        self.assertEqual(rules["PCB-GEO-001"]["status"], "NOT_EVALUATED")
        self.assertEqual(rules["PCB-GEO-001"]["constraints_used"][field]["source"], source)

        _, rules = self.evaluate(parent, profile("child", constraints={field: 0.2}))
        self.assertIn(f"constraint_sources.{field}", rules["PCB-GEO-001"]["missing_data"])
        _, rules = self.evaluate(parent, profile("child", constraints={field: None}, inputs={"pcb": None}))
        self.assertIn(f"constraints.{field}", rules["PCB-GEO-001"]["missing_data"])
        self.assertIn("inputs.pcb", rules["PCB-GEO-001"]["missing_data"])

    def test_profile_rejects_contradictions_and_nonfinite_values(self):
        with self.assertRaisesRegex(ValueError, "исключённые признаки"):
            self.evaluate(profile("test", feature_tags=["mains"], absent_feature_tags=["hazardous-voltage"]))
        with self.assertRaisesRegex(ValueError, "несовместимы"):
            self.evaluate(profile("test", feature_tags=["two-layer", "four-layer"]))
        with self.assertRaisesRegex(ValueError, "конечным"):
            self.evaluate(profile("test", constraints={"min_copper_gap_mm": float("inf")}))

    def test_inheritance_cycle_and_duplicate_document_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cycle.json"
            path.write_text(json.dumps(profile("cycle", inherits="cycle")), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Цикл"):
                self.review.profile(path)

            for name, content in [("duplicate.yaml", "approved: true\napproved: false\n"),
                                  ("duplicate.json", '{"approved": true, "approved": false}')]:
                with self.subTest(name=name):
                    path = Path(folder) / name
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Повторяющийся ключ"):
                        load_document(path)

    def test_catalog_rejects_duplicate_ids_unknown_policy_ids_and_missing_references(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            for directory in ("rules", "schemas", "references"):
                shutil.copytree(ROOT / directory, target / directory)
            policy = target / "rules" / "profile-checks.yaml"
            original = policy.read_text(encoding="utf-8")
            policy.write_text(original.replace("PCB-GEO-001", "PCB-GEO-999"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PCB-GEO-999"):
                ProfileReview(target)
            policy.write_text(original, encoding="utf-8")
            catalog = target / "rules" / "catalog.yaml"
            original = catalog.read_text(encoding="utf-8")
            catalog.write_text(original.replace("id: PCB-GEO-003", "id: PCB-GEO-001"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Повторяющийся ID"):
                ProfileReview(target)
            catalog.write_text(original, encoding="utf-8")
            (target / "references" / "layout-power.md").unlink()
            with self.assertRaisesRegex(ValueError, "Недоступная ссылка"):
                ProfileReview(target)

    def test_security_scope_has_separate_output_and_input(self):
        report, _ = self.evaluate(profile("info", feature_tags=["information-security"]))
        self.assertEqual(report["information_security"]["status"], "REVIEW_REQUIRED")
        self.assertIn("inputs.information_security_scope", report["missing_data"])
        self.assertIn("## Информационная безопасность и ПЭМИН", markdown(report))

    def test_cli_creates_valid_profile_and_refuses_to_overwrite(self):
        command = [sys.executable, str(ROOT / "scripts" / "pcb_profile.py")]
        with tempfile.TemporaryDirectory() as folder:
            base = ["init", "demo", "--from", "industrial-24v", "--description", "Учебная плата",
                    "--feature", "relay", "--absent-feature", "hazardous-voltage", "--output", "demo.json"]
            subprocess.run([*command, *base], cwd=folder, check=True, capture_output=True)
            original = (Path(folder) / "demo.json").read_bytes()
            result = subprocess.run(
                [*command, "analyze", "demo.json", "--format", "json"],
                cwd=folder, check=True, capture_output=True,
            )
            report = json.loads(result.stdout)
            rules = {item["rule_id"]: item for item in report["rules"]}
            self.assertEqual(rules["PCB-RELAY-006"]["status"], "N_A")
            self.assertIn("relay_coil", report["profile"]["inputs"])
            conflict = subprocess.run([*command, *base], cwd=folder, capture_output=True)
            self.assertEqual(conflict.returncode, 2)
            self.assertEqual((Path(folder) / "demo.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
