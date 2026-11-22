import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path

from scripts.kicad_copper_gap import compare_copper_gap
from scripts.pcb_profile import ROOT, ProfileReview, validate_document


DEMO = Path("/usr/share/kicad/demos/pic_programmer/pic_programmer.kicad_pcb")
DEFAULT_UUID = "0c5e0d77-bd06-42ea-b464-6c6d48556abc"
POWER_UUID = "234aa39b-64ad-4fb9-b7a9-cdc8cbfb4541"
REVISION = "KICAD-DEMO"
MODE = "normal"


class CopperGapComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()

    def setUp(self):
        self.profile = self.review.profile(ROOT / "examples/kicad-multinetclasses/profile.yaml")
        self.profile["design_revision"] = REVISION
        self.profile["review_objects"] = {
            "DEFAULT": {"kind": "net_class", "modes": [MODE]},
            "POWER": {"kind": "net_class", "modes": [MODE]},
            "DEFAULT-POWER": {"kind": "net_class_pair", "members": ["POWER", "DEFAULT"], "modes": [MODE]},
        }
        self.criterion = {
            "rule_id": "PCB-GEO-001", "object_id": "DEFAULT-POWER", "mode": MODE,
            "metric": "copper_gap", "limit": 0.5, "unit": "mm",
            "source": {"document": "Synthetic fixture", "revision": "A", "section": "gap criterion"},
        }
        self.profile["criteria"] = {"POWER-GAP": self.criterion}
        self.context = {
            "board_source": "test.kicad_pcb", "board_sha256": "a" * 64, "design_revision": REVISION,
            "mode": MODE, "trace_pairs": [[POWER_UUID, DEFAULT_UUID]],
            "items": {DEFAULT_UUID: {"object_id": "DEFAULT"}, POWER_UUID: {"object_id": "POWER"}},
        }
        self.tracks = {
            DEFAULT_UUID: {"segment": ((0, 0), (1000, 0)), "width": 101},
            POWER_UUID: {"segment": ((1000, 700), (1000, 1700)), "width": 299},
        }
        self.board = {
            "source": "test.kicad_pcb", "board_sha256": "a" * 64, "pcbnew_version": "10.0.8",
            "items": {
                DEFAULT_UUID: {"kind": "PCB_TRACK", "net": "SIGNAL", "layers": ["F.Cu"]},
                POWER_UUID: {"kind": "PCB_TRACK", "net": "VCC", "layers": ["F.Cu"]},
            },
            "trace_geometry": {"units_per_mm": 1000, "tracks": self.tracks},
        }

    def result(self):
        return compare_copper_gap(self.profile, self.board, self.context, self.review.analyze(self.profile))

    def test_interclass_boundary_uses_both_widths_and_unordered_pair(self):
        result = self.result()
        self.assertEqual(result["overall_status"], "PASS")
        self.assertEqual(result["scope_pairs"], [[DEFAULT_UUID, POWER_UUID]])
        row, = result["checks"]
        self.assertEqual((row["object_id"], row["criterion_id"], row["pcb_value"]), ("DEFAULT-POWER", "POWER-GAP", 0.5))
        self.assertEqual([trace["width_mm"] for trace in row["traces"]], [0.101, 0.299])
        self.criterion["limit"] = 0.500001
        self.assertEqual(self.result()["overall_status"], "FAIL")
        self.tracks[POWER_UUID]["width"] = 300
        self.criterion["limit"] = 0.4995
        self.assertEqual(self.result()["overall_status"], "PASS")

    def test_intraclass_and_intersection(self):
        self.context["items"][POWER_UUID]["object_id"] = "DEFAULT"
        self.criterion.update(object_id="DEFAULT", rule_id="PCB-DFM-001")
        self.tracks[POWER_UUID]["segment"] = ((500, -1000), (500, 1000))
        row, = self.result()["checks"]
        self.assertEqual((row["rule_id"], row["status"], row["pcb_value"]), ("PCB-DFM-001", "FAIL", 0))
        self.tracks[POWER_UUID]["segment"] = ((1200, 0), (2200, 0))
        self.assertEqual(self.result()["checks"][0]["pcb_value"], 0)

    def test_same_net_and_different_layers_are_not_applicable(self):
        trace = self.board["items"][POWER_UUID]
        trace["net"] = "SIGNAL"
        result = self.result()
        self.assertEqual(result["overall_status"], "N_A")
        self.assertEqual(result["checks"][0]["reason"], "same_net")
        trace.update(net="VCC", layers=["B.Cu"])
        self.assertEqual(self.result()["checks"][0]["reason"], "different_copper_layers")

    def test_unknown_geometry_bindings_and_rules_require_review(self):
        trace = self.board["items"][POWER_UUID]
        trace["kind"] = "PCB_ARC"
        self.assertEqual(self.result()["checks"][0]["reason"], "unsupported_trace_geometry")
        trace.update(kind="PCB_TRACK", net="")
        self.context["items"][POWER_UUID]["net"] = "VCC"
        self.assertEqual(self.result()["checks"][0]["status"], "REVIEW_REQUIRED")
        trace["net"] = "VCC"
        self.context["items"][POWER_UUID]["layer"] = "B.Cu"
        self.assertEqual(self.result()["checks"][0]["reason"], "board_context_conflict")
        del self.context["items"][POWER_UUID]["layer"]
        del self.profile["criteria"]["POWER-GAP"]
        row, = self.result()["checks"]
        self.assertEqual((row["reason"], row["status"]), ("gap_criterion_unconfirmed", "REVIEW_REQUIRED"))
        del self.profile["review_objects"]["DEFAULT-POWER"]
        self.assertEqual(self.result()["checks"][0]["reason"], "net_class_pair_or_mode_unconfirmed")

    def test_provenance_blockers_and_missing_scope_cannot_pass_or_fail(self):
        self.criterion["limit"] = 0.6
        self.context["board_sha256"] = "b" * 64
        self.context["design_revision"] = "UNRELATED"
        report = self.result()
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertIn("board_sha256_mismatch", report["blockers"])
        self.assertIn("design_revision_unconfirmed", report["blockers"])
        self.assertEqual(report["checks"][0]["reason"], "scope_unverified")
        del self.context["trace_pairs"]
        report = self.result()
        self.assertIn("trace_pair_scope_empty", report["blockers"])
        self.assertEqual(report["checks"], [])

    def test_unresolved_source_conflict_cannot_confirm_measurement_status(self):
        self.criterion["alternative_sources"] = [{
            "limit": 0.6, "source": {"document": "Alternative fixture", "revision": "A", "section": "gap"},
        }]
        row, = self.result()["checks"]
        self.assertEqual((row["status"], row["pcb_value"]), ("REVIEW_REQUIRED", 0.5))
        self.assertIn("source_conflict", row["missing_data"])

    def test_aggregate_status_accounts_for_unverified_pairs(self):
        missing_uuid = "00000000-0000-0000-0000-000000000001"
        self.context["trace_pairs"].append([POWER_UUID, missing_uuid])
        report = self.result()
        self.assertEqual(report["status_counts"], {"PASS": 1, "REVIEW_REQUIRED": 1})
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.criterion["limit"] = 0.6
        self.assertEqual(self.result()["overall_status"], "FAIL")

    def test_schema_rejects_self_pairs_and_comparison_rejects_reversed_duplicates(self):
        schema = self.review.validators["kicad-context"]
        self.context["trace_pairs"].append([DEFAULT_UUID, POWER_UUID])
        validate_document(self.context, schema, "test")
        with self.assertRaisesRegex(ValueError, "повтор пары"):
            self.result()
        self.context["trace_pairs"] = [[DEFAULT_UUID, DEFAULT_UUID]]
        with self.assertRaises(ValueError):
            validate_document(self.context, schema, "test")

    @unittest.skipUnless(find_spec("pcbnew") and DEMO.is_file() and DEMO.with_suffix(".kicad_pro").is_file(),
                         "Нужны pcbnew и демонстрационный проект")
    def test_cli_measures_real_default_power_pair_without_changing_project(self):
        project_path = DEMO.with_suffix(".kicad_pro")
        project_data = project_path.read_bytes()
        pcb_hash = sha256(DEMO.read_bytes()).hexdigest()
        settings = json.loads(project_data)["net_settings"]
        classes = {item["name"]: item["clearance"] for item in settings["classes"]}
        limit = max(classes[name] for name in ("Default", "POWER"))
        profile = {
            "profile_id": "gap-demo", "description": "Сверка зазора в демонстрационном проекте",
            "inherits": None, "design_revision": REVISION, "approved": False, "feature_tags": [], "constraints": {},
            "review_objects": self.profile["review_objects"],
            "criteria": {"POWER-GAP": self.criterion | {
                "limit": limit, "source": {
                    "document": project_path.name, "revision": "KiCad demo",
                    "section": "net_settings.classes, больший clearance из Default и POWER",
                },
            }},
        }
        context = self.context | {"board_source": DEMO.name, "board_sha256": pcb_hash}
        self.assertIn({"netclass": "POWER", "pattern": "VCC"}, settings["netclass_patterns"])
        with tempfile.TemporaryDirectory() as folder:
            profile_path, context_path = (Path(folder) / name for name in ("profile.json", "context.json"))
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            context_path.write_text(json.dumps(context), encoding="utf-8")
            command = [sys.executable, "-B", str(ROOT / "scripts/pcb_profile.py"), "compare-copper-gap",
                       str(profile_path), "--board", str(DEMO), "--context", str(context_path)]
            result = json.loads(subprocess.check_output(command, timeout=30))
        self.assertEqual(result["overall_status"], "PASS")
        self.assertTrue(result["board"]["sha256_verified_against_context"])
        row, = result["checks"]
        self.assertEqual([trace["net"] for trace in row["traces"]], ["Net-(D4-A)", "VCC"])
        self.assertAlmostEqual(row["pcb_value"], 0.62)
        self.assertEqual(row["criterion"]["min"], limit)
        self.assertEqual(sha256(DEMO.read_bytes()).hexdigest(), pcb_hash)
        self.assertEqual(project_path.read_bytes(), project_data)
