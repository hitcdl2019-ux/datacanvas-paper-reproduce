# 结果图复现接口

## `paper_result_figure_specs.json`

根对象包含 `schema_version=1.0`、论文信息和 `figures`。每个 figure 使用：

```json
{
  "figure_id": "Fig. 4",
  "caption": "论文图注",
  "source_page": "p. 8",
  "reference_image_path": "images/result_figures/fig_4.png",
  "category": "experimental_result",
  "case_name": "Case 1",
  "status": "ready",
  "reproduction_evidence": [
    {"text": "论文或附录中的绘制方法", "source_page": "p. 7", "section": "Experiments"}
  ],
  "renderer": "generic",
  "data_requirements": [
    {
      "id": "x",
      "path": "run-001/step_7/raw/x.npy",
      "variable": "x",
      "coordinates": "streamwise coordinate",
      "units": "m",
      "sampling_range": [0, 1],
      "model_source": "self_trained"
    }
  ],
  "transforms": [
    {"binding": "y", "op": "scale", "value": 0.001, "evidence": "converted to kPa", "source_page": "p. 7"}
  ],
  "layout": {"rows": 1, "cols": 1, "width_inches": 7.0, "height_inches": 4.5},
  "panels": [
    {
      "title": "Panel a",
      "chart_type": "line",
      "x_binding": "x",
      "series": [{"binding": "y", "label": "Prediction", "color": "#1f77b4", "linestyle": "-", "marker": ""}],
      "x_axis": {"label": "x", "unit": "m", "scale": "linear", "limits": [0, 1], "ticks": [0, 0.5, 1]},
      "y_axis": {"label": "p", "unit": "kPa", "scale": "linear", "limits": [-1, 1], "ticks": [-1, 0, 1]},
      "legend": {"show": true, "location": "best"}
    }
  ],
  "blocking_reasons": []
}
```

`chart_type` 支持 `line`、`scatter`、`bar`、`histogram`、`pdf`、`heatmap`、`contour`、`field`。热图、等值线和场图使用 `z_binding`，并提供 `colorbar.label`、`colorbar.unit`、`colorbar.limits`、`colorbar.ticks`。

允许的 transform 为 `scale`、`offset`、`abs`、`normalize`、`denormalize`、`slice`、`mean`、`std`、`histogram`。每个 transform 都必须有非空 `evidence` 与 `source_page`。

## `figure_reproduction_result.json`

根对象包含 `schema_version=1.0`、`overall_status` 和 `figures`。每个结果记录 `figure_id`、`case_name`、`status`、`blocking_reasons`、`input_artifacts`、`applied_transforms`、`compliance`、`output_artifacts` 及可选的官方脚本证据。

`compliance` 固定检查 `coordinates`、`units`、`axis_scales`、`axis_limits`、`ticks`、`legend`、`layout`、`colorbar`。`passed` 要求所有适用检查通过。
