# Research shortlist

## MCP
- `vendor/blender-mcp-lightweight` — small Blender MCP bridge; good reference for a minimal maintainable protocol.
- `vendor/blender-mcp-69-tools` — broader structured Blender toolset and geometry inspection.
- `vendor/blender-mcp-headless` — headless-first, deterministic/allowlisted approach; useful for CI-style validation.

## Agent skills
- `vendor/cc-blender-skill` — contains a dedicated `reference-to-3d` workflow with overlay gates and orthographic registration.
- `vendor/create-3d-model-skill` — Codex-oriented Blender skill with multi-view validation/checkpoints.

## Camera matching
- `vendor/fspy-blender` — still-image camera matching/import into Blender.
- AutoCamMatch/GeoCalib is worth evaluating separately; the repository URL surfaced by search was not cloneable, so it is not vendored yet.

## Pipeline ideas to adopt now
1. Treat the supplied reference as a contract, not inspiration.
2. Lock cameras per view and never compare renders from drifting cameras.
3. Save a versioned `.blend` milestone before every geometry pass.
4. Validate front / three-quarter / side independently.
5. Split score into silhouette, landmarks, and local facial/ear masks; do not call silhouette IoU “visual similarity”.
6. Use headless Blender for deterministic renders and exports; keep large logs on disk.