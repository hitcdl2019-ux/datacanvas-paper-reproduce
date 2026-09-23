#!/usr/bin/env python3
"""Deterministic, evidence-preserving SciML contract extraction."""

from __future__ import annotations

import json
import re
from pathlib import Path


FIELDS = {
    "governing_equations": ("navier-stokes", "giesekus", "pde", "constitutive equation", "momentum equation"),
    "state_variables": ("velocity", "pressure", "stress", "vorticity", "temperature", "conformation tensor"),
    "units": ("units", "dimensionless", "m/s", "pa", "kg/m", "second"),
    "dimensionless_numbers": ("reynolds", "weissenberg", "deborah", "mach", "prandtl", "reτ", "re_tau"),
    "geometry": ("geometry", "channel", "cylinder", "contraction", "domain"),
    "mesh": ("mesh", "grid", "cells", "resolution"),
    "initial_conditions": ("initial condition", "initially"),
    "boundary_conditions": ("boundary condition", "inlet", "outlet", "no-slip", "periodic"),
    "material_parameters": ("viscosity", "density", "relaxation time", "mobility factor", "material parameter"),
    "solvers": ("openfoam", "rheotool", "dns", "les", "finite element", "spectral solver"),
    "numerical_methods": ("discretization", "finite volume", "finite difference", "spectral method", "time integration"),
    "time_step_and_convergence": ("time step", "timestep", "cfl", "courant", "convergence", "residual"),
    "validation_observables": ("rmse", "relative error", "reynolds stress", "tke", "energy spectrum", "pdf", "correlation"),
}

RUNTIME_MARKERS = {
    "python": ("python", "pytorch", "tensorflow", "jax"),
    "julia": ("julia", "flux.jl", "diffeqflux", "sciml.jl", "bson"),
    "openfoam": ("openfoam", "rheotool", "simplefoam", "pimplefoam"),
    "mpi": ("mpi", "mpirun", "domain decomposition"),
    "container": ("docker", "singularity", "apptainer", "container"),
}

REPRODUCTION_LAYERS = (
    "public_weights_evaluation",
    "physics_statistics_evaluation",
    "staged_training",
    "numerical_data_regeneration",
    "coupled_solver_simulation",
    "full_chain",
)


def _snippets(text: str, markers: tuple[str, ...], limit: int = 5) -> list[dict]:
    normalized = " ".join(text.split())
    lowered = normalized.casefold()
    evidence = []
    for marker in markers:
        start = lowered.find(marker.casefold())
        if start < 0:
            continue
        left = max(0, start - 90)
        right = min(len(normalized), start + len(marker) + 140)
        evidence.append({"source": "paper_text", "marker": marker, "excerpt": normalized[left:right]})
        if len(evidence) >= limit:
            break
    return evidence


def detect_profile(text: str) -> str:
    lowered = text.casefold()
    physics_hits = sum(lowered.count(marker) for markers in FIELDS.values() for marker in markers)
    ml_hits = sum(lowered.count(marker) for marker in ("neural", "learning", "diffusion", "training", "network"))
    if physics_hits >= 5 and ml_hits >= 2:
        return "physics_ml"
    if physics_hits >= 5:
        return "computational_physics"
    # This pipeline is intentionally physics-only. Sparse evidence must produce
    # missing-field blockers, never route the paper into a conventional-ML path.
    return "computational_physics"


def _extract_values(text: str, field: str, markers: tuple[str, ...], evidence: list[dict]) -> list[str]:
    values = []
    if field == "dimensionless_numbers":
        for name, value in re.findall(r"\b(Re(?:ynolds)?|Re[_τa-z]*|Wi|We(?:issenberg)?|De(?:borah)?|Ma(?:ch)?)\s*(?:number\s*)?(?:=|of|:)\s*([0-9.eE+\-]+)", text, re.I):
            values.append(f"{name}={value}")
    elif field == "mesh":
        values.extend(match.replace(" ", "") for match in re.findall(r"\b\d+\s*[x×]\s*\d+(?:\s*[x×]\s*\d+)?\b", text, re.I))
    elif field == "time_step_and_convergence":
        values.extend(f"dt={value}" for value in re.findall(r"(?:Δt|dt|time\s*step)\s*(?:=|of|:)\s*([0-9.eE+\-]+)", text, re.I))
    if not values:
        values = [item["marker"] for item in evidence]
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result[:20]


def build_contract(text: str, source: str = "paper_text.txt", metadata: dict | None = None) -> dict:
    profile = detect_profile(text)
    physics_spec = {}
    missing = []
    for name, markers in FIELDS.items():
        evidence = _snippets(text, markers)
        status = "disclosed" if evidence else "not_found"
        physics_spec[name] = {"status": status, "values": _extract_values(text, name, markers, evidence), "evidence": evidence}
        if not evidence:
            missing.append(name)

    runtime = {}
    for name, markers in RUNTIME_MARKERS.items():
        evidence = _snippets(text, markers, limit=3)
        runtime[name] = {"required_or_mentioned": bool(evidence), "evidence": evidence}

    config_critical = [
        key for key in ("governing_equations", "state_variables", "geometry", "mesh", "boundary_conditions")
        if physics_spec[key]["status"] == "not_found"
    ]
    physics_gate = {
        "fit_status": "incomplete" if config_critical else "ready_for_plan_selection",
        "action": "block_affected_execution" if config_critical else "proceed",
        "execution_blockers": config_critical,
        "claim_blockers": missing,
        "message": (
            "关键物理配置缺失；只阻断依赖这些配置的执行候选。"
            if config_critical else "关键物理配置已有文本证据；仍需在 step_7 验证实际数值。"
        ),
    }
    return {
        "schema_version": "1.0",
        "source": source,
        "metadata": metadata or {},
        "project_profile": profile,
        "reproduction_layers": [{"type": layer, "availability": "candidate"} for layer in REPRODUCTION_LAYERS],
        "physics_spec": physics_spec,
        "runtime_requirements": runtime,
        "physics_gate": physics_gate,
    }


def write_contract(text_path: str | Path, output_path: str | Path, metadata_path: str | Path | None = None) -> dict:
    text_path = Path(text_path)
    metadata = {}
    if metadata_path and Path(metadata_path).exists():
        metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    contract = build_contract(text_path.read_text(encoding="utf-8", errors="replace"), str(text_path), metadata)
    Path(output_path).write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    return contract
