import bpy
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
center_z=0.115
# Local head-height correction only. Body is frozen at r41.
names=('Sculpted_Head_MASTER','Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER','Pink_Nose_MASTER','Recessed_Philtrum_MASTER','Mouth_Left_MASTER','Mouth_Right_MASTER','Pupil_Left_DECOR','Pupil_Right_DECOR')
for n in names:
    o=bpy.data.objects.get(n)
    if not o: continue
    for v in o.data.vertices:
        v.co.z=center_z+(v.co.z-center_z)*1.06
# slight skull depth increase, only head shell, to avoid flattened profile
head=bpy.data.objects.get('Sculpted_Head_MASTER')
if head:
    ys=[v.co.y for v in head.data.vertices]; cy=(min(ys)+max(ys))/2
    for v in head.data.vertices: v.co.y=cy+(v.co.y-cy)*1.025
bpy.ops.wm.save_as_mainfile(filepath=str(P/'source/cat_fit_r42.blend'))
print('R42 local head height/depth refinement')