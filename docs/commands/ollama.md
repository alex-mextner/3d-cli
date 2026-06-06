# `3d ollama` - dry-run local Ollama request planning

Validates local Ollama endpoint settings and prints the exact `/api/generate`
request that a later network-enabled workflow would send. The command never opens a
socket; it is a deterministic planning step for local AI-assisted CAD workflows.

## Usage

```bash
3d ollama --model MODEL --prompt TEXT [options]
```

| Option | Default | What |
|---|---|---|
| `--config PATH` | `~/.config/3d-cli/ollama.json` | JSON config with endpoint/model defaults |
| `--endpoint URL` | config, else `http://127.0.0.1:11434` | Local Ollama base URL |
| `--model NAME` | config | Ollama model name for `/api/generate` |
| `--prompt TEXT` | required | Prompt to place in the request body |
| `--system TEXT` | none | Optional system prompt |
| `--dry-run` | on | Accepted for clarity; this command always prints a plan only |

```bash
3d ollama --model llama3.2 --prompt "Suggest an OpenSCAD edit" --dry-run
3d ollama --config ~/.config/3d-cli/ollama.json --prompt "Make it hollow" --dry-run
3d ollama --endpoint localhost:11434 --model llama3.2 --prompt "Review bracket.scad" > ollama-plan.json
```

## Config

The config file is optional. When present, it must be a JSON object:

```json
{
  "endpoint": "http://127.0.0.1:11434",
  "model": "llama3.2"
}
```

Only local endpoints are accepted: `localhost`, `127.0.0.1`, or `::1`. Remote hosts,
credentials, query strings, fragments, and path-prefixed base URLs are rejected so a
dry-run plan cannot silently target an unexpected service.

## Output

The command prints JSON with `dry_run: true`, the HTTP method, normalized URL, and
request body. `stream` is set to `false` so scripts get a single response payload when
a future sender uses the plan.
