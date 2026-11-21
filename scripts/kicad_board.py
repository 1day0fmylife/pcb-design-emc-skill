"""Инвентарь объектов KiCad PCB по UUID через локальный pcbnew."""

from collections import Counter
from hashlib import sha256
from itertools import chain


NIL_UUID = "00000000-0000-0000-0000-000000000000"


def read_board_inventory(path):
    if path.suffix.lower() != ".kicad_pcb":
        raise ValueError("Для инвентаризации укажите файл .kicad_pcb")
    try:
        import pcbnew
    except ImportError as error:
        raise ValueError("Для чтения PCB нужен модуль pcbnew из локальной установки KiCad") from error

    digest = sha256(path.read_bytes()).hexdigest()
    try:
        board = pcbnew.PCB_IO_KICAD_SEXPR().LoadBoard(str(path.resolve()), None)
        if board is None:
            raise ValueError(f"KiCad не загрузил плату {path}")
        inventory = collect_objects(board)
        engine_version = pcbnew.Version()
    except (AttributeError, RuntimeError, TypeError) as error:
        raise ValueError(f"Ошибка чтения {path} через pcbnew — {error}") from error

    if sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"Файл {path} изменился во время чтения PCB")
    return {
        "report_type": "kicad_board_inventory",
        "source": str(path),
        "board_sha256": digest,
        "pcbnew_version": engine_version,
    } | inventory


def collect_objects(board):
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
        if pad_number is not None:
            row["pad_number"] = pad_number
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
