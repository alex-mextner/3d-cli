import bpy,sys,argparse,json,math
from pathlib import Path
from mathutils import Vector
ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);ap.add_argument('--params');a=ap.parse_args(sys.argv[sys.argv.index('--')+1:]);OUT=Path(a.out);OUT.mkdir(parents=True,exist_ok=True)
p={};
if a.params and Path(a.params).exists(): p=json.loads(Path(a.params).read_text()).get('params',{})
tq=float(p.get('tq_angle',32.0)); r=(.20**2+.32**2)**.5
s=bpy.context.scene;s.render.engine='BLENDER_WORKBENCH';s.display.shading.light='STUDIO';s.display.shading.color_type='MATERIAL';s.display.shading.show_shadows=True;s.display.shading.show_cavity=True
def look(c,t):c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def shot(name,loc,target=(0,.015,.08),scale=.18,res=(800,1000)):
 bpy.ops.object.camera_add(location=loc);c=bpy.context.object;c.data.type='ORTHO';c.data.ortho_scale=scale;look(c,target);s.camera=c;s.render.resolution_x=res[0];s.render.resolution_y=res[1];s.render.resolution_percentage=100;s.render.filepath=str(OUT/f'{name}.png');bpy.ops.render.render(write_still=True);bpy.data.objects.remove(c,do_unlink=True)
shot('front',(0,.35,.085));shot('threequarter',(r*math.sin(math.radians(tq)),r*math.cos(math.radians(tq)),.10),(0,.010,.080));shot('side',(.35,0,.09),(0,.010,.080));shot('face',(0,.25,.116),(0,.025,.116),.075,(900,700));shot('ear_side',(.25,0,.132),(0,.012,.132),.075,(900,700));print('FIT_VIEWS',OUT,'tq',tq)