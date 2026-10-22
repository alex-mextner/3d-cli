"""threed_vision: photo -> 3D primitives shared by the `3d` CLI and the hsw-editor vision service.

Every stage is a pure module with JSON-serialisable results; heavy dependencies (torch, sam2,
transformers, onnxruntime) are imported lazily inside the functions that need them, so importing a
stage never pulls the whole stack. See packages/threed-vision/README.md for the stage map.
"""
__version__ = "0.1.0"
