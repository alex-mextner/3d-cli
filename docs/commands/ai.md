# `3d ai` - offline AI-assist prompt bundles

Builds a deterministic prompt bundle for an AI-assisted 3D workflow without calling
an AI backend or making a network request.

## Usage

```bash
3d ai <tool> <operator> <target> [options]
```

Operators are `do`, `review`, and `loop`.

| Option | Default | What |
|---|---|---|
| `--ref PATH` | none | Reference image or mesh for match, fit-camera, critique, and similar tools |
| `--backend NAME` | `claude` | Backend name to include in the bundle: `claude`, `codex`, `opencode`, `ollama`, or `mock` |
| `--model NAME` | backend default | Model name to include in the bundle |
| `--config PATH` | `~/.config/3d-cli/ai.json` | JSON config path; honors `XDG_CONFIG_HOME`, and `THREED_AI_CONFIG` also overrides the default |
| `--context TEXT` | none | Extra task context for the user prompt |
| `--json` | off | Print the bundle as JSON |

```bash
3d ai design review bracket.scad --json
3d ai design review bracket.scad --backend=mock --context "check wall thickness"
3d ai critique review bracket.scad --ref photo.png --backend opencode
```

## Output

The command prints the chosen backend/model, target, optional reference, deterministic
preflight commands, and system/user prompts. `network_call` is always `false`.
