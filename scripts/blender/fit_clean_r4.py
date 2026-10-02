import bpy, math
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'
head=bpy.data.objects['Sculpted_Head_MASTER']
# Taper the lower cheeks/chin while keeping the upper cranium broad.
for v in head.data.vertices:
    w=max(0.0,min(1.0,(0.114-v.co.z)/0.020))
    v.co.x *= (1.0-0.075*w)
# Compact skull depth; restore forward projection on vertices participating in the Ivory muzzle.
for v in head.data.vertices:
    cy=0.0273; v.co.y=cy+(v.co.y-cy)*0.93
iv_idx=next(i for i,m in enumerate(head.data.materials) if m.name=='Ivory')
iv={vi for p in head.data.polygons if p.material_index==iv_idx for vi in p.vertices}
for i in iv:
    v=head.data.vertices[i]; front=max(0.0,min(1.0,(v.co.y-0.038)/0.013)); v.co.y += 0.0023*front
# Slant eye sockets to match the reference almond axis: outer corners slightly higher.
er_idx=next(i for i,m in enumerate(head.data.materials) if m.name=='EyeRim')
er={vi for p in head.data.polygons if p.material_index==er_idx for vi in p.vertices}
for i in er:
    v=head.data.vertices[i]; side=1 if v.co.x>=0 else -1; cx=side*0.0123; cz=0.1164; a=math.radians(-side*7.0)
    dx,dz=v.co.x-cx,v.co.z-cz; v.co.x=cx+math.cos(a)*dx+math.sin(a)*dz; v.co.z=cz-math.sin(a)*dx+math.cos(a)*dz
# Apply the same axis tilt to each closed eye MASTER.
for name in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
    o=bpy.data.objects[name]; xs=[v.co.x for v in o.data.vertices]; zs=[v.co.z for v in o.data.vertices]
    cx=(min(xs)+max(xs))/2; cz=(min(zs)+max(zs))/2; side=1 if cx>0 else -1; a=math.radians(-side*7.0)
    for v in o.data.vertices:
        dx,dz=v.co.x-cx,v.co.z-cz; v.co.x=cx+math.cos(a)*dx+math.sin(a)*dz; v.co.z=cz-math.sin(a)*dx+math.cos(a)*dz
# Rotate ears around their seated roots so the near ear exposes a real side plane instead of an edge-on blade.
for name in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
    o=bpy.data.objects[name]; side=1 if 'Left' in name else -1
    zs=[v.co.z for v in o.data.vertices]; z0=min(zs); roots=[v.co for v in o.data.vertices if v.co.z<z0+0.004]
    px=sum(v.x for v in roots)/len(roots); py=sum(v.y for v in roots)/len(roots); a=math.radians(-side*32.0)
    for v in o.data.vertices:
        dx,dy=v.co.x-px,v.co.y-py; v.co.x=px+math.cos(a)*dx-math.sin(a)*dy; v.co.y=py+math.sin(a)*dx+math.cos(a)*dy
out=PROJ/'source/cat_fit_r4.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r4.glb'),export_format='GLB')
print('FIT_R4_SAVED',out)