# Tests

Run the offline Python and ERT regressions with:

```sh
make test PYTHON=/path/to/python DEPS=/path/to/checkouts
```

`DEPS` defaults to `..`, with markdown-mode, yasnippet and posframe checked
out there. The Python interpreter needs the project's Python dependencies.
These suites cover source providers, server configuration, completion
commands, signature selection and Org Babel routing; they do not install
packages or start language servers.

Run the original end-to-end suite against installed language servers with:

```sh
LSP_BRIDGE_TEST_DEPS=/path/to/checkouts make integration-test PYTHON=/path/to/python
```

That directory may also contain compat. The local-dependency option avoids
Quelpa downloads and uses the test runner's Python interpreter for the bridge.
Without it, the original package-installing bootstrap is retained for CI.
The integration suite requires local RPC sockets, Git and the Python language
servers selected by the default configuration. It also byte-compiles the Lisp
sources. `EMACS` can select another Emacs executable.

The old, entirely commented-out completion-acceptance and same-file jump
experiments have been removed. Active Python completion and cross-file jump
tests remain, alongside the newer offline regression suites.

## Python compatibility and lockfile

Python 3.12 and 3.13 are supported. Both CI workflows use that matrix and
install from `uv.lock` with `uv sync --locked --python python`, so stale locks
fail the job. No package versions were upgraded when extending the lock to 3.12.
The launcher uses `uv run --frozen` to consume the committed lock without
rewriting it. Use `uv lock --check` when editing dependency declarations.

`lsp-bridge-server-config-features` advertises the integration contract to
client adapters. Keep the Lisp and Python parts from the same checkout.
