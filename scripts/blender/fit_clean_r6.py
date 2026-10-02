import bpy
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'
# Restore facial relief: nose/mouth were being swallowed by the unified muzzle surface.
for name,dy in [('Pink_Nose_MASTER',0.0017),('Mouth_Left_MASTER',0.00135),('Mouth_Right_MASTER',0.00135),('Recessed_Philtrum_MASTER',0.00135)]:
    o=bpy.data.objects.get(name)
    if o:
        for v in o.data.vertices: v.co.y += dy
# Broaden ear roots while preserving tip positions: reference ears are triangular, not needle-like.
for name in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
    o=bpy.data.objects[name]; xs=[v.co.x for v in o.data.vertices]; zs=[v.co.z for v in o.data.vertices]
    cx=(min(xs)+max(xs))/2; z0,z1=min(zs),max(zs)
    for v in o.data.vertices:
        t=max(0.0,min(1.0,(v.co.z-z0)/max(z1-z0,1e-6))); factor=1.16-0.16*t
        v.co.x=cx+(v.co.x-cx)*factor
out=PROJ/'source/cat_fit_r6.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r6.glb'),export_format='GLB')
print('FIT_R6_SAVED',out)