"""colour_regions — colour regions derived from per-face labels on a mesh surface (`3d colour-regions`).

Import-light on purpose (`tests/test_architecture_structure.py`): the submodules import numpy/scipy/trimesh,
so reach them as `colour_regions.build`, `colour_regions.labels`, ... and never from here.
"""

#: the command name in every structured error header
CMD = "colour-regions"
