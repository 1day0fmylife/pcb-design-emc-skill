"""Чтение только явно сохранённых данных стека из KiCad PCB."""

import math
import re
from collections import Counter
from decimal import Decimal


TOKEN = re.compile(r'^[ \t]*\#[^\r\n]*|"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+', re.MULTILINE)
ESCAPED = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
NUMERIC = {"thickness": "thickness_mm", "epsilon_r": "epsilon_r", "loss_tangent": "loss_tangent",
           "spec_frequency": "spec_frequency"}
TEXT = ("material", "color", "dielectric_model")
SETTINGS = ("copper_finish", "dielectric_constraints", "thickness_constraints",
            "edge_connector", "castellated_pads", "edge_plating")
SWITCHES = {"dielectric_constraints", "thickness_constraints", "castellated_pads", "edge_plating"}
STRUCTURAL = {"copper", "dielectric", "solder_mask"}


def atom(raw):
    if raw.startswith('"'):
        return re.sub(r'\\([\\"nrt])', lambda match: ESCAPED[match[1]], raw[1:-1])
    return raw


def read_list(tokens):
    result = []
    for token in tokens:
        if token == ")":
            return result
        result.append(read_list(tokens) if token == "(" else atom(token))
    raise ValueError("Незавершённая секция PCB при чтении стека")


def skip_list(tokens):
    depth = 1
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 0:
                return
    raise ValueError("Незавершённое выражение PCB при чтении стека")


def select_board_sections(document):
    tokens = (match[0] for match in TOKEN.finditer(document.decode("utf-8-sig"))
              if not match[0].lstrip().startswith("#"))
    if next(tokens, None) != "(" or next(tokens, None) != "kicad_pcb":
        raise ValueError("Ожидается корневая секция kicad_pcb")
    sections = {}
    for token in tokens:
        if token == ")":
            break
        if token != "(":
            continue
        name = next(tokens, None)
        if name in {"general", "setup"}:
            sections[name] = read_list(tokens)
            if len(sections) == 2:
                break
        else:
            skip_list(tokens)
    return sections


def named_lists(nodes, name):
    return [node for node in nodes if isinstance(node, list) and node and node[0] == name]


def number(value, *, allow_zero=False):
    if not isinstance(value, str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0 or (parsed == 0 and not allow_zero):
        return None
    return parsed


def read_layer(node, issues):
    name = node[1] if len(node) > 1 and isinstance(node[1], str) else None
    fields = node[2:]
    if name == "dielectric" and fields and isinstance(fields[0], str) and fields[0].isdigit():
        name = f"dielectric {fields[0]}"
        fields = fields[1:]
    if not name:
        issues.append({"kind": "unnamed_layer"})
        return None

    kind = "other"
    if name.endswith(".Cu"):
        kind = "copper"
    elif name == "dielectric" or name.startswith("dielectric "):
        kind = "dielectric"
    elif name in {"F.Mask", "B.Mask"}:
        kind = "solder_mask"

    layer_type = None
    sublayers = [{}]
    for field in fields:
        if field == "addsublayer":
            sublayers.append({})
            if kind != "dielectric":
                issues.append({"kind": "unexpected_sublayer", "layer": name})
        if not isinstance(field, list) or len(field) < 2 or not isinstance(field[1], str):
            continue
        key, value = field[:2]
        sublayer = sublayers[-1]
        if key == "type":
            layer_type = value
        elif key in NUMERIC:
            attribute = NUMERIC[key]
            parsed = number(value, allow_zero=key in {"loss_tangent", "spec_frequency"}
                            or (key == "thickness" and kind == "solder_mask"))
            if parsed is None:
                issues.append({"kind": "invalid_layer_value", "layer": name, "field": attribute,
                               "sublayer": len(sublayers), "value": value})
            sublayer[attribute] = parsed
            if key == "thickness" and "locked" in field[2:]:
                sublayer["thickness_locked"] = True
        elif key in TEXT:
            sublayer[key] = value

    if kind in STRUCTURAL and not layer_type:
        issues.append({"kind": "missing_layer_type", "layer": name})
    for index, sublayer in enumerate(sublayers, 1):
        required = ["thickness_mm"] if kind in STRUCTURAL else []
        if kind == "dielectric":
            required.extend(["material", "epsilon_r", "loss_tangent"])
        missing = [
            key for key in required if sublayer.get(key) in (None, "")
            or (key == "material" and sublayer[key].strip().lower() in {"", "not specified"})
        ]
        if missing:
            issues.append({"kind": "missing_layer_data", "layer": name, "sublayer": index, "fields": missing})
    return {"name": name, "kind": kind, "type": layer_type, "sublayers": sublayers}


def collect_stackup(document, copper_layers):
    sections = select_board_sections(document)
    nominal = named_lists(sections.get("general", []), "thickness")
    nominal_thickness = number(nominal[0][1]) if len(nominal) == 1 and len(nominal[0]) == 2 else None
    stackups = named_lists(sections.get("setup", []), "stackup")
    issues = []
    layers = []
    settings = {}
    stack_thickness = None
    if len(stackups) != 1:
        issues.append({"kind": "stackup_missing" if not stackups else "ambiguous_stackup"})
    else:
        stackup = stackups[0][1:]
        for node in named_lists(stackup, "layer"):
            layer = read_layer(node, issues)
            if layer is not None:
                layers.append(layer)
        for field in SETTINGS:
            matches = named_lists(stackup, field)
            if len(matches) == 1 and len(matches[0]) == 2 and isinstance(matches[0][1], str):
                value = matches[0][1]
                settings[field] = {"yes": True, "no": False}.get(value, value) if field in SWITCHES else value

        actual_copper = [layer["name"] for layer in layers if layer["kind"] == "copper"]
        duplicates = sorted(name for name, count in Counter(layer["name"] for layer in layers).items() if count > 1)
        if duplicates:
            issues.append({"kind": "duplicate_stack_layers", "layers": duplicates})
        topology = [layer["kind"] for layer in layers if layer["kind"] in {"copper", "dielectric"}]
        expected = ["copper", "dielectric"] * (len(copper_layers) - 1) + ["copper"]
        if actual_copper != copper_layers or topology != expected:
            issues.append({"kind": "stack_layer_order", "expected_copper_layers": copper_layers,
                           "declared_copper_layers": actual_copper})
        elif not duplicates:
            thicknesses = [row.get("thickness_mm") for layer in layers if layer["kind"] in STRUCTURAL
                           for row in layer["sublayers"]]
            if all(value is not None for value in thicknesses):
                stack_thickness = float(sum(Decimal(str(value)) for value in thicknesses))
    if nominal_thickness is None:
        issues.append({"kind": "board_thickness_missing"})

    return {
        "source": "setup/stackup" if len(stackups) == 1 else None,
        "status": "REVIEW_REQUIRED" if issues else "NOT_EVALUATED",
        "declared_board_thickness_mm": nominal_thickness,
        "declared_stack_thickness_mm": stack_thickness,
        "copper_layer_order": copper_layers,
        "layers": layers,
        "settings": settings,
        "issues": issues,
    }
