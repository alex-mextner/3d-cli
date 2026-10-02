"""Exercise real MCP initialize/list/call via stdio and the loopback Blender add-on."""
import asyncio, json, os
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
ROOT=Path(__file__).resolve().parents[2]
REPORTS=ROOT/'projects/low-poly-cat/reports'

async def main():
    env=dict(os.environ,BLENDER_MCP_HOST='127.0.0.1',BLENDER_MCP_PORT='9876')
    params=StdioServerParameters(command=str(ROOT/'.mcp-venv/bin/python'),args=[str(ROOT/'scripts/blender/mcp_server_entry.py')],env=env)
    with (REPORTS/'mcp-smoke-server.log').open('w') as err:
        async with stdio_client(params,errlog=err) as (read,write):
            async with ClientSession(read,write) as session:
                init=await session.initialize(); tools=await session.list_tools()
                assert any(t.name=='execute_blender_code' for t in tools.tools)
                code='''import bpy
assert bpy.data.objects.get('QA_MCP_SMOKE') is None
mesh=bpy.data.meshes.new('QA_MCP_SMOKE_MESH')
mesh.from_pydata([(0,0,0),(.01,0,0),(0,.01,0)],[],[(0,1,2)])
o=bpy.data.objects.new('QA_MCP_SMOKE',mesh); bpy.context.scene.collection.objects.link(o)
o.location.z=.007
result={'blender':bpy.app.version_string,'scene':bpy.data.filepath,'write_read_ok':abs(o.location.z-.007)<1e-6}
bpy.data.objects.remove(o,do_unlink=True);bpy.data.meshes.remove(mesh)
result['cleanup_ok']=bpy.data.objects.get('QA_MCP_SMOKE') is None
'''
                called=await session.call_tool('execute_blender_code',{'code':code})
                assert not called.isError,called
                response=json.loads(next(c.text for c in called.content if c.type=='text'))
                assert response.get('status')=='ok',response
                assert response['result']['write_read_ok'] and response['result']['cleanup_ok'],response
                audit=(ROOT/'scripts/blender/qa_topology.py').read_text().split("if __name__=='__main__':")[0]
                audit+='\nresult=audit_scene()\nfor p in result["parts"].values(): p.pop("boundary_segments_mm",None)\n'
                assert len(audit.encode())<16000
                checked=await session.call_tool('execute_blender_code',{'code':audit})
                assert not checked.isError,checked
                data=json.loads(next(c.text for c in checked.content if c.type=='text'))
                assert data.get('status')=='ok',data
                report=dict(transport='stdio -> TCP 127.0.0.1:9876 -> isolated Blender worker',
                            server=init.serverInfo.model_dump(),tool_count=len(tools.tools),
                            tool_names=[t.name for t in tools.tools],smoke=response['result'],audit=data['result'])
                (REPORTS/'mcp-smoke.json').write_text(json.dumps(report,indent=2))
                print('MCP_SMOKE_OK',report['tool_count'],'tools; create/read/delete passed; v15 topology_ok=',data['result']['topology_ok'])

if __name__=='__main__': asyncio.run(main())
