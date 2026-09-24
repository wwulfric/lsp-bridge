"""Local-only external source providers. No dependency on the bridge process."""
from dataclasses import dataclass, field, asdict


@dataclass
class Context:
    request_id: str = ''
    file: str = ''
    version: int = 0
    point: int = 0  # Unicode offset in the supplied, unsaved text
    text: str = ''
    position: dict = field(default_factory=dict)
    project: str = ''
    server: str = ''
    mode: str = 'jump'
    language: str = ''
    config: dict = field(default_factory=dict)


@dataclass
class Result:
    kind: str = 'documentation'  # source, declaration, generated, documentation
    path: str = ''
    line: int = 0
    character: int = 0
    documentation: str = ''
    reusable_server: bool = False
    message: str = ''

    def json(self):
        return asdict(self)


MANAGERS = {
    'java': 'Project JDK source attachments and JDT LS manage Java sources.',
    'rust': 'rustup component add rust-src (project toolchain); Cargo manages dependency sources.',
    'go': 'gopls uses GOROOT/src, the Go module cache and vendor.',
    'python': 'The project interpreter and installed packages/stubs manage Python sources.',
    'javascript': 'The project package manager and language server manage JS/TS sources.',
    'typescript': 'The project package manager and language server manage JS/TS sources.',
}


def provider(context):
    if context.language == 'haskell':
        from .haskell import Haskell
        return Haskell()
    return None
