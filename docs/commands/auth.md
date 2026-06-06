# `3d auth` - optional provider credentials

`3d auth` stores credentials for optional cloud/model providers. The first provider is
Hugging Face, used for ZeroGPU Spaces, higher public-download quotas, and gated model
weights.

## Usage

```bash
3d auth hf <login|status|logout|complete> [options]
```

## Hugging Face

```bash
3d auth hf login
3d auth hf status --json
3d auth hf logout
```

`login` prints the token settings URL, reads the token with hidden terminal input,
validates it with Hugging Face `whoami`, and writes it to:

```text
~/.config/3d-cli/auth.json
```

The file is written with `0600` permissions. `HF_TOKEN` in the environment takes
precedence over the stored token.

## Why token login first?

Hugging Face supports OAuth device code flow for registered OAuth apps, but `3d-cli` does
not yet have a registered public `client_id`. Until that exists, token login is the
simplest reliable workflow for both humans and agents:

```bash
3d auth hf login          # human pastes token interactively
3d auth hf status --json  # agents check availability
```

`3d auth hf complete CODE` is reserved for a future OAuth device-flow implementation.
