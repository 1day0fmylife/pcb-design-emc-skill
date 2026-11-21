import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.kicad_checks import FLAGS
from scripts.kicad_probe import PCBNEW_REQUIREMENTS, probe_kicad, probe_pcbnew


class KiCadProbeTest(unittest.TestCase):
    def test_reports_missing_cli_and_pcbnew_independently(self):
        with patch("scripts.kicad_probe.shutil.which", return_value=None), patch(
            "scripts.kicad_probe.import_module", side_effect=ImportError("missing pcbnew")
        ):
            report = probe_kicad("missing-cli", 1)
        self.assertIsNone(report["reported_versions_match"])
        for component, reason in (("cli", "cli_missing"), ("pcbnew", "pcbnew_unavailable")):
            self.assertTrue(report[component]["capabilities"])
            for row in report[component]["capabilities"].values():
                self.assertEqual(row, {"status": "NOT_EVALUATED", "reason": reason})

    def test_legacy_flags_and_unconfirmed_format_are_reported_separately(self):
        def run(command, directory, environment, timeout):
            self.assertEqual(environment["KICAD_CONFIG_HOME"], str(directory / "config"))
            self.assertEqual(timeout, 1)
            output = "9.0.8\n"
            exit_code = 0
            if command[-1] == "--help":
                if "netlist" in command:
                    output = "--output --format"
                elif "erc" in command:
                    output = ""
                    exit_code = 1
                else:
                    output = " ".join((*FLAGS, "json"))
            return {"exit_code": exit_code, "stdout": output, "stderr": ""}

        with patch("scripts.kicad_probe.shutil.which", return_value="/tmp/fake-kicad-cli"), patch(
            "scripts.kicad_probe.execute", side_effect=run
        ), patch("scripts.kicad_checks.execute", side_effect=run) as help_run, patch(
            "scripts.kicad_probe.probe_pcbnew", return_value={"version": "10.0.8"},
        ):
            report = probe_kicad("fake-kicad-cli", 1)
        self.assertFalse(report["reported_versions_match"])
        self.assertEqual(report["cli"]["version"], "9.0.8")
        self.assertEqual(help_run.call_count, 3)
        for call in help_run.call_args_list:
            self.assertEqual(call.args[0][-1], "--help")
            self.assertFalse(Path(call.args[1]).exists())
        capabilities = report["cli"]["capabilities"]
        self.assertEqual(capabilities["erc_json"]["reason"], "help_unavailable")
        self.assertEqual(capabilities["drc_json"]["status"], "AVAILABLE")
        self.assertEqual(capabilities["drc_refill_zones"]["missing_flags"], ["--refill-zones"])
        self.assertEqual(capabilities["netlist_kicadxml"], {
            "status": "REVIEW_REQUIRED", "reason": "output_format_unconfirmed",
        })

    def test_missing_npth_classification_does_not_hide_geometry(self):
        module = SimpleNamespace(Version=lambda: "9.0.8", __file__="/tmp/pcbnew.py")
        for symbol in set().union(*PCBNEW_REQUIREMENTS.values()) - {"PAD.IsNPTHWithNoCopper"}:
            owner, separator, attr = symbol.partition(".")
            if separator:
                if not hasattr(module, owner):
                    setattr(module, owner, SimpleNamespace())
                setattr(getattr(module, owner), attr, object())
            else:
                setattr(module, owner, object())

        with patch("scripts.kicad_probe.import_module", return_value=module):
            report = probe_pcbnew()
        self.assertEqual(report["version"], "9.0.8")
        capabilities = report["capabilities"]
        for name in ("inventory_stackup", "inventory_board", "mounting_hole_geometry"):
            self.assertEqual(capabilities[name]["status"], "AVAILABLE")
        self.assertEqual(capabilities["npth_copper_classification"], {
            "status": "NOT_EVALUATED", "reason": "required_api_missing",
            "missing_symbols": ["PAD.IsNPTHWithNoCopper"],
        })
