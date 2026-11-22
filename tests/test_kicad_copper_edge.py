import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

from scripts.kicad_board import collect_edge_geometry
from scripts.kicad_copper_edge import compare_copper_edge, segment_squared_distance
from scripts.pcb_profile import ROOT, ProfileReview


QA = ROOT / "examples/kicad-multinetclasses/profile.yaml"
DEMO = Path("/usr/share/kicad/demos/pic_programmer/pic_programmer.kicad_pcb")
DEMO_UUID = "65eb646d-9c09-4d98-b9e5-50286dd5f680"
TRACK_UUID = "12345678-1234-1234-1234-123456789abc"


def loop(prefix, vertices):
    return [(f"{prefix}-{index}", (a, b)) for index, (a, b) in enumerate(zip(vertices, vertices[1:] + vertices[:1]))]


class CopperEdgeComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()

    def setUp(self):
        self.profile = self.review.profile(QA)
        self.profile["review_objects"]["LAYOUT"] = {"kind": "board", "modes": ["qa-drc"]}
        self.criterion = {
            "rule_id": "PCB-GEO-003", "object_id": "LAYOUT", "mode": "qa-drc",
            "metric": "copper_to_edge", "limit": 0.5, "unit": "mm",
            "source": {"document": "Synthetic fixture", "revision": "A", "section": "edge criterion"},
        }
        self.profile["criteria"]["EDGE"] = self.criterion
        self.track = {"segment": ((4000, 600), (6000, 600)), "width": 200}
        self.geometry = {
            "outline_segments": loop("outer", [(0, 0), (10000, 0), (10000, 10000), (0, 10000)]),
            "outline_issues": [], "units_per_mm": 1000, "tracks": {TRACK_UUID: self.track},
        }
        self.board = {
            "source": "test.kicad_pcb", "board_sha256": "a" * 64, "pcbnew_version": "10.0.8",
            "items": {TRACK_UUID: {"kind": "PCB_TRACK", "net": "SIGNAL", "layers": ["F.Cu"]}},
            "edge_geometry": self.geometry,
        }
        self.context = {
            "board_source": "test.kicad_pcb", "board_sha256": "a" * 64,
            "design_revision": self.profile["design_revision"], "mode": "qa-drc",
            "items": {TRACK_UUID: {"object_id": "LAYOUT", "net": "SIGNAL", "layer": "F.Cu"}},
        }

    def result(self):
        return compare_copper_edge(self.profile, self.board, self.context, self.review.analyze(self.profile))

    def test_includes_track_radius_and_compares_boundary_exactly(self):
        report = self.result()
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["scope_uuids"], [TRACK_UUID])
        self.assertTrue(report["board"]["sha256_verified_against_context"])
        row, = report["checks"]
        self.assertEqual((row["edge_uuid"], row["pcb_value"], row["width_mm"]), ("outer-0", 0.5, 0.2))
        self.criterion["limit"] = 0.500001
        self.assertEqual(self.result()["overall_status"], "FAIL")

    def test_intersection_and_track_outside_board_cannot_pass(self):
        self.track["segment"] = ((5000, 1000), (5000, -1000))
        row, = self.result()["checks"]
        self.assertEqual((row["status"], row["pcb_value"]), ("FAIL", 0))
        self.track["segment"] = ((4000, -2000), (6000, -2000))
        row, = self.result()["checks"]
        self.assertEqual((row["status"], row["reason"]), ("FAIL", "trace_centerline_outside_material"))
        self.assertIsNone(row["pcb_value"])

    def test_closed_cutout_is_edge_and_has_no_board_material(self):
        self.geometry["outline_segments"].extend(
            loop("cutout", [(4000, 4000), (6000, 4000), (6000, 6000), (4000, 6000)])
        )
        self.track["segment"] = ((4500, 3800), (5500, 3800))
        row, = self.result()["checks"]
        self.assertEqual((row["status"], row["pcb_value"], row["edge_uuid"]), ("FAIL", 0.1, "cutout-0"))
        self.track["segment"] = ((4500, 5000), (5500, 5000))
        row, = self.result()["checks"]
        self.assertEqual((row["status"], row["reason"]), ("FAIL", "trace_centerline_outside_material"))

    def test_open_crossing_or_unsupported_outline_requires_review(self):
        self.geometry["outline_segments"].pop()
        self.assertEqual(self.result()["outline_issues"][0]["reason"], "outline_open_or_branched")
        self.geometry["outline_segments"] = loop("cross", [(0, 0), (10000, 10000), (0, 10000), (10000, 0)])
        report = self.result()
        self.assertEqual(report["outline_issues"][0]["reason"], "outline_intersection")
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.geometry["outline_issues"] = [{"reason": "unsupported_outline_shape", "uuid": "arc"}]
        report = self.result()
        self.assertEqual(report["outline_issues"], self.geometry["outline_issues"])
        self.assertEqual(report["checks"][0]["reason"], "outline_unconfirmed")

    def test_mismatched_revision_or_incomplete_criterion_cannot_confirm_failure(self):
        self.criterion["limit"] = 0.6
        self.context["design_revision"] = "UNRELATED"
        report = self.result()
        self.assertEqual(report["overall_status"], "REVIEW_REQUIRED")
        self.assertIn("design_revision_unconfirmed", report["blockers"])
        self.assertEqual(report["checks"][0]["reason"], "scope_unverified")
        self.context["design_revision"] = self.profile["design_revision"]
        del self.criterion["source"]
        row, = self.result()["checks"]
        self.assertEqual((row["status"], row["reason"]), ("REVIEW_REQUIRED", "edge_criterion_unconfirmed"))
        self.board["items"][TRACK_UUID]["kind"] = "PCB_ARC"
        self.assertEqual(self.result()["checks"][0]["reason"], "unsupported_trace_geometry")

    def test_distance_for_diagonal_projection_and_collinear_segments(self):
        self.assertEqual(segment_squared_distance(((0, 0), (10, 10)), ((2, 1), (9, 8))), Fraction(1, 2))
        self.assertEqual(segment_squared_distance(((0, 0), (2, 0)), ((4, 0), (5, 0))), 4)
        self.assertEqual(segment_squared_distance(((0, 0), (2, 0)), ((1, 0), (5, 0))), 0)

    def test_rectangle_is_split_into_four_edges_with_the_same_uuid(self):
        vertices = [(0, 0), (1000, 0), (1000, 2000), (0, 2000)]
        item = SimpleNamespace(
            GetLayer=lambda: 1, GetClass=lambda: "PCB_SHAPE", GetShape=lambda: 3,
            m_Uuid=SimpleNamespace(AsString=lambda: TRACK_UUID),
            GetRectCorners=lambda: [SimpleNamespace(x=x, y=y) for x, y in vertices],
        )
        board = SimpleNamespace(GetDrawings=lambda: [item], GetFootprints=list, GetTracks=list)
        api = SimpleNamespace(Edge_Cuts=1, SHAPE_T_SEGMENT=2, SHAPE_T_RECT=3, FromMM=lambda value: value * 1000)
        geometry = collect_edge_geometry(board, api, set())
        self.assertEqual(geometry["outline_segments"], [(TRACK_UUID, edge) for _, edge in loop("rect", vertices)])
        self.assertEqual(geometry["outline_issues"], [])

    @unittest.skipUnless(find_spec("pcbnew") and DEMO.with_suffix(".kicad_pro").is_file() and DEMO.is_file(),
                         "Нужны pcbnew и демонстрационный проект")
    def test_cli_reads_native_outline_and_project_limit(self):
        project_path = DEMO.with_suffix(".kicad_pro")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        limit = project["board"]["design_settings"]["rules"]["min_copper_edge_clearance"]
        checksum = sha256(DEMO.read_bytes()).hexdigest()
        profile = {
            "profile_id": "edge-demo", "description": "Чтение контура демонстрационного проекта", "inherits": None,
            "design_revision": "KICAD-DEMO", "approved": False, "feature_tags": [], "constraints": {},
            "review_objects": {"LAYOUT": {"kind": "board", "modes": ["normal"]}},
            "criteria": {"EDGE": self.criterion | {
                "mode": "normal", "limit": limit, "source": {
                    "document": project_path.name, "revision": "KiCad demo",
                    "section": "board.design_settings.rules.min_copper_edge_clearance",
                },
            }},
        }
        context = {
            "board_source": DEMO.name, "board_sha256": checksum, "design_revision": "KICAD-DEMO",
            "mode": "normal", "items": {DEMO_UUID: {"object_id": "LAYOUT"}},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            profile_path, context_path = path / "profile.json", path / "context.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            context_path.write_text(json.dumps(context), encoding="utf-8")
            command = [sys.executable, "-B", str(ROOT / "scripts/pcb_profile.py"), "compare-copper-edge",
                       str(profile_path), "--board", str(DEMO), "--context", str(context_path)]
            report = json.loads(subprocess.check_output(command, timeout=30))
        self.assertEqual(report["outline_segment_count"], 5)
        self.assertEqual(report["overall_status"], "PASS")
        self.assertAlmostEqual(report["checks"][0]["pcb_value"], 3.56)
        self.assertEqual(report["checks"][0]["criterion"]["min"], limit)
        self.assertEqual(sha256(DEMO.read_bytes()).hexdigest(), checksum)
