"""Persistencia local de exports e relatorios.

Por padrao, `write_json` nao sobrescreve arquivos existentes. Coletas novas
devem usar um diretorio de execucao proprio e gerar um manifesto com SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

MANIFEST_NAME = "manifest.json"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _serialize(payload: Any) -> Any:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, list):
        return [_serialize(item) for item in payload]
    if isinstance(payload, dict):
        return {key: _serialize(value) for key, value in payload.items()}
    return payload


def write_json(path: Path, payload: Any, *, overwrite: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"arquivo ja existe: {path}; use overwrite=True somente com revisao")
    data = _serialize(payload)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_html(path: Path, html: str, *, overwrite: bool = False) -> Path:
    """Grava HTML bruto opcional; por padrao preserva a primeira coleta."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return path
    path.write_text(html, encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(run_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Manifesto dos arquivos do run, exceto o proprio manifesto."""
    files: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        files.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = dict(metadata)
    manifest["files"] = files
    return manifest


def write_manifest(run_dir: Path, metadata: dict[str, Any], *, overwrite: bool = False) -> Path:
    manifest = build_manifest(run_dir, metadata)
    return write_json(run_dir / MANIFEST_NAME, manifest, overwrite=overwrite)


def verify_manifest(run_dir: Path) -> list[str]:
    """Retorna erros de integridade de um run; lista vazia significa OK."""
    manifest_path = run_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return [f"manifesto ausente: {manifest_path}"]
    try:
        manifest = read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifesto ilegivel: {type(exc).__name__}"]

    errors: list[str] = []
    listed: set[str] = set()
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict):
            errors.append("entrada de manifesto invalida")
            continue
        relative = entry.get("path")
        if not isinstance(relative, str):
            errors.append("entrada sem path")
            continue
        listed.add(relative)
        path = run_dir / relative
        if not path.is_file():
            errors.append(f"arquivo ausente: {relative}")
            continue
        actual = sha256_file(path)
        if actual != entry.get("sha256"):
            errors.append(f"hash divergente: {relative}")

    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        relative = path.relative_to(run_dir).as_posix()
        if relative not in listed:
            errors.append(f"arquivo nao listado: {relative}")
    return errors
