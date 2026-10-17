"""routes_cameras.py — 3d-api camera-node registry (#341).

  PUT    /v1/camera-nodes/{name}  camera | write  upsert the node's record (its `Node.record()`)
  GET    /v1/camera-nodes         read            {"items": [record, …]} by name
  GET    /v1/camera-nodes/{name}  read            record, 404 unknown
  DELETE /v1/camera-nodes/{name}  write           204, 404 unknown

PUT body: {url, stream_url?, hostname?, card?, device?, formats?, led?, version?}; url/stream_url
must be http(s), text without control characters, formats bounded (camera_nodes.MAX_*). A
token with `camera` but not `write` may only PUT the node named like the token (`3d printops
token create topcam --scopes camera` → node `topcam`), so a stolen node token cannot redirect
another node's record; `write` tokens may PUT any name. A record adds name, first_seen, last_seen, last_addr (the PUT's remote
address), updated_by (token name), age_s and online (last_seen within
printops.camera_nodes.ONLINE_S = 180 s). The `camera` scope is accepted here and nowhere else.
Domain logic: printops.camera_nodes. Errors: {"detail": message}, 422 invalid name/body.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from printops import camera_nodes as cn
from printops import db
from printops.api.deps import Db, Principal, require

router = APIRouter()

Reader = Annotated[Principal, Depends(require("read"))]
Writer = Annotated[Principal, Depends(require("write"))]
Node = Annotated[Principal, Depends(require("camera", "write"))]
JsonBody = Annotated[dict[str, Any], Body()]


@router.put("/v1/camera-nodes/{name}")
def register(
    request: Request, conn: Db, who: Node, name: str, body: JsonBody
) -> dict[str, Any]:
    if "write" not in who.scopes and name != who.name:
        raise HTTPException(
            403, f"token {who.name!r} (scope camera) may only register the camera node {who.name!r}, not {name!r}"
        )
    addr = request.client.host if request.client else None
    try:
        node, _created = cn.upsert(conn, name, body, addr=addr, actor=who.name)
    except cn.CameraNodeError as e:
        raise HTTPException(422, e.message) from e
    return node.to_dict(node.last_seen)


@router.get("/v1/camera-nodes")
def list_nodes(conn: Db, who: Reader) -> dict[str, Any]:
    now = db.utc_now()
    return {"items": [n.to_dict(now) for n in cn.list_nodes(conn)]}


@router.get("/v1/camera-nodes/{name}")
def get_node(conn: Db, who: Reader, name: str) -> dict[str, Any]:
    node = cn.get(conn, name)
    if node is None:
        raise HTTPException(404, f"no camera node {name!r}")
    return node.to_dict(db.utc_now())


@router.delete("/v1/camera-nodes/{name}", status_code=204)
def delete_node(conn: Db, who: Writer, name: str) -> Response:
    if not cn.delete(conn, name):
        raise HTTPException(404, f"no camera node {name!r}")
    return Response(status_code=204)
