import bpy, json
from pathlib import Path
out={}
for o in bpy.data.objects:
    if o.type!='MESH': continue
    vs=[o.matrix_world @ v.co for v in o.data.vertices]
    out[o.name]={
      'n':len(vs),
      'min':[min(v[i] for v in vs) for i in range(3)],
      'max':[max(v[i] for v in vs) for i in range(3)],
    }
Path('/Users/ultra/Downloads/cat_blender_work/inspect.json').write_text(json.dumps(out,indent=2))
print('objects',len(out))