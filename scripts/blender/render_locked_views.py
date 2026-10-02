import bpy, sys, argparse
from mathutils import Vector
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--out',required=True)
a=p.parse_args(sys.argv[sys.argv.index('--')+1:]); OUT=Path(a.out); OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.display.shading.light='STUDIO'; s.display.shading.color_type='MATERIAL'; s.display.shading.show_shadows=True; s.display.shading.show_cavity=True

def look(c,t): c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def shot(name,loc,target=(0,.015,.08),scale=.18,res=(800,1000)):
 bpy.ops.object.camera_add(location=loc); c=bpy.context.object; c.data.type='ORTHO'; c.data.ortho_scale=scale; look(c,target); s.camera=c
 s.render.resolution_x,resx=res[0],res[0]; s.render.resolution_x=res[0]; s.render.resolution_y=res[1]; s.render.resolution_percentage=100; s.render.filepath=str(OUT/f'{name}.png'); bpy.ops.render.render(write_still=True); bpy.data.objects.remove(c,do_unlink=True)
shot('front',(0,.35,.085)); shot('threequarter',(.20,.32,.10)); shot('side',(.35,0,.09))
shot('face',(0,.25,.116),(0,.025,.116),.075,(900,700)); shot('ear_side',(.25,0,.132),(0,.012,.132),.075,(900,700))
print('LOCKED_VIEWS_RENDERED',OUT)