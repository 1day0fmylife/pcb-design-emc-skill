import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.kicad_board import pad_electrical, read_board_inventory
from scripts.mechanical_holes import (
    COORDINATE_SYSTEM,
    compare_mechanical_holes,
    validate_mechanical_criteria,
)
from scripts.pcb_profile import ROOT


NPTH = "cb932a23-f51f-4823-bd1c-aab0db98d79c"
SLOT = "3afa030b-f931-4d1a-aa83-477f4a218256"
DEMOS = Path("/usr/share/kicad/demos")
PIC = DEMOS / "pic_programmer/pic_programmer.kicad_pcb"
MIXER = DEMOS / "multichannel/multichannel_mixer.kicad_pcb"


def exact(value):
    return {"min": value, "max": value}


class MechanicalHolesTest(unittest.TestCase):
    def setUp(self):
        self.criteria = {
            "design_revision": "DEMO-TEST",
            "board_sha256": "a" * 64,
            "source": {"document": "Synthetic fixture", "revision": "1", "section": "test only"},
            "coordinate_system": COORDINATE_SYSTEM,
            "holes": {
                "MOUNT-A": {
                    "pad_uuid": NPTH.upper(), "kind": "NPTH", "shape": "circle",
                    "x_mm": exact(77.47), "y_mm": exact(135.89), "diameter_mm": exact(4.3),
                },
                "MOUNT-B": {
                    "pad_uuid": SLOT, "kind": "PTH", "shape": "oblong",
                    "x_mm": exact(185.085), "y_mm": exact(87.385),
                    "width_mm": exact(1), "length_mm": exact(3.6),
                    "axis_angle_deg": {"nominal": 90, "tolerance": 0},
                },
            },
        }
        self.board = {
            "source": "test.kicad_pcb", "board_sha256": "a" * 64, "pcbnew_version": "10.0.8",
            "skipped_without_uuid": {},
            "items": {
                NPTH: {"kind": "PAD", "refdes": "P101", "hole": {
                    "kind": "NPTH", "shape": "circle", "x_mm": 4.3, "y_mm": 4.3,
                    "center_mm": {"x": 77.47, "y": 135.89},
                }},
                SLOT: {"kind": "PAD", "refdes": "J5", "hole": {
                    "kind": "PTH", "shape": "oblong", "x_mm": 3.6, "y_mm": 1,
                    "center_mm": {"x": 185.085, "y": 87.385}, "axis_angle_deg": 90,
                }},
            },
        }

    def result(self, revision="DEMO-TEST"):
        validate_mechanical_criteria(self.criteria)
        return compare_mechanical_holes(self.board, self.criteria, revision)

    def test_pass_requires_complete_criteria(self):
        self.assertEqual(self.result()["status_counts"], {"PASS": 12})
        del self.criteria["holes"]["MOUNT-A"]["diameter_mm"]
        report = self.result()
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        unknown, = [row for row in report["checks"] if row["status"] == "REVIEW_REQUIRED"]
        self.assertEqual((unknown["field"], unknown["reason"]), ("diameter_mm", "criterion_missing"))

    def test_reports_confirmed_geometry_and_plating_failures(self):
        self.criteria["holes"]["MOUNT-A"].update(kind="PTH", x_mm={"max": 77})
        self.criteria["holes"]["MOUNT-B"].update(length_mm={"min": 4})
        report = self.result()
        self.assertEqual(report["overall_status"], "FAIL")
        self.assertEqual(
            {(row["hole_id"], row["field"], row["rule_id"]) for row in report["checks"] if row["status"] == "FAIL"},
            {("MOUNT-A", "kind", "PCB-MECH-004"), ("MOUNT-A", "x_mm", "PCB-MECH-001"),
             ("MOUNT-B", "length_mm", "PCB-MECH-001")},
        )

    def test_axis_has_180_degree_period_and_inclusive_tolerance(self):
        self.board["items"][SLOT]["hole"]["axis_angle_deg"] = 179.5
        angle = self.criteria["holes"]["MOUNT-B"]["axis_angle_deg"]
        angle.update(nominal=0.5, tolerance=1)
        self.assertEqual(self.result()["overall_status"], "PASS")
        angle["tolerance"] = 0.9
        failure, = [row for row in self.result()["checks"] if row["status"] == "FAIL"]
        self.assertEqual((failure["field"], failure["deviation_deg"]), ("axis_angle_deg", 1))

    def test_missing_mapping_or_coordinate_system_keeps_unknowns_visible(self):
        self.criteria["coordinate_system"] = None
        report = self.result()
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual(report["status_counts"], {"PASS": 7, "REVIEW_REQUIRED": 5})
        self.assertTrue(all("deviation_deg" not in row for row in report["checks"]))
        self.criteria["holes"]["MOUNT-A"]["pad_uuid"] = None
        self.assertEqual(self.result()["checks"][0]["kind"], "pad_uuid_missing")

    def test_scope_mismatch_cannot_confirm_outcome(self):
        self.criteria["source"]["section"] = None
        self.criteria["board_sha256"] = "b" * 64
        self.criteria["holes"]["MOUNT-A"]["kind"] = "PTH"
        report = self.result(revision="OTHER")
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual([row["kind"] for row in report["checks"][:3]], [
            "design_revision_mismatch", "mechanical_source_missing", "board_sha256_mismatch",
        ])
        self.assertTrue(all(row["status"] == "REVIEW_REQUIRED" for row in report["checks"]))
        self.assertEqual(compare_mechanical_holes(None, None, None)["overall_status"], "REVIEW_REQUIRED")

    def test_missing_pad_and_wrong_shape_skip_incompatible_size_checks(self):
        self.board["items"][NPTH]["hole"]["shape"] = "oblong"
        report = self.result()
        self.assertFalse(any(row.get("field") == "diameter_mm" for row in report["checks"]))
        failure, = [row for row in report["checks"] if row["status"] == "FAIL"]
        self.assertEqual(failure["field"], "shape")
        del self.board["items"][NPTH]
        row = self.result()["checks"][0]
        self.assertEqual((row["kind"], row["status"]), ("pad_uuid_not_found", "FAIL"))
        self.board["skipped_without_uuid"]["PAD"] = 1
        self.assertEqual(self.result()["checks"][0]["status"], "REVIEW_REQUIRED")

    def test_invalid_ambiguous_and_incompatible_criteria(self):
        holes = self.criteria["holes"]
        holes["MOUNT-A"]["x_mm"] = {"min": 1, "max": 0}
        with self.assertRaisesRegex(ValueError, "min превышает max"):
            self.result()
        holes["MOUNT-A"]["x_mm"] = {"max": float("nan")}
        with self.assertRaisesRegex(ValueError, "конечными"):
            self.result()
        holes["MOUNT-A"]["x_mm"] = {}
        holes["MOUNT-B"]["diameter_mm"] = exact(1)
        with self.assertRaisesRegex(ValueError, "шириной и длиной"):
            self.result()
        del holes["MOUNT-B"]["diameter_mm"]
        holes["MOUNT-B"]["pad_uuid"] = NPTH
        with self.assertRaisesRegex(ValueError, "повторяющийся UUID"):
            self.result()

    @unittest.skipUnless(find_spec("pcbnew") and PIC.is_file() and MIXER.is_file(), "Нужны pcbnew и демонстрационные платы")
    def test_cli_reads_round_hole_and_rotated_slot_from_native_boards(self):
        command = [sys.executable, "-B", str(ROOT / "scripts/pcb_profile.py"), "compare-mounting"]
        for demo, name, refdes in [(PIC, "MOUNT-A", "P101"), (MIXER, "MOUNT-B", "J5")]:
            with self.subTest(board=demo.name), tempfile.TemporaryDirectory() as folder:
                # Целевые размеры для проверок CLI взяты из демонстрационной CAD-геометрии.
                # Паз разъёма служит примером геометрии и металлизации.
                checksum = sha256(demo.read_bytes()).hexdigest()
                manifest = self.criteria | {"holes": {name: self.criteria["holes"][name]}, "board_sha256": checksum}
                criteria_file = Path(folder) / "synthetic-criteria.json"
                criteria_file.write_text(json.dumps(manifest), encoding="utf-8")
                report = json.loads(subprocess.check_output([
                    *command, "--board", str(demo), "--mechanical", str(criteria_file),
                    "--design-revision", "DEMO-TEST",
                ], timeout=30))
                self.assertEqual(report["overall_status"], "PASS", report["checks"])
                self.assertEqual({row["refdes"] for row in report["checks"]}, {refdes})
                self.assertEqual(report["mechanical_artifact"]["sha256"], sha256(criteria_file.read_bytes()).hexdigest())
                self.assertEqual(report["board"]["board_sha256"], checksum)
                self.assertEqual(sha256(demo.read_bytes()).hexdigest(), checksum)

    @unittest.skipUnless(find_spec("pcbnew") and PIC.is_file(), "Нужны pcbnew и демонстрационная плата")
    def test_native_hole_geometry_survives_missing_npth_copper_method(self):
        import pcbnew

        checksum = sha256(PIC.read_bytes()).hexdigest()
        with patch.object(pcbnew.PAD, "IsNPTHWithNoCopper", None, create=True):
            board = read_board_inventory(PIC)
        self.assertFalse(board["items"][NPTH]["electrical"])
        self.assertEqual(board["items"][NPTH]["hole"]["center_mm"], {"x": 77.47, "y": 135.89})
        self.assertTrue(any(item.get("hole", {}).get("kind") == "PTH" and item["electrical"]
                            for item in board["items"].values()))
        criteria = self.criteria | {"holes": {"MOUNT-A": self.criteria["holes"]["MOUNT-A"]},
                                    "board_sha256": checksum}
        report = compare_mechanical_holes(board, criteria, "DEMO-TEST")
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(sha256(PIC.read_bytes()).hexdigest(), checksum)

    def test_npth_on_copper_is_unknown_without_classification_method(self):
        pcbnew = SimpleNamespace(PAD_ATTRIB_NPTH=1)
        pad = SimpleNamespace(GetAttribute=lambda: 1, IsOnCopperLayer=lambda: True)
        self.assertIsNone(pad_electrical(pad, pcbnew))
        pad.IsNPTHWithNoCopper = lambda: True
        self.assertFalse(pad_electrical(pad, pcbnew))
        pad.IsNPTHWithNoCopper = lambda: False
        self.assertTrue(pad_electrical(pad, pcbnew))
