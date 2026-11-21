import json
import subprocess
import sys
import unittest
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path

from scripts.kicad_stackup import collect_stackup
from scripts.pcb_profile import ROOT


DEMO_BOARD = Path("/usr/share/kicad/demos/cm5_minima/CM5_MINIMA_3.kicad_pcb")
COPPER = ["F.Cu", "B.Cu"]
EXPLICIT_BOARD = br'''
(kicad_pcb
  (footprint "Test with fake (setup (stackup))" (setup (stackup)))
  (general (thickness 0.4))
  (setup
    # Some CAM notes contain parentheses without quoting: ((
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.05))
      (layer "dielectric 1" (type "core") (thickness 0.1 locked)
        (material "FR-4 \"A\" (#)") (epsilon_r 4) (loss_tangent 0)
        addsublayer
        (thickness 0.2) (material "FR-4 B") (epsilon_r 4.2) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.05))
      (copper_finish "ENIG")
      (dielectric_constraints yes)
      (edge_connector bevelled))))
'''


class KiCadStackupTest(unittest.TestCase):
    def test_reads_ordered_layers_and_sublayers_from_top_level_setup(self):
        result = collect_stackup(EXPLICIT_BOARD, COPPER)
        self.assertEqual(result["status"], "NOT_EVALUATED")
        self.assertEqual(result["source"], "setup/stackup")
        self.assertEqual(result["copper_layer_order"], COPPER)
        self.assertEqual(result["declared_board_thickness_mm"], 0.4)
        self.assertEqual(result["declared_stack_thickness_mm"], 0.4)
        self.assertEqual(result["settings"],
                         {"copper_finish": "ENIG", "dielectric_constraints": True, "edge_connector": "bevelled"})
        dielectric = result["layers"][1]
        self.assertEqual(dielectric["type"], "core")
        self.assertEqual(dielectric["sublayers"], [
            {"material": 'FR-4 "A" (#)', "epsilon_r": 4, "loss_tangent": 0,
             "thickness_mm": 0.1, "thickness_locked": True},
            {"material": "FR-4 B", "epsilon_r": 4.2, "loss_tangent": 0.02, "thickness_mm": 0.2},
        ])

    def test_missing_stackup_does_not_supply_a_default_construction(self):
        result = collect_stackup(b'(kicad_pcb (general (thickness 1.6)) (setup))', COPPER)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["source"])
        self.assertEqual(result["layers"], [])
        self.assertEqual(result["copper_layer_order"], COPPER)
        self.assertEqual(result["declared_board_thickness_mm"], 1.6)
        self.assertIsNone(result["declared_stack_thickness_mm"])

    def test_partial_data_remains_missing_and_order_is_checked(self):
        partial = EXPLICIT_BOARD.replace(b"(thickness 0.2)", b"").replace(b"(epsilon_r 4.2)", b"")
        result = collect_stackup(partial, COPPER)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["declared_stack_thickness_mm"])
        self.assertEqual(result["issues"], [
            {"kind": "missing_layer_data", "layer": "dielectric 1", "sublayer": 2,
             "fields": ["thickness_mm", "epsilon_r"]},
        ])
        result = collect_stackup(EXPLICIT_BOARD, ["F.Cu", "In1.Cu", "B.Cu"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["issues"][0]["kind"], "stack_layer_order")
        self.assertIsNone(result["declared_stack_thickness_mm"])
        duplicate = EXPLICIT_BOARD.replace(b'(layer "B.Cu"', b'(layer "F.Cu"')
        self.assertIn("duplicate_stack_layers",
                      [issue["kind"] for issue in collect_stackup(duplicate, COPPER)["issues"]])

    @unittest.skipUnless(find_spec("pcbnew") and DEMO_BOARD.is_file(),
                         "Нужны pcbnew и демонстрационная плата CM5")
    def test_cli_reads_stack_from_native_six_layer_demo(self):
        digest = sha256(DEMO_BOARD.read_bytes()).hexdigest()
        command = [sys.executable, "-B", str(ROOT / "scripts" / "pcb_profile.py"),
                   "inventory-stackup", str(DEMO_BOARD)]
        report = json.loads(subprocess.check_output(command, timeout=30))
        self.assertEqual(report["report_type"], "kicad_stackup_inventory")
        self.assertEqual(report["board_sha256"], digest)
        self.assertEqual(sha256(DEMO_BOARD.read_bytes()).hexdigest(), digest)
        self.assertEqual(len(report["stackup"]["copper_layer_order"]), 6)
        self.assertEqual(sum(row["kind"] == "dielectric" for row in report["stackup"]["layers"]), 5)
        self.assertAlmostEqual(report["stackup"]["declared_stack_thickness_mm"],
                               report["stackup"]["declared_board_thickness_mm"])
