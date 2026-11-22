"""Инвентарь объектов KiCad PCB по UUID через локальный pcbnew."""

from collections import Counter
from hashlib import sha256
from itertools import chain
from math import atan2, degrees

if __package__:
    from .kicad_stackup import collect_stackup
else:
    from kicad_stackup import collect_stackup


NIL_UUID = "00000000-0000-0000-0000-000000000000"
TRACE_KINDS = {"PCB_TRACK", "PCB_ARC"}


def read_board_inventory(path, *, stackup_only=False, edge_traces=None, gap_traces=None):
    if path.suffix.lower() != ".kicad_pcb":
        raise ValueError("Для инвентаризации укажите файл .kicad_pcb")
    try:
        import pcbnew
    except ImportError as error:
        raise ValueError("Для чтения PCB нужен модуль pcbnew из локальной установки KiCad") from error

    document = path.read_bytes()
    digest = sha256(document).hexdigest()
    try:
        board = pcbnew.PCB_IO_KICAD_SEXPR().LoadBoard(str(path.resolve()), None)
        if board is None:
            raise ValueError(f"KiCad не загрузил плату {path}")
        engine_version = pcbnew.Version()
        if stackup_only:
            copper_layers = [str(board.GetStandardLayerName(layer)) for layer in board.GetEnabledLayers().CuStack()]
            inventory = {
                "report_type": "kicad_stackup_inventory",
                "file_format_version": board.GetFileFormatVersionAtLoad(),
                "stackup": collect_stackup(document, copper_layers),
            }
        else:
            inventory = {"report_type": "kicad_board_inventory"} | collect_objects(board, pcbnew)
            if edge_traces is not None:
                inventory["edge_geometry"] = collect_edge_geometry(board, pcbnew, set(edge_traces))
            if gap_traces is not None:
                inventory["trace_geometry"] = collect_trace_geometry(board, pcbnew, gap_traces)
    except (AttributeError, RuntimeError, TypeError) as error:
        raise ValueError(f"Ошибка чтения {path} через pcbnew — {error}") from error

    if sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"Файл {path} изменился во время чтения PCB")
    return {
        "source": str(path),
        "board_sha256": digest,
        "pcbnew_version": engine_version,
    } | inventory


def pad_hole(pad, pcbnew):
    attributes = {pcbnew.PAD_ATTRIB_PTH: "PTH", pcbnew.PAD_ATTRIB_NPTH: "NPTH"}
    kind = attributes.get(pad.GetAttribute())
    if kind is None:
        return None
    size = pad.GetDrillSize()
    if size.x <= 0 or size.y <= 0:
        return None
    shapes = {pcbnew.PAD_DRILL_SHAPE_CIRCLE: "circle", pcbnew.PAD_DRILL_SHAPE_OBLONG: "oblong"}
    shape = shapes.get(pad.GetDrillShape())
    x_mm, y_mm = pcbnew.ToMM(size)
    hole = {"kind": kind, "shape": shape, "x_mm": x_mm, "y_mm": y_mm}
    get_hole_shape = getattr(pad, "GetEffectiveHoleShape", None)
    effective = get_hole_shape() if get_hole_shape is not None else None
    get_segment = getattr(effective, "GetSeg", None)
    if get_segment is not None:
        segment = get_segment()
        hole["center_mm"] = {
            "x": pcbnew.ToMM(segment.A.x + segment.B.x) / 2,
            "y": pcbnew.ToMM(segment.A.y + segment.B.y) / 2,
        }
        if shape == "oblong" and (segment.A.x != segment.B.x or segment.A.y != segment.B.y):
            angle = degrees(atan2(segment.B.y - segment.A.y, segment.B.x - segment.A.x))
            hole["axis_angle_deg"] = angle % 180
    return hole


def pad_electrical(pad, pcbnew):
    if not pad.IsOnCopperLayer():
        return False
    if pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
        return True
    no_copper = getattr(pad, "IsNPTHWithNoCopper", None)
    return not no_copper() if no_copper is not None else None


def point_iu(vector):
    return vector.x, vector.y


def segment_iu(item):
    return point_iu(item.GetStart()), point_iu(item.GetEnd())


def collect_edge_geometry(board, pcbnew, trace_uuids):
    segments = []
    issues = []
    footprint_graphics = chain.from_iterable(footprint.GraphicalItems() for footprint in board.GetFootprints())
    for item in chain(board.GetDrawings(), footprint_graphics):
        if item.GetLayer() != pcbnew.Edge_Cuts:
            continue
        uuid = str(item.m_Uuid.AsString()).lower()
        if not uuid or uuid == NIL_UUID:
            issues.append({"reason": "outline_uuid_missing"})
        elif item.GetClass() == "PCB_SHAPE" and item.GetShape() == pcbnew.SHAPE_T_SEGMENT:
            segments.append((uuid, segment_iu(item)))
        elif item.GetClass() == "PCB_SHAPE" and item.GetShape() == pcbnew.SHAPE_T_RECT and hasattr(item, "GetRectCorners"):
            corners = [point_iu(corner) for corner in item.GetRectCorners()]
            if len(corners) == 4:
                segments.extend((uuid, edge) for edge in zip(corners, corners[1:] + corners[:1]))
            else:
                issues.append({"reason": "invalid_outline_rectangle", "uuid": uuid})
        else:
            issues.append({"reason": "unsupported_outline_shape", "uuid": uuid})

    return {"outline_segments": segments, "outline_issues": issues} | collect_trace_geometry(
        board, pcbnew, trace_uuids
    )


def collect_trace_geometry(board, pcbnew, trace_uuids):
    tracks = {}
    for item in board.GetTracks():
        if item.GetClass() != "PCB_TRACK":
            continue
        uuid = str(item.m_Uuid.AsString()).lower()
        if uuid in trace_uuids:
            tracks[uuid] = {"segment": segment_iu(item), "width": item.GetWidth()}
    return {"tracks": tracks, "units_per_mm": pcbnew.FromMM(1)}


def trace_scope_blockers(board, context, revision):
    blockers = []
    if board is None:
        blockers.append("board_missing")
    if context is None:
        blockers.append("context_missing")
    elif not (revision or "").strip() or context["design_revision"] != revision:
        blockers.append("design_revision_unconfirmed")
    if not (context or {}).get("mode"):
        blockers.append("mode_missing")
    if not (context or {}).get("items"):
        blockers.append("trace_scope_empty")
    if board is not None and context is not None:
        source = context["board_source"].replace("\\", "/").rsplit("/", 1)[-1]
        name = board["source"].replace("\\", "/").rsplit("/", 1)[-1]
        if source != name:
            blockers.append("board_source_mismatch")
        if context.get("board_sha256", board["board_sha256"]) != board["board_sha256"]:
            blockers.append("board_sha256_mismatch")
    return blockers


def board_evidence(board, context):
    if board is None:
        return None
    evidence = {key: board[key] for key in ("source", "board_sha256", "pcbnew_version")}
    evidence["sha256_verified_against_context"] = (context or {}).get("board_sha256") == board["board_sha256"]
    return evidence


def collect_objects(board, pcbnew):
    layers = {
        layer: str(board.GetStandardLayerName(layer))
        for layer in board.GetEnabledLayers().Seq()
    }
    items = {}
    skipped = Counter()

    def add(item, refdes=None, footprint_uuid=None, pad_number=None):
        kind = str(item.GetClass())
        uuid = str(item.m_Uuid.AsString()).lower()
        if not uuid or uuid == NIL_UUID:
            skipped[kind] += 1
            return
        if uuid in items:
            raise ValueError(f"Повтор UUID {uuid} у объектов платы")

        row = {
            "kind": kind,
            "layers": [layers[layer] for layer in item.GetLayerSet().Seq() if layer in layers],
        }
        get_net = getattr(item, "GetNetname", None)
        if get_net is not None and (net := str(get_net())):
            row["net"] = net
        if refdes:
            row["refdes"] = refdes
        if footprint_uuid:
            row["footprint_uuid"] = footprint_uuid
        if kind == "FOOTPRINT":
            row["board_only"] = bool(item.IsBoardOnly())
        if kind in TRACE_KINDS:
            get_width = getattr(item, "GetWidth", None)
            if get_width is not None and (width := get_width()) > 0:
                row["width_mm"] = pcbnew.ToMM(width)
        if pad_number is not None:
            row["pad_number"] = pad_number
            row["electrical"] = pad_electrical(item, pcbnew)
            hole = pad_hole(item, pcbnew)
            if hole is not None:
                row["hole"] = hole
            if "net" in row and item.GetNetCode() > 0:
                # KiCad keeps the full, unescaped name inside brackets. GetDisplayNetname
                # can shorten a name from the root sheet, so retain the full form here.
                label = str(item.GetNetnameMsg())
                if label.startswith("[") and label.endswith("]"):
                    row["netlist_name"] = label[1:-1]
        items[uuid] = row
        return uuid

    for item in chain(board.GetTracks(), board.Zones(), board.GetDrawings()):
        add(item)
    for footprint in board.GetFootprints():
        refdes = str(footprint.GetReference())
        parent = add(footprint, refdes)
        for pad in footprint.Pads():
            add(pad, refdes, parent, str(pad.GetNumber()))
        for item in chain(footprint.GetFields(), footprint.GraphicalItems(), footprint.Zones()):
            add(item, refdes, parent)

    nets = sorted(name for net in board.GetNetInfo().NetsByName() if (name := str(net)))
    return {
        "file_format_version": board.GetFileFormatVersionAtLoad(),
        "copper_layer_count": board.GetCopperLayerCount(),
        "layers": list(layers.values()),
        "nets": nets,
        "item_counts": dict(sorted(Counter(row["kind"] for row in items.values()).items())),
        "skipped_without_uuid": dict(sorted(skipped.items())),
        "items": dict(sorted(items.items())),
    }
