"""Проверка локальных JSON-артефактов инженерного отчёта."""

import re
from hashlib import sha256
from pathlib import Path


def read_artifact(root, artifact_id, entry, load_document):
    label = f"evidence_artifacts.{artifact_id}"
    reference = entry["path"]
    parts = reference.split("/")
    if any(char in reference for char in ("\\", ":", "\0")) or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} — требуется относительный путь внутри каталога артефактов")
    declared_path = Path(reference)
    if declared_path.suffix.lower() != ".json":
        raise ValueError(f"{label} — требуется файл .json")
    try:
        path = (root / declared_path).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{label} — артефакт недоступен") from error
    if not path.is_relative_to(root):
        raise ValueError(f"{label} — путь выходит за каталог артефактов")
    if not path.is_file():
        raise ValueError(f"{label} — ожидается обычный файл")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ValueError(f"{label} — артефакт недоступен для чтения") from error
    if sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError(f"{label} — SHA-256 не совпадает")
    try:
        return load_document(declared_path, content=data.decode("utf-8"), strict_json=True)
    except ValueError as error:
        raise ValueError(f"{label} — {error}") from error


def resolve_pointer(document, pointer):
    if not pointer.startswith("/"):
        raise ValueError("указатель JSON должен начинаться с /")
    value = document
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("допустимые экранирования указателя JSON — ~0 и ~1")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            if len(key) > len(str(len(value))) or (index := int(key)) >= len(value):
                raise ValueError("индекс указателя JSON вне массива")
            value = value[index]
        else:
            raise ValueError("запись по указателю JSON отсутствует")
    return value


def verify_evidence_artifacts(report, root, load_document):
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{root} — каталог артефактов недоступен") from error
    if not root.is_dir():
        raise ValueError(f"{root} — каталог артефактов недоступен")
    documents = {
        artifact_id: read_artifact(root, artifact_id, entry, load_document)
        for artifact_id, entry in report["evidence_artifacts"].items()
    }
    checked = 0
    for index, finding in enumerate(report["findings"]):
        if (reference := finding.get("evidence_ref")) is None:
            continue
        artifact_id = reference["artifact"]
        label = f"findings[{index}].evidence_ref"
        if artifact_id not in documents:
            raise ValueError(f"{label} — артефакт {artifact_id!r} отсутствует в evidence_artifacts")
        try:
            resolve_pointer(documents[artifact_id], reference["locator"])
        except ValueError as error:
            raise ValueError(f"{label} — {error}") from error
        checked += 1
    return {"artifacts": len(documents), "references": checked}
