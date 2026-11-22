import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from scripts.pcb_profile import ROOT


RELAY = ROOT / "examples/relay-review"
QA_PROFILE = ROOT / "examples/kicad-multinetclasses/profile.yaml"
COMMAND = [sys.executable, "-B", str(ROOT / "scripts/pcb_profile.py"), "export-report"]


def export(profile, report, *args, check=True):
    return subprocess.run([*COMMAND, str(profile), str(report), *args],
                          capture_output=True, text=True, check=check, timeout=30)


class EngineeringReportExportTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)

    def test_export_example_in_three_formats(self):
        profile, source = RELAY / "profile.yaml", RELAY / "review.json"
        report = json.loads(source.read_text(encoding="utf-8"))
        markdown = export(profile, source).stdout
        self.assertIn("Итог — `REVIEW_REQUIRED`", markdown)
        self.assertIn("Запись в артефакте — DRC · /violations/0", markdown)
        self.assertIn("Критерий — SIGNAL-GAP", markdown)
        self.assertEqual(json.loads(export(profile, source, "--format", "json").stdout), report)

        path = self.directory / "report.sarif"
        export(profile, source, "--output", str(path))
        sarif = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(sarif["version"], "2.1.0")
        run, = sarif["runs"]
        result, = run["results"]
        self.assertEqual(run["tool"]["driver"]["rules"][result["ruleIndex"]]["id"], "PCB-GEO-001")
        self.assertEqual(run["properties"]["validation"], {"artifacts": 1, "references": 1})
        self.assertEqual((result["kind"], result["level"]), ("review", "none"))
        self.assertEqual(result["properties"]["pcbFinding"], report["findings"][0])
        self.assertEqual(result["properties"]["criterion"]["object_id"], "SIGNAL")
        related, = result["relatedLocations"]
        artifact = run["artifacts"][related["physicalLocation"]["artifactLocation"]["index"]]
        self.assertEqual(artifact["hashes"]["sha-256"], report["evidence_artifacts"]["DRC"]["sha256"])
        self.assertEqual(related["message"]["text"], "/violations/0")
        self.assertNotIn("locations", result)

    def test_export_requires_valid_artifacts_and_refuses_overwrite(self):
        profile = RELAY / "profile.yaml"
        source = self.directory / "review.json"
        source.write_bytes((RELAY / "review.json").read_bytes())
        artifact = self.directory / "drc.json"
        artifact.write_bytes((RELAY / "drc.json").read_bytes() + b"\n")
        out = self.directory / "review.sarif"
        result = export(profile, source, "--output", str(out), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("SHA-256 не совпадает", result.stderr)
        self.assertFalse(out.exists())
        export(profile, source, "--artifact-root", str(RELAY), "--output", str(out))
        original = out.read_bytes()
        result = export(profile, source, "--artifact-root", str(RELAY), "--output", str(out), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_bytes(), original)

    def test_status_priority_waiver_and_uri_survive_export(self):
        artifact = self.directory / "DRC #1.json"
        artifact.write_bytes(b'{"violations":[{"width_mm":1.016},{"width_mm":0.127}]}')
        measurement = {
            "rule_id": "PCB-DFM-001", "criterion_id": "RULE-3-MAX", "object_id": "DEFAULT",
            "status": "FAIL", "evidence": "Тест [ссылки](https://test.invalid) <фрагмент>",
            "verification": "Тестовая сверка rule_3", "measurement": {"value": 1.016, "unit": "mm"},
            "evidence_ref": {"artifact": "DRC", "locator": "/violations/0"},
            "priority": "P2", "confidence": "Учебная проверка", "severity": "WARNING",
        }
        waiver = {"id": "TEST-W", "owner": "Владелец учебного отчёта", "reason": "Сценарий экспорта",
                  "design_revision": "KICAD-QA-10.0.8", "expires_on": "2026-11-30"}
        reports = [
            measurement | {"waiver": waiver},
            measurement | {"status": "PASS", "measurement": {"value": 0.127, "unit": "mm"},
                           "evidence_ref": {"artifact": "DRC", "locator": "/violations/1"}},
            *[{"rule_id": "PCB-DFM-001", "status": status, "evidence": "Учебная запись"}
              for status in ("REVIEW_REQUIRED", "NOT_EVALUATED", "N_A")],
        ]
        report = {
            "profile_id": "kicad-multinetclasses", "design_revision": "KICAD-QA-10.0.8",
            "overall_status": "FAIL", "scope_criteria": ["RULE-3-MAX"], "findings": reports,
            "evidence_artifacts": {"DRC": {"path": artifact.name, "sha256": sha256(artifact.read_bytes()).hexdigest()}},
        }
        path = self.directory / "review.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        args = ("--as-of", "2026-11-30")
        run, = json.loads(export(QA_PROFILE, path, *args, "--format", "sarif").stdout)["runs"]
        self.assertEqual(run["artifacts"][0]["location"]["uri"], "DRC%20%231.json")
        self.assertEqual(run["properties"]["overall_status"], "FAIL")
        self.assertEqual([(row["kind"], row["level"]) for row in run["results"]],
                         [("fail", "warning"), ("pass", "none"), ("review", "none"),
                          ("review", "none"), ("notApplicable", "none")])
        failed = run["results"][0]
        self.assertNotIn("suppressions", failed)
        self.assertEqual(failed["properties"]["pcbFinding"]["priority"], "P2")
        self.assertEqual(failed["properties"]["pcbFinding"]["waiver"], waiver)
        self.assertEqual(failed["properties"]["criterion"]["limit"], 0.127)
        markdown = export(QA_PROFILE, path, *args).stdout
        self.assertIn("Приоритет — P2", markdown)
        self.assertIn("Уверенность — Учебная проверка", markdown)
        self.assertIn("Исключение — ID — TEST-W", markdown)
        self.assertIn(r"\[ссылки\]", markdown)
        self.assertIn("&lt;фрагмент&gt;", markdown)
        expired = export(QA_PROFILE, path, "--as-of", "2026-12-01", check=False)
        self.assertEqual(expired.returncode, 2)
        self.assertIn("срок исключения истёк", expired.stderr)
