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


def read_board_inventory(path, *, stackup_only=False):
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
