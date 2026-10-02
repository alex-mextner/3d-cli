import bpy, numpy as np
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
def a(o): return np.array([tuple(v.co) for v in o.data.vertices],float)
def put(o,q):
 for i,v in enumerate(o.data.vertices): v.co=q[i]
# Move lower mass backward (-Y) and slightly to reference-dominant image-left (+X).
for n in ('Rounded_Torso_MASTER','Haunch_Left_MASTER','Haunch_Right_MASTER','Hind_paw_Left_MASTER','Hind_paw_Right_MASTER','Curled_tail_MASTER'):
 o=bpy.data.objects[n]; q=a(o); q[:,1]-=.0045; q[:,0]+=.0010; put(o,q)
# Head block a little farther image-right (-X) and slightly backward to line up with torso in side view.
for n in ('Sculpted_Head_MASTER','Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER','Pink_Nose_MASTER','Recessed_Philtrum_MASTER','Mouth_Left_MASTER','Mouth_Right_MASTER','Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
 o=bpy.data.objects.get(n)
 if o:
  q=a(o); q[:,0]-=.0010; q[:,1]-=.0012; put(o,q)
out=P/'source/cat_fit_r14.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('FIT_R14_SAVED',out)