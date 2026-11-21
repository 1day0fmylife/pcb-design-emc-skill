"""Локальный запуск ERC/DRC на временной копии входных файлов."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from hashlib import sha256
from pathlib import Path


CHECKS = {"erc": ("sch", ".kicad_sch"), "drc": ("pcb", ".kicad_pcb")}
SNAPSHOT_SUFFIXES = {
    ".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_sym", ".kicad_mod",
    ".kicad_wks", ".sch", ".lib", ".dcm", ".mod",
}
LIBRARY_TABLES = ("fp-lib-table", "sym-lib-table")
SEVERITIES = {"error", "warning", "exclusion"}
FLAGS = ("--format", "--units", "--severity-all", "--exit-code-violations", "--output")
STATUS_ORDER = ("FAIL", "REVIEW_REQUIRED", "PASS")


def input_paths(inputs, project_dir):
    paths = {kind: path.resolve(strict=True) for kind, path in inputs.items() if path is not None}
    if not paths:
        raise ValueError("Для запуска укажите плату или схему")
    for kind, path in paths.items():
        if not path.is_file() or path.suffix.lower() != CHECKS[kind][1]:
            raise ValueError(f"{kind.upper()} требует входной файл {CHECKS[kind][1]} — {path}")
    if project_dir is None:
        parents = {path.parent for path in paths.values()}
        if len(parents) != 1:
            raise ValueError("Для входов в разных каталогах укажите общий --project-dir")
        project_dir = parents.pop()
    else:
        project_dir = project_dir.resolve(strict=True)
    if not project_dir.is_dir() or any(not path.is_relative_to(project_dir) for path in paths.values()):
        raise ValueError("Все входные файлы должны находиться в --project-dir")
    return project_dir, {kind: path.relative_to(project_dir) for kind, path in paths.items()}


def fail_walk(error):
    raise error


def snapshot_project(project, temporary):
    destination = temporary / "project"
    destination.mkdir()
    manifest = {}
    skipped = []
    for directory, folders, names in os.walk(project, onerror=fail_walk):
        directory = Path(directory)
        for name in sorted(folders):
            child = directory / name
            if name in {".git", ".svn"} or child == temporary:
                folders.remove(name)
            elif child.is_symlink():
                folders.remove(name)
                skipped.append(child.relative_to(project).as_posix())
        for name in sorted(names):
            source = directory / name
            if source.suffix.lower() not in SNAPSHOT_SUFFIXES and name not in LIBRARY_TABLES:
                continue
            relative = source.relative_to(project)
            if not source.resolve().is_relative_to(project) or not source.is_file():
                skipped.append(relative.as_posix())
                continue
            data = source.read_bytes()
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest[relative.as_posix()] = sha256(data).hexdigest()
    return destination, dict(sorted(manifest.items())), sorted(skipped)


def changed_files(project, manifest):
    changed = []
    for name, expected in manifest.items():
        path = project / name
        try:
            if sha256(path.read_bytes()).hexdigest() != expected:
                changed.append(name)
        except OSError:
            changed.append(name)
    return changed


def execute(command, directory, environment, timeout):
    try:
        process = subprocess.run(command, cwd=directory, env=environment, timeout=timeout,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        return {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
    except subprocess.TimeoutExpired as error:
        return {"exit_code": None, "timed_out": True,
                "stdout": (error.stdout or b"").decode("utf-8", errors="replace"),
                "stderr": (error.stderr or b"").decode("utf-8", errors="replace")}
    except OSError as error:
        return {"exit_code": None, "stdout": "", "stderr": str(error)}


def user_configuration():
    if os.environ.get("KICAD_CONFIG_HOME"):
        return Path(os.environ["KICAD_CONFIG_HOME"])
    if sys.platform == "darwin":
        return Path.home() / "Library/Preferences/kicad"
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "kicad"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad"


def copy_configuration(version, temporary):
    match = re.match(r"^(\d+)\.(\d+)", version)
    if match is None:
        return {}
    major, minor = match.groups()
    subdir = f"{major}.{minor}"
    target = temporary / "config" / subdir
    target.mkdir(parents=True, exist_ok=True)
    config = user_configuration() / subdir
    templates = Path(os.environ.get(f"KICAD{major}_TEMPLATE_DIR", "/usr/share/kicad/template"))
    copies = {}
    for name in (*LIBRARY_TABLES, "kicad_common.json"):
        source = config / name
        if not source.is_file() and name in LIBRARY_TABLES:
            source = templates / name
        if source.is_file():
            data = source.read_bytes()
            (target / name).write_bytes(data)
            copies[name] = {"source": str(source), "sha256": sha256(data).hexdigest()}
    return copies


def report_counts(kind, report):
    if kind == "erc":
        sections = [sheet["violations"] for sheet in report["sheets"]]
    else:
        sections = [report[key] for key in ("violations", "unconnected_items", "schematic_parity")]
    counts = Counter("excluded" if item.get("excluded") else item["severity"]
                     for section in sections for item in section)
    return {key: counts[key] for key in ("error", "warning", "excluded")}


def evaluate_check(kind, report, exit_code, blockers):
    counts = report_counts(kind, report)
    reasons = list(blockers)
    if exit_code not in {0, 5}:
        reasons.append("execution_failed")
    included = report.get("included_severities")
    if included is None or not SEVERITIES.issubset(included):
        reasons.append("severity_coverage_unconfirmed")
    ignored = report.get("ignored_checks")
    if ignored is None:
        reasons.append("ignored_checks_unknown")
    elif ignored:
        reasons.append("ignored_checks")
    if counts["warning"]:
        reasons.append("warnings")
    if counts["excluded"]:
        reasons.append("exclusions")
    if exit_code == 0 and (counts["error"] or counts["warning"]):
        reasons.append("exit_code_conflict")
    if exit_code == 5 and not any(counts.values()):
        reasons.append("exit_code_conflict")

    status = "REVIEW_REQUIRED"
    if not blockers and exit_code in {0, 5} and counts["error"]:
        status = "FAIL"
    elif not reasons:
        status = "PASS"
    return {"status": status, "counts": counts, "included_severities": included,
            "ignored_checks": ignored, "reasons": reasons}


def run_check(kind, relative, board_root, output, executable, environment, timeout, load_report, blockers,
              cli_version, refill_zones):
    family = CHECKS[kind][0]
    command = [executable, family, kind]
    flags = [*FLAGS, "--refill-zones"] if kind == "drc" and refill_zones else FLAGS
    help_result = execute([*command, "--help"], board_root, environment, timeout)
    options = set(re.findall(r"--[a-z][a-z-]+", help_result["stdout"] + help_result["stderr"]))
    absent = sorted(set(flags) - options)
    entry = {"input": relative.as_posix(), "status": "NOT_EVALUATED"}
    if help_result["exit_code"] != 0 or absent:
        return entry | {"reasons": [*blockers, "unsupported_cli"], "unsupported_flags": absent,
                        "probe": help_result}

    native = output / f"{kind}.json"
    command += ["--format", "json", "--units", "mm", "--severity-all", "--exit-code-violations"]
    if kind == "drc" and refill_zones:
        command.append("--refill-zones")
    command += ["--output", str(native), f"./{relative.name}"]
    result = execute(command, (board_root / relative).parent, environment, timeout)
    entry.update(command=command, execution=result, native_report=native.name if native.is_file() else None)
    if not native.is_file():
        reason = "execution_failed" if result["exit_code"] not in {0, 5} else "report_missing"
        return entry | {"status": "REVIEW_REQUIRED", "reasons": [*blockers, reason]}
    entry["native_report_sha256"] = sha256(native.read_bytes()).hexdigest()
    try:
        raw = load_report(native, f"kicad-{kind}")
    except ValueError as error:
        return entry | {"status": "REVIEW_REQUIRED", "reasons": [*blockers, "invalid_report"],
                        "report_error": str(error)}
    entry.update(reported_kicad_version=raw["kicad_version"], reported_source=raw["source"])
    if raw["source"].replace("\\", "/").rsplit("/", 1)[-1] != relative.name:
        blockers = [*blockers, "report_source_mismatch"]
    if raw["kicad_version"] != cli_version:
        blockers = [*blockers, "report_version_mismatch"]
    if kind == "erc":
        entry["sheets"] = [{"path": sheet["path"], "uuid_path": sheet["uuid_path"],
                            "count": len(sheet["violations"])} for sheet in raw["sheets"]]
    return entry | evaluate_check(kind, raw, result["exit_code"], blockers)


def run_kicad_checks(inputs, project_dir, output, revision, executable, timeout, refill_zones, load_report):
    project, paths = input_paths(inputs, project_dir)
    output = output.resolve()
    output.mkdir()
    results = {kind: {"status": "NOT_EVALUATED", "reasons": ["not_requested"]} for kind in CHECKS}
    manifest, skipped, config, changed = {}, [], {}, []
    probe = None
    version = None
    executable = shutil.which(executable)
    if executable is None:
        for kind in paths:
            results[kind] = {"status": "NOT_EVALUATED", "reasons": ["cli_missing"]}
    else:
        executable = str(Path(executable).absolute())
        with tempfile.TemporaryDirectory(prefix="pcb-kicad-checks-") as directory:
            temporary = Path(directory).resolve()
            snapshot, manifest, skipped = snapshot_project(project, temporary)
            environment = os.environ | {
                "KICAD_CONFIG_HOME": str(temporary / "config"), "XDG_CONFIG_HOME": str(temporary / "xdg-config"),
                "XDG_CACHE_HOME": str(temporary / "cache"), "LC_ALL": "C",
            }
            probe = execute([executable, "version"], snapshot, environment, timeout)
            if probe["exit_code"] == 0 and probe["stdout"].strip():
                version = probe["stdout"].strip()
                config = copy_configuration(version, temporary)
            for kind, relative in paths.items():
                if version is None or relative.as_posix() not in manifest:
                    reason = "cli_version_unavailable" if version is None else "snapshot_input_missing"
                    results[kind] = {"input": relative.as_posix(), "status": "NOT_EVALUATED", "reasons": [reason]}
                    continue
                blockers = []
                if not (revision or "").strip():
                    blockers.append("design_revision_missing")
                if skipped:
                    blockers.append("snapshot_inputs_skipped")
                if relative.with_suffix(".kicad_pro").as_posix() not in manifest:
                    blockers.append("project_settings_missing")
                results[kind] = run_check(kind, relative, snapshot, output, executable, environment, timeout,
                                          load_report, blockers, version, refill_zones)
            changed = changed_files(project, manifest)
            if changed:
                for kind in paths:
                    results[kind]["status"] = "REVIEW_REQUIRED"
                    results[kind]["reasons"].append("source_inputs_changed")

    statuses = {results[kind]["status"] for kind in paths}
    overall = "NOT_EVALUATED"
    if statuses != {"NOT_EVALUATED"}:
        overall = next((status for status in STATUS_ORDER if status in statuses), "REVIEW_REQUIRED")
        if overall == "PASS" and "NOT_EVALUATED" in statuses:
            overall = "REVIEW_REQUIRED"
    return {
        "report_type": "kicad_native_checks", "design_revision": revision, "project_dir": str(project),
        "overall_status": overall, "requested_checks": list(paths), "kicad_version": version, "version_probe": probe,
        "input_sha256": manifest, "snapshot_inputs_skipped": skipped, "source_inputs_changed": changed,
        "library_configuration": config, "refill_zones_requested": refill_zones and "drc" in paths,
        "checks": results,
    }
