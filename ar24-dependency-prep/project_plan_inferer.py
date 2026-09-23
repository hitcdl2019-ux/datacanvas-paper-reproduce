import configparser
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None

READ_ME_PARSER_DIR = Path(__file__).resolve().parents[1] / "ar24-readme-parser"
if str(READ_ME_PARSER_DIR) not in sys.path:
    sys.path.insert(0, str(READ_ME_PARSER_DIR))

import readme_parser


README_GLOBS = ("README", "README.*", "readme.*")
REQUIREMENTS_GLOBS = ("requirements.txt", "requirements-*.txt", "requirements/*.txt")
ENVIRONMENT_FILES = ("environment.yml", "environment.yaml")

def infer_prep_plan_inputs(code_dir: str | Path, audit_report: str | Path | None = None) -> dict:
    code_path = Path(code_dir)
    analysis_sources = []
    warnings = []

    readmes = _unique_existing_paths(
        path
        for pattern in README_GLOBS
        for path in code_path.glob(pattern)
        if path.is_file()
    )
    readme_payloads = [(path, _read_text(path)) for path in readmes]
    analysis_sources.extend(str(path) for path, _ in readme_payloads)

    python_version = _infer_python_version(code_path, readme_payloads, audit_report)
    readme_analyses = []

    for path, text in readme_payloads:
        analysis = readme_parser.analyze_readme_text(text, source=str(path))
        readme_analyses.append(analysis)

    readme_parse = readme_parser.merge_analyses(readme_analyses) if readme_analyses else _empty_readme_parse()
    projection = readme_parse["step_projection"]

    for req_path in _requirements_files(code_path):
        analysis_sources.append(str(req_path))
        req_cmd = f"pip install -r {req_path.relative_to(code_path).as_posix()}"
        _append_unique(projection["dependency_cmds"], req_cmd)

    for env_path in _environment_files(code_path):
        analysis_sources.append(str(env_path))
        warnings.append(
            f"Found {env_path.name}; conda environment files need manual review before converting to dependency_cmds."
        )

    vcpkg_json = code_path / "vcpkg.json"
    if vcpkg_json.exists():
        analysis_sources.append(str(vcpkg_json))
        vcpkg_json_path = str(vcpkg_json)
    else:
        vcpkg_json_path = None

    return {
        "python_version": python_version,
        "vcpkg_json_path": vcpkg_json_path,
        "analysis_sources": _dedupe(analysis_sources),
        "warnings": warnings,
        "readme_parse": readme_parse,
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _unique_existing_paths(paths) -> list[Path]:
    seen = set()
    result = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def _requirements_files(code_path: Path) -> list[Path]:
    return _unique_existing_paths(
        path
        for pattern in REQUIREMENTS_GLOBS
        for path in code_path.glob(pattern)
        if path.is_file()
    )


def _environment_files(code_path: Path) -> list[Path]:
    return [code_path / name for name in ENVIRONMENT_FILES if (code_path / name).is_file()]


def _infer_python_version(
    code_path: Path,
    readme_payloads: list[tuple[Path, str]],
    audit_report: str | Path | None,
) -> str:
    for spec in _python_specs(code_path, readme_payloads, audit_report):
        version = _choose_python_version(spec)
        if version:
            return version
    return "3.10"


def _python_specs(
    code_path: Path,
    readme_payloads: list[tuple[Path, str]],
    audit_report: str | Path | None,
):
    pyproject = code_path / "pyproject.toml"
    if pyproject.exists() and tomllib:
        try:
            payload = tomllib.loads(_read_text(pyproject))
            project = payload.get("project", {})
            if project.get("requires-python"):
                yield project["requires-python"]
        except Exception:
            pass

    setup_cfg = code_path / "setup.cfg"
    if setup_cfg.exists():
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
            if parser.has_option("options", "python_requires"):
                yield parser.get("options", "python_requires")
        except configparser.Error:
            pass

    if audit_report:
        audit_path = Path(audit_report)
        if audit_path.exists():
            yield from _python_specs_from_text(_read_text(audit_path))

    for _, text in readme_payloads:
        yield from _python_specs_from_text(text)


def _python_specs_from_text(text: str):
    for match in re.finditer(r"python(?:_requires| requires| version)?\s*[:=>= ]\s*([<>=!~,\.\d ]+)", text, re.I):
        spec = match.group(1).strip()
        if re.search(r"\d+\.\d+", spec):
            yield spec


def _choose_python_version(spec: str) -> str | None:
    candidates = ("3.10", "3.11", "3.9", "3.12", "3.8")
    for candidate in candidates:
        if _satisfies_python_spec(candidate, spec):
            return candidate
    exact = re.search(r"(\d+\.\d+)", spec)
    return exact.group(1) if exact else None


def _satisfies_python_spec(version: str, spec: str) -> bool:
    version_tuple = _version_tuple(version)
    constraints = re.findall(r"(>=|<=|==|>|<|~=)\s*(\d+\.\d+)", spec)
    if not constraints:
        return bool(re.search(r"\b" + re.escape(version) + r"\b", spec))
    for operator, required in constraints:
        required_tuple = _version_tuple(required)
        if operator == ">=" and not (version_tuple >= required_tuple):
            return False
        if operator == ">" and not (version_tuple > required_tuple):
            return False
        if operator == "<=" and not (version_tuple <= required_tuple):
            return False
        if operator == "<" and not (version_tuple < required_tuple):
            return False
        if operator == "==" and not (version_tuple == required_tuple):
            return False
        if operator == "~=" and not (version_tuple >= required_tuple):
            return False
    return True


def _version_tuple(version: str) -> tuple[int, int]:
    major, minor = version.split(".", 1)
    return int(major), int(minor)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _empty_readme_parse() -> dict:
    return {
        "documents": [],
        "commands": [],
        "links": [],
        "issues": [],
        "unprojected_blocks": [],
        "block_semantics": [],
        "step_projection": {
            "dependency_cmds": [],
            "runtime_dependency_cmds": [],
            "data_cmds": [],
            "weight_cmds": [],
            "deferred_step7_cmds": [],
            "restricted_datasets": [],
            "physics_asset_links": [],
        },
        "environment_bundle": {
            "schema_version": "1.0",
            "runtimes": [],
            "system_capabilities": [],
        },
        "summary": {
            "document_count": 0,
            "block_count": 0,
            "command_count": 0,
            "link_count": 0,
            "issue_count": 0,
            "unprojected_block_count": 0,
        },
    }


def _dedupe(values) -> list:
    result = []
    seen = set()
    for value in values:
        marker = repr(value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
