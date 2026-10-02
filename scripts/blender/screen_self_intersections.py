"""Conservative non-adjacent triangle overlap screen; not full print certification."""
import bpy,bmesh,json
from pathlib import Path
from mathutils.bvhtree import BVHTree
ROOT=Path(__file__).resolve().parents[2]; result={}
for o in bpy.context.scene.objects:
    if o.type!='MESH': continue
    bm=bmesh.new();bm.from_mesh(o.data)
    bmesh.ops.triangulate(bm,faces=list(bm.faces));bm.verts.ensure_lookup_table();bm.verts.index_update();bm.faces.ensure_lookup_table()
    faces=[tuple(v.index for v in f.verts) for f in bm.faces]
    tree=BVHTree.FromPolygons([o.matrix_world@v.co for v in bm.verts],faces,all_triangles=True,epsilon=0)
    pairs=sorted({tuple(sorted((a,b))) for a,b in tree.overlap(tree) if a!=b and not(set(faces[a])&set(faces[b]))})
    result[o.name]={'nonadjacent_triangle_overlap_candidates':len(pairs),'sample_pairs':pairs[:12]}
    bm.free()
out=ROOT/'projects/low-poly-cat/reports'/f'{Path(bpy.data.filepath).stem}-intersections.json'
out.write_text(json.dumps({'source':bpy.data.filepath,'scope':'BVH screen excluding triangles sharing a vertex. Not an exact all-intersections proof.','objects':result},indent=2))
print('OVERLAP_SCREEN', {n:r['nonadjacent_triangle_overlap_candidates'] for n,r in result.items() if r['nonadjacent_triangle_overlap_candidates']})
