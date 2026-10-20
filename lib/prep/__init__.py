"""prep — model -> print-ready pipeline (`3d prep`, ticket #431).

Each step lives in its own module and returns a result object that follows `contracts.StepResult`
(`verdict`, `to_dict()`, `summary()`), so the pipeline can cache it as JSON and the proof pack can
render it without knowing the step's internals. Package import stays light; heavy deps
(numpy/trimesh/scipy) are imported by the step modules themselves.
"""
