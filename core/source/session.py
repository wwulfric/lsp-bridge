"""Bounded, process-local metadata caches. Never store copies of package sources."""
import json
import os
import shutil
import time
from pathlib import Path

ACTIVE = None


def stamp(path):
    path = Path(path).expanduser()
    try:
        stat = path.stat()
        return str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except OSError:
        return None


def watch(path):
    if ACTIVE is not None:
        ACTIVE.watch(path)


def database(path):
    watch(path)
    watch(Path(path) / 'package.cache')


class Session:
    """One serial resolver. Changes invalidate all tool results together.

    TTL is an additional bound for opaque tool wrappers, not a substitute for
    plan/database/tool tracking. Failures are never cached.
    """
    def __init__(self):
        self.commands = {}
        self.paths = {}
        self.key = None
        self.started = 0

    def watch(self, path):
        path = str(Path(path).expanduser().absolute())
        self.paths.setdefault(path, stamp(path))

    def begin(self, context):
        global ACTIVE
        key = json.dumps([context.config, context.project, dict(os.environ)], sort_keys=True)
        changed = any(stamp(p) != old for p, old in self.paths.items())
        if key != self.key or changed or time.monotonic() - self.started > 60:
            self.commands.clear()
            self.paths.clear()
            self.key, self.started = key, time.monotonic()
        ACTIVE = self
        # Include absent plan/config/tool paths: creation and symlink switches invalidate.
        origin = Path(context.origin.get('file', context.file)).resolve()
        for parent in origin.parents:
            self.watch(parent / 'dist-newstyle/cache/plan.json')
            self.watch(parent / 'cabal.project')
            self.watch(parent / 'cabal.project.local')
        # ghc-pkg prints nothing for a nonexistent user DB. Track its known
        # parent directories as well, so initial registration invalidates.
        for root in (Path.home() / '.ghc',
                     Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'ghc',
                     Path(os.environ.get('APPDATA', Path.home())) / 'ghc'):
            self.watch(root)
            if root.is_dir():
                for child in root.iterdir():
                    if child.is_dir():
                        self.watch(child)
                        database(child / 'package.conf.d')
        for db in os.environ.get('GHC_PACKAGE_PATH', '').split(os.pathsep):
            if db:
                database(db)
        for name in (context.config.get('ghc') or 'ghc', context.config.get('ghc_pkg') or 'ghc-pkg', 'cabal'):
            self.watch(shutil.which(name) or name)
        for path in (Path.home() / '.cabal/config',
                     Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'cabal/config'):
            self.watch(path)
        if os.environ.get('CABAL_CONFIG'):
            self.watch(os.environ['CABAL_CONFIG'])
        for path in context.config.get('package_dbs', []):
            database(path)

    def run(self, key, execute):
        if key not in self.commands:
            value = execute()
            if len(self.commands) >= 256:
                self.commands.clear()
            self.commands[key] = value
        return self.commands[key]
