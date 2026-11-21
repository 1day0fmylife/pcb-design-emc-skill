import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from scripts.pcb_profile import ROOT, ProfileReview


FIXTURE = ROOT / "examples/relay-review"


class ReportEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()
        cls.profile = cls.review.profile(FIXTURE / "profile.yaml")

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.artifacts = self.directory / "artifacts"
        self.artifacts.mkdir()
        self.report_path = self.directory / "review.json"
        self.report = json.loads((FIXTURE / "review.json").read_text(encoding="utf-8"))
        self.reference = self.report["findings"][0]["evidence_ref"]
        self.entry = self.report["evidence_artifacts"]["DRC"]
        self.set_artifact((FIXTURE / "drc.json").read_bytes())

    def set_artifact(self, data):
        (self.artifacts / "drc.json").write_bytes(data)
        self.entry["sha256"] = sha256(data).hexdigest()

    def validate(self, root=None):
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")
        return self.review.validate_report(self.profile, self.report_path, root)

    def test_cli_checks_bundled_example_using_default_root(self):
        command = [sys.executable, "-B", str(ROOT / "scripts/pcb_profile.py"), "validate-report",
                   str(FIXTURE / "profile.yaml"), str(FIXTURE / "review.json")]
        result = subprocess.run(command, cwd=self.directory, capture_output=True, text=True, check=True, timeout=30)
        self.assertIn("Проверено артефактов по SHA-256 — 1", result.stdout)
        self.assertIn("Проверено указателей JSON — 1", result.stdout)

    def test_explicit_root_and_rfc_pointer_escapes(self):
        self.set_artifact(b'{"path/key":{"~1":[null]},"":[]}')
        self.reference["locator"] = "/path~1key/~01/0"
        self.assertEqual(self.validate(self.artifacts), {"artifacts": 1, "references": 1})
        self.reference["locator"] = "/"
        self.assertEqual(self.validate(self.artifacts)["references"], 1)
        with self.assertRaisesRegex(ValueError, "артефакт недоступен"):
            self.validate()

    def test_file_hash_must_match_even_when_pointer_still_resolves(self):
        with (self.artifacts / "drc.json").open("ab") as file:
            file.write(b"\n")
        with self.assertRaisesRegex(ValueError, "SHA-256 не совпадает"):
            self.validate(self.artifacts)

    def test_wrong_artifact_id_or_stale_pointer_cannot_validate(self):
        self.reference["artifact"] = "MISSING"
        with self.assertRaisesRegex(ValueError, "отсутствует в evidence_artifacts"):
            self.validate(self.artifacts)
        self.reference["artifact"] = "DRC"
        for pointer in ("/violations/99", "/violations/00", "/violations/-", "/violations/0/x~9", "violations/0"):
            with self.subTest(pointer=pointer):
                self.reference["locator"] = pointer
                with self.assertRaisesRegex(ValueError, r"findings\[0\].evidence_ref"):
                    self.validate(self.artifacts)

    def test_registry_reads_json_only_and_rejects_ambiguous_content(self):
        for data, error in ((b'{"violations":[],"violations":[]}', "Повторяющийся ключ"),
                            (b'{"violations":[NaN]}', "Недопустимое значение JSON")):
            with self.subTest(data=data):
                self.set_artifact(data)
                with self.assertRaisesRegex(ValueError, error):
                    self.validate(self.artifacts)

    def test_relative_path_cannot_escape_root(self):
        outside = self.directory / "outside.json"
        outside.write_bytes((FIXTURE / "drc.json").read_bytes())
        for path in ("../outside.json", str(outside), r"..\outside.json"):
            with self.subTest(path=path):
                self.entry["path"] = path
                with self.assertRaisesRegex(ValueError, "относительный путь внутри"):
                    self.validate(self.artifacts)
        try:
            (self.artifacts / "linked.json").symlink_to(outside)
        except (NotImplementedError, OSError):
            self.skipTest("Нужна поддержка символических ссылок")
        self.entry["path"] = "linked.json"
        with self.assertRaisesRegex(ValueError, "путь выходит за каталог"):
            self.validate(self.artifacts)

    def test_legacy_report_remains_valid_and_cannot_request_verification_without_registry(self):
        del self.report["evidence_artifacts"]
        self.reference.update(artifact="original-drc.json", locator="посадочное место K1")
        self.assertIsNone(self.validate())
        with self.assertRaisesRegex(ValueError, "требуется evidence_artifacts"):
            self.validate(self.artifacts)
