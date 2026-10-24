"""The assembly toolkit behind `3d assembly` (umbrella #570, slice S3 #574).

Import-light on purpose (stdlib only): the heavy modules (`scene`, `overlap`, `sweep`, ...) import trimesh /
manifold3d / numpy lazily or at their own top level and are only imported from inside a command's `run()`.
"""
