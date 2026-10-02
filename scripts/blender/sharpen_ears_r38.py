import bpy
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
for n in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
 o=bpy.data.objects[n]; zmax=max(v.co.z for v in o.data.vertices); ids=[i for i,v in enumerate(o.data.vertices) if zmax-v.co.z<0.0005]
 if len(ids)>=2:
  cy=sum(o.data.vertices[i].co.y for i in ids)/len(ids); ids=sorted(ids,key=lambda i:o.data.vertices[i].co.y)
  for j,i in enumerate(ids): o.data.vertices[i].co.y=cy+(-0.0001 if j==0 else 0.0001)
  print(n,'side-profile cap reduced to 0.2mm, x topology preserved')
out=P/'source/cat_fit_r38.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('R38 ear side sharpening')