import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from scripts.kicad_checks import evaluate_check, run_check, snapshot_project
from scripts.pcb_profile import ROOT, ProfileReview


DEMO = Path("/usr/share/kicad/demos/pic_programmer")
ALL = ["error", "warning", "exclusion"]
HELP = "--format --units --severity-all --exit-code-violations --output"


class KiCadChecksTest(unittest.TestCase):
    def test_drc_counts_report_errors_even_when_exit_is_zero(self):
        review = ProfileReview()
        report = review.load_validated(ROOT / "examples/relay-review/drc.json", "kicad-drc")
        result = evaluate_check("drc", report | {"included_severities": ALL, "ignored_checks": []}, 0, [])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["counts"], {"error": 1, "warning": 0, "excluded": 1})
        self.assertIn("exit_code_conflict", result["reasons"])

    def test_erc_distinguishes_warnings_exclusions_and_execution_failure(self):
        report = {
            "included_severities": ALL, "ignored_checks": [],
            "sheets": [
                {"violations": [{"severity": "warning"}]},
                {"violations": [{"severity": "error", "excluded": True}]},
            ],
        }
        result = evaluate_check("erc", report, 5, [])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["counts"], {"error": 0, "warning": 1, "excluded": 1})
        self.assertEqual(result["reasons"], ["warnings", "exclusions"])
        self.assertIn("execution_failed", evaluate_check("erc", report, -11, [])["reasons"])

    def test_empty_output_has_to_confirm_coverage_and_revision(self):
        report = {"sheets": [{"violations": []}]}
        self.assertEqual(evaluate_check("erc", report, 0, [])["status"], "REVIEW_REQUIRED")
        report.update(included_severities=ALL, ignored_checks=[])
        self.assertEqual(evaluate_check("erc", report, 0, [])["status"], "PASS")
        self.assertEqual(evaluate_check("erc", report, 5, [])["status"], "REVIEW_REQUIRED")
        self.assertEqual(evaluate_check("erc", report, 0, ["design_revision_missing"])["status"],
                         "REVIEW_REQUIRED")
        report["ignored_checks"] = [{"key": "test", "description": "ignored"}]
        result = evaluate_check("erc", report, 0, [])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("ignored_checks", result["reasons"])

    def test_missing_flags_keep_check_unevaluated_and_refill_is_never_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            help_result = {"exit_code": 0, "stdout": HELP, "stderr": ""}
            with patch("scripts.kicad_checks.execute", return_value=help_result) as execute:
                result = run_check("drc", Path("test.kicad_pcb"), root, root, "kicad-cli", {}, 1,
                                   None, [], "10.0.8", True)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(result["status"], "NOT_EVALUATED")
            self.assertEqual(result["unsupported_flags"], ["--refill-zones"])

            def probe_and_timeout(command, *unused):
                if command[-1] == "--help":
                    return help_result | {"stdout": HELP + " --refill-zones"}
                self.assertIn("--refill-zones", command)
                self.assertNotIn("--save-board", command)
                return {"exit_code": None, "timed_out": True, "stdout": "", "stderr": ""}

            with patch("scripts.kicad_checks.execute", side_effect=probe_and_timeout):
                result = run_check("drc", Path("test.kicad_pcb"), root, root, "kicad-cli", {}, 1,
                                   None, [], "10.0.8", True)
            self.assertEqual(result["status"], "REVIEW_REQUIRED")
            self.assertTrue(result["execution"]["timed_out"])

    def test_invalid_export_remains_available_for_review(self):
        review = ProfileReview()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def probe_and_write(command, *unused):
                if command[-1] == "--help":
                    return {"exit_code": 0, "stdout": HELP, "stderr": ""}
                path = Path(command[command.index("--output") + 1])
                path.write_text("{}", encoding="utf-8")
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            with patch("scripts.kicad_checks.execute", side_effect=probe_and_write):
                result = run_check("erc", Path("example.kicad_sch"), root, root, "kicad-cli", {}, 1,
                                   review.load_validated, [], "10.0.8", False)
            self.assertEqual(result["status"], "REVIEW_REQUIRED")
            self.assertIn("invalid_report", result["reasons"])
            self.assertEqual(result["native_report_sha256"], sha256((root/"erc.json").read_bytes()).hexdigest())

    def test_snapshot_contains_local_cad_dependencies_and_omits_personal_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            scratch = project / "scratch"
            scratch.mkdir()
            (project / "libs").mkdir()
            files = ("design.kicad_pro", "design.kicad_dru", "design.kicad_pcb", "libs/sub.kicad_sch",
                     "libs/test.kicad_sym", "fp-lib-table", "design.kicad_prl", "unrelated.txt")
            for name in files:
                (project / name).write_text(name, encoding="utf-8")
            snapshot, manifest, skipped = snapshot_project(project, scratch)
            self.assertEqual(set(manifest), set(files[:-2]))
            self.assertEqual(skipped, [])
            self.assertEqual((snapshot / "libs/sub.kicad_sch").read_text(), "libs/sub.kicad_sch")
            self.assertFalse((snapshot/"design.kicad_prl").exists())

    @unittest.skipUnless(shutil.which("kicad-cli") and (DEMO / "pic_programmer.kicad_sch").is_file(),
                         "Нужны KiCad CLI и демонстрационный проект")
    def test_cli_runs_native_checks_on_hierarchical_demo_without_source_changes(self):
        cad = list(DEMO.glob("*.kicad_*"))
        before = {file.name: sha256(file.read_bytes()).hexdigest() for file in cad}
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            env = os.environ | {"KICAD_CONFIG_HOME": str(scratch / "mock-user-config")}
            output = scratch / "result"
            command = [sys.executable, "-B", str(ROOT / "scripts" / "pcb_profile.py"), "run-checks",
                       "--schematic", str(DEMO / "pic_programmer.kicad_sch"),
                       "--board", str(DEMO / "pic_programmer.kicad_pcb"),
                       "--design-revision", "demo", "--output-dir", str(output)]
            subprocess.run(command, env=env, capture_output=True, check=True, timeout=70)
            result = json.loads((output/"summary.json").read_text())
            self.assertTrue(result["kicad_version"])
            self.assertEqual(result["source_inputs_changed"], [])
            self.assertTrue({"erc", "drc"} <= set(result["requested_checks"]))
            self.assertGreaterEqual(len(result["checks"]["erc"]["sheets"]), 2)
            for kind in ("erc", "drc"):
                path = output / f"{kind}.json"
                self.assertTrue(path.is_file())
                self.assertEqual(result["checks"][kind]["native_report_sha256"],
                                 sha256(path.read_bytes()).hexdigest())
        self.assertEqual({file.name: sha256(file.read_bytes()).hexdigest()
                          for file in DEMO.glob("*.kicad_*")}, before)
