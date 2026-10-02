"""Isolated Blender MCP worker; no GUI preferences changed, loopback only."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
ADDON = ROOT / 'vendor/blender-mcp-lightweight/addon'
if not ADDON.is_dir(): raise RuntimeError('Missing pinned Blender MCP vendor checkout')
sys.path.insert(0, str(ADDON))
from blender_mcp_addon.cli import cli_execute
raise SystemExit(cli_execute(['--host', '127.0.0.1', '--port', '9876']))
