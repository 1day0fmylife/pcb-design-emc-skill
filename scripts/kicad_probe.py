"""Диагностика установленных KiCad CLI и Python API без входных CAD-файлов."""

import re
import shutil
import sys
import tempfile
from importlib import import_module
from pathlib import Path

if __package__:
    from .kicad_checks import CHECKS, FLAGS, execute, isolated_environment, probe_options
else:
    from kicad_checks import CHECKS, FLAGS, execute, isolated_environment, probe_options


CLI_COMMANDS = {kind: (family, kind) for kind, (family, _) in CHECKS.items()} | {
    "netlist": ("sch", "export", "netlist"),
}
CLI_REQUIREMENTS = {
    "erc_json": ("erc", FLAGS, "json"),
    "drc_json": ("drc", FLAGS, "json"),
    "drc_refill_zones": ("drc", (*FLAGS, "--refill-zones"), "json"),
    "netlist_kicadxml": ("netlist", ("--format", "--output"), "kicadxml"),
}

BOARD_READER = ("PCB_IO_KICAD_SEXPR.LoadBoard", "BOARD.GetFileFormatVersionAtLoad",
                "BOARD.GetEnabledLayers", "BOARD.GetStandardLayerName")
OBJECT_INVENTORY = (
    *BOARD_READER, "LSET.Seq", "BOARD.GetCopperLayerCount", "BOARD.GetTracks",
    "BOARD.Zones", "BOARD.GetDrawings", "BOARD.GetFootprints", "BOARD.GetNetInfo",
    "NETINFO_LIST.NetsByName", "BOARD_ITEM.GetLayerSet", "EDA_ITEM.GetClass", "EDA_ITEM.m_Uuid",
    "KIID.AsString", "FOOTPRINT.GetReference", "FOOTPRINT.IsBoardOnly", "FOOTPRINT.Pads",
    "FOOTPRINT.GetFields", "FOOTPRINT.GraphicalItems", "FOOTPRINT.Zones",
    "PAD.GetNumber", "PAD.IsOnCopperLayer",
    "PAD.GetAttribute", "PAD.GetDrillSize", "PAD.GetDrillShape", "PAD_ATTRIB_PTH", "PAD_ATTRIB_NPTH",
    "PAD_DRILL_SHAPE_CIRCLE", "PAD_DRILL_SHAPE_OBLONG", "ToMM",
    "PAD.GetNetname", "PAD.GetNetCode", "PAD.GetNetnameMsg", "VECTOR2I.x", "VECTOR2I.y",
)
PCBNEW_REQUIREMENTS = {
    "inventory_board": OBJECT_INVENTORY,
    "inventory_stackup": (*BOARD_READER, "LSET.CuStack"),
    "mounting_hole_geometry": (
        *OBJECT_INVENTORY, "PAD.GetEffectiveHoleShape", "SHAPE_SEGMENT.GetSeg", "SEG.A", "SEG.B",
    ),
    "npth_copper_classification": ("PAD.IsNPTHWithNoCopper",),
}


def not_evaluated(reason):
    return {"status": "NOT_EVALUATED", "reason": reason}


def cli_capability(result, options, flags, output_format):
    if result["exit_code"] != 0:
        return not_evaluated("help_unavailable")
    missing = sorted(set(flags) - options)
    if missing:
        return not_evaluated("required_flags_missing") | {"missing_flags": missing}
    help_text = result["stdout"] + "\n" + result["stderr"]
    if not re.search(rf"\b{re.escape(output_format)}\b", help_text, re.IGNORECASE):
        return {"status": "REVIEW_REQUIRED", "reason": "output_format_unconfirmed"}
    return {"status": "AVAILABLE"}


def probe_cli(requested, timeout):
    executable = shutil.which(requested)
    report = {"requested": requested, "path": None, "version": None}
    if executable is None:
        return report | {
            "capabilities": {name: not_evaluated("cli_missing") for name in CLI_REQUIREMENTS},
        }

    executable = str(Path(executable).absolute())
    report["path"] = executable
    with tempfile.TemporaryDirectory(prefix="pcb-kicad-probe-") as directory:
        temporary = Path(directory).resolve()
        env = isolated_environment(temporary)
        version = execute([executable, "version"], temporary, env, timeout)
        report["version_probe"] = version
        if version["exit_code"] != 0 or not version["stdout"].strip():
            return report | {
                "capabilities": {name: not_evaluated("cli_version_unavailable") for name in CLI_REQUIREMENTS},
            }
        report["version"] = version["stdout"].strip()
        help_results = {
            name: probe_options([executable, *command], temporary, env, timeout)
            for name, command in CLI_COMMANDS.items()
        }

    report["help_probes"] = {name: result for name, (result, _) in help_results.items()}
    report["capabilities"] = {
        name: cli_capability(*help_results[command], flags, output_format)
        for name, (command, flags, output_format) in CLI_REQUIREMENTS.items()
    }
    return report


def has_symbol(module, path):
    owner, separator, attr = path.partition(".")
    return hasattr(getattr(module, owner, None), attr) if separator else hasattr(module, owner)


def probe_pcbnew():
    report = {"python_executable": sys.executable, "path": None, "version": None}
    try:
        pcbnew = import_module("pcbnew")
        report["path"] = getattr(pcbnew, "__file__", None)
        report["version"] = pcbnew.Version()
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError) as error:
        return report | {
            "error": str(error),
            "capabilities": {name: not_evaluated("pcbnew_unavailable") for name in PCBNEW_REQUIREMENTS},
        }

    capabilities = {}
    for name, required in PCBNEW_REQUIREMENTS.items():
        missing = [symbol for symbol in required if not has_symbol(pcbnew, symbol)]
        capabilities[name] = (
            not_evaluated("required_api_missing") | {"missing_symbols": missing}
            if missing else {"status": "AVAILABLE"}
        )
    return report | {"capabilities": capabilities}


def probe_kicad(executable, timeout):
    cli = probe_cli(executable, timeout)
    pcbnew = probe_pcbnew()
    versions_match = cli["version"] == pcbnew["version"] if cli["version"] and pcbnew["version"] else None
    return {"report_type": "kicad_environment_probe", "cli": cli, "pcbnew": pcbnew,
            "reported_versions_match": versions_match}
