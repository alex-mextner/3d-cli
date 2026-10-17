-- 0090_camera_nodes.sql — camera-node registry (#341).
-- One row per top-camera node (topcam on the Pi Zero, `3d camera serve` on a PC). The node
-- upserts its own row with PUT /v1/camera-nodes/{name} every api.interval_s; the CLI resolves
-- `--camera NAME` through it, so a DHCP address change needs no config edit.
-- `online` is derived (last_seen within printops.camera_nodes.ONLINE_S), never stored.

CREATE TABLE camera_nodes (
    name        TEXT PRIMARY KEY,                    -- same rule as fleet printer names
    url         TEXT NOT NULL,                       -- http(s)://host:port of the node API
    stream_url  TEXT,                                -- MJPEG preview; NULL when the node runs none
    hostname    TEXT,
    card        TEXT,                                -- V4L2 card name, e.g. "HD Pro Webcam C920"
    device      TEXT,                                -- /dev/v4l/by-id/… on the node
    formats     TEXT NOT NULL DEFAULT '{}',          -- JSON {"YUYV": [[w, h], …], …}
    led         INTEGER NOT NULL DEFAULT 0 CHECK (led IN (0, 1)),
    version     TEXT,                                -- topcam.node version
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    last_addr   TEXT,                                -- remote address of the last PUT
    updated_by  TEXT NOT NULL                        -- token name of the last PUT
);
