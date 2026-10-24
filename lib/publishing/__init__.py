"""publishing — put a model project (3mf + scad source + stl + other CAD files) on MakerWorld,
Printables and Thingiverse, with the checks that must pass first.

`licenses` parses licence texts and answers "may this be remixed", `spec` loads the project's
publish.yaml and selects its files, `remix` decides whether a remix of another model may be
published, `plan` turns all of it into a per-platform plan (fields + exact steps) that
`3d models publish` prints as the dry run, `execute` carries a plan out (Thingiverse REST API,
agent-browser for the two sites without a public upload API).
Stdlib only at import time; pyyaml is imported inside `spec.load_spec`.
"""
