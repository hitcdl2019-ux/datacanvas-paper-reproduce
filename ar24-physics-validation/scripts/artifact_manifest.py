#!/usr/bin/env python3
"""Create checksummed manifests for data, meshes, cases, weights and scalers."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path


ROLE_SUFFIXES = {
    "mesh": {".msh", ".mesh", ".foam", ".vtk", ".vtu", ".stl"},
    "field_data": {".npy", ".npz", ".h5", ".hdf5", ".nc", ".csv", ".mat", ".jld2"},
    "model_weight": {".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".bson", ".jld2"},
    "normalizer": {".pkl", ".pickle", ".joblib", ".json", ".yaml", ".yml"},
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify(path: Path) -> str:
    lowered = path.name.casefold()
    if lowered in {"controlDict".casefold(), "fvSchemes".casefold(), "fvSolution".casefold()}:
        return "solver_case_configuration"
    if any(token in lowered for token in ("normalizer", "scaler", "scale", "mean", "std")):
        return "normalizer"
    for role, suffixes in ROLE_SUFFIXES.items():
        if path.suffix.casefold() in suffixes:
            return role
    return "supporting_asset"


def build_manifest(roots: list[str | Path], origin: str = "unknown", metadata: dict | None = None) -> dict:
    artifacts = []
    resolved_roots = [Path(root).resolve() for root in roots]
    for root in resolved_roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in files:
            artifacts.append({
                "role": classify(path),
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
                "origin": origin,
                "physical_metadata": (metadata or {}).get(str(path), {}),
            })
    return {
        "schema_version": "1.0",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "roots": [str(root) for root in resolved_roots],
        "artifacts": artifacts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--origin", default="unknown")
    parser.add_argument("--metadata")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8")) if args.metadata else {}
    manifest = build_manifest(args.root, args.origin, metadata)
    Path(args.output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
