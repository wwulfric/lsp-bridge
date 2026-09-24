"""Conservative import -> exact installed Haddock -> verified .hs mapping."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from functools import lru_cache
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from . import Result
from . import session


def run(command, *args):
    def execute():
        return subprocess.check_output([command, *args], text=True, encoding='utf-8',
                                       stderr=subprocess.PIPE, timeout=20).strip()
    return session.ACTIVE.run((command, *args), execute) if session.ACTIVE else execute()


def mask_comments(text):
    # Preserve offsets and newlines, including nested Haskell block comments.
    chars = list(text)
    i, depth = 0, 0
    while i < len(text):
        if text.startswith('{-', i):
            depth += 1
            chars[i:i + 2] = '  '
            i += 2
        elif depth and text.startswith('-}', i):
            depth -= 1
            chars[i:i + 2] = '  '
            i += 2
        elif depth:
            if chars[i] != '\n':
                chars[i] = ' '
            i += 1
        elif text.startswith('--', i):
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            chars[i:end] = ' ' * (end - i)
            i = end
        else:
            i += 1
    return ''.join(chars)


def import_target(text, point):
    clean = mask_comments(text)
    # CPP selection is deliberately unsupported, even if one branch looks clear.
    if re.search(r'^\s*#\s*(if|ifdef|ifndef|else|elif|endif)\b', clean, re.M):
        return None
    if point < 0 or point >= len(clean) or clean[point].isspace():
        return None
    module = r'[A-Z][\w\']*(?:\.[A-Z][\w\']*)*'
    pattern = re.compile(r'^\s*import[ \t]+(?:qualified\s+)?(' + module + r')(?![\w\'.])', re.M)
    for match in pattern.finditer(clean):
        if match.start(1) <= point < match.end(1):
            return match[1], None
        tail = re.match(r'\s*(?:qualified\s+)?(?:as\s+(' + module + r')\s*)?', clean[match.end():])
        end = match.end() + tail.end()
        if tail[1] and match.end() + tail.start(1) <= point < match.end() + tail.end(1):
            return match[1], None
        if end >= len(clean) or clean[end] != '(':
            continue
        depth, close = 1, end + 1
        while close < len(clean) and depth:
            depth += (clean[close] == '(') - (clean[close] == ')')
            close += 1
        if depth or not end < point < close - 1:
            continue
        for name in re.finditer(r"[A-Za-z_][\w']*", clean[end + 1:close - 1]):
            if end + 1 + name.start() <= point < end + 1 + name.end():
                return match[1], name[0]
    return None


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def text(self):
        if 'annottext' in self.attrs.get('class', '').split():
            return ''
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children)


class HTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ('meta', 'link', 'br', 'hr', 'img', 'input', 'wbr'):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)


def source_link(document, symbol):
    tree = html_tree(document)
    scopes = [tree] if not symbol else [n for n in tree.nodes() if n.tag == 'p' and any(
        c.attrs.get('id') in ('v:' + symbol, 't:' + symbol) for c in n.nodes())]
    for scope in scopes:
        for node in scope.nodes():
            if node.tag == 'a' and node.text().strip() == 'Source':
                parts = urlsplit(node.attrs.get('href', ''))
                if parts.scheme or parts.netloc or not parts.path:
                    continue
                path = (document.parent / unquote(parts.path)).resolve()
                if path.is_file():
                    return path, unquote(parts.fragment)
    return None


@lru_cache(maxsize=16)
def _html_tree(path, stamp):
    return HTML(Path(path).read_text(encoding='utf-8')).root


def html_tree(path):
    return _html_tree(str(path), session.stamp(path))


@lru_cache(maxsize=16)
def _source_map(path, stamp):
    tree = html_tree(Path(path))
    pre = next((n for n in tree.nodes() if n.tag == 'pre'), None)
    line, column, lines, anchors, links = 1, 0, {}, {}, []
    def walk(node):
        nonlocal line, column
        if isinstance(node, str):
            # Haddock line-N anchors define source rows, including blank rows.
            lines[line] = lines.get(line, '') + node
            column += len(node)
            return
        if 'annottext' in node.attrs.get('class', '').split():
            return
        anchor = node.attrs.get('id', '')
        if re.fullmatch(r'line-\d+', anchor):
            line, column = int(anchor[5:]), 0
        if anchor:
            anchors[anchor] = (line, column)
        start = line, column
        for child in node.children:
            walk(child)
        if node.tag == 'a' and node.attrs.get('href') and start[0] == line:
            links.append((line, start[1], column, node.attrs['href']))
    if pre:
        walk(pre)
    return lines, anchors, links


def source_map(path):
    return _source_map(str(path), session.stamp(path))


def source_location(path, fragment):
    lines, anchors, _ = source_map(path)
    target = anchors.get(fragment, (None, 0))[0] if fragment else next(
        (n for n, text in lines.items() if re.search(r'\bmodule\s+', text)), None)
    return (target, lines.get(target, '').strip()) if target else None


def occurrence_link(path, text, point):
    """Resolve a clicked occurrence, never a symbol-name search.

    point is a Unicode offset, independent of LSP encodings and tab width.
    Unsaved changes on the clicked line invalidate the HTML correspondence.
    """
    if not 0 <= point < len(text):
        return None
    line = text.count('\n', 0, point) + 1
    column = point - (text.rfind('\n', 0, point) + 1)
    lines, _, links = source_map(path)
    if lines.get(line, '').rstrip('\r\n') != text.split('\n')[line - 1].rstrip('\r'):
        return None
    matches = {href for n, start, end, href in links if n == line and start <= column < end}
    if len(matches) != 1:
        return None
    parts = urlsplit(matches.pop())
    if parts.scheme or parts.netloc or not parts.fragment:
        return None
    target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
    return (target, unquote(parts.fragment)) if target.is_file() else None


def cache_root(config):
    if config.get('cache'):
        return Path(config['cache']).expanduser()
    if sys.platform == 'darwin':
        base = Path.home() / 'Library/Caches'
    elif os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'lsp-bridge/sources'


def read_plan(context):
    start = Path(context.file).resolve().parent
    for parent in (start, *start.parents):
        plan = parent / 'dist-newstyle/cache/plan.json'
        if plan.is_file():
            return json.loads(plan.read_text(encoding='utf-8')), parent
    return None, Path(context.project or start)


def package_paths(raw):
    """ghc-pkg emits either a bare path or quoted path lists."""
    if Path(raw).is_dir():
        return [raw]
    values = shlex.split(raw, posix=os.name != 'nt')
    if os.name == 'nt':
        result = []
        for value in values:
            if value.startswith('"') and value.endswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value[1:-1]
            result.append(value)
        return result
    return values


class Haskell:
    def probe(self, context):
        config = context.config
        plan, project = read_plan(context)
        ghc = config.get('ghc') or 'ghc'
        pkg = config.get('ghc_pkg') or 'ghc-pkg'
        version = run(ghc, '--numeric-version')
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('Unrecognized GHC version')
        pkg_version = run(pkg, '--version').split()[-1]
        global_db = Path(run(ghc, '--print-global-package-db')).resolve()
        session.database(global_db)
        package_db = Path(run(pkg, '--global', 'list').splitlines()[0].rstrip(':')).resolve()
        if global_db != package_db:
            raise ValueError('GHC and ghc-pkg global package databases differ')
        if pkg_version != version:
            raise ValueError('GHC and ghc-pkg versions differ; configure matching tools')
        if plan and plan.get('compiler-id') != 'ghc-' + version:
            raise ValueError('Cabal plan and GHC differ; configure the project toolchain')
        if not plan and not (config.get('ghc') and config.get('ghc_pkg') and config.get('packages')):
            raise ValueError('No Cabal plan: configure ghc, ghc-pkg and exact package unit IDs')
        # With a plan, only its exact units may supply imports, never latest versions.
        units = {p['id']: (p['pkg-name'], p['pkg-version']) for p in plan['install-plan']} if plan else {}
        databases = list(config.get('package_dbs', []))
        if plan:
            local_db = project / 'dist-newstyle/packagedb' / ('ghc-' + version)
            session.database(local_db)
            if local_db.is_dir():
                databases.append(str(local_db))
            if shutil.which('cabal'):
                try:
                    store = Path(run('cabal', 'path', '--store-dir'))
                    identities = ['ghc-' + version]
                    if plan.get('compiler-abi'):
                        identities.insert(0, 'ghc-' + version + '-' + plan['compiler-abi'])
                    for identity in identities:
                        store_db = store / identity / 'package.db'
                        session.database(store_db)
                        if store_db.is_dir():
                            databases.append(str(store_db))
                            break
                except (OSError, subprocess.SubprocessError):
                    pass  # Older Cabal: explicit package_dbs remains available.
        if session.ACTIVE:
            # Discover the exact user DB, including its currently absent path.
            user_listing = run(pkg, '--user', 'list')
            for row in user_listing.splitlines():
                if row and not row[0].isspace() and row.rstrip().endswith(':'):
                    session.database(row.rstrip().rstrip(':'))
        args = ['--global', '--user']
        for db in dict.fromkeys(databases):
            session.database(db)
            args.extend(['--package-db', str(Path(db).expanduser())])
        self.pkg, self.pkg_args, self.units = pkg, args, units
        if not plan:
            for unit in config['packages']:
                name = self.pkg_run('--ipid', 'field', unit, 'name', '--simple-output')
                ver = self.pkg_run('--ipid', 'field', unit, 'version', '--simple-output')
                units[unit] = (name, ver)
        self.version, self.project = version, project
        return {'compiler': 'ghc-' + version, 'project': str(project), 'packages': list(units)}

    def pkg_run(self, *args):
        return run(self.pkg, *self.pkg_args, *args)

    def documents(self, module):
        units = self.pkg_run('find-module', module, '--simple-output', '--show-unit-ids').split()
        documents = []
        candidates = sorted(set(units) & self.units.keys())
        if getattr(self, 'selected', []):
            candidates = [unit for unit in candidates if unit in self.selected]
        if len(candidates) > 1:
            raise ValueError('Ambiguous installed packages for ' + module + '; configure an exact package selection')
        for unit in candidates:
            raw = self.pkg_run('--ipid', 'field', unit, 'haddock-html', '--simple-output')
            # ghc-pkg quotes paths containing spaces.
            dirs = package_paths(raw)
            for directory in dirs:
                path = Path(directory) / (module.replace('.', '-') + '.html')
                if path.is_file():
                    documents.append((unit, path.resolve()))
        if len(documents) > 1:
            raise ValueError('Ambiguous installed packages for ' + module + '; configure an exact package selection')
        return documents

    def roots(self, context):
        from .install import valid_manifest, check_cancel
        roots = []
        for entry in context.config.get('roots', []):
            # User asserts package identity; content is still checked against Haddock.
            if entry.get('compiler') == 'ghc-' + self.version and entry.get('unit') in self.units:
                roots.append((Path(entry['path']).expanduser().resolve(), entry['unit']))
        base = cache_root(context.config) / 'haskell/ghc' / self.version
        if base.is_dir():
            for manifest in base.glob('*/manifest.json'):
                check_cancel()
                data = valid_manifest(manifest, self.version)
                if data:
                    roots.append((manifest.parent / data['root'], None))
        return roots

    def resolve(self, context, result=None):
        from .install import check_cancel
        check_cancel()
        if context.origin:
            # Source buffers retain the originating project/toolchain, not a GHC checkout cradle.
            project_context = replace(context, file=context.origin['file'], origin={})
            self.probe(project_context)
            parts = urlsplit(context.origin.get('documentation', ''))
            if parts.scheme != 'file' or parts.netloc:
                return None
            html = Path(url2pathname(parts.path))
            # Verify that this is the installed documentation for the current source module.
            if not self.owner(html):
                return None
            original = self.map_source(project_context, html, '')
            if not original or original.kind != 'source' or Path(original.path).resolve() != Path(context.file).resolve():
                return None
            link = occurrence_link(html, context.text, context.point)
            if not link:
                return None
            return self.map_source(project_context, *link)
        target = import_target(context.text, context.point)
        if not target:
            return None
        self.probe(context)
        module, symbol = target
        self.selected = context.config.get('packages', [])
        docs = self.documents(module)
        if not docs:
            raise ValueError('No exact installed Haddock for ' + module)
        unit, document = docs[0]
        link = source_link(document, symbol)
        url = document.as_uri() + (('#v:' + symbol) if symbol else '')
        if not link:
            return Result(documentation=url, message='No local Haddock Source link')
        return self.map_source(context, *link, symbol=symbol)

    def owner(self, html):
        real_module = html.stem
        if not re.fullmatch(r"[A-Z][\w']*(?:\.[A-Z][\w']*)*", real_module):
            return None
        owners = set()
        for candidate in set(self.pkg_run('find-module', real_module, '--simple-output', '--show-unit-ids').split()):
            if candidate in self.units:
                raw = self.pkg_run('--ipid', 'field', candidate, 'haddock-html', '--simple-output')
                if any(html.resolve().is_relative_to(Path(d).resolve()) for d in package_paths(raw)):
                    owners.add(candidate)
        return owners.pop() if len(owners) == 1 else None

    def map_source(self, context, html, fragment, symbol=None):
        from .install import check_cancel
        url = html.as_uri() + (('#' + fragment) if fragment else '')
        fallback = Result(documentation=url, message='No verified .hs source; use lsp-bridge-source-install')
        location = source_location(html, fragment)
        if not location:
            return fallback
        line, definition = location
        real_module = html.stem
        if not re.fullmatch(r'[A-Z][\w\']*(?:\.[A-Z][\w\']*)*', real_module):
            return fallback
        owner = self.owner(html)
        if not owner:
            return fallback
        matches = []
        for root, root_unit in self.roots(context):
            if root_unit and root_unit != owner:
                continue
            # Index only registered roots, and only module-to-path, never symbols.
            index_file = root.parent / 'modules.json'
            files = ([root.parent / p for p in json.loads(index_file.read_text()).get(real_module, [])]
                     if root_unit is None and index_file.is_file()
                     else root.rglob(real_module.split('.')[-1] + '.hs'))
            for file in files:
                check_cancel()
                if not file.resolve().is_relative_to(root.resolve()):
                    continue
                if not str(file).replace('\\', '/').endswith(real_module.replace('.', '/') + '.hs'):
                    continue
                # For managed GHC roots, verify the library package version too.
                if root_unit is None:
                    name, version = self.units[owner]
                    cabals = [c for p in file.parents if p.is_relative_to(root)
                              for suffix in ('.cabal', '.cabal.in') for c in p.glob(name + suffix)]
                    major, minor, patch = self.version.split('.')
                    replacements = {'@ProjectVersion@': self.version,
                                    '@ProjectVersionForLib@': f'{major}.{int(minor):02d}{int(patch):02d}'}
                    def matching(cabal):
                        content = cabal.read_text(encoding='utf-8')
                        for key, value in replacements.items():
                            content = content.replace(key, value)
                        return re.search(r'^version\s*:\s*' + re.escape(version) + r'\s*$', content, re.M | re.I)
                    if not any(matching(c) for c in cabals):
                        continue
                text = file.read_text(encoding='utf-8')
                if not re.search(r'^\s*module\s+' + re.escape(real_module) + r'\b', mask_comments(text), re.M):
                    continue
                lines = text.splitlines()
                html_line = source_map(html)[0].get(line, '').rstrip('\r\n')
                if not 0 < line <= len(lines) or lines[line - 1] != html_line or not definition:
                    continue
                if symbol and not re.search(r'(?<![\w\'])' + re.escape(symbol) + r'(?![\w\'])', definition):
                    continue
                matches.append(file.resolve())
        if len(set(matches)) != 1:
            return fallback
        return Result(kind='source', path=str(matches[0]), line=line - 1,
                      character=source_map(html)[1].get(fragment, (line, 0))[1],
                      documentation=url, message='Verified against exact installed Haddock')

    def prepare(self, context):
        self.probe(context)
        try:
            existing = self.resolve(context) if context.text else None
        except ValueError:
            existing = None  # Missing documentation must not prevent explicit installation.
        if (existing and existing.kind == 'source') or any(unit is None for _, unit in self.roots(context)):
            return {'message': 'Matching registered sources already exist; no download', 'compiler': self.version}
        from .install import install
        return install(self.version, cache_root(context.config))
