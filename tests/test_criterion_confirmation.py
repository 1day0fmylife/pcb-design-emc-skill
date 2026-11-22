import unittest
from datetime import date, timedelta

from scripts.kicad_trace_width import compare_trace_width
from scripts.pcb_criteria import validate_report_findings
from scripts.pcb_profile import ROOT, ProfileReview, markdown, validate_document


QA = ROOT / "examples/kicad-multinetclasses"
CRITERION_ID = "RULE-3-MAX"
ALTERNATIVE = {"document": "Учебный альтернативный источник", "revision": "A", "section": "Ширина"}


class CriterionConfirmationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = ProfileReview()

    def setUp(self):
        self.chain = self.review.load_profile_chain(QA / "profile.yaml")
        self.leaf = self.chain[-1]
        self.criterion = self.leaf["criteria"][CRITERION_ID]

    def analyze(self):
        validate_document(self.leaf, self.review.validators["profile"], "fixture")
        profile = self.review.resolve_profile(self.chain)
        plan = self.review.analyze(profile)
        row = next(item for item in plan["criterion_plan"] if item["criterion_id"] == CRITERION_ID)
        return profile, plan, row

    def add_conflict(self):
        # Синтетическое значение проверяет алгоритм выбора источника.
        self.criterion["alternative_sources"] = [{"source": ALTERNATIVE.copy(), "limit": 0.254}]

    def confirm(self):
        self.criterion["confirmation"] = {
            "owner": "Владелец учебного критерия",
            "confirmed_on": "2026-01-01",
            "design_revision": self.leaf["design_revision"],
            "reason": "Выбран критерий из правила учебного QA-проекта.",
        }

    def test_conflict_blocks_comparisons_until_source_choice_is_explained(self):
        self.add_conflict()
        context = self.review.load_validated(QA / "context.yaml", "kicad-context")
        board = {
            "source": context["board_source"], "board_sha256": context["board_sha256"], "pcbnew_version": "10.0.8",
            "items": {uuid: {"kind": "PCB_TRACK", "net": item["net"], "layers": ["F.Cu"], "width_mm": 0.2}
                      for uuid, item in context["items"].items()},
        }
        report = {
            "profile_id": self.leaf["profile_id"], "design_revision": self.leaf["design_revision"],
            "overall_status": "FAIL", "scope_criteria": [CRITERION_ID],
            "findings": [{
                "criterion_id": CRITERION_ID, "object_id": "DEFAULT", "rule_id": "PCB-DFM-001",
                "status": "FAIL", "evidence": "Тестовая ширина", "verification": "Сверка профиля",
                "evidence_ref": {"artifact": "test", "locator": "/checks/0"},
                "measurement": {"value": 0.2, "unit": "mm"},
            }],
        }
        profile, plan, row = self.analyze()
        self.assertEqual((row["source_conflict"], row["status"]), ("unresolved", "REVIEW_REQUIRED"))
        self.assertIn("confirmation.owner", row["missing_data"])
        self.assertIn("source_conflict", row["missing_data"])
        with self.assertRaisesRegex(ValueError, "исходные данные критерия"):
            validate_report_findings(profile, report, plan)
        self.assertEqual(compare_trace_width(profile, board, context, plan)["status_counts"], {"REVIEW_REQUIRED": 2})

        self.confirm()
        profile, plan, row = self.analyze()
        self.assertEqual((row["source_conflict"], row["status"]), ("resolved", "NOT_EVALUATED"))
        self.assertEqual(row["limit"], self.criterion["limit"])
        self.assertFalse(row["missing_data"])
        self.assertIn("Владелец учебного критерия", markdown(plan))
        self.assertIn(ALTERNATIVE["document"], markdown(plan))
        validate_report_findings(profile, report, plan)
        self.assertEqual(compare_trace_width(profile, board, context, plan)["status_counts"], {"FAIL": 2})

    def test_confirmation_requires_current_revision_and_non_future_date(self):
        self.confirm()
        confirmation = self.criterion["confirmation"]
        confirmation["design_revision"] = "OLD-REV"
        confirmation["confirmed_on"] = (date.today() + timedelta(days=1)).isoformat()
        row = self.analyze()[2]
        self.assertEqual(row["status"], "REVIEW_REQUIRED")
        self.assertEqual(set(row["missing_data"]), {"confirmation.design_revision", "confirmation.confirmed_on"})
        confirmation["confirmed_on"] = "2026-02-29"
        with self.assertRaisesRegex(ValueError, "confirmation.confirmed_on.*календарная дата"):
            self.analyze()

    def test_metadata_is_optional_for_legacy_profiles_and_equal_sources(self):
        self.assertEqual(self.analyze()[2]["status"], "NOT_EVALUATED")
        self.criterion["alternative_sources"] = [{"source": ALTERNATIVE.copy(), "limit": self.criterion["limit"]}]
        row = self.analyze()[2]
        self.assertNotIn("source_conflict", row)
        self.assertEqual(row["status"], "NOT_EVALUATED")
        self.criterion["confirmation"] = None
        self.assertEqual(self.analyze()[2]["status"], "REVIEW_REQUIRED")

    def test_duplicate_or_incompatible_alternative_is_rejected(self):
        self.add_conflict()
        alternative = self.criterion["alternative_sources"][0]
        alternative["source"] = self.criterion["source"] | {"section": self.criterion["source"]["section"] + " "}
        with self.assertRaisesRegex(ValueError, "повтор ссылки на источник"):
            self.analyze()
        alternative["source"] = ALTERNATIVE.copy()
        alternative["tolerance"] = 0.01
        with self.assertRaisesRegex(ValueError, "tolerance допустим только для импеданса"):
            self.analyze()
        del alternative["tolerance"]
        alternative["limit"] = float("inf")
        with self.assertRaisesRegex(ValueError, "конечным числом"):
            self.analyze()

    def test_disagreement_between_alternatives_needs_selected_source(self):
        self.add_conflict()
        self.confirm()
        del self.criterion["source"]
        self.criterion["alternative_sources"].append({"source": ALTERNATIVE | {"revision": "B"}, "limit": 0.3})
        row = self.analyze()[2]
        self.assertEqual((row["source_conflict"], row["status"]), ("unresolved", "REVIEW_REQUIRED"))
        self.assertIn("source", row["missing_data"])

    def test_impedance_conflict_accounts_for_tolerance(self):
        self.leaf["review_objects"]["PORT"] = {"kind": "port", "modes": ["qa-drc"]}
        self.criterion.update(rule_id="PCB-HS-001", object_id="PORT", metric="controlled_impedance",
                              limit=50, tolerance=5, unit="ohm")
        self.criterion["alternative_sources"] = [{"source": ALTERNATIVE.copy(), "limit": 50, "tolerance": 3}]
        row = self.analyze()[2]
        self.assertEqual(row["source_conflict"], "unresolved")
        del self.criterion["alternative_sources"][0]["tolerance"]
        with self.assertRaisesRegex(ValueError, "для импеданса требуется tolerance"):
            self.analyze()
