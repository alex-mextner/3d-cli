import bpy, math
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'
# r5: reduce outward ear yaw from 32° to ~18° so both inner pink panels remain visible in the hero 3/4 view.
for name in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
    o=bpy.data.objects[name]; side=1 if 'Left' in name else -1
    zs=[v.co.z for v in o.data.vertices]; z0=min(zs); roots=[v.co for v in o.data.vertices if v.co.z<z0+0.004]
    px=sum(v.x for v in roots)/len(roots); py=sum(v.y for v in roots)/len(roots); a=math.radians(side*14.0)
    for v in o.data.vertices:
        dx,dy=v.co.x-px,v.co.y-py; v.co.x=px+math.cos(a)*dx-math.sin(a)*dy; v.co.y=py+math.sin(a)*dx+math.cos(a)*dy
out=PROJ/'source/cat_fit_r5.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r5.glb'),export_format='GLB')
print('FIT_R5_SAVED',out)