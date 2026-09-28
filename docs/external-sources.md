# External source navigation

This opt-in feature adds a **local-only Haskell import fallback and continuous source navigation** after an empty
or missing-file definition response. Valid LSP files and the existing Java,
Deno and C# virtual document resolvers retain their current paths and sessions.
Type definitions, implementations and multiple-result selection are unchanged.

```elisp
(setq lsp-bridge-source-enable t) ; default is nil
```

Use Python 3.9 or newer for the isolated source helper. Override its executable
with `lsp-bridge-source-python-command` if needed. For uv/pipx/launcher-based
bridge configurations, the helper uses a directly installed Python to avoid
implicit environment downloads. Normal navigation never
starts a new download. Existing language-server download policies are unchanged.

## Commands

- `lsp-bridge-source-install`: explicitly acquire missing official GHC sources.
  Repeating the command retries a failed or cancelled task. Existing matching
  sources are reused. Other languages explain their toolchain's source manager.
- `lsp-bridge-source-status`: asynchronously inspect the current compiler,
  exact package IDs and registered/cached roots. Results are in
  `*lsp-bridge-source-status*`; progress is in `*lsp-bridge-source-log*`.
- `lsp-bridge-source-cancel`: cancel all queued/running source tasks. A pending
  network read or tool query may take up to its timeout to observe cancellation.
- `lsp-bridge-source-open-documentation`: open the latest local Haddock source
  page, or resolve the import at point for documentation.

At most two tasks run concurrently (`lsp-bridge-source-max-tasks`). Local
resolution uses one serial, reusable helper; installation/status tasks are
isolated. The idle resolver keeps bounded in-memory metadata and HTML caches,
not additional copies of sources. `lsp-bridge-source-cancel` also stops this
resolver, clearing its caches; the next lookup starts it again. Repeated
install requests from the same project/configuration are merged. A per-GHC OS
lock also coalesces installations from different projects or Emacs instances.
An installation never replays a previous click. Move the cursor, edit, switch
away, or close the buffer to invalidate an outstanding navigation request.

## Haskell environment

Cabal projects use the nearest `dist-newstyle/cache/plan.json`; the provider
never builds a project. GHC and ghc-pkg must have the same version **and global
package database**, and GHC must match the plan's compiler ID. Only exact unit
IDs from the plan may supply documentation. Global/user databases, the existing
project package database and Cabal's existing store are queried. Cabal's
`path --store-dir` is used where supported, including the plan's compiler ABI.
No package dependency is installed or copied.

For Stack, Nix, custom cradles, or a non-PATH toolchain, set buffer/project-local
values for these variables (for example in a trusted `.dir-locals.el`):

```elisp
(setq-local lsp-bridge-source-haskell-ghc "/toolchain/bin/ghc")
(setq-local lsp-bridge-source-haskell-ghc-pkg "/toolchain/bin/ghc-pkg")
(setq-local lsp-bridge-source-haskell-package-dbs '("/project/package.db"))
(setq-local lsp-bridge-source-haskell-packages
            '("base-4.21.0.0-958c" "ghc-internal-9.1202.0-7717"))
```

Without a Cabal plan, explicit compiler tools and exact package IDs are required.
With a plan, `packages` optionally restricts the import's package selection;
re-export destinations still have to match exact plan units. An ambiguous
package is an error rather than an arbitrary choice.

Register an existing source directory without copying it:

```elisp
(setq-local lsp-bridge-source-haskell-roots
 '((("compiler" . "ghc-9.12.2")
    ("unit" . "ghc-internal-9.1202.0-7717")
    ("path" . "/existing/ghc/libraries/ghc-internal/src"))))
```

A registered identity is an explicit user assertion; module and source-line
content are still verified against installed Haddock. Only registered directories
and the provider's own cache are indexed, never arbitrary disk locations.

The resolver reads **unsaved buffer text**. It supports module names, aliases,
qualified/postpositive-qualified imports and named imports across lines. It
excludes comments, hiding-list items, operators, package-qualified imports,
CPP-conditional files and body references in project buffers. Source links identify re-exported
implementation modules. The exact HTML anchor/line, module declaration and
source text must agree; no same-name symbol guessing or semantic index is used.
Generated/preprocessed files whose line/text cannot be verified stay in HTML.

A missing `.hs` target opens local HTML during ordinary navigation. Peek only
shows a documentation-command hint. Verified external files open read-only,
without starting another language server, and use the existing jump history,
other-window and Peek mechanisms. Already-open user buffers retain their state.

## Reading and continuing through external sources

Verified source buffers enable `lsp-bridge-source-mode`, with `M-.` for definition
and `M-,` to return. They retain the originating project, exact package context
and installed source HTML, without loading HLS or the GHC checkout's cradle.
Inside these buffers, definition/Peek uses the Haddock link at the clicked
occurrence, including same-file and cross-file links. Hidden type annotations
are excluded when mapping the visible HTML line and Unicode column. Both the
clicked line and target source line must match. Missing/ambiguous links or
changed text never trigger a same-name search. Files without matching installed
hyperlinked Haddock remain outside this capability.

Personal mouse bindings can be shared with the reading mode, for example:

```elisp
(define-key lsp-bridge-source-mode-map (kbd "<s-down-mouse-1>") #'ignore)
(define-key lsp-bridge-source-mode-map (kbd "<s-mouse-1>") #'my/lsp-click)
```

The example refers to your existing click command; the package does not define
`my/lsp-click` or impose a platform-specific mouse gesture. Existing open user
buffers retain their editability and server state.

The resolver reuses successful tool queries while the environment remains
unchanged. Plan/project configuration, executable paths and symlink targets,
package database/cache changes invalidate the session's tool results. A
60-second maximum lifetime also bounds reuse for opaque tool wrappers; use
`lsp-bridge-source-cancel` to refresh immediately after changing such a wrapper's
hidden configuration. Interpreter/environment or working-directory changes
restart the helper. HTML caches are keyed by file metadata; source text,
manifest and module indexes are rechecked for every lookup. Failed tool queries
and cancelled requests are not cached. A first lookup or a newly visited HTML
module still incurs discovery/parsing cost; normal project definitions retain
LSP priority, including its response latency.

## Managed cache

Only explicitly downloaded GHC source archives go in this cache:

- macOS: `~/Library/Caches/lsp-bridge/sources`
- Linux: `$XDG_CACHE_HOME/lsp-bridge/sources` (default `~/.cache/...`)
- Windows: `%LOCALAPPDATA%/lsp-bridge/sources`

Override the root with `lsp-bridge-source-cache-directory`. Entries are under
`haskell/ghc/<version>/<SHA-256>/`, with provenance and a module-to-file index.
The archive and checksum list come from the
[official GHC release directory](https://downloads.haskell.org/ghc/).
The helper checks SHA-256, bounds download/extraction size, validates paths and
links, extracts regular files without executing scripts, then publishes atomically.
Validated links are omitted; a link-only target therefore falls back to HTML.
Cancellation/failure removes staging data and preserves published entries. There
is no resume support. A forcibly killed helper can leave a hidden `.staging-*`
directory; it is never treated as a usable cache and may be removed manually.

## Other ecosystems

| Language | Existing source owner |
| --- | --- |
| Java | Project JDK attachments and JDT LS virtual documents/dependency sources |
| Rust | rust-analyzer, rustup `rust-src`, Cargo source cache |
| Go | gopls, `GOROOT/src`, module cache and vendor |
| Python | Project interpreter, installed packages and stubs; stubs remain declarations |
| JS/TS | Project dependencies, declarations and server navigation; existing Deno resolver |

No Java dependency downloader, SDK copy, Python/npm installer, decompiler or
cross-language symbol index is added. Dedicated environment diagnostics and a
JS/TS source-definition capability command are future extensions. Remote source
installation is unsupported.

## Editor integration

Managed external source buffers keep their originating `project.el` project
by default.  Set `lsp-bridge-source-preserve-project` to nil to disable this
association; Customize also updates buffers that are already open.  Explicit
project switching and queries for other directories retain normal behavior.
This does not change file paths, `default-directory`, or project file lists.
Disabling source mode removes the buffer-local project association.

`lsp-bridge-source-buffer-p` identifies managed read-only source buffers.
`lsp-bridge-source-origin-directory` returns their originating project directory
(possibly a subdirectory), without requiring the original buffer to stay open.
Both accept an optional buffer argument.  A shared source buffer retains the
most recent navigation's origin.

`lsp-bridge-source-context-update-hook` runs in the target buffer after each
context assignment, including reuse and source-to-source navigation.  Personal
title and tab integrations can use these APIs without inspecting
internal state or changing `buffer-file-name` or `default-directory`.
Use `lsp-bridge-source-mode-hook` to clean up when the mode is disabled.
Already-open editable files are not converted to managed source buffers.

## Verification

From the lsp-bridge checkout:

```sh
python -m unittest test.test_source -v
emacs -Q --batch -L /path/to/markdown-mode -L /path/to/yasnippet -L acm -L . \
  -l test/source-tests.el -f ert-run-tests-batch-and-exit
```

`external-source-providers` CI runs offline provider/integrity/routing tests and
Emacs lifecycle tests on Linux, macOS and Windows. Download publication is also
tested with deterministic archive fixtures.

When migrating from a custom source fallback, remove its mode hooks and
restart/reopen existing buffers. Existing navigation keybindings can be retained.

For a fresh official archive install on any platform, explicitly run
`python -m test.source_install_smoke` or dispatch the CI workflow with
`download_smoke` enabled. This downloads into a temporary cache, verifies the
published modules and checks that a second installation performs no download.
