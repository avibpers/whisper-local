Builds `koli.exe` using [pyapp](https://github.com/ofek/pyapp) (Rust wrapper that bootstraps Python + pip install at first launch).

## One-time setup

```powershell
powershell.exe -ExecutionPolicy Bypass -File pyapp-build\setup-build-env.ps1
```

The setup script:
- Checks for Rust (offers to install via rustup if missing — one-time ~600 MB)
- Downloads + extracts the latest pyapp source to `pyapp-build\pyapp-source\`
- Creates `pyapp-build\build-config.json` from defaults

Re-open the terminal after installing Rust so `cargo` lands on PATH.

## Build

```powershell
powershell.exe -ExecutionPolicy Bypass -File pyapp-build\build-pyapp.ps1
```

`-Clean` flag forces full Rust rebuild.

Produces two executables in `dist\`:
- `koli.exe` — console (visible terminal)
- `koli-hideable.exe` — GUI subsystem (for `console.start_hidden`/minimize-to-tray)

## How it works

At first launch, `koli.exe` runs the embedded pyapp logic:
1. Creates a private venv under `%LOCALAPPDATA%\pyapp\data\koli\<hash>\<version>\`
2. Runs `pip install koli==<your version>` into that venv
3. Spawns the actual Python entry point from that venv

So the user never needs Python installed system-wide. They just double-click the exe.

## Two distribution flows

**A. PyPI (recommended once published):**
- Publish `koli` to PyPI (see [docs/publishing-to-pypi.md](../docs/publishing-to-pypi.md))
- Build the exe with default `setup-build-env.ps1` flow
- Distribute the exe; first launch pulls from PyPI

**B. Bundled local wheel (for testing before PyPI release):**
- Run setup with `-UseLocalWheel`: `setup-build-env.ps1 -UseLocalWheel`
- It builds a local wheel and prints the `PYAPP_PROJECT_PATH` env var to set
- Run build-pyapp.ps1 with that env var set
- The resulting exe contains the wheel — works without PyPI

## Troubleshooting

**`cargo: command not found`**: re-open the terminal after `rustup-init`.

**`pip install koli` fails at first launch**: the version in `pyproject.toml` isn't on PyPI yet. Either publish, or use the local-wheel flow.

**Slow first launch (~30s)**: normal — pyapp is downloading deps. Subsequent launches reuse the venv.
