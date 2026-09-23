# Scientific reproduction interfaces

All paths may be absolute or relative to the JSON file that refers to them.

## `scientific_repro_contract.json`

Required top-level fields are `schema_version`, `project_profile`,
`reproduction_layers`, `physics_spec`, `runtime_requirements`, and
`physics_gate`. Every `physics_spec` item records `status`, `values`, and
`evidence`; absence is explicit and never filled from model memory.

## `artifact_manifest.json`

Required fields are `schema_version`, `created_at`, `roots`, and `artifacts`.
Each artifact records its role, path, size, SHA-256 checksum, origin, and
optional physical metadata such as units, coordinates, mesh, case, and time.

## `validation_spec.json` and `validation_result.json`

The validation spec contains `reference`, `prediction`, `comparison`,
`physics_checks`, and `paper_scope_match`. Reference and prediction condition
objects use these comparison-critical keys when applicable:

- `reynolds_number`
- `mesh`
- `time_step`
- `boundary_conditions`
- `ensemble_size`
- `coordinates`, `time_indices`, `variable_order`, `units`, `normalization`

Different values block direct comparison. A mesh mismatch can proceed only
when `comparison.interpolation.allowed=true` and the interpolation method and
estimated interpolation error are recorded.

Tolerances are objects such as:

```json
{
  "rmse": {
    "operator": "<=",
    "value": 0.02,
    "source": "paper",
    "evidence": "Table 2, p. 8"
  }
}
```

Allowed sources are `paper`, `supplementary_material`, `official_code`, and
`user_confirmed`. Without one of these sources a metric is reported but cannot
pass a quantitative gate.

`scope_detail` v2 classifies work under `training`, `inference`,
`numerical_simulation`, `experimental_fitting`, and `coupled_simulation`.
Readers must continue accepting the legacy flat ML fields.

