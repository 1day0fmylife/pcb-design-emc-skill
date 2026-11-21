import copy
from hashlib import sha256
from importlib.util import find_spec
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.kicad_drc import import_drc
from scripts.pcb_criteria import validate_report_findings
from scripts.pcb_profile import ProfileReview, ROOT, markdown, validate_document


FIXTURE = ROOT / "examples" / "relay-review"
KICAD_QA = ROOT / "examples" / "kicad-multinetclasses"
KICAD_DEMO = Path("/usr/share/kicad/demos/pic_programmer/pic_programmer.kicad_pcb")


class CriteriaAndDrcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()

    def resolve_fixture(self):
        return self.review.profile(FIXTURE / "profile.yaml")

    def test_two_domain_pairs_have_independent_clearance_and_creepage(self):
        plan = self.review.analyze(self.resolve_fixture())
        rows = {
            (row["object_id"], row["mode"], row["metric"]): row for row in plan["criterion_plan"]
        }
        for object_id, mode in [("CTRL-CONTACT", "normal"), ("CONTACT-CHASSIS", "service")]:
            for metric in ("clearance", "creepage"):
                with self.subTest(object=object_id, metric=metric):
                    row = rows[object_id, mode, metric]
                    self.assertEqual(row["status"], "REVIEW_REQUIRED")
                    self.assertIn("source", row["missing_data"])
        missing = rows["CTRL-CONTACT", "normal", "creepage"]
        self.assertIsNone(missing["criterion_id"])
        self.assertIn("Критерии по объектам и режимам", markdown(plan))

    def test_matrix_rejects_wrong_units_unknown_modes_and_duplicate_slots(self):
        chain = self.review.load_profile_chain(FIXTURE / "profile.yaml")
        for change, message in [
            ({"unit": "ohm"}, "mm"),
            ({"mode": "service"}, "режим"),
            ({"object_id": "SIGNAL"}, "domain_pair"),
            ({"rule_id": "PCB-GEO-001"}, "не связано"),
        ]:
            with self.subTest(change=change):
                layers = copy.deepcopy(chain)
                leaf = layers[-1]
                leaf["criteria"]["CTRL-CLEARANCE"].update(change)
                with self.assertRaisesRegex(ValueError, message):
                    validate_document(leaf, self.review.validators["profile"], "fixture")
                    self.review.resolve_profile(layers)
        chain[-1]["criteria"]["DUPLICATE"] = chain[-1]["criteria"]["CTRL-CLEARANCE"].copy()
        with self.assertRaisesRegex(ValueError, "повтор критерия"):
            self.review.resolve_profile(chain)

    def class_pair_chain(self):
        chain = self.review.load_profile_chain(FIXTURE / "profile.yaml")
        chain[-1]["review_objects"].update({
            "POWER": {"kind": "net_class", "modes": ["normal", "service"]},
            "SIGNAL": {"kind": "net_class", "modes": ["normal", "service"]},
            "SIGNAL-POWER": {
                "kind": "net_class_pair", "members": ["SIGNAL", "POWER"], "modes": ["normal", "service"],
            },
        })
        return chain

    def test_pairs_of_net_classes_require_valid_members_and_common_modes(self):
        chain = self.class_pair_chain()
        leaf = chain[-1]
        validate_document(leaf, self.review.validators["profile"], "fixture")
        profile = self.review.resolve_profile(chain)
        rows = [row for row in self.review.analyze(profile)["criterion_plan"] if row["object_id"] == "SIGNAL-POWER"]
        self.assertEqual([(row["mode"], row["metric"], row["rule_id"], row["status"]) for row in rows], [
            ("normal", "copper_gap", "PCB-GEO-001", "REVIEW_REQUIRED"),
            ("service", "copper_gap", "PCB-GEO-001", "REVIEW_REQUIRED"),
        ])
        objects = leaf["review_objects"]
        pair = objects["SIGNAL-POWER"]
        objects["POWER-SIGNAL"] = pair | {"members": list(reversed(pair["members"]))}
        with self.assertRaisesRegex(ValueError, "Повтор пары классов сетей"):
            self.review.resolve_profile(chain)
        del objects["POWER-SIGNAL"]
        objects["POWER"]["modes"].remove("service")
        with self.assertRaisesRegex(ValueError, "не объявлен в режимах service"):
            self.review.resolve_profile(chain)
        pair["members"][1] = "UNKNOWN"
        with self.assertRaisesRegex(ValueError, "объявленным объектом net_class"):
            self.review.resolve_profile(chain)

    def test_report_compares_result_to_distinct_object_criteria(self):
        # Arbitrary synthetic test input exercises the implementation. It is not a PCB design limit.
        chain = self.review.load_profile_chain(FIXTURE / "profile.yaml")
        limits = {"CTRL-CLEARANCE": 2, "CHASSIS-CLEARANCE": 4}
        for criterion_id, number in limits.items():
            chain[-1]["criteria"][criterion_id].update(
                limit=number, source={"document": "fixture", "revision": "A", "section": criterion_id}
            )
        profile = self.review.resolve_profile(chain)
        plan = self.review.analyze(profile)
        findings = [
            {
                "rule_id": "PCB-GEO-002", "criterion_id": criterion_id,
                "object_id": profile["criteria"][criterion_id]["object_id"], "status": status,
                "evidence": "Учебный результат", "evidence_ref": {"artifact": "fixture", "locator": criterion_id},
                "measurement": {"value": 3, "unit": "mm"}, "verification": "fixture",
            }
            for criterion_id, status in [("CTRL-CLEARANCE", "PASS"), ("CHASSIS-CLEARANCE", "FAIL")]
        ]
        report = {
            "design_revision": "FIXTURE-A", "profile_id": "relay-fixture", "overall_status": "FAIL",
            "scope_criteria": list(limits), "findings": findings,
        }
        validate_document(report, self.review.validators["report"], "fixture")
        validate_report_findings(profile, report, plan)
        with self.assertRaisesRegex(ValueError, "статус не соответствует"):
            validate_report_findings(
                profile, report | {"findings": [findings[1] | {"status": "PASS"}]}, plan
            )
        with self.assertRaisesRegex(ValueError, "Ревизия"):
            validate_report_findings(profile, report | {"design_revision": "FIXTURE-B"}, plan)
        with self.assertRaisesRegex(ValueError, "единица"):
            validate_report_findings(
                profile, report | {"findings": [findings[0] | {"measurement": {"value": 3, "unit": "ohm"}}]},
                plan,
            )
        with self.assertRaisesRegex(ValueError, "исходные данные"):
            raw = self.resolve_fixture()
            validate_report_findings(raw, report, self.review.analyze(raw))
        with self.assertRaisesRegex(ValueError, "всем критериям"):
            partial = report | {"overall_status": "PASS", "findings": findings[:1]}
            validate_document(partial, self.review.validators["report"], "fixture")
            validate_report_findings(profile, partial, plan)
        missing_evidence = findings[0].copy()
        missing_evidence.pop("evidence_ref")
        with self.assertRaisesRegex(ValueError, "evidence_ref"):
            validate_document(report | {"findings": [missing_evidence, findings[1]]},
                              self.review.validators["report"], "fixture")

    def test_qualitative_review_keeps_a_verifiable_pass_path(self):
        chain = self.review.load_profile_chain(FIXTURE / "profile.yaml")
        leaf = chain[-1]
        leaf["review_objects"]["K1"] = {"kind": "component", "modes": ["normal"]}
        leaf["criteria"]["COIL-PATH"] = {
            "rule_id": "PCB-RELAY-001", "object_id": "K1", "mode": "normal", "metric": "qualitative",
            "requirement": "Путь тока катушки после отключения прослеживается по схеме.",
            "source": {"document": "fixture", "revision": "A", "section": "coil"},
        }
        validate_document(leaf, self.review.validators["profile"], "fixture")
        profile = self.review.resolve_profile(chain)
        report = {
            "design_revision": "FIXTURE-A", "profile_id": "relay-fixture", "overall_status": "PASS",
            "scope_criteria": ["COIL-PATH"],
            "findings": [{
                "rule_id": "PCB-RELAY-001", "criterion_id": "COIL-PATH", "object_id": "K1",
                "status": "PASS", "assessment": "satisfied", "evidence": "Учебная проверка схемы",
                "evidence_ref": {"artifact": "fixture", "locator": "coil"}, "verification": "Ревью схемы",
            }],
        }
        validate_document(report, self.review.validators["report"], "fixture")
        plan = self.review.analyze(profile)
        validate_report_findings(profile, report, plan)
        report["findings"][0]["assessment"] = "not_satisfied"
        with self.assertRaisesRegex(ValueError, "статус не соответствует"):
            validate_report_findings(profile, report, plan)

    def test_drc_import_preserves_errors_exclusions_and_traceability(self):
        report = self.review.import_drc(
            self.resolve_fixture(), FIXTURE / "drc.json", FIXTURE / "drc-context.yaml"
        )
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["counts"], {"error": 1, "warning": 0, "excluded": 1})
        active, excluded = report["findings"]
        self.assertEqual(active["candidate_criterion_ids"], ["SIGNAL-GAP"])
        self.assertEqual(active["items"][0]["project_context"]["net"], "RELAY_CTRL")
        self.assertEqual(active["evidence_ref"]["locator"], "/violations/0")
        self.assertEqual(report["context_artifact"], str(FIXTURE / "drc-context.yaml"))
        self.assertEqual(excluded["candidate_criterion_ids"], [])
        self.assertTrue(excluded["excluded"])
        self.assertTrue(all(item["status"] == "REVIEW_REQUIRED" for item in report["findings"]))
        self.review.validate_report(self.resolve_fixture(), FIXTURE / "review.json")

    def test_drc_clearance_links_unordered_pair_with_two_known_classes_and_nets(self):
        chain = self.class_pair_chain()
        leaf = chain[-1]
        criterion = {"object_id": "SIGNAL-POWER", "rule_id": "PCB-GEO-001", "metric": "copper_gap",
                     "unit": "mm", "limit": None}
        leaf["criteria"].update({"PAIR-NORMAL": criterion | {"mode": "normal"},
                                 "PAIR-SERVICE": criterion | {"mode": "service"}})
        validate_document(leaf, self.review.validators["profile"], "fixture")
        profile = self.review.resolve_profile(chain)
        source = self.review.load_validated(FIXTURE / "drc.json", "kicad-drc")
        context = self.review.load_validated(FIXTURE / "drc-context.yaml", "kicad-context")
        violation = source["violations"][0]
        first, second = [item["uuid"] for item in violation["items"]]
        items = {first: {"object_id": "POWER", "net": "PWR"}, second: {"object_id": "SIGNAL", "net": "CTRL"}}

        def finding(affected=violation, metadata=items, mode="normal", board=None):
            return import_drc(profile, source | {"violations": [affected]}, context | {"items": metadata, "mode": mode},
                              "fixture.json", self.review.rules, board)["findings"][0]

        result = finding(violation | {"items": list(reversed(violation["items"]))})
        self.assertEqual(result["candidate_object_id"], "SIGNAL-POWER")
        self.assertEqual(result["candidate_criterion_ids"], ["PAIR-NORMAL"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(finding(mode=None)["candidate_criterion_ids"], ["PAIR-NORMAL", "PAIR-SERVICE"])
        self.assertEqual(finding(mode="service")["candidate_criterion_ids"], ["PAIR-SERVICE"])
        self.assertEqual(finding(mode="unknown")["candidate_criterion_ids"], [])
        self.assertIsNone(finding(violation | {"excluded": True})["candidate_object_id"])
        self.assertIsNone(finding(violation | {"type": "track_width"})["candidate_object_id"])
        self.assertIsNone(finding(metadata={first: items[first], second: {"object_id": "SIGNAL"}})["candidate_object_id"])
        self.assertIsNone(finding(metadata=items | {second: items[second] | {"net": "PWR"}})["candidate_object_id"])
        self.assertIsNone(finding(metadata={first: items[first]})["candidate_object_id"])

        board = {"source": source["source"], "board_sha256": "a" * 64, "pcbnew_version": "10.0.8",
                 "items": {uuid: {"net": item["net"], "layers": ["F.Cu"]} for uuid, item in items.items()}}
        metadata = {uuid: {"object_id": item["object_id"]} for uuid, item in items.items()}
        self.assertEqual(finding(metadata=metadata, board=board)["candidate_criterion_ids"], ["PAIR-NORMAL"])
        del board["items"][first]
        self.assertIsNone(finding(board=board)["candidate_object_id"])
        with self.assertRaisesRegex(ValueError, "ID отдельного класса"):
            finding(metadata={first: {"object_id": "SIGNAL-POWER"}})

    def test_drc_leaves_unknown_objects_unmapped_and_empty_report_unevaluated(self):
        source = self.review.load_validated(FIXTURE / "drc.json", "kicad-drc")
        context = self.review.load_validated(FIXTURE / "drc-context.yaml", "kicad-context")
        with tempfile.TemporaryDirectory() as folder:
            report_file = Path(folder) / "drc.json"
            context_file = Path(folder) / "context.json"
            context_file.write_text(json.dumps(context | {"items": {}}), encoding="utf-8")
            report_file.write_text(json.dumps(source), encoding="utf-8")
            profile = self.resolve_fixture()
            active = self.review.import_drc(profile, report_file, context_file)["findings"][0]
            self.assertEqual(active["candidate_criterion_ids"], [])
            report_file.write_text(json.dumps(source | {"violations": []}), encoding="utf-8")
            self.assertEqual(
                self.review.import_drc(profile, report_file, context_file)["overall_status"], "NOT_EVALUATED"
            )
            report_file.write_text(json.dumps(source | {"source": "other.kicad_pcb"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "board_source"):
                self.review.import_drc(profile, report_file, context_file)

    def test_drc_track_width_uses_only_the_confirmed_direction(self):
        profile = self.review.profile(KICAD_QA / "profile.yaml")
        maximum = profile["criteria"]["RULE-3-MAX"]
        profile["criteria"]["WIDTH-MIN"] = maximum | {"metric": "trace_width"}
        context = self.review.load_validated(KICAD_QA / "context.yaml", "kicad-context")
        source = self.review.load_validated(FIXTURE / "drc.json", "kicad-drc")
        native = source | {
            "source": context["board_source"],
            "violations": [{
                "type": "track_width", "severity": "error", "description": "Локализованный текст",
                "items": [{"uuid": next(iter(context["items"])), "description": "Дорожка",
                           "pos": {"x": 0, "y": 0}}],
            }],
        }
        for bounds, expected in [
            ({}, []), ({"/violations/0": "min"}, ["WIDTH-MIN"]), ({"/violations/0": "max"}, ["RULE-3-MAX"]),
        ]:
            with self.subTest(bounds=bounds):
                result = import_drc(
                    profile, native, context | {"track_width_bounds": bounds}, "drc.json", self.review.rules
                )
                finding = result["findings"][0]
                self.assertEqual(finding["candidate_rule_id"], "PCB-DFM-001")
                self.assertEqual(finding["candidate_criterion_ids"], expected)
        with self.assertRaisesRegex(ValueError, "track_width_bounds"):
            import_drc(profile, native, context, "drc.json", self.review.rules)

    def test_geometric_violations_route_distinct_rules_and_keep_native_findings(self):
        profile = self.resolve_fixture()
        context = self.review.load_validated(FIXTURE / "drc-context.yaml", "kicad-context")
        native = self.review.load_validated(FIXTURE / "drc.json", "kicad-drc")
        types = {
            "copper_edge_clearance": "PCB-GEO-003", "copper_outside_board": "PCB-GEO-003",
            "annular_width": "PCB-GEO-005", "hole_clearance": "PCB-GEO-006",
            "hole_to_hole": "PCB-DFM-001", "holes_co_located": "PCB-DFM-001",
            "drill_out_of_range": "PCB-DFM-001", "microvia_drill_out_of_range": "PCB-DFM-001",
            "via_diameter": "PCB-DFM-001", "solder_mask_bridge": "PCB-DFM-003",
            "isolated_copper": "PCB-GND-003", "creepage": None,
        }
        violations = [
            native["violations"][0] | {"type": native_type, "excluded": native_type == "annular_width"}
            for native_type in types
        ]
        result = import_drc(profile, native | {"violations": violations}, context, "drc.json", self.review.rules)
        self.assertEqual({row["type"]: row["candidate_rule_id"] for row in result["findings"]}, types)
        self.assertEqual(result["findings"][2]["evidence_ref"]["locator"], "/violations/2")
        self.assertNotIn("PCB-GEO-005", result["active_candidates_by_rule"])
        self.assertTrue(all(row["status"] == "REVIEW_REQUIRED" for row in result["findings"]))
        self.assertTrue(all(row["candidate_criterion_ids"] == [] for row in result["findings"]))

    def test_copper_to_edge_requires_identified_edge_cuts_and_shared_board_scope(self):
        chain = self.review.load_profile_chain(FIXTURE / "profile.yaml")
        leaf = chain[-1]
        leaf["review_objects"]["LAYOUT"] = {"kind": "board", "modes": ["normal"]}
        leaf["criteria"]["EDGE"] = {
            "rule_id": "PCB-GEO-003", "object_id": "LAYOUT", "mode": "normal",
            "metric": "copper_to_edge", "limit": None, "unit": "mm",
        }
        validate_document(leaf, self.review.validators["profile"], "fixture")
        profile = self.review.resolve_profile(chain)
        context = self.review.load_validated(FIXTURE / "drc-context.yaml", "kicad-context")
        native = self.review.load_validated(FIXTURE / "drc.json", "kicad-drc")
        native["violations"] = [native["violations"][0] | {"type": "copper_edge_clearance"}]
        first, second = context["items"]
        for layer, expected in [("Edge.Cuts", ["EDGE"]), ("Margin", []), ("F.Cu", [])]:
            with self.subTest(layer=layer):
                items = {first: {"object_id": "LAYOUT", "layer": layer},
                         second: {"object_id": "LAYOUT", "layer": "F.Cu"}}
                result = import_drc(profile, native, context | {"items": items}, "drc.json", self.review.rules)
                finding = result["findings"][0]
                self.assertEqual(finding["candidate_rule_id"], "PCB-GEO-003")
                self.assertEqual(finding["candidate_criterion_ids"], expected)
                self.assertEqual(finding["candidate_metric"], "copper_to_edge" if expected else None)
        items[first]["layer"] = "Edge.Cuts"
        items[second].pop("object_id")
        result = import_drc(profile, native, context | {"items": items}, "drc.json", self.review.rules)
        self.assertEqual(result["findings"][0]["candidate_criterion_ids"], [])

    def test_drc_import_extracts_board_context_and_detects_mismatches(self):
        profile = self.resolve_fixture()
        native = self.review.load_validated(FIXTURE / "drc.json", "kicad-drc")
        context = self.review.load_validated(FIXTURE / "drc-context.yaml", "kicad-context")
        first, second = context["items"]
        declared = {uuid: {"object_id": "SIGNAL"} for uuid in context["items"]}
        context = context | {"board_sha256": "a" * 64, "items": declared}
        board = {
            "source": native["source"], "board_sha256": "a" * 64, "pcbnew_version": "10.0.8",
            "items": {
                first: {"kind": "PAD", "net": "RELAY_CTRL", "refdes": "K1", "layers": ["F.Cu"]},
                second: {"kind": "PAD", "net": "RELAY_SENSE", "refdes": "Q1", "layers": ["F.Cu", "B.Cu"]},
            },
        }

        def run(metadata, inventory):
            return import_drc(profile, native, metadata, "drc.json", self.review.rules, inventory)

        result = run(context, board)
        self.assertTrue(result["board"]["sha256_verified_against_context"])
        self.assertEqual(result["board"]["matches"], {"matched": 2, "not_found": 0})
        finding = result["findings"][0]
        self.assertEqual(finding["candidate_criterion_ids"], ["SIGNAL-GAP"])
        self.assertEqual(finding["items"][0]["project_context"],
                         {"object_id": "SIGNAL", "refdes": "K1", "net": "RELAY_CTRL", "layer": "F.Cu"})
        self.assertNotIn("layer", finding["items"][1]["project_context"])
        self.assertEqual(finding["items"][1]["board_item"]["layers"], ["F.Cu", "B.Cu"])

        partial = run(context, board | {"items": {first: board["items"][first]}})
        self.assertEqual(partial["board"]["matches"], {"matched": 1, "not_found": 1})
        self.assertEqual(partial["findings"][0]["candidate_criterion_ids"], [])
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            run(context | {"board_sha256": "b" * 64}, board)
        for key, value in [("refdes", "K2"), ("net", "SENSE"), ("layer", "B.Cu")]:
            items = declared | {first: declared[first] | {key: value}}
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "расходится с PCB"):
                run(context | {"items": items}, board)

    @unittest.skipUnless(find_spec("pcbnew") is not None and KICAD_DEMO.is_file(),
                         "Нужны pcbnew и установленная демонстрационная плата KiCad")
    def test_inventory_cli_reads_native_demo_without_modification(self):
        before = sha256(KICAD_DEMO.read_bytes()).hexdigest()
        command = [sys.executable, "-B", str(ROOT / "scripts" / "pcb_profile.py")]
        result = subprocess.run(
            [*command, "inventory-board", str(KICAD_DEMO)], check=True, capture_output=True,
        )
        inventory = json.loads(result.stdout)
        self.assertEqual(inventory["board_sha256"], before)
        self.assertEqual(sha256(KICAD_DEMO.read_bytes()).hexdigest(), before)
        self.assertTrue(inventory["nets"])
        for kind in ("FOOTPRINT", "PAD", "PCB_TRACK", "PCB_VIA", "ZONE"):
            self.assertGreater(inventory["item_counts"][kind], 0)
        for item in inventory["items"].values():
            if item["kind"] == "PAD":
                footprint = inventory["items"][item["footprint_uuid"]]
                self.assertEqual(item["refdes"], footprint["refdes"])
        holes = [item["hole"] for item in inventory["items"].values() if "hole" in item]
        self.assertEqual({hole["kind"] for hole in holes}, {"PTH", "NPTH"})
        self.assertTrue(all(hole["x_mm"] > 0 and hole["y_mm"] > 0 for hole in holes))

    def test_report_compares_width_against_an_upper_bound(self):
        profile = self.review.profile(KICAD_QA / "profile.yaml")
        plan = self.review.analyze(profile)
        finding = {
            "rule_id": "PCB-DFM-001", "criterion_id": "RULE-3-MAX", "object_id": "DEFAULT", "status": "FAIL",
            "evidence": "Ширина сегмента QA-платы из KiCad DRC.",
            "evidence_ref": {"artifact": "drc.json", "locator": "/violations/0"},
            "measurement": {"unit": "mm", "value": 1.016}, "verification": "DRC",
        }
        report = {
            "design_revision": profile["design_revision"], "profile_id": profile["profile_id"],
            "overall_status": "FAIL", "scope_criteria": ["RULE-3-MAX"], "findings": [finding],
        }
        validate_document(report, self.review.validators["report"], "fixture")
        validate_report_findings(profile, report, plan)
        reported_pass = report | {"overall_status": "PASS", "findings": [finding | {"status": "PASS"}]}
        validate_document(reported_pass, self.review.validators["report"], "fixture")
        with self.assertRaisesRegex(ValueError, "статус не соответствует"):
            validate_report_findings(profile, reported_pass, plan)

    def test_cli_runs_fixture_without_kicad(self):
        cmd = [sys.executable, "-B", str(ROOT / "scripts" / "pcb_profile.py")]
        profile = str(FIXTURE / "profile.yaml")
        result = subprocess.run(
            [*cmd, "import-drc", profile, str(FIXTURE / "drc.json"),
             "--context", str(FIXTURE / "drc-context.yaml")],
            check=True, capture_output=True,
        )
        self.assertEqual(json.loads(result.stdout)["report_type"], "kicad_drc_import")
        subprocess.run(
            [*cmd, "validate-report", profile, str(FIXTURE / "review.json")],
            check=True, capture_output=True,
        )
