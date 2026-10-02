"""Deterministic regression checks for the cat's topology release blocker."""
import bpy, bmesh, sys, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from qa_topology import audit_objects
RESULTS=[]

def cube():
    mesh=bpy.data.meshes.new('TestMesh'); bm=bmesh.new()
    bmesh.ops.create_cube(bm,size=.02); bm.to_mesh(mesh); bm.free()
    o=bpy.data.objects.new('QA_TEST',mesh); bpy.context.scene.collection.objects.link(o)
    return o

def check(name,obs,expected):
    report=audit_objects(obs); actual=report['topology_ok']
    RESULTS.append(dict(name=name,passed=actual==expected,boundary=report['boundary_edges']))
    assert actual==expected,(name,report)
    for o in obs:
        mesh=o.data; bpy.data.objects.remove(o,do_unlink=True); bpy.data.meshes.remove(mesh)

check('closed_cube',[cube()],True)
o=cube(); bm=bmesh.new(); bm.from_mesh(o.data)
bmesh.ops.delete(bm,geom=[next(iter(bm.faces))],context='FACES_ONLY')
bm.to_mesh(o.data); bm.free(); check('missing_cap',[o],False)
o=cube()
for p in o.data.polygons: p.flip()
check('inverted_shell',[o],False)

def split_cube(shift=0):
    src=cube(); obs=[]
    for idx,face in enumerate(src.data.polygons):
        vs=[tuple(src.data.vertices[i].co) for i in face.vertices]
        if idx==0: vs=[(x+shift,y,z) for x,y,z in vs]
        mesh=bpy.data.meshes.new('SplitTest'); mesh.from_pydata(vs,[],[tuple(range(len(vs)))])
        obj=bpy.data.objects.new('QA_MAT_'+str(idx),mesh); bpy.context.scene.collection.objects.link(obj); obs.append(obj)
    mesh=src.data; bpy.data.objects.remove(src,do_unlink=True); bpy.data.meshes.remove(mesh)
    return obs

check('coincident_material_seams',split_cube(),True)
check('separate_material_deformation',split_cube(.002),False)
a,b=cube(),cube(); b.location.x=.04
check('disconnected_closed_shells',[a,b],False)
o=cube(); o.data.polygons[0].flip()
check('one_reversed_face',[o],False)
# A closed manifold torus can still intersect itself: topology alone is insufficient.
import math
verts=[]; faces=[]; nu,nv=32,16
for u in range(nu):
    a=2*math.pi*u/nu
    for v in range(nv):
        b=2*math.pi*v/nv; radius=.006+.009*math.cos(b)
        verts.append((radius*math.cos(a),radius*math.sin(a),.009*math.sin(b)))
for u in range(nu):
    for v in range(nv): faces.append((u*nv+v,((u+1)%nu)*nv+v,((u+1)%nu)*nv+(v+1)%nv,u*nv+(v+1)%nv))
mesh=bpy.data.meshes.new('SpindleTorus');mesh.from_pydata(verts,[],faces)
o=bpy.data.objects.new('QA_TORUS',mesh);bpy.context.scene.collection.objects.link(o)
assert audit_objects([o])['nonadjacent_overlap_candidates']>0
check('closed_but_self_intersecting',[o],False)

root=Path(__file__).resolve().parents[2]
out=root/'projects/low-poly-cat/reports/topology-regression.json'
out.write_text(json.dumps(dict(tests=RESULTS,passed=len(RESULTS)),indent=2))
print('REGRESSION_PASSED',len(RESULTS))
