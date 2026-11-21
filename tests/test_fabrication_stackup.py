import copy
import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path

from scripts.fabrication_stackup import compare_fabrication_stackup, validate_fabrication_criteria
from scripts.pcb_profile import ROOT


DEMO = Path("/usr/share/kicad/demos/pic_programmer/pic_programmer.kicad_pcb")
DOCUMENT = {
    "manufacturer": "Учебный контрольный источник",
    "document": "synthetic test fixture",
    "revision": "v1",
    "section": "условия теста",
}


class FabricationStackupTest(unittest.TestCase):
    def setUp(self):
        self.board = {
            "source": "sample.kicad_pcb", "board_sha256": "a" * 64, "pcbnew_version": "10.0.8",
            "stackup": {
                "declared_board_thickness_mm": 2, "declared_stack_thickness_mm": 1.9,
                "settings": {"copper_finish": "Test finish"}, "issues": [],
                "layers": [
                    {"name": "F.Cu", "kind": "copper", "sublayers": [{"thickness_mm": 0.05}]},
                    {"name": "dielectric 1", "kind": "dielectric", "type": "core", "sublayers": [{
                        "thickness_mm": 1.8, "material": "Test laminate", "epsilon_r": 4,
                        "loss_tangent": 0, "spec_frequency": 1000000000,
                    }]},
                    {"name": "B.Cu", "kind": "copper", "sublayers": [{"thickness_mm": 0.05}]},
                ],
            },
        }
        self.fabrication = {
            "design_revision": "A", "board_sha256": "a" * 64, "source": DOCUMENT.copy(),
            "total_thickness": {"basis": "board", "thickness_mm": {"min": 2, "max": 2}},
            "copper_finish": "Test finish",
            "layers": [
                {"name": "F.Cu", "sublayers": [{"thickness_mm": {"max": 0.05}}]},
                {"name": "dielectric 1", "type": "core", "sublayers": [{
                    "thickness_mm": {"min": 1.7, "max": 1.9}, "material": "Test laminate",
                    "epsilon_r": {"min": 4, "max": 4}, "loss_tangent": {"max": 0},
                    "spec_frequency_hz": 1000000000,
                }]},
                {"name": "B.Cu", "sublayers": [{"thickness_mm": {"min": 0.05}}]},
            ],
        }

    def outcome(self, revision="A"):
        validate_fabrication_criteria(self.fabrication)
        return compare_fabrication_stackup(self.board, self.fabrication, revision)

    def test_passes_only_documented_criteria_and_inclusive_bounds(self):
        report = self.outcome()
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["rule_id"], "PCB-DFM-002")
        self.assertEqual(report["fabrication_source"], DOCUMENT)
        self.assertEqual(report["checks"][0]["basis"], "board")
        self.fabrication["total_thickness"]["basis"] = "stack"
        report = self.outcome()
        self.assertEqual(report["overall_status"], "FAIL")
        self.assertEqual(report["checks"][0]["pcb_value"], 1.9)

    def test_checks_order_and_sublayer_counts_before_positional_comparison(self):
        self.fabrication["layers"].reverse()
        layer = next(entry for entry in self.fabrication["layers"] if entry["name"] == "dielectric 1")
        layer["sublayers"].append(copy.deepcopy(layer["sublayers"][0]))
        report = self.outcome()
        failures = [row for row in report["checks"] if row["status"] == "FAIL"]
        self.assertEqual([row["kind"] for row in failures], ["layer_order", "sublayer_count"])
        self.assertFalse(any(row.get("sublayer") and row.get("layer") == "dielectric 1"
                             for row in report["checks"]))

    def test_needs_compatible_frequency_and_a_named_material(self):
        layer = self.board["stackup"]["layers"][1]["sublayers"][0]
        layer["spec_frequency"] = 2000000000
        layer["material"] = "Not specified"
        report = self.outcome()
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual(
            {row["field"] for row in report["checks"] if row["status"] == "REVIEW_REQUIRED"},
            {"material", "epsilon_r", "loss_tangent"},
        )
        self.assertEqual(report["status_counts"].get("FAIL", 0), 0)

    def test_scope_and_source_mismatches_cannot_confirm_pass_or_fail(self):
        self.fabrication["total_thickness"]["thickness_mm"] = {"max": 1}
        self.fabrication["source"]["section"] = None
        self.fabrication["board_sha256"] = "b" * 64
        report = self.outcome(revision="B")
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual([row["kind"] for row in report["checks"][:3]],
                         ["design_revision_mismatch", "fabrication_source_missing", "board_sha256_mismatch"])
        self.assertTrue(all(row["status"] == "REVIEW_REQUIRED" for row in report["checks"]))

    def test_partial_cad_and_fabrication_keep_unknown_checks_visible(self):
        self.fabrication["layers"].pop()
        report = self.outcome()
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertIn("fabrication_layer_missing", [row["kind"] for row in report["checks"]])
        self.board["stackup"].update(layers=[], issues=[{"kind": "stackup_missing"}])
        report = self.outcome()
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertIn("pcb_stackup_structure", [row["kind"] for row in report["checks"]])
        self.assertEqual(compare_fabrication_stackup(None, None, None)["overall_status"], "REVIEW_REQUIRED")

    def test_invalid_and_ambiguous_tolerance_input_is_rejected(self):
        self.fabrication["total_thickness"]["thickness_mm"] = {"min": 2, "max": 1}
        with self.assertRaisesRegex(ValueError, "min превышает max"):
            self.outcome()
        self.fabrication["total_thickness"]["thickness_mm"] = {"max": float("inf")}
        with self.assertRaisesRegex(ValueError, "конечными"):
            self.outcome()
        self.fabrication["total_thickness"]["thickness_mm"] = None
        self.fabrication["layers"].append(self.fabrication["layers"][0])
        with self.assertRaisesRegex(ValueError, "Повтор имени слоя"):
            self.outcome()

    @unittest.skipUnless(find_spec("pcbnew") and DEMO.is_file(), "Нужны pcbnew и демонстрационная PCB")
    def test_cli_compares_real_cad_with_synthetic_fabrication_sheet(self):
        checksum = sha256(DEMO.read_bytes()).hexdigest()
        command = [sys.executable, "-B", str(ROOT / "scripts" / "pcb_profile.py")]
        inventory = json.loads(subprocess.check_output([*command, "inventory-stackup", str(DEMO)], timeout=30))
        stackup = inventory["stackup"]
        layers = [
            {"name": layer["name"], "sublayers": [
                {"thickness_mm": {"max": row["thickness_mm"], "min": row["thickness_mm"]}}
                for row in layer["sublayers"]
            ]}
            for layer in stackup["layers"] if layer["kind"] in {"copper", "dielectric"}
        ]
        fabrication = {
            "source": DOCUMENT, "design_revision": "DEMO-TEST", "board_sha256": checksum,
            "total_thickness": {"basis": "stack", "thickness_mm": {"max": stackup["declared_stack_thickness_mm"]}},
            "layers": layers,
        }
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "synthetic-fabrication.json"
            document.write_text(json.dumps(fabrication), encoding="utf-8")
            args = [*command, "compare-stackup", "--board", str(DEMO), "--fabrication", str(document),
                    "--design-revision", "DEMO-TEST"]
            passing = json.loads(subprocess.check_output(args, timeout=30))
            self.assertEqual(passing["overall_status"], "PASS")
            self.assertEqual(passing["fabrication_artifact"]["sha256"], sha256(document.read_bytes()).hexdigest())
            layers[0]["sublayers"][0]["thickness_mm"] = {"max": layers[0]["sublayers"][0]["thickness_mm"]["max"] / 2}
            document.write_text(json.dumps(fabrication), encoding="utf-8")
            failing = json.loads(subprocess.check_output(args, timeout=30))
            self.assertEqual(failing["overall_status"], "FAIL")
            failures = [row for row in failing["checks"] if row["status"] == "FAIL"]
            self.assertEqual([(row["layer"], row["field"]) for row in failures], [("F.Cu", "thickness_mm")])
        self.assertEqual(sha256(DEMO.read_bytes()).hexdigest(), checksum)
