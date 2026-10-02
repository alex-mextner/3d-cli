import bpy, math
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'
head=bpy.data.objects['Sculpted_Head_MASTER']
idx={m.name:i for i,m in enumerate(head.data.materials)}
iv={vi for p in head.data.polygons if p.material_index==idx['Ivory'] for vi in p.vertices}
# Build two rounded muzzle lobes in the same closed head mesh.
for i in iv:
    v=head.data.vertices[i]; ax=abs(v.co.x)
    lobe=math.exp(-((ax-0.0080)/0.0048)**2) * math.exp(-((v.co.z-0.1045)/0.0085)**2)
    center=math.exp(-(ax/0.0038)**2) * math.exp(-((v.co.z-0.105)/0.009)**2)
    front=max(0.0,min(1.0,(v.co.y-0.039)/0.013))
    v.co.y += front*(0.0018*lobe-0.00055*center)
    v.co.x *= 1.0 + 0.035*lobe
    v.co.z += 0.00045*lobe
# Tuck the dark lower central jaw behind the white lobes rather than making a heavy ring.
for i,v in enumerate(head.data.vertices):
    if i in iv: continue
    if abs(v.co.x)<0.015 and v.co.z<0.105 and v.co.y>0.040:
        w=(1-abs(v.co.x)/0.015)*max(0.0,min(1.0,(0.105-v.co.z)/0.014))
        v.co.y -= 0.00125*w
out=PROJ/'source/cat_fit_r10.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r10.glb'),export_format='GLB')
print('FIT_R10_SAVED',out)