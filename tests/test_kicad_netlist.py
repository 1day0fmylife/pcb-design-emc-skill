import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from xml.etree import ElementTree

from scripts.kicad_netlist import RULE_ID, compare_netlist, read_kicad_netlist
from scripts.pcb_profile import ROOT


DEMO = Path("/usr/share/kicad/demos/pic_programmer")
XML = """<export version="E"><design><source>sample.kicad_sch</source></design>
<components><comp ref="U1"/><comp ref="R2"/></components>
<nets>
  <net name="/VPP/MCLR"><node ref="U1" pin="1"/></net>
  <net name="unconnected-(U1-Pad2)"><node ref="U1" pin="2" pintype="input+no_connect"/></net>
  <net name="GND"><node ref="R2" pin="1"/><node ref="U1" pin="3"/></net>
</nets></export>"""


def pad(refdes, number, parent, raw=None, expanded=None, electrical=True):
    item = {
        "kind": "PAD", "refdes": refdes, "pad_number": number, "footprint_uuid": parent,
        "electrical": electrical,
    }
    if raw is not None:
        item.update(net=raw, netlist_name=expanded or raw)
    return item


class NetlistComparisonTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.netlist_path = Path(scratch.name) / "sample.xml"
        self.netlist_path.write_text(XML, encoding="utf-8")
        self.board = {
            "source": "sample.kicad_pcb", "board_sha256": "a" * 64, "pcbnew_version": "10.0.8",
            "items": {
                "u": {"kind": "FOOTPRINT", "board_only": False, "refdes": "U1"},
                "r": {"kind": "FOOTPRINT", "board_only": False, "refdes": "R2"},
                "m": {"kind": "FOOTPRINT", "board_only": True, "refdes": "H1"},
                "pad-1": pad("U1", "1", "u", "/VPP{slash}MCLR", "/VPP/MCLR"),
                "pad-2": pad("U1", "2", "u"),
                "pad-3": pad("U1", "3", "u", "GND"),
                "pad-4": pad("R2", "1", "r", "GND"),
                "drill": pad("R2", "3", "r", electrical=False),
            },
        }

    def run_comparison(self, revision="A"):
        return compare_netlist(self.board, read_kicad_netlist(self.netlist_path), revision)

    def test_matching_pins_allow_explicit_no_connect_to_have_no_net(self):
        result = self.run_comparison()
        self.assertEqual(result["overall_status"], "PASS")
        self.assertEqual(result["rule_id"], RULE_ID)
        self.assertEqual(result["matched_components"], 2)
        self.assertEqual(result["checked_pins"], 4)

    def test_reports_missing_components_pads_and_wrong_nets(self):
        items = self.board["items"]
        del items["r"], items["pad-3"], items["pad-4"]
        items["pad-1"].update(net="GND", netlist_name="GND")
        items["p-extra"] = pad("U1", "4", "u", "GND")
        items["r-extra"] = {"kind": "FOOTPRINT", "board_only": False, "refdes": "R3"}
        result = self.run_comparison()
        self.assertEqual(result["overall_status"], "FAIL")
        findings = {item["kind"]: item for item in result["findings"]}
        self.assertEqual(findings["missing_footprint"]["refdes"], "R2")
        self.assertEqual(findings["extra_footprint"]["refdes"], "R3")
        self.assertEqual(findings["missing_pad"]["pin"], "3")
        self.assertEqual(findings["extra_connected_pad"]["pin"], "4")
        self.assertEqual(findings["net_mismatch"]["schematic_net"], "/VPP/MCLR")
        self.assertEqual(findings["net_mismatch"]["pad_uuids"], ["pad-1"])

    def test_incomplete_and_ambiguous_inputs_require_review(self):
        self.assertEqual(compare_netlist(None, None, None)["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual(self.run_comparison(revision=None)["overall_status"], "REVIEW_REQUIRED")
        self.board["items"]["p-duplicate"] = pad("U1", "1", "u", "/VPP/MCLR")
        result = self.run_comparison()
        self.assertEqual(result["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["findings"][0]["kind"], "ambiguous_pcb_net_name")

        self.netlist_path.write_text(XML.replace('<node ref="R2" pin="1"/>', '<node ref="U1" pin="1"/>'),
                                    encoding="utf-8")
        result = self.run_comparison()
        self.assertEqual(result["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["checked_pins"], 0)
        self.assertIn("ambiguous_schematic_pin", [item["kind"] for item in result["findings"]])

    def test_unclassified_pads_require_review_only_where_they_affect_parity(self):
        items = self.board["items"]
        items["npth-unnumbered"] = pad("R2", "", "r", electrical=None)
        items["npth-board-only"] = pad("H1", "", "m", "GND", electrical=None)
        items["npth-extra"] = pad("R2", "4", "r", electrical=None)
        self.assertEqual(self.run_comparison()["overall_status"], "PASS")

        items["pad-1"]["electrical"] = None
        items["npth-extra"].update(net="GND", netlist_name="GND")
        items["npth-duplicate"] = pad("R2", "1", "r", "VCC", electrical=None)
        unknown = self.run_comparison()
        self.assertEqual(unknown["overall_status"], "REVIEW_REQUIRED")
        self.assertEqual(unknown["checked_pins"], 2)
        self.assertEqual({row["kind"] for row in unknown["findings"]}, {"unresolved_pad_electrical"})
        self.assertEqual({(row["refdes"], row["pin"], row["unclassified_pads"][0]["uuid"])
                          for row in unknown["findings"]},
                         {("U1", "1", "pad-1"), ("R2", "1", "npth-duplicate"), ("R2", "4", "npth-extra")})

        items["pad-4"].update(net="VCC", netlist_name="VCC")
        items["npth-unnumbered"]["net"] = "GND"
        result = self.run_comparison()
        self.assertEqual(result["overall_status"], "FAIL")
        failure, = [row for row in result["findings"] if row["status"] == "FAIL"]
        self.assertEqual((failure["kind"], failure["refdes"], failure["pin"]), ("net_mismatch", "R2", "1"))
        self.assertTrue(any(row.get("unclassified_pads") == [{"uuid": "npth-unnumbered", "net": "GND"}]
                            for row in result["findings"]))

    def test_rejects_non_kicad_xml_and_dtd(self):
        for invalid in ("<data/>", "<!DOCTYPE export><export/>"):
            self.netlist_path.write_text(invalid, encoding="utf-8")
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "XML-нетлист KiCad"):
                read_kicad_netlist(self.netlist_path)

    @unittest.skipUnless(find_spec("pcbnew") and shutil.which("kicad-cli") and
                         (DEMO / "pic_programmer.kicad_sch").is_file(),
                         "Нужны KiCad CLI, pcbnew и демонстрационный проект")
    def test_cli_compares_native_demo_and_detects_changed_export(self):
        board = DEMO / "pic_programmer.kicad_pcb"
        schematic = DEMO / "pic_programmer.kicad_sch"
        files = [board, *DEMO.glob("*.kicad_sch")]
        before = [sha256(path.read_bytes()).hexdigest() for path in files]
        directory = self.netlist_path.parent
        env = os.environ | {"XDG_CONFIG_HOME": str(directory / "config"),
                            "XDG_CACHE_HOME": str(directory / "cache")}
        subprocess.run(
            ["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
             "--output", str(self.netlist_path), str(schematic)],
            cwd=directory, env=env, capture_output=True, check=True, timeout=30,
        )
        command = [sys.executable, "-B", str(ROOT / "scripts" / "pcb_profile.py"), "compare-netlist",
                   "--board", str(board), "--netlist", str(self.netlist_path), "--design-revision", "demo"]
        clean = json.loads(subprocess.check_output(command, timeout=30))
        self.assertEqual(clean["overall_status"], "PASS")
        self.assertGreater(clean["checked_pins"], 0)
        tree = ElementTree.parse(self.netlist_path)
        original = tree.find("./nets/net[@name='/VPP/MCLR']")
        modified = tree.find("./nets/net[@name='GND']")
        node = original.find("node[@ref='P2'][@pin='1']")
        original.remove(node)
        modified.append(node)
        tree.write(self.netlist_path, encoding="utf-8", xml_declaration=True)
        changed = json.loads(subprocess.check_output(command, timeout=30))
        self.assertEqual(changed["overall_status"], "FAIL")
        self.assertEqual([(item["kind"], item["refdes"], item["pin"]) for item in changed["findings"]],
                         [("net_mismatch", "P2", "1")])
        self.assertEqual([sha256(path.read_bytes()).hexdigest() for path in files], before)
