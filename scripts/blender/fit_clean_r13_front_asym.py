import bpy, numpy as np
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
def arr(o): return np.array([tuple(v.co) for v in o.data.vertices],float)
def put(o,a):
 for i,v in enumerate(o.data.vertices): v.co=a[i]
# Shift facial block slightly toward image-right (-X in world for locked front camera).
for n in ('Sculpted_Head_MASTER','Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER','Pink_Nose_MASTER','Recessed_Philtrum_MASTER','Mouth_Left_MASTER','Mouth_Right_MASTER','Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
 o=bpy.data.objects.get(n)
 if o: a=arr(o); a[:,0]-=.0016; put(o,a)
# Lower torso asymmetry: reference has much less image-right mass. Image-right corresponds world -X.
o=bpy.data.objects['Rounded_Torso_MASTER']; a=arr(o); z=a[:,2]; low=np.clip((.075-z)/.045,0,1); neg=a[:,0]<0; pos=~neg
a[neg,0]*=(1-.18*low[neg]); a[pos,0]*=(1-.04*low[pos]); put(o,a)
# Right/world-negative haunch inward/narrower; keep left haunch/tail dominant.
o=bpy.data.objects['Haunch_Right_MASTER']; a=arr(o); c=(a.min(0)+a.max(0))/2; a[:,0]=(a[:,0]-c[0])*.78+c[0]+.0025; put(o,a)
o=bpy.data.objects['Haunch_Left_MASTER']; a=arr(o); c=(a.min(0)+a.max(0))/2; a[:,0]=(a[:,0]-c[0])*.96+c[0]+.001; put(o,a)
o=bpy.data.objects['Curled_tail_MASTER']; a=arr(o); a[:,0]+=.0025; put(o,a)
out=P/'source/cat_fit_r13.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('FIT_R13_SAVED',out)