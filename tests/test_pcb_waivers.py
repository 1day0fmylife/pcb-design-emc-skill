import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from hashlib import sha256
from pathlib import Path

from scripts.pcb_profile import ROOT, ProfileReview


QA = ROOT / "examples/kicad-multinetclasses/profile.yaml"
RELAY = ROOT / "examples/relay-review/profile.yaml"
EXPIRY = date(2026, 11, 30)


class WaiverValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "review.json"
        self.profile = self.review.profile(QA)
        self.waiver = {
            "id": "QA-W-01",
            "owner": "Владелец учебного примера",
            "reason": "Фиксирует намеренное нарушение в тесте импорта.",
            "design_revision": self.profile["design_revision"],
            "expires_on": EXPIRY.isoformat(),
        }
        self.finding = {
            "rule_id": "PCB-DFM-001",
            "object_id": "DEFAULT",
            "criterion_id": "RULE-3-MAX",
            "status": "FAIL",
            "evidence": "Учебный пример превышения верхней границы.",
            "evidence_ref": {"artifact": "DRC", "locator": "/violations/0"},
            "verification": "Сверка ширины дорожки с ограничением rule_3.",
            "measurement": {"value": 1.016, "unit": "mm"},
            "waiver": self.waiver,
        }
        self.report = {
            "profile_id": self.profile["profile_id"], "design_revision": self.profile["design_revision"],
            "overall_status": "FAIL", "scope_criteria": ["RULE-3-MAX"], "findings": [self.finding],
        }

    def validate(self, as_of=EXPIRY):
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        return self.review.validate_report(self.profile, self.path, as_of=as_of)

    def test_valid_waiver_keeps_fail_and_verifies_registered_evidence(self):
        evidence = b'{"violations":[{"width_mm":1.016}]}'
        (self.path.parent / "drc.json").write_bytes(evidence)
        self.report["evidence_artifacts"] = {"DRC": {"path": "drc.json", "sha256": sha256(evidence).hexdigest()}}
        self.assertEqual(self.validate(), {"waivers": 1, "as_of": "2026-11-30", "artifacts": 1, "references": 1})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["overall_status"], "FAIL")
        self.report["overall_status"] = "PASS"
        with self.assertRaisesRegex(ValueError, "FAIL"):
            self.validate()

    def test_calendar_date_and_revision_must_match(self):
        with self.assertRaisesRegex(ValueError, "срок исключения истёк"):
            self.validate(date(2026, 12, 1))
        self.waiver["expires_on"] = "2026-02-29"
        with self.assertRaisesRegex(ValueError, "календарная дата YYYY-MM-DD"):
            self.validate()
        self.waiver["expires_on"] = EXPIRY.isoformat()
        self.waiver["design_revision"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "ревизия исключения расходится"):
            self.validate()

    def test_waiver_needs_confirmed_failure_owner_and_reason(self):
        for key in ("owner", "reason"):
            with self.subTest(key=key):
                saved = self.waiver.pop(key)
                with self.assertRaisesRegex(ValueError, key):
                    self.validate()
                self.waiver[key] = saved
        self.report["overall_status"] = "REVIEW_REQUIRED"
        self.finding["status"] = "REVIEW_REQUIRED"
        with self.assertRaisesRegex(ValueError, "FAIL"):
            self.validate()
        self.report["overall_status"] = self.finding["status"] = "FAIL"
        self.finding["measurement"]["value"] = 0.127
        with self.assertRaisesRegex(ValueError, "статус не соответствует"):
            self.validate()

    def test_waiver_id_is_unique_in_report(self):
        self.report["findings"].append(deepcopy(self.finding))
        with self.assertRaisesRegex(ValueError, "повтор ID исключения QA-W-01"):
            self.validate()

    def test_safety_check_uses_the_policy_domain(self):
        chain = self.review.load_profile_chain(RELAY)
        # Учебное число проверяет ветвление валидатора.
        chain[-1]["criteria"]["CTRL-CLEARANCE"].update(
            limit=2, source={"document": "Test data", "revision": "1", "section": "clearance"}
        )
        self.profile = self.review.resolve_profile(chain)
        self.report.update(profile_id=self.profile["profile_id"], design_revision=self.profile["design_revision"],
                           scope_criteria=["CTRL-CLEARANCE"])
        self.waiver["design_revision"] = self.profile["design_revision"]
        self.finding.update(rule_id="PCB-GEO-002", criterion_id="CTRL-CLEARANCE", object_id="CTRL-CONTACT")
        with self.assertRaisesRegex(ValueError, "исключения по электробезопасности запрещены"):
            self.validate()

    def test_cli_checks_reference_date_and_rejects_expired_waiver(self):
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        command = [sys.executable, "-B", str(ROOT / "scripts/pcb_profile.py"), "validate-report",
                   str(QA), str(self.path), "--as-of"]
        result = subprocess.run([*command, EXPIRY.isoformat()], capture_output=True, text=True, check=True, timeout=30)
        self.assertIn("Проверено исключений — 1. Дата проверки — 2026-11-30.", result.stdout)
        result = subprocess.run([*command, "2026-12-01"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("срок исключения истёк", result.stderr)
