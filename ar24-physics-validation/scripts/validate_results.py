#!/usr/bin/env python3
"""Step-7 scientific validation with strict condition and claim gates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


CONDITION_KEYS = (
    "reynolds_number", "mesh", "time_step", "boundary_conditions",
    "coordinates", "time_indices", "variable_order", "units", "normalization",
)
AUTHORITATIVE_SOURCES = {"paper", "supplementary_material", "official_code", "user_confirmed"}
LEVELS = ("software_runnable", "numerically_consistent", "physics_validated", "paper_scope_reproduced")
VALIDATION_PHASES = (
    "validation_preflight",
    "reference_acquisition",
    "prediction_acquisition",
    "field_alignment",
    "quantitative_validation",
    "physics_validation",
    "validation_artifacts",
)


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        return round(value, 12)
    return value


def preflight(reference: dict, prediction: dict, task_type: str, interpolation: dict | None = None) -> dict:
    reference_condition = reference.get("condition") or {}
    prediction_condition = prediction.get("condition") or {}
    mismatches = []
    interpolation = interpolation or {}
    for key in CONDITION_KEYS:
        if key not in reference_condition and key not in prediction_condition:
            continue
        if _canonical(reference_condition.get(key)) == _canonical(prediction_condition.get(key)):
            continue
        if key == "mesh" and interpolation.get("allowed") is True and interpolation.get("method") and interpolation.get("estimated_error") is not None:
            continue
        mismatches.append({"field": key, "reference": reference_condition.get(key), "prediction": prediction_condition.get(key)})
    if task_type in {"stochastic", "conditional_generation"}:
        ref_n = reference_condition.get("ensemble_size")
        pred_n = prediction_condition.get("ensemble_size")
        if ref_n != pred_n:
            mismatches.append({"field": "ensemble_size", "reference": ref_n, "prediction": pred_n})
    return {
        "status": "passed" if not mismatches else "blocked",
        "mismatches": mismatches,
        "quantitative_comparison_allowed": not mismatches,
    }


def _resolve(spec_path: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else spec_path.parent / path


def _load_array(path: Path):
    suffix = path.suffix.casefold()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("data", payload) if isinstance(payload, dict) else payload
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return [[float(value) for value in row] for row in csv.reader(handle) if row]
    if suffix in {".npy", ".npz"}:
        import numpy as np
        value = np.load(path)
        if suffix == ".npz":
            value = value[value.files[0]]
        return value.tolist()
    raise ValueError(f"unsupported array format: {path.suffix}")


def _flatten(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    else:
        yield float(value)


def deterministic_metrics(reference, prediction) -> dict:
    ref = list(_flatten(reference))
    pred = list(_flatten(prediction))
    if len(ref) != len(pred) or not ref:
        raise ValueError(f"aligned arrays must have the same non-zero size: {len(ref)} != {len(pred)}")
    errors = [a - b for a, b in zip(pred, ref)]
    abs_errors = [abs(value) for value in errors]
    l1 = sum(abs_errors)
    l2 = math.sqrt(sum(value * value for value in errors))
    rmse = math.sqrt(sum(value * value for value in errors) / len(errors))
    ref_l2 = math.sqrt(sum(value * value for value in ref))
    return {
        "l1": l1,
        "l2": l2,
        "rmse": rmse,
        "relative_l2": l2 / ref_l2 if ref_l2 else None,
        "max_error": max(abs_errors),
        "sample_count": len(ref),
    }


def ensemble_metrics(reference, prediction) -> dict:
    if not isinstance(reference, list) or not isinstance(prediction, list) or not reference or not prediction:
        raise ValueError("ensemble inputs must be non-empty lists of samples")
    ref_samples = [list(_flatten(sample)) for sample in reference]
    pred_samples = [list(_flatten(sample)) for sample in prediction]
    width = len(ref_samples[0])
    if any(len(sample) != width for sample in ref_samples + pred_samples):
        raise ValueError("ensemble samples must have aligned shapes")
    ref_mean = [sum(sample[i] for sample in ref_samples) / len(ref_samples) for i in range(width)]
    pred_mean = [sum(sample[i] for sample in pred_samples) / len(pred_samples) for i in range(width)]
    mean_metrics = deterministic_metrics(ref_mean, pred_mean)
    ref_rms = math.sqrt(sum(value * value for sample in ref_samples for value in sample) / (len(ref_samples) * width))
    pred_rms = math.sqrt(sum(value * value for sample in pred_samples for value in sample) / (len(pred_samples) * width))
    ref_variance = sum((value - ref_mean[i]) ** 2 for sample in ref_samples for i, value in enumerate(sample)) / (len(ref_samples) * width)
    pred_variance = sum((value - pred_mean[i]) ** 2 for sample in pred_samples for i, value in enumerate(sample)) / (len(pred_samples) * width)

    combined = [value for sample in ref_samples + pred_samples for value in sample]
    low, high = min(combined), max(combined)
    bins = 20
    span = high - low or 1.0
    def histogram(samples):
        counts = [0] * bins
        values = [value for sample in samples for value in sample]
        for value in values:
            index = min(bins - 1, max(0, int((value - low) / span * bins)))
            counts[index] += 1
        return [count / len(values) for count in counts]
    ref_pdf, pred_pdf = histogram(ref_samples), histogram(pred_samples)
    pdf_l1 = sum(abs(a - b) for a, b in zip(ref_pdf, pred_pdf))

    def lag_one(values):
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        denominator = sum((value - mean) ** 2 for value in values)
        return sum((values[i] - mean) * (values[i + 1] - mean) for i in range(len(values) - 1)) / denominator if denominator else 0.0

    def spectral_energy(values):
        values = values[:64]
        spectrum = []
        for frequency in range(min(16, len(values))):
            real = sum(value * math.cos(2 * math.pi * frequency * index / len(values)) for index, value in enumerate(values))
            imag = -sum(value * math.sin(2 * math.pi * frequency * index / len(values)) for index, value in enumerate(values))
            spectrum.append((real * real + imag * imag) / max(1, len(values)))
        return spectrum
    ref_spectrum, pred_spectrum = spectral_energy(ref_mean), spectral_energy(pred_mean)
    spectrum_rmse = deterministic_metrics(ref_spectrum, pred_spectrum)["rmse"] if ref_spectrum else 0.0

    pred_std = [math.sqrt(sum((sample[i] - pred_mean[i]) ** 2 for sample in pred_samples) / len(pred_samples)) for i in range(width)]
    coverage = sum(1 for i, value in enumerate(ref_mean) if pred_mean[i] - 1.96 * pred_std[i] <= value <= pred_mean[i] + 1.96 * pred_std[i]) / width
    half = max(1, len(pred_samples) // 2)
    half_mean = [sum(sample[i] for sample in pred_samples[:half]) / half for i in range(width)]
    return {
        "ensemble_mean_rmse": mean_metrics["rmse"],
        "reference_rms": ref_rms,
        "prediction_rms": pred_rms,
        "rms_error": abs(pred_rms - ref_rms),
        "reference_ensemble_size": len(ref_samples),
        "prediction_ensemble_size": len(pred_samples),
        "pdf_l1": pdf_l1,
        "reynolds_normal_stress_error": abs(pred_variance - ref_variance),
        "tke_proxy_error": 0.5 * abs(pred_variance - ref_variance),
        "tke_spectrum_rmse": spectrum_rmse,
        "two_point_correlation_error": abs(lag_one(pred_mean) - lag_one(ref_mean)),
        "confidence_interval_coverage": coverage,
        "statistical_convergence_rmse": deterministic_metrics(pred_mean, half_mean)["rmse"],
    }


def conditional_metrics(reference, prediction) -> dict:
    metrics = ensemble_metrics(reference, prediction)
    ref_flat = list(_flatten(reference))
    pred_flat = list(_flatten(prediction))
    paired = deterministic_metrics(ref_flat, pred_flat)
    metrics.update({
        "paired_mse": paired["rmse"] ** 2,
        "recovery_region_rmse": paired["rmse"],
        "uncertainty_coverage": metrics["confidence_interval_coverage"],
    })
    return metrics


def evaluate_tolerances(metrics: dict, tolerances: dict, data_path: str | None = None) -> list[dict]:
    results = []
    for name, metric_value in metrics.items():
        if not isinstance(metric_value, (int, float)) or isinstance(metric_value, bool):
            continue
        rule = tolerances.get(name)
        if not isinstance(rule, dict):
            results.append({
                "metric": name, "reference_value": None, "measured_value": metric_value,
                "tolerance": None, "status": "reported_no_tolerance", "data_path": data_path,
                "evidence_source": None, "evidence": None,
            })
            continue
        source = rule.get("source")
        threshold = rule.get("value")
        operator = rule.get("operator", "<=")
        if source not in AUTHORITATIVE_SOURCES or not isinstance(threshold, (int, float)):
            results.append({
                "metric": name, "reference_value": rule.get("reference_value"), "measured_value": metric_value,
                "status": "invalid_tolerance_source", "tolerance": rule, "data_path": rule.get("data_path") or data_path,
                "evidence_source": source, "evidence": rule.get("evidence"),
            })
            continue
        passed = metric_value <= threshold if operator == "<=" else metric_value >= threshold if operator == ">=" else False
        results.append({
            "metric": name, "reference_value": rule.get("reference_value"), "measured_value": metric_value,
            "tolerance": {"operator": operator, "value": threshold}, "status": "passed" if passed else "failed",
            "data_path": rule.get("data_path") or data_path, "evidence_source": source, "evidence": rule.get("evidence"),
        })
    return results


def _file_record(path: Path, role: str) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"role": role, "path": str(path), "sha256": digest, "size_bytes": path.stat().st_size}


def _polyline(values, x0, y0, width, height, color):
    values = list(values)[:500]
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low or 1.0
    points = []
    for index, value in enumerate(values):
        x = x0 + (index / max(1, len(values) - 1)) * width
        y = y0 + height - ((value - low) / span) * height
        points.append(f"{x:.1f},{y:.1f}")
    return f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{" ".join(points)}"/>'


def _write_svg(path: Path, title: str, panels: list[tuple[str, list[float], str]]) -> None:
    width = 900
    panel_width = width / max(1, len(panels))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="300" viewBox="0 0 {width} 300">', '<rect width="100%" height="100%" fill="white"/>', f'<text x="20" y="25" font-family="sans-serif" font-size="16">{title}</text>']
    for index, (label, values, color) in enumerate(panels):
        x0 = index * panel_width + 25
        parts.append(f'<rect x="{x0}" y="50" width="{panel_width - 45}" height="210" fill="none" stroke="#999"/>')
        parts.append(f'<text x="{x0}" y="285" font-family="sans-serif" font-size="12">{label}</text>')
        parts.append(_polyline(values, x0, 50, panel_width - 45, 210, color))
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _heatmap_cells(values, x0, y0, width, height):
    values = list(values)[:2500]
    if not values:
        return ""
    columns = max(1, int(math.sqrt(len(values))))
    rows = math.ceil(len(values) / columns)
    low, high = min(values), max(values)
    span = high - low or 1.0
    cell_width, cell_height = width / columns, height / rows
    cells = []
    for index, value in enumerate(values):
        ratio = (value - low) / span
        red = int(255 * ratio)
        blue = int(255 * (1 - ratio))
        x = x0 + (index % columns) * cell_width
        y = y0 + (index // columns) * cell_height
        cells.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{cell_width + 0.2:.2f}" height="{cell_height + 0.2:.2f}" fill="rgb({red},70,{blue})"/>')
    return "".join(cells)


def _write_field_triptych(path: Path, reference, prediction, error) -> None:
    panels = (("Ground Truth", reference), ("Prediction", prediction), ("Absolute Error Map", error))
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="900" height="300" viewBox="0 0 900 300">', '<rect width="100%" height="100%" fill="white"/>', '<text x="20" y="25" font-family="sans-serif" font-size="16">Aligned field comparison</text>']
    for index, (label, values) in enumerate(panels):
        x0 = index * 300 + 25
        parts.append(_heatmap_cells(values, x0, 50, 250, 210))
        parts.append(f'<rect x="{x0}" y="50" width="250" height="210" fill="none" stroke="#333"/>')
        parts.append(f'<text x="{x0}" y="285" font-family="sans-serif" font-size="12">{label}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def create_validation_plots(reference, prediction, task_type: str, output_dir: Path) -> list[dict]:
    ref = list(_flatten(reference))
    pred = list(_flatten(prediction))
    count = min(len(ref), len(pred))
    ref, pred = ref[:count], pred[:count]
    error = [abs(a - b) for a, b in zip(ref, pred)]
    triptych_path = output_dir / "field_triptych.svg"
    _write_field_triptych(triptych_path, ref, pred, error)
    records = [_file_record(triptych_path, "validation_plot")]
    plot_specs = [
        ("slice_comparison.svg", "Slice / Profile Comparison", [("Ground Truth", ref, "#1f77b4"), ("Prediction", pred, "#ff7f0e")]),
        ("error_evolution.svg", "Error Evolution", [("Absolute Error", error, "#d62728")]),
    ]
    if task_type in {"stochastic", "conditional_generation"}:
        plot_specs.append(("ensemble_statistics.svg", "Ensemble Statistical Evidence", [("Reference ensemble", ref, "#9467bd"), ("Prediction ensemble", pred, "#8c564b")]))
    for filename, title, panels in plot_specs:
        path = output_dir / filename
        _write_svg(path, title, panels)
        records.append(_file_record(path, "validation_plot"))
    return records


def evaluate_physics_checks(raw_checks: list[dict]) -> list[dict]:
    checks = []
    for raw in raw_checks:
        check = dict(raw)
        if check.get("applicable") is False:
            check["status"] = "not_applicable"
            checks.append(check)
            continue
        measured = check.get("measured_value")
        reference = check.get("reference_value")
        tolerance = check.get("tolerance")
        source = check.get("evidence_source")
        evidence = check.get("evidence") or check.get("data_path")
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (measured, reference, tolerance)) and source in AUTHORITATIVE_SOURCES and evidence:
            check["status"] = "passed" if abs(measured - reference) <= tolerance else "failed"
        elif check.get("status") == "passed" and source in AUTHORITATIVE_SOURCES and evidence:
            check["status"] = "passed"
        elif check.get("status") == "failed":
            check["status"] = "failed"
        else:
            check["status"] = "not_evaluated"
        checks.append(check)
    return checks


def validate(spec: dict, spec_path: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    reference = spec.get("reference") or {}
    prediction = spec.get("prediction") or {}
    comparison = spec.get("comparison") or {}
    task_type = comparison.get("task_type", "deterministic")
    gate = preflight(reference, prediction, task_type, comparison.get("interpolation"))
    result = {
        "schema_version": "1.0",
        "phases": {
            "validation_preflight": gate,
            "reference_acquisition": {"status": "not_run"},
            "prediction_acquisition": {"status": "not_run"},
            "field_alignment": {
                "status": "blocked" if gate["status"] == "blocked" else "passed",
                "reference_condition": reference.get("condition", {}),
                "prediction_condition": prediction.get("condition", {}),
                "interpolation": comparison.get("interpolation") or {"allowed": False},
            },
            "quantitative_validation": {"status": "blocked" if gate["status"] == "blocked" else "pending", "metrics": []},
            "physics_validation": {"status": "pending", "checks": []},
            "validation_artifacts": {"status": "pending", "artifacts": []},
        },
    }
    artifacts = []
    software_runnable = False
    numerical_passed = False
    physics_passed = False
    if gate["status"] == "passed":
        ref_path = _resolve(spec_path, reference["data_path"])
        pred_path = _resolve(spec_path, prediction["data_path"])
        ref_data = _load_array(ref_path)
        pred_data = _load_array(pred_path)
        artifacts.extend((_file_record(ref_path, "ground_truth"), _file_record(pred_path, "prediction")))
        result["phases"]["reference_acquisition"] = {"status": "passed", "data_path": str(ref_path), "provenance": reference.get("provenance", {})}
        result["phases"]["prediction_acquisition"] = {"status": "passed", "data_path": str(pred_path), "provenance": prediction.get("provenance", {})}
        software_runnable = True
        metrics = conditional_metrics(ref_data, pred_data) if task_type == "conditional_generation" else ensemble_metrics(ref_data, pred_data) if task_type == "stochastic" else deterministic_metrics(ref_data, pred_data)
        metric_results = evaluate_tolerances(metrics, comparison.get("tolerances") or {}, str(pred_path))
        authoritative = [item for item in metric_results if item["status"] in {"passed", "failed"}]
        numerical_passed = bool(authoritative) and all(item["status"] == "passed" for item in authoritative)
        result["phases"]["quantitative_validation"] = {
            "status": "passed" if numerical_passed else "failed" if any(item["status"] == "failed" for item in authoritative) else "qualitative_only",
            "metrics": metric_results,
        }
        artifacts.extend(create_validation_plots(ref_data, pred_data, task_type, output_dir))

    checks = evaluate_physics_checks(spec.get("physics_checks") or [])
    applicable = [check for check in checks if check["status"] != "not_applicable"]
    physics_passed = numerical_passed and bool(applicable) and all(check["status"] == "passed" for check in applicable)
    result["phases"]["physics_validation"] = {
        "status": "passed" if physics_passed else "failed" if any(check["status"] == "failed" for check in applicable) else "not_fully_evaluated",
        "checks": checks,
    }
    result["phases"]["validation_artifacts"] = {"status": "passed" if artifacts else "not_available", "artifacts": artifacts}

    achieved = None
    if software_runnable:
        achieved = LEVELS[0]
    if numerical_passed:
        achieved = LEVELS[1]
    if physics_passed:
        achieved = LEVELS[2]
    if physics_passed and spec.get("paper_scope_match") is True:
        achieved = LEVELS[3]
    result["claim_gate"] = {
        "achieved_level": achieved,
        "software_runnable": software_runnable,
        "numerically_consistent": numerical_passed,
        "physics_validated": physics_passed,
        "paper_scope_reproduced": achieved == LEVELS[3],
        "promotion_blockers": gate["mismatches"] or [
            name for name, ok in (
                ("authoritative_numerical_tolerance", numerical_passed),
                ("applicable_physics_checks", physics_passed),
                ("paper_scope_match", spec.get("paper_scope_match") is True),
            ) if not ok
        ],
    }
    return result


def aggregate_validation_results(
    cases: list[dict],
    output_dir: Path,
    selected_execution_plan: dict | None = None,
) -> dict:
    if not isinstance(cases, list) or not cases:
        raise ValueError("aggregate cases must be a non-empty list")
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_cases = []
    phase_inputs = {name: [] for name in VALIDATION_PHASES}
    achieved_levels = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"cases[{index}] must be an object")
        case_name = str(case.get("case_name") or "").strip()
        result_path = Path(str(case.get("validation_result") or "")).expanduser()
        if not case_name or not result_path.is_file():
            raise ValueError(f"cases[{index}] requires case_name and readable validation_result")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        phases = result.get("phases")
        claim = result.get("claim_gate")
        if not isinstance(phases, dict) or not isinstance(claim, dict):
            raise ValueError(f"{case_name} validation_result lacks phases or claim_gate")
        for phase_name in VALIDATION_PHASES:
            phase = phases.get(phase_name)
            if not isinstance(phase, dict) or not str(phase.get("status") or "").strip():
                raise ValueError(f"{case_name} lacks phase {phase_name}")
            phase_inputs[phase_name].append((case_name, phase))
        achieved = claim.get("achieved_level")
        if achieved not in LEVELS:
            raise ValueError(f"{case_name} has invalid achieved_level: {achieved!r}")
        achieved_levels.append(achieved)
        normalized_cases.append(
            {
                "case_name": case_name,
                "validation_result_path": str(result_path.resolve()),
                "claim_gate": claim,
            }
        )

    aggregated_phases = {}
    for phase_name, entries in phase_inputs.items():
        statuses = [str(phase.get("status")).casefold() for _, phase in entries]
        if all(status == "passed" for status in statuses):
            aggregate_status = "passed"
        elif any(status in {"failed", "blocked"} for status in statuses):
            aggregate_status = "failed"
        else:
            aggregate_status = "partial"
        aggregate_phase = {
            "status": aggregate_status,
            "cases": [
                {"case_name": case_name, "status": phase.get("status")}
                for case_name, phase in entries
            ],
        }
        if phase_name == "quantitative_validation":
            aggregate_phase["metrics"] = [
                dict(metric, case_name=case_name)
                for case_name, phase in entries
                for metric in phase.get("metrics") or []
                if isinstance(metric, dict)
            ]
        elif phase_name == "physics_validation":
            aggregate_phase["checks"] = [
                dict(check, case_name=case_name)
                for case_name, phase in entries
                for check in phase.get("checks") or []
                if isinstance(check, dict)
            ]
        elif phase_name == "validation_artifacts":
            aggregate_phase["artifacts"] = [
                dict(artifact, case_name=case_name)
                for case_name, phase in entries
                for artifact in phase.get("artifacts") or []
                if isinstance(artifact, dict)
            ]
        aggregated_phases[phase_name] = aggregate_phase

    achieved_level = min(achieved_levels, key=LEVELS.index)
    result = {
        "schema_version": "1.1",
        "selected_execution_plan": selected_execution_plan or {},
        "cases": normalized_cases,
        "phases": aggregated_phases,
        "claim_gate": {
            "achieved_level": achieved_level,
            "aggregation_rule": "minimum_credible_level",
            "case_levels": {
                item["case_name"]: item["claim_gate"]["achieved_level"]
                for item in normalized_cases
            },
            "software_runnable": all(
                LEVELS.index(level) >= LEVELS.index("software_runnable")
                for level in achieved_levels
            ),
            "numerically_consistent": all(
                LEVELS.index(level) >= LEVELS.index("numerically_consistent")
                for level in achieved_levels
            ),
            "physics_validated": all(
                LEVELS.index(level) >= LEVELS.index("physics_validated")
                for level in achieved_levels
            ),
            "paper_scope_reproduced": all(
                level == "paper_scope_reproduced" for level in achieved_levels
            ),
        },
    }
    result_path = output_dir / "validation_result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec")
    source.add_argument("--aggregate-cases")
    parser.add_argument("--selected-plan")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    if args.aggregate_cases:
        cases = json.loads(Path(args.aggregate_cases).read_text(encoding="utf-8"))
        selected_plan = (
            json.loads(Path(args.selected_plan).read_text(encoding="utf-8"))
            if args.selected_plan
            else None
        )
        aggregate_validation_results(cases, output_dir, selected_plan)
    else:
        spec_path = Path(args.spec).resolve()
        result = validate(json.loads(spec_path.read_text(encoding="utf-8")), spec_path, output_dir)
        result_path = output_dir / "validation_result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path = output_dir / "validation_result.json"
    print(result_path)


if __name__ == "__main__":
    main()
