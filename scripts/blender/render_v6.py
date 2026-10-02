import bpy, math
from mathutils import Vector
from pathlib import Path
OUT=Path('/Users/ultra/Downloads/cat_blender_work/renders'); OUT.mkdir(exist_ok=True)
# transparent studio renders, orthographic, fixed target at head/body center
scene=bpy.context.scene
scene.render.engine='BLENDER_EEVEE'; scene.render.film_transparent=True
scene.render.resolution_percentage=100
# flat-ish material lighting
bpy.ops.object.light_add(type='AREA',location=(0.12,-0.18,0.24)); bpy.context.object.data.energy=700; bpy.context.object.data.shape='DISK'; bpy.context.object.data.size=0.18
bpy.ops.object.light_add(type='AREA',location=(-0.14,-0.05,0.16)); bpy.context.object.data.energy=350; bpy.context.object.data.size=0.20

def look(cam,target):
    cam.rotation_euler=(Vector(target)-cam.location).to_track_quat('-Z','Y').to_euler()
def shot(name,loc,res=(800,1000),scale=.18,target=(0,0,0.078)):
    bpy.ops.object.camera_add(location=loc); cam=bpy.context.object; cam.data.type='ORTHO'; cam.data.ortho_scale=scale; look(cam,target); scene.camera=cam
    scene.render.resolution_x=res[0];scene.render.resolution_y=res[1];scene.render.filepath=str(OUT/f'{name}.png');bpy.ops.render.render(write_still=True);bpy.data.objects.remove(cam,do_unlink=True)
shot('front',(0,-0.35,0.085),target=(0,-0.015,0.078))
shot('threequarter',(0.20,-0.32,0.10),target=(0,-0.010,0.080))
shot('side',(0.35,0,0.09),target=(0,-0.010,0.080))
shot('face',(0,-0.25,0.116),res=(900,700),scale=.075,target=(0,-0.025,0.116))
shot('ear_side',(0.25,0,0.132),res=(900,700),scale=.075,target=(0,-0.012,0.132))
print('rendered')