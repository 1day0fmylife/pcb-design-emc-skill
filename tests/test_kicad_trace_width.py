import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path

from scripts.kicad_trace_width import compare_trace_width
from scripts.pcb_profile import ROOT, ProfileReview


EXAMPLE = ROOT / "examples/kicad-multinetclasses"
DEMO = Path("/usr/share/kicad/demos/royalblue54L_feather/nfc_antenna/"
            "RoyalBlue54L-NFC-Antenna/RoyalBlue54L-NFC-Antenna.kicad_pcb")
DEMO_ARC = "0041e8ea-5200-4c01-ad54-b9bca8959216"
DEMO_TRACK = "0664b9e5-b611-47f6-b766-465745b22c64"


class TraceWidthComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()

    def setUp(self):
        self.profile = self.review.profile(EXAMPLE / "profile.yaml")
        self.context = self.review.load_validated(EXAMPLE / "context.yaml", "kicad-context")
        self.first, self.second = self.context["items"]
        self.board = {
            "source": self.context["board_source"], "board_sha256": self.context["board_sha256"],
            "pcbnew_version": "10.0.8",
            "items": {
                self.first: {"kind": "PCB_TRACK", "net": "Net-(R1-Pad2)", "layers": ["F.Cu"], "width_mm": 0.1},
                self.second: {"kind": "PCB_ARC", "net": "Net-(R3-Pad1)", "layers": ["F.Cu", "F.Mask"],
                              "width_mm": 0.127},
            },
        }

    def result(self):
        return compare_trace_width(self.profile, self.board, self.context, self.review.analyze(self.profile))

    def test_inclusive_bounds_and_scope_for_lines_and_arcs(self):
        self.profile["criteria"]["TEST-MIN"] = self.profile["criteria"]["RULE-3-MAX"] | {
            "metric": "trace_width", "limit": 0.1,
            "source": {"document": "Synthetic width fixture", "revision": "1", "section": "minimum"},
        }
        result = self.result()
        self.assertEqual(result["overall_status"], "PASS")
        self.assertEqual(result["status_counts"], {"PASS": 4})
        self.assertEqual(set(result["scope_uuids"]), {self.first, self.second})
        self.assertTrue(result["board"]["sha256_verified_against_context"])

        self.board["items"][self.first]["width_mm"] = 0.09
        self.board["items"][self.second]["width_mm"] = 0.128
        result = self.result()
        self.assertEqual(result["overall_status"], "FAIL")
        failures = {(row["uuid"], row["criterion_id"], row["evidence_ref"]["locator"])
                    for row in result["checks"] if row["status"] == "FAIL"}
        self.assertEqual(failures, {
            (self.first, "TEST-MIN", f"uuid:{self.first}"),
            (self.second, "RULE-3-MAX", f"uuid:{self.second}"),
        })

    def test_missing_or_conflicting_criteria_remain_unconfirmed(self):
        maximum = self.profile["criteria"]["RULE-3-MAX"]
        self.profile["criteria"]["TEST-MIN"] = maximum | {
            "metric": "trace_width", "limit": 0.15,
            "source": {"document": "Synthetic conflict fixture", "revision": "1", "section": "minimum"},
        }
        result = self.result()
        self.assertEqual(result["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual({row["reason"] for row in result["checks"]}, {"conflicting_width_bounds"})
        del self.profile["criteria"]["TEST-MIN"]
        self.board["items"][self.second]["width_mm"] = 0.508
        del maximum["source"]
        result = self.result()
        self.assertEqual(result["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual({row["reason"] for row in result["checks"]}, {"width_criterion_unconfirmed"})

    def test_scope_conflicts_and_sparse_evidence_do_not_confirm_a_result(self):
        self.context["design_revision"] = "OTHER"
        self.context["board_source"] = "other.kicad_pcb"
        self.context["board_sha256"] = "b" * 64
        self.board["items"][self.first]["width_mm"] = 2
        result = self.result()
        self.assertEqual(result["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["blockers"], [
            "design_revision_unconfirmed", "board_source_mismatch", "board_sha256_mismatch",
        ])
        self.assertFalse(result["board"]["sha256_verified_against_context"])
        self.assertEqual({row["reason"] for row in result["checks"]}, {"scope_unverified"})

        self.context.update(design_revision=self.profile["design_revision"], board_source=self.board["source"],
                            board_sha256=self.board["board_sha256"])
        self.context["items"][self.second]["net"] = "WRONG"
        result = self.result()
        self.assertEqual(result["overall_status"], "FAIL")
        self.assertEqual(result["status_counts"], {"FAIL": 1, "REVIEW_REQUIRED": 1})
        self.assertEqual(result["checks"][1]["reason"], "board_context_conflict")

        self.board["items"][self.first]["kind"] = "PCB_VIA"
        del self.board["items"][self.second]
        result = self.result()
        self.assertEqual(result["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual([row["reason"] for row in result["checks"]], ["not_a_trace", "uuid_not_found"])

    def test_mode_width_and_binding_must_be_known(self):
        self.context.pop("mode")
        result = self.result()
        self.assertIn("mode_missing", result["blockers"])
        self.assertEqual(result["overall_status"], "REVIEW_REQUIRED")
        self.context["mode"] = "qa-drc"
        del self.context["items"][self.first]["object_id"]
        del self.board["items"][self.second]["width_mm"]
        self.assertEqual([row["reason"] for row in self.result()["checks"]],
                         ["net_class_binding_missing_or_invalid", "pcb_value_missing"])
        self.context["items"].clear()
        self.assertIn("trace_scope_empty", self.result()["blockers"])

    @unittest.skipUnless(find_spec("pcbnew") and DEMO.is_file(), "Нужны pcbnew и демонстрационная PCB с дугой")
    def test_cli_reads_native_line_and_arc_width(self):
        checksum = sha256(DEMO.read_bytes()).hexdigest()
        source = {"document": "Synthetic acceptance fixture", "revision": "1", "section": "width"}
        profile = {
            "profile_id": "trace-demo", "description": "Тестовый профиль дуги и отрезка",
            "design_revision": "DEMO-TEST", "approved": False, "inherits": None,
            "feature_tags": [], "constraints": {},
            "review_objects": {"ANT": {"kind": "net_class", "modes": ["normal"]}},
            "criteria": {
                "DEMO-MAX": {"object_id": "ANT", "mode": "normal", "rule_id": "PCB-DFM-001",
                             "metric": "trace_width_max", "unit": "mm", "limit": 0.3, "source": source},
            },
        }
        context = {
            "design_revision": "DEMO-TEST", "board_source": DEMO.name, "board_sha256": checksum, "mode": "normal",
            "items": {uuid: {"object_id": "ANT", "net": "/ANT"} for uuid in (DEMO_ARC, DEMO_TRACK)},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            profile_path, context_path = path / "profile.json", path / "context.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            context_path.write_text(json.dumps(context), encoding="utf-8")
            command = [sys.executable, "-B", str(ROOT / "scripts/pcb_profile.py"), "compare-trace-width",
                       str(profile_path), "--board", str(DEMO), "--context", str(context_path)]
            result = json.loads(subprocess.check_output(command, timeout=30))
            self.assertEqual(result["overall_status"], "PASS")
            self.assertEqual(result["context_artifact"]["sha256"], sha256(context_path.read_bytes()).hexdigest())
        self.assertEqual({row["pcb_object_kind"]: row["pcb_value"] for row in result["checks"]},
                         {"PCB_ARC": 0.3, "PCB_TRACK": 0.29997})
        self.assertEqual(sha256(DEMO.read_bytes()).hexdigest(), checksum)
