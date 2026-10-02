import bpy, math
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'

def mat_vertices(obj, mat_name):
    idx=next(i for i,m in enumerate(obj.data.materials) if m.name==mat_name)
    return {vi for p in obj.data.polygons if p.material_index==idx for vi in p.vertices}

def bbox_center(obj):
    xs=[v.co.x for v in obj.data.vertices]; ys=[v.co.y for v in obj.data.vertices]; zs=[v.co.z for v in obj.data.vertices]
    return ((min(xs)+max(xs))/2,(min(ys)+max(ys))/2,(min(zs)+max(zs))/2)

# Narrow the visible sockets vertically while keeping the head one closed MASTER shell.
head=bpy.data.objects['Sculpted_Head_MASTER']; eye_vids=mat_vertices(head,'EyeRim')
for i in eye_vids:
    v=head.data.vertices[i]; side=1 if v.co.x>=0 else -1
    cx=side*0.0123; cz=0.1164
    v.co.x=cx+(v.co.x-cx)*1.04
    v.co.z=cz+(v.co.z-cz)*0.70

# Iris+pupil are already one closed eye MASTER per side: make them feline almonds.
for name in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
    o=bpy.data.objects[name]; cx,cy,cz=bbox_center(o); side=1 if cx>0 else -1
    for v in o.data.vertices:
        v.co.x=cx+(v.co.x-cx)*1.22 + side*0.00055
        v.co.z=cz+(v.co.z-cz)*0.60
        v.co.y += 0.00015
# Give ears real side-view breadth without opening them: scale depth around each root and lean tips slightly forward.
for name in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
    o=bpy.data.objects[name]
    zs=[v.co.z for v in o.data.vertices]; z0,z1=min(zs),max(zs)
    ys=[v.co.y for v in o.data.vertices]; cy=sum(ys)/len(ys)
    for v in o.data.vertices:
        t=max(0.0,min(1.0,(v.co.z-z0)/max(z1-z0,1e-6)))
        v.co.y=cy+(v.co.y-cy)*1.35 + 0.0012*t

# Slightly compact the skull in width while preserving depth/height; reference is less round than the baseline.
for v in head.data.vertices:
    v.co.x *= 0.975

# Preserve exact total height by only clamping accidental overshoot at ear tips.
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150

out=PROJ/'source/cat_fit_r2.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(out))
bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r2.glb'),export_format='GLB')
print('FIT_R2_SAVED',out)