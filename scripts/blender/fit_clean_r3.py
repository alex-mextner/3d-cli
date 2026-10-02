import bpy
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'
# r3: correct the ear's side-profile lean. Baseline tips were ~10-12 mm too far toward the rear of the skull.
for name in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
    o=bpy.data.objects[name]
    zs=[v.co.z for v in o.data.vertices]; z0,z1=min(zs),max(zs)
    for v in o.data.vertices:
        t=max(0.0,min(1.0,(v.co.z-z0)/max(z1-z0,1e-6)))
        v.co.y += 0.0105*(t**1.15)
# Keep closed MASTER topology untouched; only vertex positions change.
out=PROJ/'source/cat_fit_r3.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(out))
bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r3.glb'),export_format='GLB')
print('FIT_R3_SAVED',out)