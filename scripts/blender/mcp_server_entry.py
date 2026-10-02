"""Project-local MCP launch; worker is isolated and never exposed through Funnel."""
import os, socket, subprocess, time, shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; PROJ=ROOT/'projects/low-poly-cat'

def listening():
    with socket.socket() as s:
        s.settimeout(.3); return s.connect_ex(('127.0.0.1',9876))==0

if not listening():
    seed=PROJ/'source/cat_recovery_topology.blend'
    if not seed.is_file(): raise RuntimeError('Missing validated recovery seed')
    session=PROJ/'source/mcp_working_copy.blend'; shutil.copy2(seed,session)
    log=(PROJ/'reports/mcp-worker.log').open('a')
    proc=subprocess.Popen(['/Applications/Blender.app/Contents/MacOS/Blender','--factory-startup','-b',str(session),'--python-exit-code','1','--python',str(ROOT/'scripts/blender/mcp_worker.py')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    (PROJ/'reports/mcp-worker.pid').write_text(str(proc.pid))
    for _ in range(50):
        if listening(): break
        if proc.poll() is not None: raise RuntimeError('Blender MCP worker exited; inspect its log')
        time.sleep(.1)
    if not listening(): raise RuntimeError('Blender MCP startup deadline exceeded')
env=dict(os.environ,BLENDER_MCP_HOST='127.0.0.1',BLENDER_MCP_PORT='9876')
server=ROOT/'.mcp-venv/bin/blender-mcp'
os.execve(str(server),[str(server),'--transport','stdio'],env)
