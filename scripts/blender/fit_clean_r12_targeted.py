import bpy, numpy as np
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
def coords(o): return np.array([tuple(v.co) for v in o.data.vertices],dtype=float)
def setc(o,q):
 for i,v in enumerate(o.data.vertices): v.co=q[i]
# Head: keep crown width, narrow cheek/lower skull; add depth high on skull and reduce low jaw depth.
o=bpy.data.objects['Sculpted_Head_MASTER']; a=coords(o); q=a.copy(); z=a[:,2]; wx=np.clip((z-.112)/.018,0,1); sx=.94+.06*wx; q[:,0]*=sx
yc=.027; sy=np.where(z>.122,1.055,np.where(z<.112,.945,1.0)); q[:,1]=(q[:,1]-yc)*sy+yc; setc(o,q)
# Ears slightly farther out; retain current watertight ear geometry.
for n in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
 o=bpy.data.objects[n]; a=coords(o); s=1 if a[:,0].mean()>0 else -1; a[:,0]+=s*.0012; setc(o,a)
# Torso less deep in profile.
o=bpy.data.objects['Rounded_Torso_MASTER']; a=coords(o); c=(a.min(0)+a.max(0))/2; a[:,1]=(a[:,1]-c[1])*.94+c[1]; setc(o,a)
# Asymmetric haunches: reference front has more mass/tail on image-left (+X) and less on image-right (-X).
for n in ('Haunch_Left_MASTER','Haunch_Right_MASTER'):
 o=bpy.data.objects[n]; a=coords(o); c=(a.min(0)+a.max(0))/2; left=c[0]>0; sx=.90 if left else .76; a[:,0]=(a[:,0]-c[0])*sx+c[0]+(.001 if left else .002); a[:,1]=(a[:,1]-c[1])*.92+c[1]; setc(o,a)
# Keep tail biased toward image-left (+X) and slightly higher/forward.
o=bpy.data.objects['Curled_tail_MASTER']; a=coords(o); a[:,0]+=.0015; a[:,2]+=.001; setc(o,a)
out=P/'source/cat_fit_r12.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('FIT_R12_SAVED',out)