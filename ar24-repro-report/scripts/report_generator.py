import math
import os
import json
import csv
import hashlib
import importlib.util
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _writable(path):
    """目录可写判定：存在且可写，或其最近的已存在父级可写。"""
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return bool(p) and os.access(p, os.W_OK)


def resolve_workspace_root():
    """
    Project work root must be explicitly selected by the user.
    """
    env = (os.environ.get("WORKSPACE_ROOT") or "").strip()
    if env:
        return env
    raise RuntimeError(
        "WORKSPACE_ROOT is required before report generation. Complete the "
        "path-confirmation step and export WORKSPACE_ROOT=<repro_root>."
    )


try:
    import docx
    from docx.shared import Pt, RGBColor, Inches, Emu
    from docx.oxml.ns import qn, nsdecls
    from docx.oxml import parse_xml, OxmlElement
    from docx.oxml.shape import CT_Inline
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
    from docx.image.image import Image as DocxImage
    from docx.parts.image import ImagePart
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
except ImportError as exc:
    raise RuntimeError(
        "python-docx is required before report generation. Install it during "
        "step_4 dependency preparation; step_8 must not install dependencies."
    ) from exc


# ───────── 字体与排版底层 XML 工具 ─────────

REPORT_FONT_ENGLISH = "Times New Roman"
REPORT_FONT_CHINESE = "SimSun"
REPORT_BLUE = RGBColor(0x1A, 0x56, 0xDB)
REPORT_GRAY = RGBColor(0x66, 0x66, 0x66)


def _set_run_font(run, font_en='Times New Roman', font_cn='SimSun', size_pt=11, bold=False, color=None):
    """精确控制中英文字体、字号、加粗与颜色"""
    run.font.name = font_en
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = parse_xml(f'<w:rFonts {nsdecls("w")} />')
        rPr.insert(0, rFonts)
    rFonts.set(qn('w:eastAsia'), font_cn)
    rFonts.set(qn('w:ascii'), font_en)
    rFonts.set(qn('w:hAnsi'), font_en)
    rFonts.set(qn('w:cs'), font_en)
    if color:
        run.font.color.rgb = color


def _add_styled_paragraph(doc, text, font_en='Times New Roman', font_cn='SimSun', size_pt=11, bold=False, color=None, alignment=None, space_after_pt=6):
    """添加格式化段落"""
    p = doc.add_paragraph()
    if text:
        run = p.add_run(text)
        _set_run_font(run, font_en, font_cn, size_pt, bold, color)
    if alignment is not None:
        p.alignment = alignment
    p.paragraph_format.space_after = Pt(space_after_pt)
    return p


def _add_styled_heading(doc, text, level=1, font_en='Times New Roman', font_cn='SimSun', size_pt=None, color=None):
    """添加与标准报告一致的大纲标题。"""
    size_map = {1: 14, 2: 13, 3: 11, 4: 10.5}
    if size_pt is None:
        size_pt = size_map.get(level, 11)
    heading = doc.add_heading(level=level)
    run = heading.add_run(text)
    _set_run_font(run, font_en, font_cn, size_pt, bold=True, color=color or REPORT_BLUE)
    heading.paragraph_format.space_before = Pt(24 if level == 1 else 10)
    heading.paragraph_format.space_after = Pt(6)
    heading.paragraph_format.keep_with_next = True
    return heading


def _set_default_font(doc, font_en='Times New Roman', font_cn='SimSun', size_pt=11):
    """设置标准报告的页面、正文与标题样式。"""
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin = Inches(0.98)
    section.right_margin = Inches(0.98)
    section.top_margin = Inches(0.79)
    section.bottom_margin = Inches(0.79)

    style_specs = {
        "Normal": (size_pt, False, None, 0, 10),
        "Title": (28, True, REPORT_BLUE, 0, 4),
        "Subtitle": (14, False, REPORT_GRAY, 0, 18),
        "Heading 1": (14, True, REPORT_BLUE, 24, 6),
        "Heading 2": (13, True, REPORT_BLUE, 10, 6),
        "Heading 3": (11, True, REPORT_BLUE, 10, 4),
        "List Bullet": (10.5, False, None, 0, 3),
        "List Number": (10.5, False, None, 0, 3),
    }
    for name, (font_size, bold, color, before, after) in style_specs.items():
        if name not in doc.styles:
            continue
        style = doc.styles[name]
        style.font.name = font_en
        style.font.size = Pt(font_size)
        style.font.bold = bold
        if color:
            style.font.color.rgb = color
        rPr = style.element.get_or_add_rPr()
        rFonts = rPr.find(qn('w:rFonts'))
        if rFonts is None:
            rFonts = parse_xml(f'<w:rFonts {nsdecls("w")} />')
            rPr.insert(0, rFonts)
        for attribute, value in (
            ('eastAsia', font_cn),
            ('ascii', font_en),
            ('hAnsi', font_en),
            ('cs', font_en),
        ):
            rFonts.set(qn(f'w:{attribute}'), value)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        if name.startswith("Heading"):
            style.paragraph_format.keep_with_next = True


def _iter_cell_paragraphs(cell):
    for paragraph in cell.paragraphs:
        yield paragraph
    for table in cell.tables:
        for row in table.rows:
            for nested_cell in row.cells:
                yield from _iter_cell_paragraphs(nested_cell)


def _iter_document_paragraphs(doc):
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_cell_paragraphs(cell)
    for section in doc.sections:
        for part in (section.header, section.footer):
            yield from part.paragraphs
            for table in part.tables:
                for row in table.rows:
                    for cell in row.cells:
                        yield from _iter_cell_paragraphs(cell)


def _enforce_document_fonts(doc, font_en=REPORT_FONT_ENGLISH, font_cn=REPORT_FONT_CHINESE):
    """Apply the fixed bilingual font contract to every generated run."""
    _set_default_font(doc, font_en, font_cn, 11)
    for paragraph in _iter_document_paragraphs(doc):
        for run in paragraph.runs:
            size = run.font.size.pt if run.font.size else 11
            bold = bool(run.bold)
            color = run.font.color.rgb
            _set_run_font(run, font_en, font_cn, size, bold, color)


def _set_cell_font(cell, font_en='Times New Roman', font_cn='SimSun', size_pt=9, bold=False):
    """设置表格内文字样式"""
    for paragraph in cell.paragraphs:
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.space_before = Pt(2)
        for run in paragraph.runs:
            _set_run_font(run, font_en, font_cn, size_pt, bold)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def _shade_cells(cells, color_hex='F2F2F2'):
    """设置表格单元格背景色"""
    for cell in cells:
        shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color_hex}"/>')
        cell._element.get_or_add_tcPr().append(shading)


def _set_table_borders_and_margins(table, color_hex="5B9BD5"):
    """设置与标准报告一致的蓝色边框、交替底色和单元格边距。"""
    tblPr = table._element.xpath('w:tblPr')
    if tblPr:
        # 设置细灰色轻量边框 (1/4 pt)
        borders = parse_xml(
            f'<w:tblBorders {nsdecls("w")}>'
            f'  <w:top w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
            f'  <w:bottom w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
            f'  <w:left w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
            f'  <w:right w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
            f'  <w:insideH w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
            f'  <w:insideV w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
            f'</w:tblBorders>'
        )
        tblPr[0].append(borders)
        
        # 设置内部边距: 上下 120 dxa (6pt)，左右 150 dxa (7.5pt)
        margins = parse_xml(
            f'<w:tblCellMar {nsdecls("w")}>'
            f'  <w:top w:w="120" w:type="dxa"/>'
            f'  <w:bottom w:w="120" w:type="dxa"/>'
            f'  <w:left w:w="150" w:type="dxa"/>'
            f'  <w:right w:w="150" w:type="dxa"/>'
            f'</w:tblCellMar>'
        )
        tblPr[0].append(margins)
    for row_index, row in enumerate(table.rows[1:], start=1):
        if row_index % 2 == 1:
            _shade_cells(row.cells, "D9E6F5")
        for cell in row.cells:
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def _set_table_widths(table, widths, *, repeat_header=True):
    """Apply deterministic widths, repeat headers, and keep rows intact across pages."""
    table.autofit = False
    total_twips = sum(int(width / 635) for width in widths)
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(total_twips))
    tbl_w.set(qn("w:type"), "dxa")
    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths:
        column = OxmlElement("w:gridCol")
        column.set(qn("w:w"), str(int(width / 635)))
        grid.append(column)
    for row in table.rows:
        tr_pr = row._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:cantSplit")) is None:
            tr_pr.append(OxmlElement("w:cantSplit"))
        for index, width in enumerate(widths):
            if index >= len(row.cells):
                break
            row.cells[index].width = width
            tc_pr = row.cells[index]._tc.get_or_add_tcPr()
            tc_w = tc_pr.find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.append(tc_w)
            tc_w.set(qn("w:w"), str(int(width / 635)))
            tc_w.set(qn("w:type"), "dxa")
    if repeat_header and table.rows:
        tr_pr = table.rows[0]._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:tblHeader")) is None:
            header = OxmlElement("w:tblHeader")
            header.set(qn("w:val"), "true")
            tr_pr.append(header)


def _set_table_no_borders(table):
    """Render a compact metadata table without visible grid lines."""
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.find(qn("w:tblBorders"))
    if borders is not None:
        tbl_pr.remove(borders)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "nil")
        borders.append(element)
    tbl_pr.append(borders)


def _add_list_item(doc, text, font_english='Times New Roman', font_chinese='SimSun'):
    """Add a real Word bullet paragraph instead of a glyph-prefixed paragraph."""
    paragraph = doc.add_paragraph(style="List Bullet")
    run = paragraph.add_run(str(text))
    _set_run_font(run, font_english, font_chinese, 10)
    paragraph.paragraph_format.space_after = Pt(3)
    return paragraph


def _as_list(value):
    if isinstance(value, list):
        return value
    if value:
        return [value]
    return []


def _case_label(item):
    if not isinstance(item, dict):
        return str(item)
    for key in (
        "case_name", "name", "task_name", "plan_name", "title",
        "execution_name", "id", "type"
    ):
        value = item.get(key)
        if value:
            return str(value)
    return "-"


def _normalize_name(value):
    return str(value or "").strip().lower()


def _stringify(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def _index_by_case_name(rows):
    indexed = {}
    for row in _as_list(rows):
        if not isinstance(row, dict):
            continue
        for key in ("case_name", "name", "task_name", "plan_name", "title", "id"):
            name = row.get(key)
            if name:
                indexed[_normalize_name(name)] = row
    return indexed


def _extract_execution_plan_items(execution_plan_selection):
    """Return execution-plan rows in the exact order chosen by the user."""
    if not execution_plan_selection:
        return []
    if isinstance(execution_plan_selection, str):
        return [{"name": execution_plan_selection}]
    if isinstance(execution_plan_selection, list):
        return execution_plan_selection
    if not isinstance(execution_plan_selection, dict):
        return [{"name": str(execution_plan_selection)}]

    selected = (
        execution_plan_selection.get("selected_execution_plan")
        or execution_plan_selection.get("selected_plan")
        or execution_plan_selection.get("selected_option")
    )
    if selected:
        return _extract_execution_plan_items(selected)

    for key in (
        "execution_items", "reproduction_points", "cases", "tasks",
        "steps", "plan_items"
    ):
        items = execution_plan_selection.get(key)
        if isinstance(items, list) and items:
            return items

    options = execution_plan_selection.get("options")
    if isinstance(options, list) and options:
        return options
    if any(execution_plan_selection.get(key) for key in ("name", "case_name", "type", "id")):
        return [execution_plan_selection]
    return []


def _align_rows_to_execution_plan(execution_plan_selection, rows, row_type):
    """Use execution-plan items as the authoritative left column for sections III/IV."""
    plan_items = _extract_execution_plan_items(execution_plan_selection)
    if not plan_items:
        return rows

    existing = _index_by_case_name(rows)
    aligned = []
    for plan_item in plan_items:
        if not isinstance(plan_item, dict):
            plan_item = {"name": str(plan_item)}
        label = _case_label(plan_item)
        source = existing.get(_normalize_name(label), {})

        if row_type == "readiness":
            aligned.append({
                "case_name": label,
                "config_path": source.get("config_path") or plan_item.get("config_path") or plan_item.get("code_path") or plan_item.get("entrypoint") or "-",
                "status": source.get("status") or plan_item.get("code_support") or plan_item.get("availability") or plan_item.get("readiness") or "-",
                "barrier": source.get("barrier") or _stringify(plan_item.get("blocking_reasons")) or _stringify(plan_item.get("risks")) or plan_item.get("risk") or "-",
                "gpu_spec": source.get("gpu_spec") or plan_item.get("recommended_gpu") or _stringify(plan_item.get("required_resources")) or plan_item.get("gpu_spec") or "-",
                "est_time": source.get("est_time") or plan_item.get("estimated_gpu_time") or plan_item.get("estimated_time") or "-",
            })
        else:
            aligned.append({
                "case_name": label,
                "status": source.get("status") or plan_item.get("run_status") or plan_item.get("status") or "未执行/待执行",
                "output_detail": source.get("output_detail") or _stringify(plan_item.get("success_criteria")) or plan_item.get("expected_output") or plan_item.get("paper_evidence") or "-",
                "actual_time": source.get("actual_time") or plan_item.get("actual_time") or plan_item.get("estimated_gpu_time") or "-",
            })
    return aligned


def _nonnegative_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _positive_int(value):
    number = _nonnegative_number(value)
    if number is None or not number.is_integer() or number <= 0:
        return None
    return int(number)


def _duration_hours(item, primary_key="duration_hours"):
    hours = _nonnegative_number(item.get(primary_key))
    if hours is not None:
        return hours
    days = _nonnegative_number(item.get(primary_key.replace("hours", "days")))
    return days * 24 if days is not None else None


def _round_metric(value):
    return round(value, 6) if value is not None else None


_SCOPE_DETAIL_FIELDS = (
    "dataset",
    "data_scale",
    "epochs",
    "steps",
    "sample_count",
    "checkpoint",
    "evaluation_protocol",
)
_SCOPE_DETAIL_V2_CATEGORIES = (
    "training", "inference", "numerical_simulation", "experimental_fitting", "coupled_simulation"
)

_INVALID_SCOPE_DETAIL_VALUES = {"unknown", "undisclosed"}
_NOT_APPLICABLE = "not_applicable"
_COMPLETE_PHASE_STATUSES = {
    "complete",
    "completed",
    "passed",
    "success",
    "succeeded",
}


def _normalized_scope_detail(value):
    """Preserve supported detail values while rejecting unsupported shapes."""
    if isinstance(value, dict):
        if str(value.get("schema_version", "")).startswith("2") or any(key in value for key in _SCOPE_DETAIL_V2_CATEGORIES):
            normalized = {"schema_version": "2.0"}
            for category in _SCOPE_DETAIL_V2_CATEGORIES:
                if category in value:
                    normalized[category] = value.get(category)
            return normalized
        return {field: value.get(field) for field in _SCOPE_DETAIL_FIELDS if field in value}
    if isinstance(value, str):
        return value.strip() or None
    return None


def _scope_detail_signature(value):
    """Return a signature only when every canonical scope field is usable."""
    if not isinstance(value, dict):
        return None

    if str(value.get("schema_version", "")).startswith("2") or any(key in value for key in _SCOPE_DETAIL_V2_CATEGORIES):
        signature = {"schema_version": "2.0"}
        found = False
        for category in _SCOPE_DETAIL_V2_CATEGORIES:
            if category not in value:
                continue
            details = value.get(category)
            if not isinstance(details, dict) or not details:
                return None
            normalized_details = {}
            for key, raw in sorted(details.items()):
                if isinstance(raw, bool) or raw is None:
                    return None
                if isinstance(raw, str):
                    normalized = " ".join(raw.split()).casefold()
                    if not normalized or normalized in _INVALID_SCOPE_DETAIL_VALUES:
                        return None
                    normalized_details[key] = normalized
                elif isinstance(raw, (int, float)) and math.isfinite(float(raw)):
                    normalized_details[key] = float(raw)
                elif isinstance(raw, (list, dict)):
                    normalized_details[key] = json.dumps(raw, sort_keys=True, ensure_ascii=False).casefold()
                else:
                    return None
            signature[category] = normalized_details
            found = True
        return signature if found else None

    signature = {}
    for field in _SCOPE_DETAIL_FIELDS:
        if field not in value:
            return None
        raw = value.get(field)
        if isinstance(raw, bool) or raw is None:
            return None
        if isinstance(raw, str):
            normalized = " ".join(raw.split()).casefold()
            if not normalized or normalized in _INVALID_SCOPE_DETAIL_VALUES:
                return None
            signature[field] = normalized
            continue
        if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
            signature[field] = float(raw)
            continue
        return None
    return signature


def _is_training_scope(scope):
    normalized = str(scope or "").strip().casefold().replace("-", "_")
    return any(marker in normalized for marker in ("train", "fine_tun", "finetun"))


def _has_training_progress(scope, signature):
    if not _is_training_scope(scope) or signature is None:
        return True
    if signature.get("schema_version") == "2.0":
        training = signature.get("training")
        return isinstance(training, dict) and any(training.get(field) not in (None, _NOT_APPLICABLE) for field in ("epochs", "steps"))
    return any(signature[field] != _NOT_APPLICABLE for field in ("epochs", "steps"))


def _nonempty_text(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _summarize_compute_usage(compute_usage):
    """Normalize an optional compute ledger and derive comparable metrics."""
    notes = []
    if not isinstance(compute_usage, dict):
        compute_usage = {}
        notes.append("未提供有效的算力使用台账。")

    paper_input = compute_usage.get("paper")
    if not isinstance(paper_input, dict):
        paper_input = {}
        notes.append("未提供有效的论文算力记录。")

    reproduction_input = compute_usage.get("reproduction")
    if not isinstance(reproduction_input, dict):
        reproduction_input = {}
        notes.append("未提供有效的复现算力记录。")

    comparison_input = compute_usage.get("comparison")
    if not isinstance(comparison_input, dict):
        comparison_input = {}

    paper_scope = str(paper_input.get("scope") or "").strip()
    paper_scope_detail = _normalized_scope_detail(paper_input.get("scope_detail"))
    paper_scope_signature = _scope_detail_signature(paper_input.get("scope_detail"))
    paper_resource_type = str(paper_input.get("resource_type") or "").strip().lower()
    paper_disclosed = paper_input.get("disclosed") is True
    paper_evidence = _nonempty_text(paper_input.get("evidence"))
    paper_source_page = _nonempty_text(paper_input.get("source_page"))
    paper_wall_clock_hours = None
    paper_device_hours = None
    paper_device_count = _positive_int(paper_input.get("device_count"))
    paper_device_hours_consistent = True

    if paper_disclosed:
        paper_wall_clock_hours = _duration_hours(paper_input, "wall_clock_hours")
        explicit_paper_device_hours = _nonnegative_number(paper_input.get("device_hours"))
        expected_paper_device_hours = None
        if paper_wall_clock_hours is not None and paper_device_count is not None:
            expected_paper_device_hours = paper_wall_clock_hours * paper_device_count
        if (
            explicit_paper_device_hours is not None
            and expected_paper_device_hours is not None
            and not math.isclose(
                explicit_paper_device_hours,
                expected_paper_device_hours,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
        ):
            paper_device_hours_consistent = False
            notes.append(
                "论文显式设备小时与墙钟时间 × 设备数量不一致，已拒绝该设备小时总数。"
            )
        elif explicit_paper_device_hours is not None:
            paper_device_hours = explicit_paper_device_hours
        elif expected_paper_device_hours is not None:
            paper_device_hours = expected_paper_device_hours
        if paper_wall_clock_hours is None:
            notes.append("论文未提供有效的墙钟时间。")
        if paper_device_hours is None:
            notes.append("论文未提供可计算的设备时。")
        if explicit_paper_device_hours is not None and paper_device_count is None:
            notes.append("论文设备数量缺失或无效，显式设备小时仅展示，不参与设备小时比较。")
    elif paper_input:
        notes.append("论文算力用量未披露，不推断论文耗时。")

    reproduction_scope = str(reproduction_input.get("scope") or "").strip()
    reproduction_scope_detail = _normalized_scope_detail(reproduction_input.get("scope_detail"))
    reproduction_scope_signature = _scope_detail_signature(reproduction_input.get("scope_detail"))
    reproduction_end_to_end_hours = _duration_hours(reproduction_input, "end_to_end_hours")
    reproduction_step_7_hours = _duration_hours(reproduction_input, "step_7_hours")
    reproduction_device_hours = {}
    normalized_phases = []
    phases = reproduction_input.get("phases", [])
    phases_complete = True
    invalid_phase_count = 0
    if not isinstance(phases, list):
        phases = []
        phases_complete = False
        invalid_phase_count = 1
        notes.append("复现阶段记录格式无效。")
    elif not phases:
        phases_complete = False
        if reproduction_input:
            notes.append("复现未提供阶段记录，设备小时比较不可用。")

    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            phases_complete = False
            invalid_phase_count += 1
            notes.append(f"复现阶段 {index + 1} 不是有效对象，已忽略并阻断设备小时比较。")
            continue
        duration_hours = _duration_hours(phase)
        device_count = _positive_int(phase.get("device_count"))
        resource_type = str(phase.get("resource_type") or "").strip().lower()
        phase_status = _nonempty_text(phase.get("status"))
        normalized_phase_status = phase_status.casefold() if phase_status else None
        phase_incomplete = (
            phase.get("complete") is False
            or (
                normalized_phase_status is not None
                and normalized_phase_status not in _COMPLETE_PHASE_STATUSES
            )
        )
        if phase_incomplete:
            phase_name = str(phase.get("name") or index + 1)
            status_label = phase_status or "complete=false"
            notes.append(
                f"复现阶段 {phase_name} 的完成状态为 {status_label}，已阻断设备小时比较。"
            )
        raw_device_count_evidence = phase.get("device_count_evidence")
        device_count_evidence = (
            raw_device_count_evidence.strip() or None
            if isinstance(raw_device_count_evidence, str)
            else None
        )
        if duration_hours is None or device_count is None or resource_type not in {"cpu", "gpu"}:
            phase_name = str(phase.get("name") or index + 1)
            phases_complete = False
            invalid_phase_count += 1
            notes.append(
                f"复现阶段 {phase_name} 的时长、设备数量或资源类型无效，已忽略并阻断设备小时比较。"
            )
            continue
        device_hours = None
        if resource_type == "cpu" and device_count_evidence is None:
            phase_name = str(phase.get("name") or index + 1)
            phase_incomplete = True
            notes.append(
                f"复现阶段 {phase_name} 缺少命令、配置或环境变量形式的逻辑核分配证据，"
                "未计算 CPU 设备时并阻断设备小时比较。"
            )
        else:
            device_hours = duration_hours * device_count
        if phase_incomplete:
            phases_complete = False
            invalid_phase_count += 1
        normalized_phases.append({
            "name": phase.get("name"),
            "status": phase_status,
            "duration_hours": _round_metric(duration_hours),
            "resource_type": resource_type,
            "device_model": phase.get("device_model"),
            "device_count": device_count,
            "device_count_evidence": device_count_evidence,
            "device_hours": _round_metric(device_hours),
        })
        if device_hours is not None:
            reproduction_device_hours[resource_type] = (
                reproduction_device_hours.get(resource_type, 0) + device_hours
            )

    reproduction_device_hours = {
        resource_type: _round_metric(device_hours)
        for resource_type, device_hours in reproduction_device_hours.items()
    }

    comparable = comparison_input.get("comparable") is True
    comparison_reason = str(comparison_input.get("reason") or "").strip()
    if comparable and (not paper_scope or not reproduction_scope):
        comparable = False
        comparison_reason = "不可直接比较：论文与复现均须提供实验范围。"
        notes.append(comparison_reason)
    elif paper_scope and reproduction_scope and paper_scope != reproduction_scope:
        comparable = False
        comparison_reason = f"不可直接比较：实验范围不同，论文为 {paper_scope}，复现为 {reproduction_scope}。"
        notes.append(comparison_reason)
    elif comparable and not paper_disclosed:
        comparable = False
        comparison_reason = "不可直接比较：论文未披露算力用量。"
        notes.append(comparison_reason)
    elif comparable and paper_wall_clock_hours is None:
        comparable = False
        comparison_reason = "不可直接比较：论文缺少有效的墙钟时间。"
        notes.append(comparison_reason)
    elif comparable and (paper_evidence is None or paper_source_page is None):
        comparable = False
        comparison_reason = "不可直接比较：论文必须提供非空原文证据和来源页码。"
        notes.append(comparison_reason)
    elif comparable and (paper_scope_signature is None or reproduction_scope_signature is None):
        comparable = False
        comparison_reason = "不可直接比较：论文与复现均须提供完整结构化实验范围细节。"
        notes.append(comparison_reason)
    elif comparable and (
        not _has_training_progress(paper_scope, paper_scope_signature)
        or not _has_training_progress(reproduction_scope, reproduction_scope_signature)
    ):
        comparable = False
        comparison_reason = "不可直接比较：训练范围的 epochs 或 steps 至少一项必须为实质值。"
        notes.append(comparison_reason)
    elif comparable and paper_scope_signature != reproduction_scope_signature:
        comparable = False
        comparison_reason = "不可直接比较：论文与复现实验范围细节不一致。"
        notes.append(comparison_reason)
    elif not comparable and not comparison_reason:
        comparison_reason = "算力记录未声明为可直接比较。"

    wall_clock_ratio = None
    device_hours_ratio = None
    wall_clock_comparable = False
    device_hours_comparable = False
    if comparable:
        if paper_wall_clock_hours is not None and paper_wall_clock_hours > 0 and reproduction_step_7_hours is not None:
            wall_clock_ratio = reproduction_step_7_hours / paper_wall_clock_hours
            wall_clock_comparable = True
        else:
            notes.append("墙钟耗时缺失或论文墙钟耗时不是正数，无法计算墙钟比率。")

        reproduction_resource_hours = reproduction_device_hours.get(paper_resource_type)
        if not phases_complete:
            notes.append("复现阶段记录不完整，设备小时比较已阻断；墙钟比较不受此项影响。")
        elif paper_device_count is None:
            notes.append("论文设备数量缺失或无效，设备小时比较已阻断。")
        elif not paper_device_hours_consistent:
            notes.append("论文设备小时总数未通过一致性校验，设备小时比较已阻断。")
        elif paper_device_hours is not None and paper_device_hours > 0 and reproduction_resource_hours is not None:
            device_hours_ratio = reproduction_resource_hours / paper_device_hours
            device_hours_comparable = True
        else:
            notes.append("论文与复现缺少可匹配的资源类型或设备时，无法计算设备时比率。")

    wall_clock_ratio = _round_metric(wall_clock_ratio)
    device_hours_ratio = _round_metric(device_hours_ratio)
    wall_clock_savings_percent = _round_metric(
        (1 - wall_clock_ratio) * 100 if wall_clock_ratio is not None else None
    )
    device_hours_savings_percent = _round_metric(
        (1 - device_hours_ratio) * 100 if device_hours_ratio is not None else None
    )

    return {
        "paper": {
            "disclosed": paper_disclosed,
            "scope": paper_scope or None,
            "scope_detail": paper_scope_detail,
            "wall_clock_hours": _round_metric(paper_wall_clock_hours),
            "resource_type": paper_resource_type or None,
            "device_model": paper_input.get("device_model"),
            "device_count": paper_device_count,
            "device_hours": _round_metric(paper_device_hours),
            "evidence": paper_evidence,
            "source_page": paper_source_page,
        },
        "reproduction": {
            "scope": reproduction_scope or None,
            "scope_detail": reproduction_scope_detail,
            "end_to_end_hours": _round_metric(reproduction_end_to_end_hours),
            "step_7_hours": _round_metric(reproduction_step_7_hours),
            "phases": normalized_phases,
            "phases_complete": phases_complete,
            "invalid_phase_count": invalid_phase_count,
            "device_hours": reproduction_device_hours,
        },
        "comparison": {
            "comparable": comparable,
            "reason": comparison_reason or None,
            "wall_clock_comparable": wall_clock_comparable,
            "device_hours_comparable": device_hours_comparable,
            "wall_clock_ratio": wall_clock_ratio,
            "device_hours_ratio": device_hours_ratio,
            "wall_clock_savings_percent": wall_clock_savings_percent,
            "device_hours_savings_percent": device_hours_savings_percent,
        },
        "notes": notes,
    }


def _format_hours(value):
    """Format a normalized wall-clock duration for report display."""
    return "未采集" if value is None else f"{value:.2f} 小时"


def _format_device_hours(value, resource_type=None):
    """Format device-hours without combining unlike resource types."""
    if value is None:
        return "未采集"
    label = str(resource_type or "device").upper()
    return f"{value:.2f} {label}-hours"


def _add_compute_usage_section(
    doc,
    compute_usage,
    font_english,
    font_chinese,
    *,
    include_heading=True,
):
    """Render the optional compute ledger as a dedicated report chapter."""
    if include_heading:
        _add_styled_heading(
            doc,
            "五、算力资源消耗对比 (Compute Usage Comparison)",
            level=1,
            font_en=font_english,
            font_cn=font_chinese,
        )
    if not isinstance(compute_usage, dict) or not compute_usage:
        _add_styled_paragraph(
            doc,
            "未采集算力账本",
            font_en=font_english,
            font_cn=font_chinese,
        )
        return

    summary = _summarize_compute_usage(compute_usage)
    paper = summary["paper"]
    reproduction = summary["reproduction"]
    comparison = summary["comparison"]

    paper_hours = (
        _format_hours(paper["wall_clock_hours"])
        if paper["disclosed"]
        else "论文未披露"
    )
    paper_device_hours = (
        _format_device_hours(paper["device_hours"], paper["resource_type"])
        if paper["disclosed"]
        else "论文未披露"
    )
    reason = str(comparison.get("reason") or "证据不足").strip()
    if reason.startswith("不可直接比较："):
        reason = reason[len("不可直接比较："):]
    if comparison["comparable"]:
        if comparison["wall_clock_comparable"] and not comparison["device_hours_comparable"]:
            comparability = "墙钟可直接比较；设备小时不可直接比较"
        elif comparison["device_hours_comparable"] and not comparison["wall_clock_comparable"]:
            comparability = "设备小时可直接比较；墙钟不可直接比较"
        else:
            comparability = "可直接比较"
    else:
        comparability = f"不可直接比较：{reason}"

    summary_rows = [
        ("端到端墙钟时间", _format_hours(reproduction["end_to_end_hours"])),
        ("step_7 核心算力时间", _format_hours(reproduction["step_7_hours"])),
        ("论文墙钟时间", paper_hours),
        ("本次复现 GPU 设备小时", _format_device_hours(reproduction["device_hours"].get("gpu"), "gpu")),
        ("本次复现 CPU 设备小时", _format_device_hours(reproduction["device_hours"].get("cpu"), "cpu")),
        ("论文设备小时", paper_device_hours),
        ("可比性", comparability),
    ]
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = table.rows[0].cells
    headers[0].text = "摘要指标"
    headers[1].text = "结果"
    _shade_cells(headers, "D9EAF7")
    for cell in headers:
        _set_cell_font(cell, font_english, font_chinese, 9, bold=True)
    for label, value in summary_rows:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
        for cell in cells:
            _set_cell_font(cell, font_english, font_chinese, 9)
    _set_table_borders_and_margins(table)
    _add_styled_paragraph(doc, "", space_after_pt=8)

    phases = reproduction["phases"]
    if phases:
        _add_styled_heading(
            doc,
            "阶段算力明细",
            level=2,
            font_en=font_english,
            font_cn=font_chinese,
        )
        phase_table = doc.add_table(rows=1, cols=6)
        phase_table.style = "Table Grid"
        phase_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        phase_headers = phase_table.rows[0].cells
        for index, text in enumerate((
            "阶段", "资源类型", "设备型号", "设备数量", "墙钟时间", "设备小时"
        )):
            phase_headers[index].text = text
            _set_cell_font(phase_headers[index], font_english, font_chinese, 9, bold=True)
        _shade_cells(phase_headers, "E2EFDA")
        for phase in phases:
            cells = phase_table.add_row().cells
            cells[0].text = str(phase.get("name") or "-")
            cells[1].text = str(phase["resource_type"]).upper()
            cells[2].text = str(phase.get("device_model") or "未披露")
            device_count_text = str(phase["device_count"])
            if phase["resource_type"] == "cpu" and phase.get("device_count_evidence"):
                device_count_text += f"\n分配依据：{phase['device_count_evidence']}"
            cells[3].text = device_count_text
            cells[4].text = _format_hours(phase["duration_hours"])
            cells[5].text = _format_device_hours(phase["device_hours"], phase["resource_type"])
            for cell in cells:
                _set_cell_font(cell, font_english, font_chinese, 9)
        _set_table_borders_and_margins(phase_table)
        _add_styled_paragraph(doc, "", space_after_pt=8)

    if summary["notes"]:
        _add_styled_heading(
            doc,
            "数据校验说明",
            level=2,
            font_en=font_english,
            font_cn=font_chinese,
        )
        for note in summary["notes"]:
            _add_styled_paragraph(
                doc,
                f"• {note}",
                font_en=font_english,
                font_cn=font_chinese,
                size_pt=9,
            )

    evidence = str(paper.get("evidence") or "").strip()
    source_page = str(paper.get("source_page") or "").strip()
    if evidence or source_page:
        evidence_text = evidence or "未提供原文摘录"
        if source_page:
            evidence_text = f"{evidence_text}（来源：{source_page}）"
        _add_styled_paragraph(
            doc,
            f"论文披露证据：{evidence_text}",
            font_en=font_english,
            font_cn=font_chinese,
        )

    if comparison["comparable"]:
        wall_clock_ratio = comparison.get("wall_clock_ratio")
        wall_clock_savings = comparison.get("wall_clock_savings_percent")
        if wall_clock_ratio is not None and wall_clock_savings is not None:
            _add_styled_paragraph(
                doc,
                f"墙钟时间对比：复现/论文比率 {wall_clock_ratio:.2f}，节省 {wall_clock_savings:.2f}%",
                font_en=font_english,
                font_cn=font_chinese,
            )
        device_hours_ratio = comparison.get("device_hours_ratio")
        device_hours_savings = comparison.get("device_hours_savings_percent")
        if device_hours_ratio is not None and device_hours_savings is not None:
            _add_styled_paragraph(
                doc,
                f"设备小时对比：复现/论文比率 {device_hours_ratio:.2f}，节省 {device_hours_savings:.2f}%",
                font_en=font_english,
                font_cn=font_chinese,
            )
    else:
        _add_styled_paragraph(
            doc,
            comparability,
            font_en=font_english,
            font_cn=font_chinese,
        )

    _add_styled_paragraph(
        doc,
        "注：设备小时表示分配设备数 × 墙钟时间，不是利用率加权或功耗积分的消耗量。",
        font_en=font_english,
        font_cn=font_chinese,
        size_pt=9,
    )


# ==========================================
# 📊 核心 Word 报告生成器 (DOCX Only)
# ==========================================
def _load_json_artifact(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            with open(value, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}
    return {}


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg"}
_TABLE_EXTENSIONS = {".csv", ".tsv", ".json"}
_INPUT_ROLES = {
    "field_data", "mesh", "model_weight", "normalizer",
    "solver_case_configuration", "ground_truth", "dataset",
}
_OUTPUT_ROLES = {
    "prediction", "validation_plot", "result", "output", "metric_table",
}
_BODY_IMAGE_LIMIT = 8
_TABLE_PREVIEW_ROWS = 20
_TABLE_PREVIEW_COLUMNS = 10


def _load_json_value_with_base(value, fallback_base):
    """Load a JSON-backed value and retain the directory used for relative paths."""
    fallback = Path(fallback_base or os.getcwd()).resolve()
    if isinstance(value, (dict, list)):
        return value, fallback
    if isinstance(value, str) and value.strip():
        path = Path(value).expanduser()
        try:
            resolved = path.resolve()
            with resolved.open("r", encoding="utf-8") as handle:
                return json.load(handle), resolved.parent
        except (OSError, ValueError, TypeError):
            return None, fallback
    return None, fallback


_HISTORY_TERMINAL_STATUSES = {"passed", "partial", "failed", "skipped"}


def _load_execution_run_history(value, fallback_base):
    """Load and strictly validate the required report-authorized run ledger."""
    if value in (None, "", [], {}):
        raise ValueError(
            "execution_run_history 是生成最终 DOCX 的必填授权台账；"
            "请先完成 finish-run、展示三项选择并保存用户明确授权"
        )
    history, base_dir = _load_json_value_with_base(value, fallback_base)
    if not isinstance(history, dict):
        raise ValueError("execution_run_history 无法读取或根节点不是对象")
    if (
        str(history.get("schema_version") or "") != "2.0"
        or history.get("layout_version") != 3
    ):
        raise ValueError(f"execution_run_history schema_version 不受支持：{history.get('schema_version')!r}")
    history_script = (
        Path(__file__).resolve().parents[2]
        / "ar24-auto-reproduct"
        / "scripts"
        / "execution_history.py"
    )
    if not history_script.is_file():
        raise ValueError(f"缺少执行台账校验器：{history_script}")
    spec = importlib.util.spec_from_file_location("ar24_report_history_validator", history_script)
    history_validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history_validator)
    completeness_errors = history_validator.validate_history(
        history,
        for_report=True,
        artifact_base_dir=base_dir,
    )
    if completeness_errors:
        raise ValueError("；".join(completeness_errors))
    runs = history.get("runs")
    if not isinstance(runs, list):
        raise ValueError("execution_run_history.runs 必须是数组")
    seen_ids = set()
    seen_sequences = set()
    running = []
    recorded_sequences = []
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"execution_run_history.runs[{index}] 必须是对象")
        run_id = str(run.get("run_id") or "").strip()
        sequence = run.get("sequence")
        status = str(run.get("status") or "").strip().casefold()
        if not run_id or run_id in seen_ids:
            raise ValueError(f"execution_run_history 存在缺失或重复 run_id：{run_id or index}")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0 or sequence in seen_sequences:
            raise ValueError(f"execution_run_history 存在无效或重复 sequence：{sequence!r}")
        summary = (
            run.get("terminal_summary")
            if isinstance(run.get("terminal_summary"), dict)
            else {}
        )
        early_terminal = str(summary.get("stage") or "") in {
            "step_precheck",
            "step_0",
            "step_1",
            "step_2",
            "step_2_5",
        }
        if (
            status != "reserved"
            and not early_terminal
            and not isinstance(run.get("selected_execution_plan"), (dict, list, str))
        ):
            raise ValueError(f"{run_id} 缺少 selected_execution_plan")
        if status not in _HISTORY_TERMINAL_STATUSES | {"reserved", "planned", "running"}:
            raise ValueError(f"{run_id} 存在无效状态：{status or '缺失'}")
        seen_ids.add(run_id)
        seen_sequences.add(sequence)
        recorded_sequences.append(sequence)
        if status == "running":
            running.append(run_id)
    if recorded_sequences != sorted(recorded_sequences):
        raise ValueError("execution_run_history.runs 必须按 sequence 升序保存")
    if running:
        raise ValueError("存在未解释的 running 记录，必须先恢复或标记失败：" + ", ".join(running))
    gate = history.get("report_gate")
    if not isinstance(gate, dict) or gate.get("status") != "confirmed" or gate.get("decision") != "generate_report":
        raise ValueError("用户尚未在 step_7.5 明确确认生成 DOCX")
    normalized_user_response = "".join(
        unicodedata.normalize(
            "NFKC",
            str(gate.get("user_response") or ""),
        ).strip().casefold().split()
    ).rstrip(".。!")
    explicit_docx_responses = {
        "1",
        "1直接生成最终docx",
        "直接生成最终docx",
        "生成最终docx",
        "生成docx",
        "输出docx",
    }
    if (
        gate.get("decision_source") != "explicit_user_reply"
        or not str(gate.get("confirmation_id") or "").strip()
        or not str(gate.get("prompt_displayed_at") or "").strip()
        or normalized_user_response not in explicit_docx_responses
    ):
        raise ValueError(
            "step_7.5 报告授权证据无效：必须先展示三项选择，并保存用户明确选择最终 DOCX 的原始回复"
        )
    report_runs = [
        run for run in sorted(runs, key=lambda item: item["sequence"])
        if str(run.get("status") or "").strip().casefold() in _HISTORY_TERMINAL_STATUSES
    ]
    if not report_runs:
        raise ValueError("execution_run_history 中没有已实际执行的终态记录")
    project_name = str(history.get("project_name") or "").strip()
    if not project_name or project_name in {".", ".."} or Path(project_name).name != project_name:
        raise ValueError("execution_run_history.project_name 无效，无法定位规范审计报告")
    audit_report_path = history_validator._canonical_audit_report_path(
        history,
        base_dir,
    )
    if not audit_report_path.is_file():
        raise ValueError(f"最终产物缺少规范审计报告：{audit_report_path}")
    try:
        audit_report_text = audit_report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"规范审计报告不可读：{audit_report_path}（{exc}）") from exc
    if not audit_report_text.strip():
        raise ValueError(f"规范审计报告为空：{audit_report_path}")
    audit_score_path = audit_report_path.parent / "audit_score.json"
    if not audit_score_path.is_file():
        raise ValueError(f"规范审计报告缺少配套机器评分：{audit_score_path}")
    try:
        audit_score = json.loads(audit_score_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"配套机器评分不可读或不是有效 JSON：{audit_score_path}（{exc}）"
        ) from exc
    required_audit_dimensions = {
        "documentation_entry",
        "environment",
        "artifacts",
        "migration_cost",
        "experiment_reproduction",
        "physics_fidelity",
        "risk_gate",
    }
    resource_gate = audit_score.get("resource_gate") if isinstance(audit_score, dict) else None
    physics_gate = audit_score.get("physics_gate") if isinstance(audit_score, dict) else None
    overall_score = audit_score.get("overall_score") if isinstance(audit_score, dict) else None
    if (
        not isinstance(audit_score, dict)
        or audit_score.get("scoring_profile") != "sciml_physics"
        or not isinstance(overall_score, (int, float))
        or isinstance(overall_score, bool)
        or not 0 <= overall_score <= 100
        or not str(audit_score.get("verdict") or "").strip()
        or not required_audit_dimensions.issubset(audit_score.get("dimensions") or {})
        or not isinstance(resource_gate, dict)
        or not {"fit_status", "action", "recommended_resource", "message"}.issubset(resource_gate)
        or not isinstance(physics_gate, dict)
        or not {"action", "execution_blockers", "claim_blockers", "message"}.issubset(physics_gate)
    ):
        raise ValueError(
            f"配套机器评分缺少 SciML/计算物理 v4 必需字段：{audit_score_path}"
        )
    for name, dimension in audit_score["dimensions"].items():
        if not isinstance(dimension, dict):
            raise ValueError(f"机器评分维度 {name} 不是对象：{audit_score_path}")
        score = dimension.get("score", dimension.get("earned"))
        maximum = dimension.get("max", dimension.get("max_score"))
        if (
            isinstance(score, (int, float))
            and isinstance(maximum, (int, float))
            and score < maximum
            and not (
                dimension.get("deductions")
                or dimension.get("evidence")
                or dimension.get("assessment")
                or dimension.get("message")
            )
        ):
            raise ValueError(f"机器评分维度 {name} 存在扣分但缺少证据：{audit_score_path}")
    normalized = dict(history)
    normalized["_report_runs"] = report_runs
    normalized["_audit_report_path"] = str(audit_report_path)
    normalized["_audit_report_text"] = audit_report_text
    normalized["_audit_score_path"] = str(audit_score_path)
    normalized["_audit_score"] = audit_score
    return normalized, base_dir


def _add_audit_report_appendix(
    doc,
    audit_report_text,
    audit_report_path,
    font_english,
    font_chinese,
):
    """Embed the canonical Markdown audit report as a fixed DOCX appendix."""
    doc.add_page_break()
    _add_styled_heading(
        doc,
        "附录 C、完整项目审计报告 (Complete Project Audit Report)",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_styled_paragraph(
        doc,
        f"审计报告来源：{audit_report_path}",
        font_en=font_english,
        font_cn=font_chinese,
        size_pt=9,
    )
    for raw_line in str(audit_report_text or "").splitlines():
        line = raw_line.rstrip()
        if not line:
            _add_styled_paragraph(doc, "", space_after_pt=3)
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            _add_styled_heading(
                doc,
                heading_match.group(2).strip(),
                level=min(len(heading_match.group(1)) + 1, 3),
                font_en=font_english,
                font_cn=font_chinese,
            )
            continue
        bullet_match = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if bullet_match:
            _add_list_item(
                doc,
                bullet_match.group(1).strip(),
                font_english,
                font_chinese,
            )
            continue
        _add_styled_paragraph(
            doc,
            line,
            font_en=font_english,
            font_cn=font_chinese,
            size_pt=9.5,
        )


def _history_runs(history):
    if not isinstance(history, dict):
        return []
    return history.get("_report_runs") or []


def _history_reference(value, base_dir):
    """Resolve a per-run relative JSON/evidence path against the ledger directory."""
    if not isinstance(value, str) or not value.strip():
        return value
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    return str((Path(base_dir) / candidate).resolve())


def _run_plan_selection(run):
    return {"selected_execution_plan": run.get("selected_execution_plan")}


def _run_plan_name(run):
    plan = run.get("selected_execution_plan")
    if isinstance(plan, dict):
        for key in ("name", "plan_name", "title", "label", "id"):
            if plan.get(key):
                return str(plan[key])
    if isinstance(plan, str) and plan.strip():
        return plan.strip()
    return "未命名计划"


def _run_title(run):
    sequence = run.get("sequence") or "-"
    option_id = str(run.get("selected_option_id") or "自定义").strip()
    return f"第 {sequence} 次执行｜计划 {option_id}｜{_run_plan_name(run)}"


def _reuse_summary(run):
    decisions = run.get("reuse_decisions")
    if not isinstance(decisions, dict) or not decisions:
        return "未提供"
    parts = []
    for step in ("step_3", "step_4", "step_5", "step_6", "step_7"):
        item = decisions.get(step)
        if not isinstance(item, dict):
            continue
        parts.append(f"{step}：{'复用' if item.get('reused') else '重新执行'}")
    return "；".join(parts) or "未提供"


def _history_rows_for_run(run, row_type):
    if row_type == "outcome":
        rows = run.get("actual_test_outcomes")
        if not isinstance(rows, list):
            rows = []
    else:
        rows = run.get("readiness_matrix")
        if not isinstance(rows, list):
            rows = []
    aligned = _align_rows_to_execution_plan(_run_plan_selection(run), rows, row_type)
    if aligned:
        if not rows:
            for item in aligned:
                if row_type == "readiness" and item.get("status") == "-":
                    item["status"] = run.get("status") or "未提供"
                elif row_type == "outcome" and item.get("status") == "未执行/待执行":
                    item["status"] = run.get("status") or "未提供"
                    if item.get("output_detail") == "-":
                        item["output_detail"] = "该轮在准备或执行阶段结束，未提供逐案例结果"
                    if item.get("actual_time") == "-":
                        item["actual_time"] = _run_elapsed_text(run)
        return aligned
    plan_name = _run_plan_name(run)
    if row_type == "readiness":
        return [{
            "case_name": plan_name,
            "config_path": "-",
            "status": run.get("status") or "-",
            "barrier": "-",
            "gpu_spec": "-",
            "est_time": "-",
        }]
    return [{
        "case_name": plan_name,
        "status": run.get("status") or "未提供",
        "output_detail": "该轮未提供逐案例结果",
        "actual_time": _run_elapsed_text(run),
    }]


def _filter_history_conclusions(history, rows):
    if not isinstance(rows, list):
        return rows
    run_names = {}
    for run in _history_runs(history):
        names = {
            _normalize_name(_case_label(item))
            for item in _extract_execution_plan_items(_run_plan_selection(run))
        }
        run_names[str(run.get("run_id"))] = names
    filtered = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        case_name = _case_label(row)
        normalized = _normalize_name(case_name)
        run_id = str(row.get("run_id") or "").strip()
        if run_id:
            if run_id in run_names and (not normalized or normalized in run_names[run_id]):
                item = dict(row)
                item["case_name"] = f"{run_id}｜{case_name or _run_plan_name(next(run for run in _history_runs(history) if str(run.get('run_id')) == run_id))}"
                filtered.append(item)
            continue
        matches = [candidate for candidate, names in run_names.items() if normalized and normalized in names]
        if len(matches) == 1:
            item = dict(row)
            item["case_name"] = f"{matches[0]}｜{case_name}"
            filtered.append(item)
        elif len(matches) > 1:
            item = dict(row)
            item["case_name"] = f"公共/未归属结论｜{case_name}"
            filtered.append(item)
    return filtered


def _run_elapsed_text(run):
    compute_usage = run.get("compute_usage")
    if isinstance(compute_usage, dict):
        reproduction = compute_usage.get("reproduction")
        if isinstance(reproduction, dict):
            hours = _nonnegative_number(reproduction.get("end_to_end_hours"))
            if hours is not None:
                return _format_hours(hours)
    started = str(run.get("started_at") or "").strip()
    ended = str(run.get("ended_at") or "").strip()
    if started and ended:
        try:
            duration = datetime.fromisoformat(ended) - datetime.fromisoformat(started)
            return _format_hours(max(duration.total_seconds(), 0) / 3600)
        except (TypeError, ValueError):
            pass
    return "未提供"


def _normalized_experiment_items(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("experiments", "experiment_details", "items"):
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        if any(value.get(key) for key in ("case_name", "name", "task_name", "id")):
            return [value]
    return []


def _artifact_case_name(item):
    if not isinstance(item, dict):
        return ""
    for key in ("case_name", "experiment_name", "experiment_id", "task_name", "case"):
        if item.get(key):
            return str(item[key])
    metadata = item.get("physical_metadata")
    if isinstance(metadata, dict):
        for key in ("case_name", "experiment_name", "experiment_id", "task_name", "case"):
            if metadata.get(key):
                return str(metadata[key])
    return ""


def _artifact_items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("artifacts", "items", "content"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    if value:
        return [value]
    return []


def _artifact_extension(item):
    path = str(item.get("local_path") or item.get("path") or item.get("file") or "")
    return Path(urlparse(path).path).suffix.casefold()


def _normalize_artifact(item, base_dir, default_origin=None, featured=False):
    if isinstance(item, str):
        item = {"path": item}
    if not isinstance(item, dict):
        return None
    artifact = dict(item)
    raw_path = artifact.get("path") or artifact.get("file") or artifact.get("local_path") or ""
    artifact["path"] = str(raw_path)
    artifact["local_path"] = str(artifact.get("local_path") or raw_path)
    artifact["name"] = str(
        artifact.get("name")
        or artifact.get("title")
        or (Path(urlparse(str(raw_path)).path).name if raw_path else "内嵌产物")
    )
    artifact["role"] = str(
        artifact.get("role") or artifact.get("artifact_type") or artifact.get("type") or "supporting_asset"
    )
    artifact["origin"] = str(artifact.get("origin") or default_origin or "未提供")
    artifact["featured"] = bool(
        artifact.get("featured") or artifact.get("is_key_output") or artifact.get("include_in_report") or featured
    )
    artifact["_base_dir"] = str(Path(base_dir).resolve())
    parsed_path = urlparse(str(artifact["local_path"]))
    if parsed_path.scheme not in {"http", "https", "s3", "gs"} and artifact["local_path"]:
        local_path = Path(artifact["local_path"]).expanduser()
        if not local_path.is_absolute():
            local_path = Path(base_dir) / local_path
        try:
            local_path = local_path.resolve()
            if local_path.is_file():
                artifact.setdefault("size_bytes", local_path.stat().st_size)
                artifact.setdefault("sha256", _file_sha256(local_path))
        except OSError:
            pass
    return artifact


def _normalize_artifacts(value, base_dir, default_origin=None, featured=False):
    result = []
    for item in _artifact_items(value):
        artifact = _normalize_artifact(item, base_dir, default_origin, featured)
        if artifact:
            result.append(artifact)
    return result


def _resolved_artifact_path(artifact):
    raw = str(artifact.get("local_path") or artifact.get("path") or "").strip()
    if not raw or urlparse(raw).scheme in {"http", "https", "s3", "gs"}:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(artifact.get("_base_dir") or os.getcwd()) / path
    try:
        return path.resolve()
    except OSError:
        return path


def _artifact_identity(artifact):
    checksum = str(artifact.get("sha256") or "").strip().casefold()
    if checksum:
        return f"sha256:{checksum}"
    resolved = _resolved_artifact_path(artifact)
    if resolved:
        return f"path:{str(resolved).casefold()}"
    path = str(artifact.get("path") or "").strip().casefold()
    if path:
        return f"display:{path}"
    inline = {
        key: artifact.get(key)
        for key in ("name", "role", "headers", "rows", "data")
        if artifact.get(key) is not None
    }
    return f"inline:{json.dumps(inline, ensure_ascii=False, sort_keys=True, default=str)}"


def _deduplicate_artifacts(artifacts):
    merged = {}
    order = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        key = _artifact_identity(artifact)
        if key not in merged:
            merged[key] = dict(artifact)
            order.append(key)
            continue
        for field, value in artifact.items():
            if value not in (None, "", [], {}):
                merged[key][field] = value
    return [merged[key] for key in order]


def _normalize_sections(experiment, base_dir):
    sections = experiment.get("sections") if isinstance(experiment, dict) else None
    normalized = []
    if isinstance(sections, list):
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            kind = str(section.get("kind") or section.get("type") or "text").strip().casefold()
            title = str(section.get("title") or section.get("name") or f"补充内容 {index + 1}")
            content = section.get("content")
            if content is None:
                content = section.get("items") if "items" in section else section.get("data")
            entry = {"title": title, "kind": kind, "content": content}
            if kind == "artifacts":
                entry["content"] = _normalize_artifacts(
                    content, base_dir, default_origin="experiment_details", featured=True
                )
            normalized.append(entry)
    aliases = (
        ("parameters", "仿真参数", "key_value"),
        ("simulation_parameters", "仿真参数", "key_value"),
        ("data_flow", "数据流", "flow"),
        ("artifacts", "实验产物", "artifacts"),
    )
    existing_titles = {_normalize_name(section["title"]) for section in normalized}
    for key, title, kind in aliases:
        if not isinstance(experiment, dict) or experiment.get(key) in (None, "", [], {}):
            continue
        if _normalize_name(title) in existing_titles:
            continue
        content = experiment[key]
        if kind == "artifacts":
            content = _normalize_artifacts(content, base_dir, default_origin="experiment_details", featured=True)
        normalized.append({"title": title, "kind": kind, "content": content})
        existing_titles.add(_normalize_name(title))
    return normalized


def _merge_sections(auto_sections, explicit_sections):
    """Explicit same-title sections replace inferred content; artifact lists are merged."""
    merged = [dict(section) for section in auto_sections]
    positions = {_normalize_name(section.get("title")): index for index, section in enumerate(merged)}
    for explicit in explicit_sections:
        key = _normalize_name(explicit.get("title"))
        if key in positions:
            index = positions[key]
            if explicit.get("kind") == "artifacts" and merged[index].get("kind") == "artifacts":
                merged[index] = dict(explicit)
                merged[index]["content"] = _deduplicate_artifacts(
                    _artifact_items(auto_sections[index].get("content"))
                    + _artifact_items(explicit.get("content"))
                )
            else:
                merged[index] = dict(explicit)
        else:
            positions[key] = len(merged)
            merged.append(dict(explicit))
    return [section for section in merged if section.get("content") not in (None, "", [], {})]


def _collect_mapping(source, keys):
    result = {}
    if not isinstance(source, dict):
        return result
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            if key in {"scope_detail", "config", "configuration"}:
                result[key] = value
            else:
                result.update(value)
        elif value not in (None, "", [], {}):
            result[key] = value
    return result


def _flatten_key_values(value, prefix=""):
    rows = []
    if isinstance(value, dict):
        for key, item in value.items():
            label = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, dict):
                if item:
                    rows.extend(_flatten_key_values(item, label))
                else:
                    rows.append((label, "未提供"))
            elif isinstance(item, (list, tuple, set)):
                rows.append((label, _stringify(item) or "未提供"))
            else:
                rows.append((label, "未提供" if item in (None, "") else str(item)))
    elif value not in (None, ""):
        rows.append((prefix or "参数", str(value)))
    return rows


def _validation_metric_rows(validation):
    if not isinstance(validation, dict):
        return []
    phases = validation.get("phases") or {}
    rows = []
    for metric in (phases.get("quantitative_validation") or {}).get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        tolerance = metric.get("tolerance")
        rows.append({
            "指标/检查": metric.get("metric") or metric.get("name") or "未命名指标",
            "参考值": metric.get("reference_value", "未提供"),
            "实测值": metric.get("measured_value", "未提供"),
            "容差": _stringify(tolerance) or "未提供",
            "状态": metric.get("status", "未提供"),
            "证据": metric.get("evidence") or metric.get("data_path") or "未提供",
            "_case_name": _artifact_case_name(metric),
        })
    for check in (phases.get("physics_validation") or {}).get("checks") or []:
        if not isinstance(check, dict):
            continue
        rows.append({
            "指标/检查": check.get("name") or "未命名物理检查",
            "参考值": check.get("reference_value", "未提供"),
            "实测值": check.get("measured_value", "未提供"),
            "容差": check.get("tolerance", "未提供"),
            "状态": check.get("status", "未提供"),
            "证据": check.get("evidence") or check.get("data_path") or "未提供",
            "_case_name": _artifact_case_name(check),
        })
    return rows


def _scientific_parameter_evidence(contract, compute_usage, validation):
    result = {}
    reproduction = (compute_usage or {}).get("reproduction") if isinstance(compute_usage, dict) else {}
    if isinstance(reproduction, dict) and isinstance(reproduction.get("scope_detail"), dict):
        result["scope_detail"] = reproduction["scope_detail"]
    physics_spec = (contract or {}).get("physics_spec") if isinstance(contract, dict) else {}
    if isinstance(physics_spec, dict):
        disclosed = {}
        for name, item in physics_spec.items():
            if isinstance(item, dict) and item.get("status") == "disclosed":
                disclosed[name] = item.get("values") or "已披露（值未结构化）"
        if disclosed:
            result["physics_spec"] = disclosed
    alignment = ((validation or {}).get("phases") or {}).get("field_alignment") if isinstance(validation, dict) else {}
    if isinstance(alignment, dict):
        reference = alignment.get("reference_condition")
        prediction = alignment.get("prediction_condition")
        if isinstance(reference, dict) and reference:
            result["reference_condition"] = reference
        if isinstance(prediction, dict) and prediction:
            result["prediction_condition"] = prediction
    return result


def _first_recorded(source, keys, default=None):
    if not isinstance(source, dict):
        return default
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _auto_flow_rows(record, artifacts):
    rows = []
    inputs = [artifact for artifact in artifacts if str(artifact.get("role", "")).casefold() in _INPUT_ROLES]
    outputs = [artifact for artifact in artifacts if str(artifact.get("role", "")).casefold() in _OUTPUT_ROLES]
    if inputs:
        rows.append({
            "stage": "输入资产",
            "input": "\n".join(artifact.get("path") or artifact.get("name") for artifact in inputs[:8]),
            "operation": "按资产清单加载数据、网格、权重或配置",
            "output": "执行阶段输入",
        })
    command = _first_recorded(record.get("_outcome"), ("command", "commands", "executed_command", "run_command"))
    if command is None:
        command = _first_recorded(record.get("_plan_item"), ("command", "commands", "entrypoint", "run_command"))
    if command is not None:
        rows.append({
            "stage": "实验执行",
            "input": "已准备的代码与资产",
            "operation": _stringify(command),
            "output": record.get("output_detail") or "运行输出未结构化记录",
        })
    if outputs:
        rows.append({
            "stage": "验证与产物生成",
            "input": "参考数据与本次预测/仿真结果",
            "operation": "字段对齐、定量指标及适用物理检查",
            "output": "\n".join(artifact.get("path") or artifact.get("name") for artifact in outputs[:8]),
        })
    elif record.get("output_detail"):
        rows.append({
            "stage": "结果输出",
            "input": "实验执行结果",
            "operation": "记录实际输出与状态",
            "output": record["output_detail"],
        })
    return rows


def _metric_table_section(rows):
    cleaned = []
    for row in rows:
        cleaned.append({key: value for key, value in row.items() if not key.startswith("_")})
    return {
        "title": "定量与物理验证明细",
        "kind": "table",
        "content": {"rows": cleaned},
    }


def _assign_evidence(items, records, shared, case_getter, target_key):
    """Assign labeled evidence exactly; keep unlabeled evidence shared for multi-case plans."""
    record_index = {_normalize_name(record["case_name"]): record for record in records}
    for item in items:
        label = case_getter(item)
        normalized = _normalize_name(label)
        if normalized:
            record = record_index.get(normalized)
            if record is not None:
                record[target_key].append(item)
            # A label for an unselected candidate is intentionally excluded.
        elif len(records) == 1:
            records[0][target_key].append(item)
        else:
            shared[target_key].append(item)


def _build_experiment_records(
    execution_plan_selection,
    actual_test_outcomes,
    experiment_details,
    compute_usage,
    validation_result,
    scientific_repro_contract,
    artifact_manifest,
    output_dir,
):
    details_value, details_base = _load_json_value_with_base(experiment_details, output_dir)
    explicit_items = _normalized_experiment_items(details_value)
    explicit_index = {_normalize_name(_case_label(item)): item for item in explicit_items}
    outcomes = [item for item in _as_list(actual_test_outcomes) if isinstance(item, dict)]
    outcome_index = _index_by_case_name(outcomes)
    plan_items = _extract_execution_plan_items(execution_plan_selection)
    if plan_items:
        anchors = plan_items
    elif explicit_items:
        anchors = explicit_items
    else:
        anchors = outcomes

    validation, validation_base = _load_json_value_with_base(validation_result, output_dir)
    contract, _ = _load_json_value_with_base(scientific_repro_contract, output_dir)
    manifest, manifest_base = _load_json_value_with_base(artifact_manifest, output_dir)
    usage, _ = _load_json_value_with_base(compute_usage, output_dir)
    usage = usage if isinstance(usage, dict) else (compute_usage if isinstance(compute_usage, dict) else {})
    validation = validation if isinstance(validation, dict) else {}
    contract = contract if isinstance(contract, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}

    has_global_evidence = bool(validation or contract or manifest or usage)
    if not anchors and has_global_evidence:
        anchors = [{"name": "本次复现实验"}]

    records = []
    for anchor in anchors:
        if not isinstance(anchor, dict):
            anchor = {"name": str(anchor)}
        case_name = _case_label(anchor)
        normalized = _normalize_name(case_name)
        outcome = outcome_index.get(normalized, {})
        explicit = explicit_index.get(normalized, {})
        record = {
            "case_name": case_name,
            "status": _first_recorded(explicit, ("status",))
            or _first_recorded(outcome, ("status",))
            or _first_recorded(anchor, ("run_status", "status"), "未执行/待执行"),
            "actual_time": _first_recorded(explicit, ("actual_time", "duration"))
            or _first_recorded(outcome, ("actual_time", "duration"))
            or "未提供",
            "output_detail": _first_recorded(explicit, ("output_detail", "summary"))
            or _first_recorded(outcome, ("output_detail", "summary"))
            or _first_recorded(anchor, ("expected_output", "success_criteria"))
            or "未提供",
            "sections": [],
            "_artifacts": [],
            "_metrics": [],
            "_plan_item": anchor,
            "_outcome": outcome,
            "_explicit": explicit,
        }
        for source, origin, base in (
            (anchor, "execution_plan", output_dir),
            (outcome, "actual_test_outcomes", output_dir),
        ):
            for key in ("artifacts", "output_artifacts", "output_paths", "files"):
                if isinstance(source, dict) and source.get(key) not in (None, "", [], {}):
                    record["_artifacts"].extend(_normalize_artifacts(source[key], base, origin))
        records.append(record)

    shared = {"_artifacts": [], "_metrics": [], "parameters": {}}
    manifest_artifacts = _normalize_artifacts(
        manifest.get("artifacts") or [], manifest_base, default_origin="artifact_manifest"
    )
    validation_artifacts = _normalize_artifacts(
        (((validation.get("phases") or {}).get("validation_artifacts") or {}).get("artifacts") or []),
        validation_base,
        default_origin="validation_result",
    )
    _assign_evidence(
        manifest_artifacts + validation_artifacts,
        records,
        shared,
        _artifact_case_name,
        "_artifacts",
    )
    metric_rows = _validation_metric_rows(validation)
    _assign_evidence(
        metric_rows,
        records,
        shared,
        lambda item: item.get("_case_name", ""),
        "_metrics",
    )

    global_parameters = _scientific_parameter_evidence(contract, usage, validation)
    if len(records) == 1:
        records[0]["_global_parameters"] = global_parameters
    elif global_parameters:
        shared["parameters"] = global_parameters

    for record in records:
        explicit_sections = _normalize_sections(record["_explicit"], details_base)
        for section in explicit_sections:
            if section.get("kind") == "artifacts":
                record["_artifacts"].extend(_artifact_items(section.get("content")))
        parameters = {}
        parameters.update(_collect_mapping(record["_plan_item"], (
            "parameters", "simulation_parameters", "hyperparameters", "scope_detail", "config", "configuration"
        )))
        parameters.update(_collect_mapping(record["_outcome"], (
            "parameters", "simulation_parameters", "hyperparameters", "scope_detail", "config", "configuration"
        )))
        parameters.update(record.get("_global_parameters") or {})
        record["_artifacts"] = _deduplicate_artifacts(record["_artifacts"])
        auto_sections = []
        if parameters:
            auto_sections.append({"title": "仿真参数", "kind": "key_value", "content": parameters})
        flow_rows = _auto_flow_rows(record, record["_artifacts"])
        if flow_rows:
            auto_sections.append({"title": "数据流", "kind": "flow", "content": flow_rows})
        if record["_metrics"]:
            auto_sections.append(_metric_table_section(record["_metrics"]))
        if record["_artifacts"]:
            auto_sections.append({"title": "实验产物", "kind": "artifacts", "content": record["_artifacts"]})
        record["sections"] = _merge_sections(auto_sections, explicit_sections)

    shared_sections = []
    assigned_artifact_ids = {
        _artifact_identity(artifact)
        for record in records
        for artifact in record["_artifacts"]
    }
    shared["_artifacts"] = [
        artifact
        for artifact in shared["_artifacts"]
        if _artifact_identity(artifact) not in assigned_artifact_ids
    ]
    shared["_artifacts"] = _deduplicate_artifacts(shared["_artifacts"])
    if shared["parameters"]:
        shared_sections.append({"title": "公共执行参数与验证条件", "kind": "key_value", "content": shared["parameters"]})
    if shared["_metrics"]:
        shared_sections.append(_metric_table_section(shared["_metrics"]))
    if shared["_artifacts"]:
        shared_sections.append({"title": "公共/未归属产物", "kind": "artifacts", "content": shared["_artifacts"]})
    return records, shared_sections


def _table_data(content):
    headers = []
    rows = []
    if isinstance(content, dict):
        raw_headers = content.get("headers") or content.get("columns")
        raw_rows = content.get("rows") if "rows" in content else content.get("data")
        headers = [str(item) for item in raw_headers] if isinstance(raw_headers, list) else []
    else:
        raw_rows = content
    if isinstance(raw_rows, list) and raw_rows:
        if all(isinstance(item, dict) for item in raw_rows):
            if not headers:
                for item in raw_rows:
                    for key in item:
                        if not str(key).startswith("_") and str(key) not in headers:
                            headers.append(str(key))
            rows = [[item.get(key, "未提供") for key in headers] for item in raw_rows]
        elif all(isinstance(item, (list, tuple)) for item in raw_rows):
            rows = [list(item) for item in raw_rows]
            if not headers and rows:
                headers = [f"列 {index + 1}" for index in range(max(len(row) for row in rows))]
    return headers, rows


_SPARSE_TABLE_MISSING_THRESHOLD = 0.5
_MISSING_TABLE_MARKERS = {
    "",
    "-",
    "—",
    "未提供",
    "未记录",
    "not_recorded",
    "n/a",
    "na",
    "null",
    "none",
    "unknown",
    "未知",
}


def _is_missing_table_value(value):
    """Return whether a value is a generic placeholder rather than evidence."""
    if value is None:
        return True
    if isinstance(value, (dict, list, tuple, set)):
        return not value
    if isinstance(value, float) and math.isnan(value):
        return True
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    if normalized in _MISSING_TABLE_MARKERS:
        return True
    return (
        normalized.startswith("未提供")
        or normalized.startswith("未记录")
        or normalized.startswith("not_recorded")
    )


def _sparse_table_stats(rows, value_columns=None):
    """Count missing body cells, optionally excluding label columns."""
    rows = list(rows or [])
    if value_columns is None:
        column_count = max((len(row) for row in rows), default=0)
        value_columns = tuple(range(column_count))
    else:
        value_columns = tuple(value_columns)
    total = len(rows) * len(value_columns)
    missing = 0
    for row in rows:
        row = list(row) if isinstance(row, (list, tuple)) else [row]
        for index in value_columns:
            value = row[index] if index < len(row) else None
            if _is_missing_table_value(value):
                missing += 1
    ratio = (missing / total) if total else 1.0
    return {"missing": missing, "total": total, "ratio": ratio}


def _should_omit_sparse_table(rows, value_columns=None):
    stats = _sparse_table_stats(rows, value_columns=value_columns)
    return stats["total"] == 0 or stats["ratio"] > _SPARSE_TABLE_MISSING_THRESHOLD, stats


def _add_sparse_table_note(doc, stats, font_english, font_chinese):
    return _add_styled_paragraph(
        doc,
        (
            "有效信息不足，已省略该表格"
            f"（缺失 {stats['missing']}/{stats['total']}，比例 {stats['ratio']:.1%}）。"
        ),
        font_en=font_english,
        font_cn=font_chinese,
        size_pt=9,
        color=REPORT_GRAY,
    )


def _artifact_table_preview(artifact):
    inline_headers, inline_rows = _table_data(artifact)
    if inline_headers or inline_rows:
        return inline_headers, inline_rows, False, None
    path = _resolved_artifact_path(artifact)
    if not path or not path.is_file():
        return [], [], False, "文件不存在或不可读取"
    extension = path.suffix.casefold()
    try:
        if extension in {".csv", ".tsv"}:
            delimiter = "\t" if extension == ".tsv" else ","
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter)
                loaded = []
                for index, row in enumerate(reader):
                    loaded.append(row)
                    if index >= _TABLE_PREVIEW_ROWS + 1:
                        break
            if not loaded:
                return [], [], False, "表格为空"
            headers = [str(item) for item in loaded[0]]
            rows = loaded[1:_TABLE_PREVIEW_ROWS + 1]
            truncated = len(loaded) > _TABLE_PREVIEW_ROWS + 1 or len(headers) > _TABLE_PREVIEW_COLUMNS
            return headers, rows, truncated, None
        if extension == ".json":
            if path.stat().st_size > 5 * 1024 * 1024:
                return [], [], False, "JSON 超过 5 MB，仅保留在完整产物清单"
            value = json.loads(path.read_text(encoding="utf-8"))
            headers, rows = _table_data(value)
            truncated = len(rows) > _TABLE_PREVIEW_ROWS or len(headers) > _TABLE_PREVIEW_COLUMNS
            return headers, rows, truncated, None
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        return [], [], False, f"表格预览失败：{exc}"
    return [], [], False, "格式不支持表格预览"


def _add_generic_table(doc, headers, rows, font_english, font_chinese, max_rows=_TABLE_PREVIEW_ROWS, max_columns=_TABLE_PREVIEW_COLUMNS):
    headers = [str(item) for item in headers[:max_columns]]
    if not headers:
        _add_sparse_table_note(
            doc,
            _sparse_table_stats([]),
            font_english,
            font_chinese,
        )
        return None
    source_rows = [list(row)[:len(headers)] for row in rows]
    omit, stats = _should_omit_sparse_table(
        source_rows,
        value_columns=range(len(headers)),
    )
    if omit:
        _add_sparse_table_note(doc, stats, font_english, font_chinese)
        return None
    rows = source_rows[:max_rows]
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    header_cells = table.rows[0].cells
    for index, header in enumerate(headers):
        header_cells[index].text = header
        _set_cell_font(header_cells[index], font_english, font_chinese, 8.5 if len(headers) <= 6 else 7.5, bold=True)
    _shade_cells(header_cells, "EAF2F8")
    for raw_row in rows:
        cells = table.add_row().cells
        for index in range(len(headers)):
            value = raw_row[index] if index < len(raw_row) else "未提供"
            cells[index].text = "未提供" if value in (None, "") else str(value)
            _set_cell_font(cells[index], font_english, font_chinese, 8.5 if len(headers) <= 6 else 7.5)
    total_width = Inches(6.45)
    widths = [Emu(int(total_width / len(headers))) for _ in headers]
    _set_table_widths(table, widths)
    _set_table_borders_and_margins(table)
    return table


def _add_key_value_table(doc, content, font_english, font_chinese):
    rows = _flatten_key_values(content)
    omit, stats = _should_omit_sparse_table(rows, value_columns=(1,))
    if omit:
        _add_sparse_table_note(doc, stats, font_english, font_chinese)
        return None
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.rows[0].cells[0].text = "参数/字段"
    table.rows[0].cells[1].text = "取值/证据"
    _shade_cells(table.rows[0].cells, "D9EAF7")
    for cell in table.rows[0].cells:
        _set_cell_font(cell, font_english, font_chinese, 9, bold=True)
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
        for cell in cells:
            _set_cell_font(cell, font_english, font_chinese, 9)
    _set_table_widths(table, [Inches(2.0), Inches(4.45)])
    _set_table_borders_and_margins(table)
    return table


def _add_flow_table(doc, content, font_english, font_chinese):
    rows = content if isinstance(content, list) else []
    normalized_rows = []
    for row in rows:
        row = row if isinstance(row, dict) else {"stage": row}

        def first_value(*keys):
            for key in keys:
                if key in row and row[key] not in (None, ""):
                    return row[key]
            return "未提供"

        normalized_rows.append((
            first_value("stage", "name"),
            first_value("input", "source", "inputs"),
            first_value("operation", "process", "command"),
            first_value("output", "destination", "outputs"),
        ))
    omit, stats = _should_omit_sparse_table(
        normalized_rows,
        value_columns=(1, 2, 3),
    )
    if omit:
        _add_sparse_table_note(doc, stats, font_english, font_chinese)
        return None
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ("阶段", "输入/来源", "处理过程", "输出/去向")
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8.5, bold=True)
    _shade_cells(table.rows[0].cells, "E2EFDA")
    for values in normalized_rows:
        cells = table.add_row().cells
        for index, value in enumerate(values):
            cells[index].text = _stringify(value) or "未提供"
            _set_cell_font(cells[index], font_english, font_chinese, 8.5)
    _set_table_widths(table, [Inches(1.0), Inches(1.75), Inches(2.0), Inches(1.7)])
    _set_table_borders_and_margins(table)
    return table


def _parse_svg_measure(value):
    match = re.match(r"^\s*([0-9.]+)\s*(px|pt|in|cm|mm)?\s*$", str(value or ""), re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "px").casefold()
    factors = {"px": 1 / 96, "pt": 1 / 72, "in": 1, "cm": 1 / 2.54, "mm": 1 / 25.4}
    return number * factors[unit]


def _svg_display_dimensions(blob, max_width=6.15, max_height=7.0):
    width_in = height_in = None
    try:
        root = ET.fromstring(blob)
        width_in = _parse_svg_measure(root.get("width"))
        height_in = _parse_svg_measure(root.get("height"))
        view_box = root.get("viewBox") or root.get("viewbox")
        if view_box:
            parts = [float(part) for part in re.split(r"[\s,]+", view_box.strip()) if part]
            if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
                ratio = parts[2] / parts[3]
                if width_in and not height_in:
                    height_in = width_in / ratio
                elif height_in and not width_in:
                    width_in = height_in * ratio
                elif not width_in and not height_in:
                    width_in, height_in = min(max_width, 6.0), min(max_height, 6.0 / ratio)
    except (ET.ParseError, ValueError, TypeError):
        pass
    width_in = width_in or min(max_width, 6.0)
    height_in = height_in or min(max_height, width_in / 2)
    scale = min(1.0, max_width / width_in, max_height / height_in)
    return Inches(width_in * scale), Inches(height_in * scale)


def _add_svg_picture(run, path, alt_text):
    blob = path.read_bytes()
    width, height = _svg_display_dimensions(blob)
    package = run.part.package
    digest = hashlib.sha1(blob).hexdigest()
    image_part = next(
        (part for part in package.image_parts if part.sha1 == digest and part.content_type == "image/svg+xml"),
        None,
    )
    if image_part is None:
        partname = package.image_parts._next_image_partname("svg")
        image_part = ImagePart(partname, "image/svg+xml", blob)
        package.image_parts.append(image_part)
    relation_id = run.part.relate_to(image_part, RT.IMAGE)
    inline = CT_Inline.new_pic_inline(run.part.next_id, relation_id, path.name, width, height)
    inline.docPr.set("descr", alt_text)
    inline.docPr.set("title", alt_text)
    run._r.add_drawing(inline)
    return inline


def _add_raster_picture(run, path, alt_text):
    image = DocxImage.from_file(str(path))
    native_width, native_height = image.scaled_dimensions()
    scale = min(1.0, Inches(6.15) / native_width, Inches(7.0) / native_height)
    shape = run.add_picture(
        str(path),
        width=Emu(int(native_width * scale)),
        height=Emu(int(native_height * scale)),
    )
    shape._inline.docPr.set("descr", alt_text)
    shape._inline.docPr.set("title", alt_text)
    return shape


def _artifact_preview_priority(artifact, original_index):
    if str(artifact.get("role", "")).casefold() == "paper_result_figure":
        tier = 0
    elif artifact.get("featured"):
        tier = 1
    elif str(artifact.get("role", "")).casefold() == "validation_plot":
        tier = 2
    else:
        tier = 3
    return tier, original_index


def _artifact_state(artifact):
    path = _resolved_artifact_path(artifact)
    if not artifact.get("path") and (artifact.get("rows") is not None or artifact.get("data") is not None):
        return "内嵌数据"
    raw = str(artifact.get("path") or "")
    remote_path = str(artifact.get("remote_path") or "").strip()
    if remote_path and (not path or not path.exists()):
        return f"文件未同步：远端文件位于 {remote_path}"
    if urlparse(raw).scheme in {"http", "https", "s3", "gs"}:
        return "外部路径（未内嵌）"
    if not path or not path.exists():
        return "文件不存在"
    if not path.is_file():
        return "不是普通文件"
    if path.suffix.casefold() in _IMAGE_EXTENSIONS | _TABLE_EXTENSIONS:
        return "可用"
    return "仅清单（格式不支持预览）"


def _image_embedding_error(artifact):
    extension = _artifact_extension(artifact)
    if extension not in _IMAGE_EXTENSIONS:
        return "格式不属于支持的实验图片类型"
    path = _resolved_artifact_path(artifact)
    if not path or not path.is_file():
        return _artifact_state(artifact)
    try:
        if extension == ".svg":
            ET.fromstring(path.read_bytes())
        else:
            DocxImage.from_file(str(path))
    except Exception as exc:
        return f"文件损坏或无法读取：{exc}"
    return None


def _select_body_image_identities(entries, limit=_BODY_IMAGE_LIMIT):
    artifacts = _deduplicate_history_artifacts(entries)
    ranked = []
    for index, artifact in enumerate(artifacts):
        if _artifact_extension(artifact) not in _IMAGE_EXTENSIONS:
            continue
        if _image_embedding_error(artifact):
            continue
        ranked.append((artifact, _artifact_preview_priority(artifact, index)))
    ranked.sort(key=lambda item: item[1])
    selected = ranked[:limit]
    return {
        "selected": {_artifact_identity(artifact) for artifact, _ in selected},
        "rendered": set(),
        "valid_count": len(ranked),
        "limit": limit,
    }


def _format_bytes(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "不适用"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(number)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return str(number)


def _chunk_checksum(value):
    text = str(value or "未提供校验和")
    if text == "未提供校验和":
        return text
    return "\n".join(text[index:index + 32] for index in range(0, len(text), 32))


def _add_caption(doc, text, font_english, font_chinese):
    paragraph = doc.add_paragraph(style="Caption")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run(text)
    _set_run_font(run, font_english, font_chinese, 9)
    paragraph.paragraph_format.space_before = Pt(2)
    paragraph.paragraph_format.space_after = Pt(8)
    return paragraph


def _render_artifact_section(
    doc,
    artifacts,
    counters,
    font_english,
    font_chinese,
    include_manifest=True,
    body_image_state=None,
):
    artifacts = _deduplicate_artifacts(_artifact_items(artifacts))
    if body_image_state is None:
        local_entries = [(None, None, artifact) for artifact in artifacts]
        body_image_state = _select_body_image_identities(local_entries)
    for artifact in artifacts:
        extension = _artifact_extension(artifact)
        path = _resolved_artifact_path(artifact)
        title = str(artifact.get("caption") or artifact.get("name") or "实验产物")
        if extension in _IMAGE_EXTENSIONS:
            identity = _artifact_identity(artifact)
            if (
                identity not in body_image_state["selected"]
                or identity in body_image_state["rendered"]
            ):
                continue
            try:
                paragraph = doc.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.paragraph_format.keep_with_next = True
                run = paragraph.add_run()
                if extension == ".svg":
                    _add_svg_picture(run, path, title)
                else:
                    _add_raster_picture(run, path, title)
                counters["figure"] += 1
                body_image_state["rendered"].add(identity)
                _add_caption(doc, f"图 7-{counters['figure']}  {title}", font_english, font_chinese)
            except (OSError, ValueError, TypeError) as exc:
                _add_styled_paragraph(
                    doc, f"⚠️ 无法嵌入 {title}：{exc}",
                    font_en=font_english, font_cn=font_chinese, size_pt=9,
                )
        elif (
            extension in _TABLE_EXTENSIONS
            or artifact.get("rows") is not None
            or artifact.get("data") is not None
        ):
            headers, rows, truncated, error = _artifact_table_preview(artifact)
            if headers:
                table = _add_generic_table(doc, headers, rows, font_english, font_chinese)
                if table is not None:
                    counters["table"] += 1
                    _add_caption(doc, f"表 7-{counters['table']}  {title}", font_english, font_chinese)
                    if truncated or len(rows) > _TABLE_PREVIEW_ROWS or len(headers) > _TABLE_PREVIEW_COLUMNS:
                        _add_styled_paragraph(
                            doc, "注：正文仅展示前 20 行、10 列；完整文件见下方产物清单。",
                            font_en=font_english, font_cn=font_chinese, size_pt=8.5,
                        )
            elif error:
                _add_styled_paragraph(
                    doc, f"⚠️ {title} 未生成表格预览：{error}",
                    font_en=font_english, font_cn=font_chinese, size_pt=9,
                )

    if not include_manifest:
        return
    _add_styled_paragraph(
        doc, f"完整产物清单（{len(artifacts)} 项）",
        font_en=font_english, font_cn=font_chinese, size_pt=10, bold=True,
    )
    if not artifacts:
        _add_styled_paragraph(doc, "未提供实验产物。", font_en=font_english, font_cn=font_chinese)
        return
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ("产物名称 / 角色", "路径", "来源 / 大小", "SHA-256 / 状态")
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8, bold=True)
    _shade_cells(table.rows[0].cells, "F1F5F9")
    for artifact in artifacts:
        cells = table.add_row().cells
        cells[0].text = f"{artifact.get('name', '未命名')}\n角色：{artifact.get('role', '未提供')}"
        display_path = artifact.get("path") or "内嵌数据"
        remote_path = artifact.get("remote_path")
        if remote_path and remote_path != display_path:
            display_path = f"本地：{display_path}\n远端：{remote_path}"
        cells[1].text = str(display_path)
        size = artifact.get("size_bytes")
        resolved = _resolved_artifact_path(artifact)
        if size is None and resolved and resolved.is_file():
            try:
                size = resolved.stat().st_size
            except OSError:
                size = None
        cells[2].text = f"{artifact.get('origin', '未提供')}\n{_format_bytes(size)}"
        cells[3].text = f"{_chunk_checksum(artifact.get('sha256'))}\n{_artifact_state(artifact)}"
        for cell in cells:
            _set_cell_font(cell, font_english, font_chinese, 7.5)
    _set_table_widths(table, [Inches(1.35), Inches(2.35), Inches(1.15), Inches(1.6)])
    _set_table_borders_and_margins(table)


def _render_dynamic_section(
    doc,
    section,
    counters,
    font_english,
    font_chinese,
    heading_level=3,
    include_artifact_manifest=True,
    body_image_state=None,
):
    kind = str(section.get("kind") or "text").casefold()
    content = section.get("content")
    _add_styled_heading(
        doc, str(section.get("title") or "实验详情"), level=heading_level,
        font_en=font_english, font_cn=font_chinese,
    )
    if kind == "key_value":
        _add_key_value_table(doc, content, font_english, font_chinese)
    elif kind == "flow":
        _add_flow_table(doc, content, font_english, font_chinese)
    elif kind == "table":
        headers, rows = _table_data(content)
        if headers:
            table = _add_generic_table(doc, headers, rows, font_english, font_chinese)
            if table is not None:
                counters["table"] += 1
                _add_caption(doc, f"表 7-{counters['table']}  {section.get('title')}", font_english, font_chinese)
                if len(rows) > _TABLE_PREVIEW_ROWS or len(headers) > _TABLE_PREVIEW_COLUMNS:
                    _add_styled_paragraph(
                        doc, "注：正文仅展示前 20 行、10 列。",
                        font_en=font_english, font_cn=font_chinese, size_pt=8.5,
                    )
        else:
            _add_sparse_table_note(
                doc,
                _sparse_table_stats([]),
                font_english,
                font_chinese,
            )
    elif kind == "list":
        for item in _as_list(content):
            _add_list_item(doc, _stringify(item) or "未提供", font_english, font_chinese)
    elif kind == "artifacts":
        _render_artifact_section(
            doc,
            content,
            counters,
            font_english,
            font_chinese,
            include_manifest=include_artifact_manifest,
            body_image_state=body_image_state,
        )
    else:
        if isinstance(content, list):
            for item in content:
                _add_styled_paragraph(doc, _stringify(item) or "未提供", font_en=font_english, font_cn=font_chinese)
        else:
            _add_styled_paragraph(doc, _stringify(content) or "未提供", font_en=font_english, font_cn=font_chinese)


def _render_experiment_records(
    doc,
    records,
    shared_sections,
    counters,
    font_english,
    font_chinese,
    *,
    experiment_heading_level=2,
    section_heading_level=3,
    include_artifact_manifest=True,
    body_image_state=None,
):
    for index, record in enumerate(records, start=1):
        _add_styled_heading(
            doc, f"{index}. {record['case_name']}", level=experiment_heading_level,
            font_en=font_english, font_cn=font_chinese,
        )
        overview = doc.add_table(rows=0, cols=2)
        overview.style = "Table Grid"
        overview.alignment = WD_TABLE_ALIGNMENT.CENTER
        for label, value in (
            ("执行状态", record.get("status") or "未提供"),
            ("实际耗时", record.get("actual_time") or "未提供"),
            ("输出摘要", record.get("output_detail") or "未提供"),
        ):
            cells = overview.add_row().cells
            cells[0].text = label
            cells[1].text = _stringify(value) or "未提供"
            _shade_cells([cells[0]], "F2F2F2")
            _set_cell_font(cells[0], font_english, font_chinese, 9, bold=True)
            _set_cell_font(cells[1], font_english, font_chinese, 9)
        _set_table_widths(overview, [Inches(1.35), Inches(5.1)])
        _set_table_borders_and_margins(overview)
        if record["sections"]:
            for section in record["sections"]:
                _render_dynamic_section(
                    doc,
                    section,
                    counters,
                    font_english,
                    font_chinese,
                    heading_level=section_heading_level,
                    include_artifact_manifest=include_artifact_manifest,
                    body_image_state=body_image_state,
                )
        else:
            _add_styled_paragraph(
                doc,
                "⚠️ 该实验尚未提供参数、数据流或可交付产物。",
                font_en=font_english,
                font_cn=font_chinese,
            )
    if shared_sections:
        _add_styled_heading(
            doc, "公共/未归属证据", level=experiment_heading_level,
            font_en=font_english, font_cn=font_chinese,
        )
        _add_styled_paragraph(
            doc,
            "以下证据仅属于当前执行轮次，但现有记录不足以将其可靠归入某一个实验，因此不会复制到多个实验或其他轮次。",
            font_en=font_english,
            font_cn=font_chinese,
            size_pt=9,
        )
        for section in shared_sections:
            _render_dynamic_section(
                doc,
                section,
                counters,
                font_english,
                font_chinese,
                heading_level=section_heading_level,
                include_artifact_manifest=include_artifact_manifest,
                body_image_state=body_image_state,
            )


def _add_history_run_overview(doc, run, base_dir, font_english, font_chinese):
    validation_value = _history_reference(run.get("validation_result"), base_dir)
    validation, _ = _load_json_value_with_base(validation_value, base_dir)
    claim_level = "未提供"
    if isinstance(validation, dict):
        claim_level = str((validation.get("claim_gate") or {}).get("achieved_level") or "未提供")
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for label, value in (
        ("轮次状态", run.get("status") or "未提供"),
        ("执行后端", run.get("backend") or "未提供"),
        ("起止时间", f"{run.get('started_at') or '未提供'} → {run.get('ended_at') or '未提供'}"),
        ("本轮耗时", _run_elapsed_text(run)),
        ("准备步骤", _reuse_summary(run)),
        ("验证等级", claim_level),
        ("运行产物根", run.get("run_output_root") or "未提供"),
    ):
        cells = table.add_row().cells
        cells[0].text = str(label)
        cells[1].text = _stringify(value) or "未提供"
        _shade_cells([cells[0]], "EAF2F8")
        _set_cell_font(cells[0], font_english, font_chinese, 8.5, bold=True)
        _set_cell_font(cells[1], font_english, font_chinese, 8.5)
    _set_table_widths(table, [Inches(1.35), Inches(5.1)])
    _set_table_borders_and_margins(table)


def _add_history_cloud_resources_section(
    doc,
    history,
    font_english,
    font_chinese,
):
    cci_runs = [
        run
        for run in _history_runs(history)
        if str(run.get("backend") or "").casefold() == "cci"
    ]
    if not cci_runs:
        return
    _add_styled_heading(
        doc,
        "2.3 CCI 云实例与费用证据",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    rows = []
    for run in cci_runs:
        resources = run.get("cloud_resources")
        if isinstance(resources, dict):
            items = resources.get("resources", resources)
            if isinstance(items, dict):
                items = [
                    {"role": role, **value}
                    for role, value in items.items()
                    if isinstance(value, dict)
                ]
            elif not isinstance(items, list):
                items = []
        elif isinstance(resources, list):
            items = resources
        else:
            items = []
        if not items:
            rows.append(
                (
                    f"{run.get('run_id') or '未提供'} / 云资源",
                    "已声明 CCI 后端，但未提供可验证的 cloud_resources。",
                )
            )
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            price = item.get("price")
            price_unit = item.get("price_unit")
            price_text = (
                f"{price} {price_unit or ''}".strip()
                if price not in (None, "")
                else "未提供"
            )
            actual_cost = item.get("actual_cost")
            cost_text = (
                str(actual_cost)
                if actual_cost not in (None, "")
                else f"未计算（{item.get('cost_calculation') or '证据不足'}）"
            )
            rows.append(
                (
                    f"{run.get('run_id') or '未提供'} / {item.get('role') or '实例'}",
                    "；".join(
                        [
                            f"实例 ID={item.get('instance_id') or '未提供'}",
                            f"起止={item.get('created_at') or item.get('started_at') or '未提供'} → {item.get('released_at') or item.get('ended_at') or '未提供'}",
                            f"时长={item.get('duration_hours') if item.get('duration_hours') is not None else '未提供'} 小时",
                            f"原始价格={price_text}",
                            f"实际费用={cost_text}",
                            f"远端路径={item.get('remote_path') or '未提供'}",
                            f"本地路径={item.get('local_path') or '未提供'}",
                        ]
                    ),
                )
            )
    _add_key_value_rows_table(
        doc,
        rows,
        font_english,
        font_chinese,
        headers=("轮次 / 角色", "实例、价格、时长与路径证据"),
    )


def _history_artifact_entries(records, shared_sections, run):
    artifacts = []
    for record in records:
        for artifact in record.get("_artifacts") or []:
            annotated = dict(artifact)
            annotated["_report_case_name"] = record.get("case_name") or "未提供"
            artifacts.append(annotated)
    for section in shared_sections:
        if str(section.get("kind") or "").casefold() == "artifacts":
            for artifact in _artifact_items(section.get("content")):
                annotated = dict(artifact)
                annotated["_report_case_name"] = "公共/未归属证据"
                artifacts.append(annotated)
    return [
        (run.get("run_id"), _run_title(run), artifact)
        for artifact in _deduplicate_artifacts(artifacts)
    ]


def _deduplicate_history_artifacts(entries):
    merged = {}
    order = []
    for run_id, run_title, artifact in entries:
        if not isinstance(artifact, dict):
            continue
        key = _artifact_identity(artifact)
        if key not in merged:
            merged[key] = dict(artifact)
            merged[key]["run_ids"] = []
            merged[key]["run_titles"] = []
            merged[key]["case_names"] = []
            order.append(key)
        if run_id and run_id not in merged[key]["run_ids"]:
            merged[key]["run_ids"].append(str(run_id))
        if run_title and run_title not in merged[key]["run_titles"]:
            merged[key]["run_titles"].append(str(run_title))
        case_name = artifact.get("_report_case_name")
        if case_name and str(case_name) not in merged[key]["case_names"]:
            merged[key]["case_names"].append(str(case_name))
        for field, value in artifact.items():
            if field not in merged[key] or merged[key][field] in (None, "", [], {}):
                if value not in (None, "", [], {}):
                    merged[key][field] = value
    return [merged[key] for key in order]


def _add_history_artifact_manifest(doc, entries, font_english, font_chinese):
    artifacts = _deduplicate_history_artifacts(entries)
    _add_styled_heading(
        doc,
        f"跨轮次完整产物清单（去重后 {len(artifacts)} 项）",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_styled_paragraph(
        doc,
        "相同路径或 SHA-256 的文件仅列一次；“关联执行”保留该文件出现过的全部 run_id。",
        font_en=font_english,
        font_cn=font_chinese,
        size_pt=9,
    )
    if not artifacts:
        _add_styled_paragraph(doc, "未提供实验产物。", font_en=font_english, font_cn=font_chinese)
        return
    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, header in enumerate(("产物名称 / 角色", "关联执行", "路径", "来源 / 大小", "SHA-256 / 状态")):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 7.5, bold=True)
    _shade_cells(table.rows[0].cells, "F1F5F9")
    for artifact in artifacts:
        cells = table.add_row().cells
        cells[0].text = f"{artifact.get('name', '未命名')}\n角色：{artifact.get('role', '未提供')}"
        cells[1].text = "\n".join(artifact.get("run_ids") or ["未提供"])
        display_path = artifact.get("path") or "内嵌数据"
        remote_path = artifact.get("remote_path")
        if remote_path and remote_path != display_path:
            display_path = f"本地：{display_path}\n远端：{remote_path}"
        cells[2].text = str(display_path)
        size = artifact.get("size_bytes")
        resolved = _resolved_artifact_path(artifact)
        if size is None and resolved and resolved.is_file():
            try:
                size = resolved.stat().st_size
            except OSError:
                size = None
        cells[3].text = f"{artifact.get('origin', '未提供')}\n{_format_bytes(size)}"
        cells[4].text = f"{_chunk_checksum(artifact.get('sha256'))}\n{_artifact_state(artifact)}"
        for cell in cells:
            _set_cell_font(cell, font_english, font_chinese, 7)
    _set_table_widths(
        table,
        [Inches(1.2), Inches(0.75), Inches(1.8), Inches(1.05), Inches(1.65)],
    )
    _set_table_borders_and_margins(table)


def _prepare_history_experiment_payload(
    execution_run_history,
    history_base_dir,
    output_dir,
):
    base_dir = history_base_dir or output_dir
    all_entries = []
    prepared_runs = []
    for run in _history_runs(execution_run_history):
        records, shared_sections = _build_experiment_records(
            _run_plan_selection(run),
            run.get("actual_test_outcomes"),
            run.get("experiment_details"),
            run.get("compute_usage"),
            _history_reference(run.get("validation_result"), base_dir),
            _history_reference(run.get("scientific_repro_contract"), base_dir),
            _history_reference(run.get("artifact_manifest"), base_dir),
            base_dir,
        )
        prepared_runs.append((run, records, shared_sections))
        all_entries.extend(_history_artifact_entries(records, shared_sections, run))
    return prepared_runs, all_entries


def _add_core_result_figures(
    doc,
    entries,
    body_image_state,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "6.3 按论文规范复现的实验结果图", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    artifacts = [
        artifact
        for artifact in _deduplicate_history_artifacts(entries)
        if str(artifact.get("role") or "").casefold() == "paper_result_figure"
    ]
    figure_number = 0
    for artifact in artifacts:
        identity = _artifact_identity(artifact)
        if (
            identity not in body_image_state["selected"]
            or identity in body_image_state["rendered"]
        ):
            continue
        path = _resolved_artifact_path(artifact)
        error = _image_embedding_error(artifact)
        if error:
            continue
        title = str(artifact.get("caption") or artifact.get("name") or "论文结果图")
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.keep_with_next = True
        run = paragraph.add_run()
        if _artifact_extension(artifact) == ".svg":
            _add_svg_picture(run, path, title)
        else:
            _add_raster_picture(run, path, title)
        figure_number += 1
        body_image_state["rendered"].add(identity)
        _add_caption(
            doc,
            f"图 6-{figure_number}  {title}",
            font_english,
            font_chinese,
        )
        _add_styled_paragraph(
            doc,
            (
                f"关联 run_id：{', '.join(artifact.get('run_ids') or ['未提供'])}；"
                f"关联实验：{', '.join(artifact.get('case_names') or ['未提供'])}"
            ),
            font_en=font_english,
            font_cn=font_chinese,
            size_pt=8,
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
        )
    if not figure_number:
        _add_styled_paragraph(
            doc,
            "本次没有通过严格绘图规范门禁且可嵌入的论文实验结果图；缺口保留在实验详情与产物清单中。",
            font_en=font_english,
            font_cn=font_chinese,
        )


def _add_core_validation_section(
    doc,
    history,
    history_base_dir,
    repro_conclusions,
    artifact_entries,
    body_image_state,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "六、核心验证结果", level=1,
        font_en=font_english, font_cn=font_chinese,
    )
    _add_history_scientific_validation_section(
        doc,
        history,
        history_base_dir,
        font_english,
        font_chinese,
        heading_text="6.1 数值、统计与物理验证",
        heading_level=2,
    )
    _add_styled_heading(
        doc, "6.2 论文指标与本次结果", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    conclusions = _filter_history_conclusions(history, repro_conclusions)
    if not isinstance(conclusions, list) or not conclusions:
        _add_styled_paragraph(
            doc,
            "本次没有具备论文基准与实测值双重证据的指标，不据此提高科学复现等级。",
            font_en=font_english,
            font_cn=font_chinese,
        )
    else:
        metric_names = []
        for item in conclusions:
            metric = str(
                item.get("metric")
                or item.get("metric_name")
                or item.get("case_name")
                or "指标"
            )
            if metric not in metric_names:
                metric_names.append(metric)
        table = doc.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for index, header in enumerate(
            ("案例", "指标", "论文 / 官方基准", "本次实测", "判定")
        ):
            table.rows[0].cells[index].text = header
            _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8.5, bold=True)
        _shade_cells(table.rows[0].cells, "E2EFDA")
        for item in conclusions:
            cells = table.add_row().cells
            values = (
                item.get("case_name", "-"),
                item.get("metric") or item.get("metric_name") or "动态指标",
                item.get("paper_data", "-"),
                item.get("repro_data", "-"),
                item.get("status", "-"),
            )
            for index, value in enumerate(values):
                cells[index].text = _stringify(value) or "-"
                _set_cell_font(cells[index], font_english, font_chinese, 8.5)
        _set_table_widths(
            table,
            [Inches(1.25), Inches(1.15), Inches(1.45), Inches(1.45), Inches(1.15)],
        )
        _set_table_borders_and_margins(table)
    _add_core_result_figures(
        doc,
        artifact_entries,
        body_image_state,
        font_english,
        font_chinese,
    )


def _add_experiment_details_section(
    doc,
    execution_run_history,
    history_base_dir,
    output_dir,
    font_english,
    font_chinese,
    *,
    heading_text="七、实验详情与产物",
    heading_level=1,
    prepared_runs=None,
    all_entries=None,
    body_image_state=None,
):
    _add_styled_heading(
        doc, heading_text, level=heading_level,
        font_en=font_english, font_cn=font_chinese,
    )
    counters = {"figure": 0, "table": 0}
    base_dir = history_base_dir or output_dir
    if prepared_runs is None or all_entries is None:
        prepared_runs, all_entries = _prepare_history_experiment_payload(
            execution_run_history,
            history_base_dir,
            output_dir,
        )
    if body_image_state is None:
        body_image_state = _select_body_image_identities(all_entries)
    for run, records, shared_sections in prepared_runs:
        _add_styled_heading(
            doc,
            _run_title(run),
            level=min(heading_level + 1, 4),
            font_en=font_english,
            font_cn=font_chinese,
        )
        _add_history_run_overview(doc, run, base_dir, font_english, font_chinese)
        if not records:
            _add_styled_paragraph(
                doc,
                "⚠️ 本轮没有可归属的实验记录。",
                font_en=font_english,
                font_cn=font_chinese,
            )
        _render_experiment_records(
            doc,
            records,
            shared_sections,
            counters,
            font_english,
            font_chinese,
            experiment_heading_level=min(heading_level + 2, 4),
            section_heading_level=min(heading_level + 3, 4),
            include_artifact_manifest=False,
            body_image_state=body_image_state,
        )
    valid_count = body_image_state["valid_count"]
    rendered_count = len(body_image_state["rendered"])
    if valid_count:
        _add_styled_paragraph(
            doc,
            (
                f"正文共展示 {rendered_count} 张实验图片，整篇正文上限为 {_BODY_IMAGE_LIMIT} 张；"
                f"全部 {valid_count} 张有效实验图片见附录 A。"
            ),
            font_en=font_english,
            font_cn=font_chinese,
            size_pt=9,
        )
    else:
        _add_styled_paragraph(
            doc,
            "正文未发现可嵌入的有效实验图片；图片缺口与失败原因见附录 A。",
            font_en=font_english,
            font_cn=font_chinese,
            size_pt=9,
        )
    return all_entries


def _add_scientific_validation_evidence(doc, validation_result, scientific_contract, artifact_manifest, font_english, font_chinese):
    validation = _load_json_artifact(validation_result)
    contract = _load_json_artifact(scientific_contract)
    manifest = _load_json_artifact(artifact_manifest)
    if contract:
        _add_styled_heading(
            doc, "物理工况与数值方法契约", level=2,
            font_en=font_english, font_cn=font_chinese,
        )
        physics_spec = contract.get("physics_spec") or {}
        disclosed = [name for name, item in physics_spec.items() if isinstance(item, dict) and item.get("status") == "disclosed"]
        missing = [name for name, item in physics_spec.items() if isinstance(item, dict) and item.get("status") != "disclosed"]
        _add_styled_paragraph(doc, f"项目 profile：{contract.get('project_profile', '-')}；已有证据：{', '.join(disclosed) or '无'}；未披露：{', '.join(missing) or '无'}", font_en=font_english, font_cn=font_chinese)
    if manifest:
        artifacts = manifest.get("artifacts") or []
        roles = {}
        for item in artifacts:
            role = item.get("role", "unknown") if isinstance(item, dict) else "unknown"
            roles[role] = roles.get(role, 0) + 1
        _add_styled_paragraph(doc, f"资产清单：{len(artifacts)} 个文件；分类：{roles}", font_en=font_english, font_cn=font_chinese)
    if not validation:
        _add_styled_paragraph(doc, "⚠️ 未提供 validation_result.json；软件运行状态不能解释为数值或物理复现成功。", font_en=font_english, font_cn=font_chinese)
        return
    claim = validation.get("claim_gate") or {}
    _add_styled_heading(
        doc, "强制验证与复现等级", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    _add_styled_paragraph(doc, f"最终复现等级：{claim.get('achieved_level') or '未达到 software_runnable'}", font_en=font_english, font_cn=font_chinese, bold=True)
    phases = validation.get("phases") or {}
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    headers = table.rows[0].cells
    for index, label in enumerate(("验证阶段", "状态", "证据/阻断")):
        headers[index].text = label
        _set_cell_font(headers[index], font_english, font_chinese, 9, bold=True)
    _shade_cells(headers, "F1F5F9")
    for name in ("validation_preflight", "reference_acquisition", "prediction_acquisition", "field_alignment", "quantitative_validation", "physics_validation", "validation_artifacts"):
        phase = phases.get(name) or {}
        cells = table.add_row().cells
        cells[0].text = name
        cells[1].text = str(phase.get("status") or "未提供阶段状态（报告门禁应阻断）")
        cells[2].text = _stringify(phase.get("mismatches") or phase.get("metrics") or phase.get("checks") or "-")
        for cell in cells:
            _set_cell_font(cell, font_english, font_chinese, 8.5)
    _set_table_borders_and_margins(table)


def _add_history_readiness_section(
    doc,
    history,
    font_english,
    font_chinese,
    *,
    heading_text="3.2 实验与执行计划概述",
    heading_level=2,
):
    _add_styled_heading(
        doc,
        heading_text,
        level=heading_level,
        font_en=font_english,
        font_cn=font_chinese,
    )
    widths = [Inches(1.1), Inches(1.2), Inches(0.9), Inches(1.5), Inches(0.9), Inches(0.9)]
    headers = ["复现点/案例", "对应代码路径", "当前准备度", "阻碍复现因素", "推荐 GPU 算力", "预计时效 (本机)"]
    for run in _history_runs(history):
        _add_styled_heading(
            doc,
            _run_title(run),
            level=min(heading_level + 1, 4),
            font_en=font_english,
            font_cn=font_chinese,
        )
        _add_styled_paragraph(
            doc,
            f"轮次状态：{run.get('status') or '未提供'}；准备步骤：{_reuse_summary(run)}",
            font_en=font_english,
            font_cn=font_chinese,
            size_pt=9,
        )
        table = doc.add_table(rows=1, cols=6)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for index, header in enumerate(headers):
            table.rows[0].cells[index].text = header
            _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8, bold=True)
        _shade_cells(table.rows[0].cells, "D9E2F3")
        for item in _history_rows_for_run(run, "readiness"):
            cells = table.add_row().cells
            values = (
                item.get("case_name", "-"),
                item.get("config_path", "-"),
                item.get("status", "-"),
                item.get("barrier", "-"),
                item.get("gpu_spec", "-"),
                item.get("est_time", "-"),
            )
            for index, value in enumerate(values):
                cells[index].text = str(value)
                _set_cell_font(cells[index], font_english, font_chinese, 8)
        _set_table_widths(table, widths)
        _set_table_borders_and_margins(table)
        _add_styled_paragraph(doc, "", space_after_pt=8)


def _add_history_outcomes_section(
    doc,
    history,
    font_english,
    font_chinese,
    *,
    heading_text="7.1 实际测试结果与真实耗时",
    heading_level=2,
):
    _add_styled_heading(
        doc,
        heading_text,
        level=heading_level,
        font_en=font_english,
        font_cn=font_chinese,
    )
    widths = [Inches(1.2), Inches(1.0), Inches(3.1), Inches(1.2)]
    headers = ["评估维度/案例", "本次运行状态", "实际输出物理成果", "单样本实际耗时 (本机)"]
    for run in _history_runs(history):
        _add_styled_heading(
            doc,
            _run_title(run),
            level=min(heading_level + 1, 4),
            font_en=font_english,
            font_cn=font_chinese,
        )
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for index, header in enumerate(headers):
            table.rows[0].cells[index].text = header
            _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8.5, bold=True)
        _shade_cells(table.rows[0].cells, "E2EFDA")
        for item in _history_rows_for_run(run, "outcome"):
            cells = table.add_row().cells
            values = (
                item.get("case_name", "-"),
                item.get("status", "-"),
                item.get("output_detail", "-"),
                item.get("actual_time", "-"),
            )
            for index, value in enumerate(values):
                cells[index].text = str(value)
                _set_cell_font(cells[index], font_english, font_chinese, 8.5)
        _set_table_widths(table, widths)
        _set_table_borders_and_margins(table)
        _add_styled_paragraph(doc, "", space_after_pt=8)


def _add_history_compute_usage_section(
    doc,
    history,
    font_english,
    font_chinese,
    *,
    heading_text="2.2 论文原始与复现算力对比",
    heading_level=2,
):
    _add_styled_heading(
        doc,
        heading_text,
        level=heading_level,
        font_en=font_english,
        font_cn=font_chinese,
    )
    summaries = []
    for run in _history_runs(history):
        usage = run.get("compute_usage")
        if isinstance(usage, dict) and usage:
            summaries.append((run, _summarize_compute_usage(usage)))
    total_wall_clock = sum(summary["reproduction"]["end_to_end_hours"] or 0 for _, summary in summaries)
    total_gpu = sum(summary["reproduction"]["device_hours"].get("gpu") or 0 for _, summary in summaries)
    total_cpu = sum(summary["reproduction"]["device_hours"].get("cpu") or 0 for _, summary in summaries)
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.rows[0].cells[0].text = "累计指标"
    table.rows[0].cells[1].text = "结果"
    _shade_cells(table.rows[0].cells, "D9EAF7")
    for cell in table.rows[0].cells:
        _set_cell_font(cell, font_english, font_chinese, 9, bold=True)
    for label, value in (
        ("已执行轮次", str(len(_history_runs(history)))),
        ("有算力台账的轮次", str(len(summaries))),
        ("累计端到端墙钟时间", _format_hours(total_wall_clock)),
        ("累计 GPU 设备小时", _format_device_hours(total_gpu, "gpu")),
        ("累计 CPU 设备小时", _format_device_hours(total_cpu, "cpu")),
        ("累计比较限制", "累计值仅表示本次会话运行成本；不同实验范围不合并计算论文效率比例"),
    ):
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
        for cell in cells:
            _set_cell_font(cell, font_english, font_chinese, 9)
    _set_table_widths(table, [Inches(2.0), Inches(4.45)])
    _set_table_borders_and_margins(table)
    for run in _history_runs(history):
        _add_styled_heading(
            doc,
            _run_title(run),
            level=min(heading_level + 1, 4),
            font_en=font_english,
            font_cn=font_chinese,
        )
        _add_compute_usage_section(
            doc,
            run.get("compute_usage"),
            font_english,
            font_chinese,
            include_heading=False,
        )


def _add_history_scientific_validation_section(
    doc,
    history,
    base_dir,
    font_english,
    font_chinese,
    *,
    heading_text="9.1 数值与物理验证结论",
    heading_level=2,
):
    _add_styled_heading(
        doc,
        heading_text,
        level=heading_level,
        font_en=font_english,
        font_cn=font_chinese,
    )
    for run in _history_runs(history):
        _add_styled_heading(
            doc,
            _run_title(run),
            level=min(heading_level + 1, 4),
            font_en=font_english,
            font_cn=font_chinese,
        )
        _add_scientific_validation_evidence(
            doc,
            _history_reference(run.get("validation_result"), base_dir),
            _history_reference(run.get("scientific_repro_contract"), base_dir),
            _history_reference(run.get("artifact_manifest"), base_dir),
            font_english,
            font_chinese,
        )


REPORT_MODULES = (
    "一、执行摘要",
    "二、复现环境与算力配置",
    "三、论文模型与实验概述",
    "四、代码审计与可行性评估",
    "五、依赖环境准备",
    "六、数据与模型权重",
    "七、执行详情与产物",
    "八、代码缺陷与排坑自愈指南",
    "九、论文复现结论",
    "十、后续建议",
    "附录 A：全部实验产出图片",
    "附录 B：输出文件索引",
    "附录 C：完整项目审计报告",
)


def _project_profile_items(project_profile):
    if not isinstance(project_profile, dict):
        return [("项目", _stringify(project_profile) or "未提供")]
    return [
        (str(key), _stringify(value) or "未提供")
        for key, value in project_profile.items()
    ]


def _add_key_value_rows_table(
    doc,
    rows,
    font_english,
    font_chinese,
    *,
    headers=("项目", "值"),
    widths=(Inches(2.0), Inches(4.45)),
    suppress_if_sparse=False,
):
    rows = list(rows or [])
    if suppress_if_sparse:
        omit, stats = _should_omit_sparse_table(rows, value_columns=(1,))
        if omit:
            _add_sparse_table_note(doc, stats, font_english, font_chinese)
            return None
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = str(header)
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 9, bold=True)
    _shade_cells(table.rows[0].cells, "D9EAF7")
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = str(label)
        cells[1].text = _stringify(value) or "未提供"
        _set_cell_font(cells[0], font_english, font_chinese, 9, bold=True)
        _set_cell_font(cells[1], font_english, font_chinese, 9)
    _set_table_widths(table, list(widths))
    _set_table_borders_and_margins(table)
    return table


def _add_standard_cover(
    doc,
    repo_name,
    project_profile,
    history,
    font_english,
    font_chinese,
):
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(42)
    title = doc.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    title.paragraph_format.space_after = Pt(14)
    run = title.add_run(f"{repo_name} 论文复现技术报告")
    _set_run_font(run, font_english, font_chinese, 28, True, REPORT_BLUE)
    subtitle_text = ""
    if isinstance(project_profile, dict):
        for key in ("论文标题", "paper_title", "论文", "项目描述", "description"):
            if project_profile.get(key):
                subtitle_text = _stringify(project_profile[key])
                break
    subtitle = doc.add_paragraph(style="Subtitle")
    subtitle.alignment = WD_ALIGN_PARAGRAPH.LEFT
    subtitle.paragraph_format.space_after = Pt(34)
    subtitle_run = subtitle.add_run(subtitle_text or "Scientific Reproduction Technical Report")
    _set_run_font(subtitle_run, font_english, font_chinese, 14, False, REPORT_GRAY)

    metadata = _project_profile_items(project_profile)
    existing_labels = {label for label, _ in metadata}
    cover_rows = metadata[:7]
    if "执行后端" not in existing_labels:
        backends = "、".join(dict.fromkeys(
            str(run.get("backend") or "未提供")
            for run in _history_runs(history)
        ))
        cover_rows.append(("执行后端", backends or "未提供"))
    if "报告生成时间" not in existing_labels:
        cover_rows.append(("报告生成时间", datetime.now().astimezone().isoformat(timespec="seconds")))
    table = doc.add_table(rows=0, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    for label, value in cover_rows:
        cells = table.add_row().cells
        cells[0].text = str(label)
        cells[1].text = _stringify(value) or "未提供"
        _set_cell_font(cells[0], font_english, font_chinese, 9.5, bold=True)
        _set_cell_font(cells[1], font_english, font_chinese, 9.5)
    _set_table_widths(
        table,
        [Inches(1.55), Inches(4.9)],
        repeat_header=False,
    )
    _set_table_no_borders(table)
    doc.add_page_break()


def _add_standard_contents(doc, font_english, font_chinese):
    _add_styled_heading(
        doc,
        "目录",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    for module in REPORT_MODULES:
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(0.18)
        paragraph.paragraph_format.space_after = Pt(7)
        run = paragraph.add_run(module)
        _set_run_font(run, font_english, font_chinese, 11, False, REPORT_BLUE)
    doc.add_page_break()


def _add_project_information_section(
    doc,
    project_profile,
    history,
    base_dir,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "一、项目基本信息", level=1,
        font_en=font_english, font_cn=font_chinese,
    )
    scorecard = history.get("_audit_score") or {}
    rows = list(_project_profile_items(project_profile))
    existing = {label for label, _ in rows}
    generated = (
        ("项目名称", history.get("project_name") or "未提供"),
        ("审计评分", scorecard.get("overall_score", "未提供")),
        ("审计结论", scorecard.get("verdict", "未提供")),
        ("实际执行轮次", len(_history_runs(history))),
        (
            "执行后端",
            "、".join(dict.fromkeys(
                str(run.get("backend") or "未提供")
                for run in _history_runs(history)
            )) or "未提供",
        ),
        ("产物根路径", str(base_dir)),
    )
    rows.extend((label, value) for label, value in generated if label not in existing)
    _add_key_value_rows_table(
        doc,
        rows,
        font_english,
        font_chinese,
        headers=("项目", "内容"),
        widths=(Inches(1.7), Inches(4.75)),
    )
    _add_styled_heading(
        doc, "1.1 执行状态摘要", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    status_rows = [
        (
            _run_title(run),
            (
                f"状态={run.get('status') or '未提供'}；"
                f"后端={run.get('backend') or '未提供'}；"
                f"耗时={_run_elapsed_text(run)}；"
                f"claim={_validation_claim_level(run, base_dir)}"
            ),
        )
        for run in _history_runs(history)
    ]
    _add_key_value_rows_table(
        doc,
        status_rows or [("执行轮次", "未提供")],
        font_english,
        font_chinese,
        headers=("实际执行计划", "状态"),
        widths=(Inches(2.25), Inches(4.2)),
    )
    _add_styled_heading(
        doc, "1.2 项目审计评分", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    dimensions = scorecard.get("dimensions") or {}
    if dimensions:
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for index, header in enumerate(("审计维度", "得分", "满分", "评估")):
            table.rows[0].cells[index].text = header
            _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8.5, bold=True)
        _shade_cells(table.rows[0].cells, "D9EAF7")
        for dimension, value in dimensions.items():
            cells = table.add_row().cells
            for index, item in enumerate(_audit_dimension_values(dimension, value)):
                cells[index].text = str(item)
                _set_cell_font(cells[index], font_english, font_chinese, 8)
        _set_table_widths(table, [Inches(1.6), Inches(0.7), Inches(0.7), Inches(3.45)])
        _set_table_borders_and_margins(table)
    else:
        _add_styled_paragraph(
            doc,
            "审计评分维度未提供；完整审计正文见附录 C。",
            font_en=font_english,
            font_cn=font_chinese,
        )


def _add_research_background_section(
    doc,
    scientific_context,
    history,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "二、研究背景与复现目标", level=1,
        font_en=font_english, font_cn=font_chinese,
    )
    context = scientific_context if isinstance(scientific_context, dict) else {}
    background = context.get("background") if context else scientific_context
    _add_styled_heading(
        doc, "2.1 研究背景", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    _add_styled_paragraph(
        doc,
        _stringify(background) or "论文未披露或调用方未提供研究背景。",
        font_en=font_english,
        font_cn=font_chinese,
    )
    innovations = context.get("innovations") or []
    _add_styled_heading(
        doc, "2.2 核心创新", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    if innovations:
        for item in _as_list(innovations):
            _add_list_item(doc, _stringify(item) or "未提供", font_english, font_chinese)
    else:
        _add_styled_paragraph(
            doc,
            "论文未披露或调用方未提供核心创新说明。",
            font_en=font_english,
            font_cn=font_chinese,
        )
    goals = (
        context.get("reproduction_goals")
        or context.get("objectives")
        or context.get("goals")
        or [
            f"按用户确认的 {_run_plan_name(run)} 计划验证实际可执行范围。"
            for run in _history_runs(history)
        ]
    )
    _add_styled_heading(
        doc, "2.3 复现目标", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    for item in _as_list(goals):
        _add_list_item(doc, _stringify(item) or "未提供", font_english, font_chinese)


def _add_execution_plan_overview_section(
    doc,
    history,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "三、执行计划概览", level=1,
        font_en=font_english, font_cn=font_chinese,
    )
    _add_history_readiness_section(
        doc,
        history,
        font_english,
        font_chinese,
        heading_text="3.1 已实际执行的计划与案例",
        heading_level=2,
    )
    if history.get("report_requirements"):
        _add_styled_heading(
            doc, "3.2 用户补充要求", level=2,
            font_en=font_english, font_cn=font_chinese,
        )
        for item in history["report_requirements"]:
            request = item.get("request") if isinstance(item, dict) else item
            _add_list_item(doc, _stringify(request) or "未提供", font_english, font_chinese)


def _environment_profile_rows(project_profile, history):
    rows = []
    tokens = (
        "环境", "依赖", "python", "pytorch", "cuda", "gpu", "cpu", "硬件",
        "backend", "后端", "算力", "系统", "compiler", "编译",
    )
    if isinstance(project_profile, dict):
        rows.extend(
            (str(key), value)
            for key, value in project_profile.items()
            if any(token in str(key).casefold() for token in tokens)
        )
    if not rows:
        rows = [
            (
                _run_title(run),
                f"backend={run.get('backend') or '未提供'}；准备={_reuse_summary(run)}",
            )
            for run in _history_runs(history)
        ]
    return rows or [("环境", "未提供")]


def _add_environment_and_dependencies_section(
    doc,
    project_profile,
    history,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "四、环境与依赖", level=1,
        font_en=font_english, font_cn=font_chinese,
    )
    _add_styled_heading(
        doc, "4.1 执行环境", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    _add_key_value_rows_table(
        doc,
        _environment_profile_rows(project_profile, history),
        font_english,
        font_chinese,
        headers=("配置项", "版本 / 配置"),
        suppress_if_sparse=True,
    )
    _add_styled_heading(
        doc, "4.2 依赖准备与环境指纹", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, header in enumerate(("执行轮次", "状态", "环境指纹", "证据")):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8.5, bold=True)
    _shade_cells(table.rows[0].cells, "D9EAF7")
    for run in _history_runs(history):
        status, fingerprint, evidence = _preparation_record(run, "step_4")
        cells = table.add_row().cells
        for index, value in enumerate((_run_title(run), status, fingerprint, evidence)):
            cells[index].text = str(value)
            _set_cell_font(cells[index], font_english, font_chinese, 8)
    _set_table_widths(table, [Inches(1.8), Inches(0.8), Inches(1.7), Inches(2.15)])
    _set_table_borders_and_margins(table)


def _add_data_and_model_section(
    doc,
    history,
    base_dir,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "五、数据与模型准备", level=1,
        font_en=font_english, font_cn=font_chinese,
    )
    for subsection, step in (("5.1 数据准备", "step_5"), ("5.2 模型与权重准备", "step_6")):
        _add_styled_heading(
            doc, subsection, level=2,
            font_en=font_english, font_cn=font_chinese,
        )
        rows = []
        for run in _history_runs(history):
            status, fingerprint, evidence = _preparation_record(run, step)
            rows.append((_run_title(run), f"状态={status}；指纹={fingerprint}；证据={evidence}"))
        _add_key_value_rows_table(
            doc,
            rows or [("执行轮次", "未提供")],
            font_english,
            font_chinese,
            headers=("执行轮次", "准备结果"),
            widths=(Inches(2.1), Inches(4.35)),
        )
    _add_styled_heading(
        doc, "5.3 数据与模型资产", level=2,
        font_en=font_english, font_cn=font_chinese,
    )
    assets = []
    for run in _history_runs(history):
        manifest, manifest_base = _load_json_value_with_base(
            _history_reference(run.get("artifact_manifest"), base_dir),
            base_dir,
        )
        if not isinstance(manifest, dict):
            continue
        for artifact in _normalize_artifacts(
            manifest.get("artifacts") or [],
            manifest_base,
            default_origin="artifact_manifest",
        ):
            role_text = " ".join(
                str(artifact.get(key) or "") for key in ("role", "name", "origin")
            ).casefold()
            if any(token in role_text for token in (
                "data", "dataset", "weight", "checkpoint", "model", "数据", "权重",
            )):
                assets.append((run.get("run_id"), _run_title(run), artifact))
    if assets:
        _add_history_artifact_manifest(doc, assets, font_english, font_chinese)
    else:
        _add_styled_paragraph(
            doc,
            "资产清单中没有可明确归类的数据或模型条目。",
            font_en=font_english, font_cn=font_chinese,
        )


def _validation_claim_level(run, base_dir):
    validation = _load_json_artifact(_history_reference(run.get("validation_result"), base_dir))
    if not isinstance(validation, dict):
        return "未提供"
    return str((validation.get("claim_gate") or {}).get("achieved_level") or "未提供")


def _add_execution_summary(doc, history, base_dir, font_english, font_chinese):
    _add_styled_heading(doc, "一、执行摘要", level=1, font_en=font_english, font_cn=font_chinese)
    runs = _history_runs(history)
    status_counts = {}
    for run in runs:
        status = str(run.get("status") or "未提供")
        status_counts[status] = status_counts.get(status, 0) + 1
    status_text = "；".join(f"{key} {value} 轮" for key, value in status_counts.items()) or "未提供"
    _add_styled_paragraph(
        doc,
        (
            f"本报告记录项目 {history.get('project_name') or '未命名项目'} 的完整复现过程。"
            f"共纳入 {len(runs)} 个实际终态轮次，状态汇总为：{status_text}。"
            "所有结论仅依据执行台账、审计证据与验证结果生成。"
        ),
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_styled_heading(
        doc,
        "1.1 执行状态摘要",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    rows = []
    for run in runs:
        rows.append(
            (
                _run_title(run),
                (
                    f"状态={run.get('status') or '未提供'}；"
                    f"后端={run.get('backend') or '未提供'}；"
                    f"耗时={_run_elapsed_text(run)}；"
                    f"claim={_validation_claim_level(run, base_dir)}"
                ),
            )
        )
    _add_key_value_rows_table(
        doc,
        rows or [("执行轮次", "未提供")],
        font_english,
        font_chinese,
        headers=("执行计划", "结果摘要"),
        widths=(Inches(2.25), Inches(4.2)),
    )
    _add_styled_heading(
        doc,
        "1.2 项目基本信息",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    scorecard = history.get("_audit_score") or {}
    project_rows = [
        ("项目名称", history.get("project_name") or "未提供"),
        ("审计评分", scorecard.get("overall_score", "未提供")),
        ("审计结论", scorecard.get("verdict", "未提供")),
        ("实际执行轮次", len(runs)),
        (
            "执行后端",
            "、".join(dict.fromkeys(
                str(run.get("backend") or "未提供")
                for run in runs
            )) or "未提供",
        ),
        ("产物根路径", str(base_dir)),
    ]
    _add_key_value_rows_table(
        doc,
        project_rows,
        font_english,
        font_chinese,
        headers=("项目", "内容"),
        widths=(Inches(1.7), Inches(4.75)),
    )
    if history.get("report_requirements"):
        _add_styled_heading(doc, "1.3 报告补充要求", level=2, font_en=font_english, font_cn=font_chinese)
        for item in history["report_requirements"]:
            request = item.get("request") if isinstance(item, dict) else item
            _add_list_item(doc, _stringify(request) or "未提供", font_english, font_chinese)


def _add_environment_section(
    doc,
    project_profile,
    history,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc,
        "二、复现环境与算力配置",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_styled_heading(doc, "2.1 执行环境配置", level=2, font_en=font_english, font_cn=font_chinese)
    _add_key_value_rows_table(
        doc,
        _environment_profile_rows(project_profile, history),
        font_english,
        font_chinese,
        headers=("配置项", "版本 / 配置"),
        suppress_if_sparse=True,
    )
    _add_history_compute_usage_section(
        doc,
        history,
        font_english,
        font_chinese,
        heading_text="2.2 算力资源消耗对比（论文原始与本次复现）",
        heading_level=2,
    )
    _add_history_cloud_resources_section(
        doc,
        history,
        font_english,
        font_chinese,
    )


def _add_paper_overview_section(
    doc,
    scientific_context,
    history,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc,
        "三、论文模型与实验概述",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_styled_heading(doc, "3.1 模型架构与科学背景", level=2, font_en=font_english, font_cn=font_chinese)
    context = scientific_context if isinstance(scientific_context, dict) else {}
    if context:
        _add_styled_paragraph(
            doc,
            _stringify(context.get("background")) or "论文未披露或调用方未提供模型与科学背景。",
            font_en=font_english,
            font_cn=font_chinese,
        )
        innovations = context.get("innovations")
        if innovations:
            _add_styled_heading(doc, "核心创新点", level=3, font_en=font_english, font_cn=font_chinese)
            for item in _as_list(innovations):
                _add_list_item(doc, _stringify(item) or "未提供", font_english, font_chinese)
    else:
        _add_styled_paragraph(
            doc,
            _stringify(scientific_context) or "未提供论文模型与科学背景。",
            font_en=font_english,
            font_cn=font_chinese,
        )
    _add_styled_heading(doc, "3.2 复现目标", level=2, font_en=font_english, font_cn=font_chinese)
    goals = (
        context.get("reproduction_goals")
        or context.get("objectives")
        or context.get("goals")
        or [
            f"按用户确认的 {_run_plan_name(run)} 计划验证实际可执行范围。"
            for run in _history_runs(history)
        ]
    )
    for item in _as_list(goals):
        _add_list_item(doc, _stringify(item) or "未提供", font_english, font_chinese)
    _add_history_readiness_section(
        doc,
        history,
        font_english,
        font_chinese,
        heading_text="3.3 实验与执行计划概述",
        heading_level=2,
    )


def _audit_dimension_values(name, value):
    if not isinstance(value, dict):
        return name, "-", "-", _stringify(value) or "审计维度结构无效"
    score = value.get("score", value.get("earned", "-"))
    maximum = value.get("max", value.get("max_score", "-"))
    evidence = value.get("assessment") or value.get("message") or value.get("deductions") or value.get("evidence")
    if not evidence and isinstance(score, (int, float)) and score == maximum:
        evidence = "满分，无扣分项"
    return name, score, maximum, _stringify(evidence) or "缺少扣分证据（报告门禁应阻断）"


def _add_audit_summary_section(doc, history, font_english, font_chinese):
    _add_styled_heading(
        doc,
        "四、代码审计与可行性评估",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    scorecard = history.get("_audit_score") or {}
    _add_styled_heading(
        doc,
        "4.1 审计维度评分 (SciML Physics v4)",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, header in enumerate(("审计维度", "得分", "满分", "评估")):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 9, bold=True)
    _shade_cells(table.rows[0].cells, "D9EAF7")
    for dimension, value in (scorecard.get("dimensions") or {}).items():
        cells = table.add_row().cells
        for index, item in enumerate(_audit_dimension_values(dimension, value)):
            cells[index].text = str(item)
            _set_cell_font(cells[index], font_english, font_chinese, 8.5)
    _set_table_widths(table, [Inches(1.65), Inches(0.7), Inches(0.7), Inches(3.4)])
    _set_table_borders_and_margins(table)
    _add_styled_paragraph(
        doc,
        f"综合评分：{scorecard.get('overall_score', '未提供')}；最终判定：{scorecard.get('verdict', '未提供')}。",
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_styled_heading(doc, "4.2 资源与物理门控", level=2, font_en=font_english, font_cn=font_chinese)
    gates = []
    for label, key in (("资源门控", "resource_gate"), ("物理门控", "physics_gate")):
        gate = scorecard.get(key) or {}
        decision = gate.get("fit_status") or gate.get("action") or "未提供"
        details = gate.get("message") or gate.get("claim_blockers") or gate.get("execution_blockers") or "未提供"
        gates.append((label, f"{decision}；{_stringify(details)}"))
    _add_key_value_rows_table(
        doc,
        gates,
        font_english,
        font_chinese,
        headers=("门控项", "判定与详情"),
    )


def _preparation_record(run, step):
    preparation = run.get("preparation")
    record = preparation.get(step) if isinstance(preparation, dict) else None
    if not isinstance(record, dict):
        return "未提供", "未提供", "未提供"
    status = str(record.get("status") or "未提供")
    reason = str(record.get("reason") or "").strip()
    if status == "skipped_not_required":
        status = f"本步骤不需要：{reason or '计划明确不需要此资产或操作'}"
    elif status == "not_reached":
        status = f"未进入 {step}：{reason or '上游步骤未完成'}"
    elif status == "failed" and reason:
        status = f"失败：{reason}"
    return (
        status,
        record.get("fingerprint") or "未提供",
        _stringify(record.get("evidence")) or "未提供",
    )


def _add_dependency_section(doc, history, font_english, font_chinese):
    _add_styled_heading(
        doc,
        "五、依赖环境准备",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, header in enumerate(("执行轮次", "状态", "环境指纹", "证据")):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8.5, bold=True)
    _shade_cells(table.rows[0].cells, "D9EAF7")
    for run in _history_runs(history):
        status, fingerprint, evidence = _preparation_record(run, "step_4")
        cells = table.add_row().cells
        for index, value in enumerate((_run_title(run), status, fingerprint, evidence)):
            cells[index].text = str(value)
            _set_cell_font(cells[index], font_english, font_chinese, 8)
    _set_table_widths(table, [Inches(1.8), Inches(0.8), Inches(1.7), Inches(2.15)])
    _set_table_borders_and_margins(table)


def _add_data_weight_section(doc, history, base_dir, font_english, font_chinese):
    _add_styled_heading(
        doc,
        "六、数据与模型权重",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    for subsection, step in (("6.1 数据准备", "step_5"), ("6.2 模型权重准备", "step_6")):
        _add_styled_heading(doc, subsection, level=2, font_en=font_english, font_cn=font_chinese)
        rows = []
        for run in _history_runs(history):
            status, fingerprint, evidence = _preparation_record(run, step)
            rows.append((_run_title(run), f"状态={status}；指纹={fingerprint}；证据={evidence}"))
        _add_key_value_rows_table(
            doc,
            rows or [("执行轮次", "未提供")],
            font_english,
            font_chinese,
            headers=("执行轮次", "准备结果"),
            widths=(Inches(2.1), Inches(4.35)),
        )

    _add_styled_heading(doc, "6.3 数据与权重资产", level=2, font_en=font_english, font_cn=font_chinese)
    assets = []
    for run in _history_runs(history):
        manifest, manifest_base = _load_json_value_with_base(
            _history_reference(run.get("artifact_manifest"), base_dir),
            base_dir,
        )
        if not isinstance(manifest, dict):
            continue
        normalized = _normalize_artifacts(
            manifest.get("artifacts") or [],
            manifest_base,
            default_origin="artifact_manifest",
        )
        for artifact in normalized:
            role_text = " ".join(
                str(artifact.get(key) or "") for key in ("role", "name", "origin")
            ).casefold()
            if any(token in role_text for token in ("data", "dataset", "weight", "checkpoint", "model", "数据", "权重")):
                assets.append((run.get("run_id"), _run_title(run), artifact))
    if assets:
        _add_history_artifact_manifest(doc, assets, font_english, font_chinese)
    else:
        _add_styled_paragraph(
            doc,
            "资产清单中未提供可明确归类的数据或模型权重条目。",
            font_en=font_english,
            font_cn=font_chinese,
        )


def _add_output_index_appendix(
    doc,
    entries,
    output_dir,
    repo_name,
    audit_report_path,
    font_english,
    font_chinese,
):
    doc.add_page_break()
    _add_styled_heading(
        doc,
        "附录 B、输出文件索引 (Output File Index)",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_key_value_rows_table(
        doc,
        (
            ("最终复现报告", str(Path(output_dir) / f"{repo_name}_final_reproduce_report.docx")),
            ("项目审计报告", str(audit_report_path)),
        ),
        font_english,
        font_chinese,
        headers=("必交产物", "路径"),
        widths=(Inches(1.6), Inches(4.85)),
    )
    _add_history_artifact_manifest(doc, entries, font_english, font_chinese)


def _add_image_failure_table(doc, failures, font_english, font_chinese):
    _add_styled_heading(
        doc,
        "未嵌入图片及原因",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    if not failures:
        _add_styled_paragraph(
            doc,
            "所有已记录的实验图片均已成功嵌入。",
            font_en=font_english,
            font_cn=font_chinese,
        )
        return
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, header in enumerate(("图片名称", "关联执行 / 实验", "路径", "未嵌入原因")):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8, bold=True)
    _shade_cells(table.rows[0].cells, "FCE8E6")
    for artifact, reason in failures:
        cells = table.add_row().cells
        associations = (
            f"执行：{', '.join(artifact.get('run_ids') or ['未提供'])}\n"
            f"实验：{', '.join(artifact.get('case_names') or ['未提供'])}"
        )
        display_path = artifact.get("path") or artifact.get("remote_path") or "未提供"
        for index, value in enumerate(
            (
                artifact.get("name") or "未命名图片",
                associations,
                display_path,
                reason,
            )
        ):
            cells[index].text = str(value)
            _set_cell_font(cells[index], font_english, font_chinese, 7.5)
    _set_table_widths(table, [Inches(1.2), Inches(1.45), Inches(2.1), Inches(1.7)])
    _set_table_borders_and_margins(table)


def _add_all_experiment_images_appendix(
    doc,
    entries,
    font_english,
    font_chinese,
):
    doc.add_page_break()
    _add_styled_heading(
        doc,
        "附录 A、全部实验产出图片 (All Experiment Images)",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    artifacts = [
        artifact
        for artifact in _deduplicate_history_artifacts(entries)
        if _artifact_extension(artifact) in _IMAGE_EXTENSIONS
    ]
    valid_groups = {}
    failures = []
    for artifact in artifacts:
        error = _image_embedding_error(artifact)
        if error:
            failures.append((artifact, error))
            continue
        run_titles = artifact.get("run_titles") or ["未提供执行"]
        case_names = artifact.get("case_names") or ["未提供实验"]
        run_title = run_titles[0] if len(run_titles) == 1 else "跨轮次共享图片"
        case_name = case_names[0] if len(case_names) == 1 else "多实验共享图片"
        valid_groups.setdefault((run_title, case_name), []).append(artifact)

    figure_number = 0
    for (run_title, case_name), group_artifacts in valid_groups.items():
        _add_styled_heading(
            doc,
            str(run_title),
            level=2,
            font_en=font_english,
            font_cn=font_chinese,
        )
        _add_styled_heading(
            doc,
            str(case_name),
            level=3,
            font_en=font_english,
            font_cn=font_chinese,
        )
        for artifact in group_artifacts:
            title = str(artifact.get("caption") or artifact.get("name") or "实验图片")
            path = _resolved_artifact_path(artifact)
            try:
                paragraph = doc.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.paragraph_format.keep_with_next = True
                run = paragraph.add_run()
                if _artifact_extension(artifact) == ".svg":
                    _add_svg_picture(run, path, title)
                else:
                    _add_raster_picture(run, path, title)
                figure_number += 1
                _add_caption(
                    doc,
                    f"图 A-{figure_number}  {title}",
                    font_english,
                    font_chinese,
                )
                _add_styled_paragraph(
                    doc,
                    (
                        f"关联 run_id：{', '.join(artifact.get('run_ids') or ['未提供'])}；"
                        f"关联实验：{', '.join(artifact.get('case_names') or ['未提供'])}"
                    ),
                    font_en=font_english,
                    font_cn=font_chinese,
                    size_pt=8,
                    alignment=WD_ALIGN_PARAGRAPH.CENTER,
                    space_after_pt=6,
                )
            except Exception as exc:
                failures.append((artifact, f"嵌入 DOCX 失败：{exc}"))

    if not figure_number:
        _add_styled_paragraph(
            doc,
            "没有可成功嵌入的实验产出图片。",
            font_en=font_english,
            font_cn=font_chinese,
        )
    _add_styled_paragraph(
        doc,
        f"附录 A 共成功嵌入 {figure_number} 张去重后的实验产出图片。",
        font_en=font_english,
        font_cn=font_chinese,
        size_pt=9,
    )
    _add_image_failure_table(doc, failures, font_english, font_chinese)


def _add_pitfalls_section(
    doc,
    se_audit_results,
    font_english,
    font_chinese,
    *,
    heading_text="八、代码缺陷与排坑记录",
    section_prefix="8",
):
    _add_styled_heading(
        doc,
        heading_text,
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    if not isinstance(se_audit_results, dict):
        _add_styled_paragraph(
            doc,
            "本次执行台账未附加独立的部署排坑记录；完整静态审计证据见附录 C。",
            font_en=font_english,
            font_cn=font_chinese,
        )
        return

    _add_styled_heading(
        doc,
        f"{section_prefix}.1 安全与路径检查",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_key_value_rows_table(
        doc,
        (
            ("敏感密钥防泄漏", se_audit_results.get("secrets_leak", "安全合规")),
            ("代码路径污染", se_audit_results.get("path_pollution", "未发现绝对路径污染")),
        ),
        font_english,
        font_chinese,
        headers=("检查项", "结果"),
    )

    _add_styled_heading(
        doc,
        f"{section_prefix}.2 部署与推理排坑记录",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    pitfalls = se_audit_results.get("pitfalls")
    if isinstance(pitfalls, list) and pitfalls:
        table = doc.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for index, header in enumerate(("排坑编号", "部署/推理问题", "修复动作")):
            table.rows[0].cells[index].text = header
            _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 9, bold=True)
        _shade_cells(table.rows[0].cells, "D9EAF7")
        for item in pitfalls:
            item = item if isinstance(item, dict) else {"problem": item}
            cells = table.add_row().cells
            for index, value in enumerate(
                (
                    item.get("id", "-"),
                    item.get("problem", "未提供"),
                    item.get("solution", "未提供"),
                )
            ):
                cells[index].text = _stringify(value) or "未提供"
                _set_cell_font(cells[index], font_english, font_chinese, 9)
        _set_table_widths(table, [Inches(0.85), Inches(2.35), Inches(3.25)])
        _set_table_borders_and_margins(table)
    else:
        _add_styled_paragraph(
            doc,
            "未提供需要单独列示的部署或推理故障。",
            font_en=font_english,
            font_cn=font_chinese,
        )

    _add_styled_heading(
        doc,
        f"{section_prefix}.3 后续处理清单",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    healing_actions = se_audit_results.get("healing_actions")
    if isinstance(healing_actions, list) and healing_actions:
        for action in healing_actions:
            _add_list_item(
                doc,
                f"待执行：{_stringify(action) or '未提供'}",
                font_english,
                font_chinese,
            )
    else:
        _add_styled_paragraph(
            doc,
            "未提供待执行的路径或依赖自愈动作。",
            font_en=font_english,
            font_cn=font_chinese,
        )


def _add_conclusions_section(
    doc,
    history,
    history_base_dir,
    repro_conclusions,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc,
        "九、论文复现结论",
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    _add_history_scientific_validation_section(
        doc,
        history,
        history_base_dir,
        font_english,
        font_chinese,
        heading_text="9.1 数值、统计与物理验证结论",
        heading_level=2,
    )
    _add_styled_heading(
        doc,
        "9.2 论文基准与本次结果对比",
        level=2,
        font_en=font_english,
        font_cn=font_chinese,
    )
    conclusions = _filter_history_conclusions(history, repro_conclusions)
    if not isinstance(conclusions, list) or not conclusions:
        _add_styled_paragraph(
            doc,
            "本次未提供可验证的论文基准对比表，不据此提高科学复现声明等级。",
            font_en=font_english,
            font_cn=font_chinese,
        )
        return
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ("复现案例 / 任务", "原论文 / 标称基准", "本次实际结果", "物理特性 / 判定")
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 8.5, bold=True)
    _shade_cells(table.rows[0].cells, "D9EAF7")
    for item in conclusions:
        item = item if isinstance(item, dict) else {"case_name": item}
        cells = table.add_row().cells
        for index, value in enumerate(
            (
                item.get("case_name", "-"),
                item.get("paper_data", "-"),
                item.get("repro_data", "-"),
                item.get("status", "-"),
            )
        ):
            cells[index].text = _stringify(value) or "-"
            _set_cell_font(cells[index], font_english, font_chinese, 8.5)
    _set_table_widths(table, [Inches(1.25), Inches(1.75), Inches(2.15), Inches(1.3)])
    _set_table_borders_and_margins(table)


def _add_recommendations_section(
    doc,
    recommendations,
    font_english,
    font_chinese,
    *,
    heading_text="十一、后续优化建议",
    section_prefix="11",
):
    _add_styled_heading(
        doc,
        heading_text,
        level=1,
        font_en=font_english,
        font_cn=font_chinese,
    )
    sections = (
        (f"{section_prefix}.1 未完成或部分完成项的原因", "unfinished_reasons"),
        (f"{section_prefix}.2 内存与显存问题处理建议", "oom_fix"),
        (f"{section_prefix}.3 指标与验证证据补齐建议", "metrics_completion"),
        (f"{section_prefix}.4 后续训练、推理与算力优化建议", "training_advices"),
    )
    for heading, key in sections:
        _add_styled_heading(
            doc,
            heading,
            level=2,
            font_en=font_english,
            font_cn=font_chinese,
        )
        value = recommendations.get(key) if isinstance(recommendations, dict) else None
        items = value if isinstance(value, list) else str(value or "").splitlines()
        items = [str(item).strip() for item in items if str(item).strip()]
        if not items:
            _add_styled_paragraph(
                doc,
                "未提供。",
                font_en=font_english,
                font_cn=font_chinese,
            )
            continue
        for item in items:
            _add_list_item(doc, item, font_english, font_chinese)


def _add_compute_usage_chapter(
    doc,
    history,
    font_english,
    font_chinese,
):
    _add_styled_heading(
        doc, "九、算力使用统计", level=1,
        font_en=font_english, font_cn=font_chinese,
    )
    _add_history_compute_usage_section(
        doc,
        history,
        font_english,
        font_chinese,
        heading_text="9.1 算力资源消耗对比与各轮论文可比性",
        heading_level=2,
    )


def _scope_value(scope_detail, names):
    if not isinstance(scope_detail, dict):
        return ""
    for name in names:
        if scope_detail.get(name) not in (None, "", [], {}):
            return _stringify(scope_detail[name])
    for value in scope_detail.values():
        found = _scope_value(value, names)
        if found:
            return found
    return ""


def _run_comparison_scope(run):
    usage = run.get("compute_usage")
    reproduction = usage.get("reproduction") if isinstance(usage, dict) else {}
    if not isinstance(reproduction, dict):
        reproduction = {}
    detail = reproduction.get("scope_detail") if isinstance(reproduction, dict) else {}
    return {
        "scope": _stringify(reproduction.get("scope")) or "未提供",
        "dataset": _scope_value(detail, ("dataset", "data", "case")) or "未提供",
        "evaluation_protocol": _scope_value(
            detail,
            ("evaluation_protocol", "protocol", "evaluation"),
        ) or "未提供",
    }


def _metric_value(item):
    if not isinstance(item, dict):
        return None
    for key in (
        "measured_value", "repro_data", "value", "actual", "result", "score",
    ):
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _run_metric_records(run, base_dir):
    plan_items = _extract_execution_plan_items(_run_plan_selection(run))
    sources = {}
    for item in plan_items:
        if not isinstance(item, dict):
            continue
        label = _case_label(item)
        sources[_normalize_name(label)] = str(
            item.get("model_source") or "not_applicable"
        )
    scope = _run_comparison_scope(run)
    records = []
    outcomes = run.get("actual_test_outcomes")
    if isinstance(outcomes, list):
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            case_name = _case_label(outcome)
            metrics = outcome.get("metrics")
            if isinstance(metrics, dict):
                iterable = [
                    {"metric": name, "value": value}
                    for name, value in metrics.items()
                ]
            elif isinstance(metrics, list):
                iterable = metrics
            else:
                iterable = []
            for metric in iterable:
                if not isinstance(metric, dict):
                    continue
                name = str(metric.get("metric") or metric.get("name") or "").strip()
                value = _metric_value(metric)
                if name and value not in (None, ""):
                    records.append({
                        "run_id": run.get("run_id"),
                        "case_name": case_name,
                        "model_source": sources.get(_normalize_name(case_name), "not_applicable"),
                        "metric": name,
                        "value": value,
                        **scope,
                    })
    validation, _ = _load_json_value_with_base(
        _history_reference(run.get("validation_result"), base_dir),
        base_dir,
    )
    metrics = (
        ((validation.get("phases") or {}).get("quantitative_validation") or {}).get("metrics")
        if isinstance(validation, dict)
        else None
    )
    if isinstance(metrics, list):
        default_case = _case_label(plan_items[0]) if len(plan_items) == 1 else ""
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            case_name = str(metric.get("case_name") or default_case or "").strip()
            name = str(metric.get("metric") or metric.get("name") or "").strip()
            value = _metric_value(metric)
            if case_name and name and value not in (None, ""):
                record = {
                    "run_id": run.get("run_id"),
                    "case_name": case_name,
                    "model_source": sources.get(_normalize_name(case_name), "not_applicable"),
                    "metric": name,
                    "value": value,
                    **scope,
                }
                if not any(
                    existing["case_name"] == case_name
                    and existing["metric"] == name
                    and existing["value"] == value
                    for existing in records
                ):
                    records.append(record)
    return records


def _add_model_source_comparison_chapter(
    doc,
    history,
    base_dir,
    font_english,
    font_chinese,
    *,
    heading_text="十、自训练模型 vs 预训练模型对比",
    heading_level=1,
):
    _add_styled_heading(
        doc, heading_text, level=heading_level,
        font_en=font_english, font_cn=font_chinese,
    )
    records = [
        record
        for run in _history_runs(history)
        for record in _run_metric_records(run, base_dir)
        if record.get("model_source") in {"self_trained", "pretrained"}
    ]
    grouped = {}
    for record in records:
        key = (
            _normalize_name(record["case_name"]),
            _normalize_name(record["metric"]),
            _normalize_name(record["scope"]),
            _normalize_name(record["dataset"]),
            _normalize_name(record["evaluation_protocol"]),
        )
        grouped.setdefault(key, {"self_trained": [], "pretrained": []})
        grouped[key][record["model_source"]].append(record)
    comparable = [
        (key, value)
        for key, value in grouped.items()
        if value["self_trained"] and value["pretrained"]
    ]
    if not comparable:
        sources = sorted({record.get("model_source") for record in records})
        if not records:
            reason = (
                "执行计划或实测指标没有同时提供 model_source、案例、指标、数据集和评估协议证据，"
                "因此本章不计算自训练与预训练差值。"
            )
        elif len(sources) < 2:
            reason = (
                f"仅记录到 {sources[0] if sources else '一种'} 模型来源，"
                "缺少同案例的另一模型来源结果，不能直接比较。"
            )
        else:
            reason = (
                "虽记录了两类模型来源，但案例、数据集、评估协议或指标不一致，"
                "不能合并计算差值。"
            )
        _add_styled_paragraph(
            doc,
            reason,
            font_en=font_english,
            font_cn=font_chinese,
        )
        return
    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = (
        "案例", "数据集", "评估协议", "指标",
        "自训练模型", "预训练模型", "对比结论",
    )
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = header
        _set_cell_font(table.rows[0].cells[index], font_english, font_chinese, 7.5, bold=True)
    _shade_cells(table.rows[0].cells, "FFF2CC")
    for _, group in comparable:
        self_record = group["self_trained"][-1]
        pretrained_record = group["pretrained"][-1]
        self_value = self_record["value"]
        pretrained_value = pretrained_record["value"]
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (self_value, pretrained_value)
        ):
            conclusion = f"自训练 - 预训练 = {self_value - pretrained_value:.6g}"
        else:
            conclusion = "同协议结果并列展示；非数值结果不计算差值"
        cells = table.add_row().cells
        values = (
            self_record["case_name"],
            self_record["dataset"],
            self_record["evaluation_protocol"],
            self_record["metric"],
            self_value,
            pretrained_value,
            conclusion,
        )
        for index, value in enumerate(values):
            cells[index].text = _stringify(value) or "-"
            _set_cell_font(cells[index], font_english, font_chinese, 7.5)
    _set_table_widths(
        table,
        [
            Inches(0.95), Inches(0.9), Inches(1.1), Inches(0.8),
            Inches(0.85), Inches(0.85), Inches(1.15),
        ],
    )
    _set_table_borders_and_margins(table)


def generate_report(
    repo_name,
    project_profile,               # 封面与执行摘要项目档案 (dict)
    scientific_context=None,       # 第三章科学背景、创新与目标 (dict)
    readiness_matrix=None,         # 兼容参数；真实值以执行台账为准
    actual_test_outcomes=None,     # 兼容参数；真实值以执行台账为准
    repro_conclusions=None,        # 第九章学术复现比对结论 (list)
    se_audit_results=None,         # 第八章代码缺陷与排坑记录 (dict)
    repro_recommendations=None,    # 第十章未完成项原因与后续建议 (dict)
    output_dir=None,               # 归一化输出路径
    font_english='Times New Roman',
    font_chinese='SimSun',
    compute_usage=None,            # 兼容参数；第二章算力数据以执行台账为准
    execution_plan_selection=None, # 兼容参数；用户选择计划以执行台账为准
    validation_result=None,        # step_7 validation_result.json 或已解析对象
    scientific_repro_contract=None,# step_1 scientific_repro_contract.json
    artifact_manifest=None,        # step_5/6 artifact_manifest.json
    experiment_details=None,       # 兼容参数；第七章详情以执行台账为准
    execution_run_history=None,    # 逻辑必填：step_7.5 授权与事实台账
):
    """
    生成与标准报告同构的最终 Word 报告。

    字体规范是不可覆盖的报告标准：中文统一宋体（SimSun），拉丁字符统一
    Times New Roman。保留 font_english/font_chinese 参数仅用于兼容旧调用方。
    """
    try:
        font_english = REPORT_FONT_ENGLISH
        font_chinese = REPORT_FONT_CHINESE
        history_fallback = output_dir or resolve_workspace_root()
        run_history, run_history_base = _load_execution_run_history(
            execution_run_history,
            history_fallback,
        )
        if (
            isinstance(run_history, dict)
            and str(run_history.get("project_name") or "").strip() != str(repo_name).strip()
        ):
            raise ValueError(
                "execution_run_history.project_name 与 repo_name 不一致，"
                "无法组装同一项目的最终产物集"
            )
        audit_report_path = run_history["_audit_report_path"]
        audit_report_text = run_history["_audit_report_text"]
        gate_run_id = str(run_history.get("report_gate", {}).get("run_id") or "")
        authorized_run = next(
            (
                run
                for run in run_history.get("runs", [])
                if str(run.get("run_id") or "") == gate_run_id
            ),
            None,
        )
        if not isinstance(authorized_run, dict):
            raise ValueError("报告授权缺少对应 run_id")
        step_dirs = authorized_run.get("step_dirs")
        if not isinstance(step_dirs, dict) or not str(step_dirs.get("step_8") or "").strip():
            raise ValueError("授权 run 缺少 step_8 输出目录")
        canonical_output = Path(str(step_dirs["step_8"])).expanduser()
        if not canonical_output.is_absolute():
            canonical_output = Path(run_history_base) / canonical_output
        output_dir = str(canonical_output.resolve())
        os.makedirs(output_dir, exist_ok=True)

        doc = docx.Document()
        _set_default_font(doc, font_english, font_chinese, 11)
        _add_standard_cover(
            doc,
            repo_name,
            project_profile,
            run_history,
            font_english,
            font_chinese,
        )
        _add_standard_contents(doc, font_english, font_chinese)
        prepared_runs, artifact_entries = _prepare_history_experiment_payload(
            run_history,
            run_history_base,
            output_dir,
        )
        body_image_state = _select_body_image_identities(artifact_entries)
        _add_execution_summary(
            doc,
            run_history,
            run_history_base,
            font_english,
            font_chinese,
        )
        _add_environment_section(
            doc,
            project_profile,
            run_history,
            font_english,
            font_chinese,
        )
        _add_paper_overview_section(
            doc,
            scientific_context,
            run_history,
            font_english,
            font_chinese,
        )
        _add_audit_summary_section(doc, run_history, font_english, font_chinese)
        _add_dependency_section(doc, run_history, font_english, font_chinese)
        _add_data_weight_section(
            doc,
            run_history,
            run_history_base,
            font_english,
            font_chinese,
        )
        _add_styled_heading(
            doc, "七、执行详情与产物", level=1,
            font_en=font_english, font_cn=font_chinese,
        )
        _add_history_outcomes_section(
            doc,
            run_history,
            font_english,
            font_chinese,
            heading_text="7.1 实际测试结果与真实耗时",
            heading_level=2,
        )
        _add_experiment_details_section(
            doc,
            run_history,
            run_history_base,
            output_dir,
            font_english,
            font_chinese,
            heading_text="7.2 实验详情与关键产物",
            heading_level=2,
            prepared_runs=prepared_runs,
            all_entries=artifact_entries,
            body_image_state=body_image_state,
        )
        _add_pitfalls_section(
            doc,
            se_audit_results,
            font_english,
            font_chinese,
            heading_text="八、代码缺陷与排坑自愈指南",
            section_prefix="8",
        )
        _add_conclusions_section(
            doc,
            run_history,
            run_history_base,
            repro_conclusions,
            font_english,
            font_chinese,
        )
        _add_model_source_comparison_chapter(
            doc,
            run_history,
            run_history_base,
            font_english,
            font_chinese,
            heading_text="9.3 自训练模型 vs 预训练模型对比",
            heading_level=2,
        )
        _add_recommendations_section(
            doc,
            repro_recommendations,
            font_english,
            font_chinese,
            heading_text="十、后续建议",
            section_prefix="10",
        )
        _add_all_experiment_images_appendix(
            doc,
            artifact_entries,
            font_english,
            font_chinese,
        )
        _add_output_index_appendix(
            doc,
            artifact_entries,
            output_dir,
            repo_name,
            audit_report_path,
            font_english,
            font_chinese,
        )
        _add_audit_report_appendix(
            doc,
            audit_report_text,
            audit_report_path,
            font_english,
            font_chinese,
        )

        _enforce_document_fonts(doc, font_english, font_chinese)
        docx_path = os.path.join(output_dir, f"{repo_name}_final_reproduce_report.docx")
        doc.save(docx_path)

        result = (
            "✅ 最终产物生成成功！已按标准报告模块生成 Word 报告，"
            "并强制使用中文宋体、英文 Times New Roman："
            f"\n  📄 DOCX: `{docx_path}`"
        )
        result += f"\n  🧾 审计报告: `{audit_report_path}`"
        return result

    except Exception as e:
        return f"❌ 报告生成遭遇底层失败：{str(e)}"
