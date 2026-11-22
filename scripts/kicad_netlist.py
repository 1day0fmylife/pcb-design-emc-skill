"""Сверка назначений сетей по RefDes и номеру вывода в KiCad."""

from collections import Counter, defaultdict
from hashlib import sha256
from xml.etree import ElementTree


RULE_ID = "PCB-CONN-001"
FOOTPRINT_RULE_ID = "PCB-CONN-002"


def read_kicad_netlist(path):
    document = path.read_bytes()
    text = document.decode("utf-8-sig")
    if "<!DOCTYPE" in text:
        raise ValueError(f"{path} — ожидается XML-нетлист KiCad без декларации DTD")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise ValueError(f"{path} — ошибочный XML-нетлист: {error}") from error
    if root.tag != "export":
        raise ValueError(f"{path} — ожидается XML-нетлист KiCad с корнем export")

    problems = []
    for section in ("design", "components", "nets"):
        if len(root.findall(section)) != 1:
            problems.append({"kind": "netlist_section", "section": section})
    source = (root.findtext("design/source") or "").strip()
    if not source or not root.get("version"):
        problems.append({"kind": "netlist_provenance"})

    components = set()
    excluded = set()
    footprint_assignments = {}
    for element in root.findall("components/comp"):
        refdes = element.get("ref")
        if not refdes or "?" in refdes:
            problems.append({"kind": "unannotated_symbol", "refdes": refdes})
        elif refdes in components or refdes in excluded:
            problems.append({"kind": "duplicate_symbol", "refdes": refdes})
        elif element.find("property[@name='exclude_from_board']") is not None:
            excluded.add(refdes)
        else:
            components.add(refdes)
            assignment = element.findall("footprint")
            if len(assignment) == 1 and len(assignment[0]) == 0:
                footprint_assignments[refdes] = (assignment[0].text or "").strip() or None

    pins = {}
    net_names = set()
    for net in root.findall("nets/net"):
        name = net.get("name")
        if not name or name in net_names:
            problems.append({"kind": "invalid_net_name", "schematic_net": name})
            continue
        net_names.add(name)
        for node in net.findall("node"):
            refdes, pin = node.get("ref"), node.get("pin")
            if refdes in excluded:
                continue
            if refdes not in components or not pin:
                problems.append({"kind": "invalid_netlist_node", "refdes": refdes, "pin": pin})
                continue
            key = (refdes, pin)
            data = {"name": name, "no_connect": "no_connect" in (node.get("pintype") or "").split("+")}
            if key in pins and pins[key] != data:
                problems.append({"kind": "ambiguous_schematic_pin", "refdes": refdes, "pin": pin})
            pins[key] = data
    if not components:
        problems.append({"kind": "no_exported_components"})
    if not pins:
        problems.append({"kind": "no_exported_pins"})

    return {
        "source": str(path),
        "sha256": sha256(document).hexdigest(),
        "export_version": root.get("version"),
        "schematic_source": source or None,
        "tool": root.findtext("design/tool"),
        "variant": root.findtext("design/variant"),
        "components": components,
        "excluded": excluded,
        "footprint_assignments": footprint_assignments,
        "pins": pins,
        "problems": problems,
    }


def complete_library_id(value):
    library, _, name = (value or "").partition(":")
    return bool(library.strip() and name.strip() and ":" not in name)


def compare_footprint_assignment(refdes, uuid, schematic_id, pcb_id):
    row = {"rule_id": FOOTPRINT_RULE_ID, "refdes": refdes, "footprint_uuid": uuid,
           "schematic_footprint_id": schematic_id, "pcb_footprint_id": pcb_id}
    missing = [field for field, value in (("schematic", schematic_id), ("board", pcb_id))
               if not complete_library_id(value)]
    if missing:
        return row | {"status": "REVIEW_REQUIRED", "kind": "unresolved_footprint_assignment",
                      "unresolved_sources": missing}
    if schematic_id != pcb_id:
        return row | {"status": "FAIL", "kind": "footprint_assignment_mismatch"}
    return row | {"status": "PASS"}


def compare_components(board_items, netlist, *, check_footprints):
    footprints = defaultdict(list)
    board_only = set()
    pads = defaultdict(list)
    unclassified = defaultdict(list)
    pcb_net_names = defaultdict(set)
    findings = []
    for uuid, item in board_items.items():
        if item["kind"] != "FOOTPRINT":
            continue
        if item["board_only"]:
            board_only.add(uuid)
            continue
        refdes = item.get("refdes")
        if not refdes or "?" in refdes:
            findings.append({"status": "REVIEW_REQUIRED", "kind": "unannotated_footprint",
                             "refdes": refdes, "footprint_uuid": uuid})
        else:
            footprints[refdes].append(uuid)

    for uuid, item in board_items.items():
        if item["kind"] != "PAD" or item.get("footprint_uuid") in board_only:
            continue
        electrical = item.get("electrical")
        if electrical is False:
            continue
        refdes = item.get("refdes")
        pin = item["pad_number"]
        if electrical is None:
            evidence = {"uuid": uuid, "net": item.get("net")}
            if pin:
                unclassified[(refdes, pin)].append(evidence)
            elif evidence["net"]:
                findings.append({"status": "REVIEW_REQUIRED", "kind": "unresolved_pad_electrical",
                                 "refdes": refdes, "unclassified_pads": [evidence]})
        elif not pin:
            if item.get("net"):
                findings.append({"status": "REVIEW_REQUIRED", "kind": "unnumbered_connected_pad",
                                 "refdes": refdes, "pad_uuids": [uuid]})
        else:
            pads[(refdes, pin)].append((uuid, item))
            if "netlist_name" in item:
                pcb_net_names[item["netlist_name"]].add(item["net"])

    components = netlist["components"]
    checked_footprints = 0
    complete_references = not any(item["kind"] == "unannotated_footprint" for item in findings)
    for refdes in sorted(footprints.keys() | components):
        matches = footprints.get(refdes, ())
        if len(matches) > 1:
            findings.append({"status": "REVIEW_REQUIRED", "kind": "duplicate_footprint",
                             "refdes": refdes, "footprint_uuids": matches})
        elif refdes not in components:
            findings.append({"status": "FAIL", "kind": "extra_footprint",
                             "refdes": refdes, "footprint_uuid": matches[0]})
        elif not matches:
            findings.append({"status": "FAIL" if complete_references else "REVIEW_REQUIRED",
                             "kind": "missing_footprint", "refdes": refdes})
        elif check_footprints:
            uuid, = matches
            schematic_id = netlist.get("footprint_assignments", {}).get(refdes)
            row = compare_footprint_assignment(refdes, uuid, schematic_id, board_items[uuid].get("library_id"))
            if row["status"] != "REVIEW_REQUIRED":
                checked_footprints += 1
            if row["status"] != "PASS":
                findings.append(row)

    comparable = {refdes for refdes in footprints.keys() & components if len(footprints[refdes]) == 1}
    ambiguous_nets = {name for name, originals in pcb_net_names.items() if len(originals) > 1}
    expected = netlist["pins"]
    keys = {key for key in expected.keys() | pads.keys() | unclassified.keys() if key[0] in comparable}
    checked = 0
    for refdes, pin in sorted(keys):
        requirement = expected.get((refdes, pin))
        actual = pads.get((refdes, pin), ())
        unknown = unclassified.get((refdes, pin), ())
        raw_nets = {row["net"] for _, row in actual if "net" in row}
        actual_nets = {row.get("netlist_name") for _, row in actual}
        evidence = {"refdes": refdes, "pin": pin, "pad_uuids": [uuid for uuid, _ in actual],
                    "pcb_nets": sorted(raw_nets)}
        if unknown:
            evidence["unclassified_pads"] = unknown
        unresolved = {"status": "REVIEW_REQUIRED", "kind": "unresolved_pad_electrical"}
        if requirement is None:
            if raw_nets:
                findings.append(evidence | {"status": "FAIL", "kind": "extra_connected_pad"})
            elif any(row["net"] for row in unknown):
                findings.append(evidence | unresolved)
            continue
        evidence["schematic_net"] = requirement["name"]
        if not actual:
            findings.append(evidence | (unresolved if unknown else {"status": "FAIL", "kind": "missing_pad"}))
        elif any("net" in row and "netlist_name" not in row for _, row in actual):
            findings.append(evidence | {"status": "REVIEW_REQUIRED", "kind": "unresolved_pcb_net"})
        elif actual_nets & ambiguous_nets:
            findings.append(evidence | {"status": "REVIEW_REQUIRED", "kind": "ambiguous_pcb_net_name"})
        else:
            allowed = {requirement["name"]}
            if requirement["no_connect"]:
                allowed.add(None)
            if actual_nets - allowed:
                findings.append(evidence | {"status": "FAIL", "kind": "net_mismatch"})
            elif unknown:
                findings.append(evidence | unresolved)
                continue
            checked += 1
    return findings, checked, len(comparable), checked_footprints


def compare_netlist(board, netlist, design_revision, *, check_footprints=False):
    findings = []
    missing = []
    if board is None:
        missing.append("board")
    if netlist is None:
        missing.append("netlist")
    if not (design_revision or "").strip():
        missing.append("design_revision")

    board_uuid_gaps = {
        kind: amount for kind, amount in (board or {}).get("skipped_without_uuid", {}).items()
        if kind in {"FOOTPRINT", "PAD"}
    }
    if board_uuid_gaps:
        findings.append({"status": "REVIEW_REQUIRED", "kind": "board_uuid_missing", "counts": board_uuid_gaps})
    if netlist is not None:
        findings.extend(problem | {"status": "REVIEW_REQUIRED"} for problem in netlist["problems"])

    checked = matched = checked_footprints = 0
    if board is not None and netlist is not None and not findings:
        findings, checked, matched, checked_footprints = compare_components(
            board["items"], netlist, check_footprints=check_footprints
        )
        if not checked and not findings:
            findings.append({"status": "REVIEW_REQUIRED", "kind": "no_comparable_pins"})
    if missing:
        findings = [item | {"status": "REVIEW_REQUIRED"} for item in findings]
    for field in missing:
        findings.append({"status": "REVIEW_REQUIRED", "kind": "missing_input", "field": field})

    counts = Counter(item["status"] for item in findings)
    overall = "FAIL" if counts["FAIL"] else "REVIEW_REQUIRED" if findings else "PASS"
    report = {
        "report_type": "kicad_netlist_comparison",
        "rule_id": RULE_ID,
        "overall_status": overall,
        "design_revision": design_revision,
        "board": {key: board[key] for key in ("source", "board_sha256", "pcbnew_version")}
        if board is not None else None,
        "netlist": {key: netlist[key] for key in ("source", "sha256", "export_version", "schematic_source", "tool", "variant")}
        if netlist is not None else None,
        "schematic_components": len(netlist["components"]) if netlist is not None else 0,
        "schematic_pins": len(netlist["pins"]) if netlist is not None else 0,
        "matched_components": matched,
        "checked_pins": checked,
        "finding_counts": dict(counts),
        "findings": findings,
    }
    if check_footprints:
        report["footprint_assignment"] = {"rule_id": FOOTPRINT_RULE_ID, "checked_count": checked_footprints}
    return report
