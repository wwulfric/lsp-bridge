"""Acquire and verify source archives for GHC and Cabal dependencies."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener, urlopen

CANCEL = None
LAST_PROGRESS = 0


def check_cancel():
    if CANCEL and Path(CANCEL).exists():
        raise InterruptedError('Source task cancelled')


def progress(message):
    global LAST_PROGRESS
    check_cancel()
    now = time.monotonic()
    if now - LAST_PROGRESS >= .5:
        print(message, file=sys.stderr, flush=True)
        LAST_PROGRESS = now


@contextmanager
def lock(path):
    """OS releases this lock even if a helper crashes or is terminated."""
    with path.open('a+b') as stream:
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            while True:
                check_cancel()
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(.2)
        else:
            import fcntl
            while True:
                check_cancel()
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.2)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class RepositoryRedirect(HTTPRedirectHandler):
    """Follow a repository's HTTP(S) redirects, including mirror CDNs."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in ('http', 'https'):
            raise ValueError('Unsupported repository redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination, limit, origin="https://downloads.haskell.org/"):
    digest, total = hashlib.sha256(), 0
    check_cancel()
    open_url = build_opener(RepositoryRedirect()).open if origin is None else urlopen
    with open_url(url, timeout=15) as response, destination.open('wb') as output:
        if origin is not None and not response.url.startswith(origin):
            raise ValueError('Unexpected download redirect')
        while True:
            check_cancel()
            chunk = response.read1(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError('Download size limit exceeded')
            digest.update(chunk)
            output.write(chunk)
            progress('Downloading sources: %d MiB' % (total // 1048576))
    return digest.hexdigest()


def safe_path(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe archive path: ' + name)
    # Windows also treats these names specially, even with extensions.
    if any(part.endswith((' ', '.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part) for part in p.parts):
        raise ValueError('Nonportable archive path: ' + name)
    return p


def extract(archive, destination):
    total, names = 0, set()
    with tarfile.open(archive, 'r:*') as tar:
        for member in tar:
            check_cancel()
            relative = safe_path(member.name)
            target = destination.joinpath(*relative.parts)
            if member.issym() or member.islnk():
                # Validate links but do not create them. Navigation uses regular .hs files.
                link = member.linkname
                if PurePosixPath(link).is_absolute() or '\\' in link or ':' in link:
                    raise ValueError('Unsafe archive link')
                base = target.parent if member.issym() else destination
                if not (base / link).resolve().is_relative_to(destination.resolve()):
                    raise ValueError('Escaping archive link')
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError('Unsupported archive entry')
            key = str(relative).casefold()
            if key in names:
                raise ValueError('Duplicate archive entry')
            names.add(key)
            total += member.size
            if total > 2 * 1024 ** 3 or len(names) > 200000:
                raise ValueError('Expanded archive size limit exceeded')
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, target.open('xb') as dst:
                while True:
                    check_cancel()
                    chunk = src.read(256 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            progress('Extracting verified sources')


def repository_tarball(repo, name, version):
    """Build Cabal's package URL relative to the repository in the plan."""
    if not isinstance(repo, dict) or repo.get('type') not in ('secure-repo', 'remote-repo'):
        return None
    uri = repo.get('uri')
    if not isinstance(uri, str):
        return None
    base = urlsplit(uri)
    if base.scheme not in ('http', 'https') or not base.hostname:
        return None
    package = f'{name}-{version}.tar.gz'
    path = base.path.rstrip('/')
    if base.hostname == 'hackage.haskell.org' and path == '/packages/archive':
        path += f'/{name}/{version}/{package}'
    else:
        path += '/package/' + package
    return base._replace(path=path, fragment='').geturl()


def dependency_source(cache, name, version, digest, repo):
    """Publish the Cabal plan's exact source tarball once across resolvers."""
    if not (re.fullmatch(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*', name)
            and re.fullmatch(r'\d+(?:\.\d+)*', version)
            and re.fullmatch(r'[a-f0-9]{64}', digest)):
        raise ValueError('Invalid Cabal source package identity')
    package = f'{name}-{version}'
    base = cache / 'haskell/packages' / digest
    base.mkdir(parents=True, exist_ok=True)
    root = base / package
    with lock(base / '.install.lock'):
        if root.is_dir():
            return root
        with tempfile.TemporaryDirectory(dir=base) as staging:
            stage = Path(staging)
            archive = stage / 'source.tar.gz'
            try:
                repo_cache = Path(subprocess.check_output(
                    ['cabal', 'path', '--ignore-project', '--remote-repo-cache'], text=True,
                    stderr=subprocess.PIPE, timeout=20).strip())
            except (OSError, subprocess.SubprocessError):
                repo_cache = None
            if repo_cache and repo_cache.is_absolute():
                for local in repo_cache.glob(f'*/{name}/{version}/{package}.tar.gz'):
                    shutil.copyfile(local, archive)
                    with archive.open('rb') as stream:
                        checksum = hashlib.sha256()
                        for chunk in iter(lambda: stream.read(256 * 1024), b''):
                            check_cancel()
                            checksum.update(chunk)
                    if checksum.hexdigest() == digest:
                        break
                else:
                    archive.unlink(missing_ok=True)
            if not archive.exists():
                url = repository_tarball(repo, name, version)
                if not url:
                    raise ValueError('No verified local source archive for ' + package)
                if download(url, archive, 256 * 1024**2, origin=None) != digest:
                    raise ValueError('Package archive differs from Cabal plan: ' + package)
            unpacked = stage / 'unpacked'
            extract(archive, unpacked)
            package_root = unpacked / package
            if not (package_root / (name + '.cabal')).is_file():
                raise ValueError('Missing package description in archive: ' + package)
            check_cancel()
            package_root.rename(root)
    return root


def valid_manifest(manifest, version):
    """Incomplete/corrupt staging or cache metadata is never reusable."""
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
        root = 'ghc-' + version
        if (data.get('compiler') != root or data.get('root') != root
                or data.get('digest') != manifest.parent.name
                or not re.fullmatch('[0-9a-f]{64}', manifest.parent.name)
                or not (manifest.parent / root / 'libraries').is_dir()):
            return None
        index = json.loads((manifest.parent / 'modules.json').read_text(encoding='utf-8'))
        if not isinstance(index, dict):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def install(version, cache):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid compiler version')
    base = cache / 'haskell/ghc' / version
    base.mkdir(parents=True, exist_ok=True)
    with lock(base / '.install.lock'):
        # A duplicate helper waits for and reuses the first published result.
        for manifest in base.glob('*/manifest.json'):
            if valid_manifest(manifest, version):
                return {'message': 'Sources already installed', 'path': str(manifest.parent)}
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=base) as temporary:
            stage = Path(temporary)
            archive_name = 'ghc-' + version + '-src.tar.xz'
            origin = 'https://downloads.haskell.org/ghc/' + version + '/'
            sums = stage / 'SHA256SUMS'
            download(origin + 'SHA256SUMS', sums, 1024 * 1024)
            entries = [line.split() for line in sums.read_text().splitlines()]
            digests = [p[0].lower() for p in entries if len(p) == 2 and p[1].lstrip('*') in (archive_name, './' + archive_name)]
            if len(digests) != 1 or not re.fullmatch('[0-9a-f]{64}', digests[0]):
                raise ValueError('Official SHA256SUMS has no unique source archive checksum')
            digest = digests[0]
            archive = stage / archive_name
            if download(origin + archive_name, archive, 256 * 1024 ** 2) != digest:
                raise ValueError('GHC source SHA-256 mismatch')
            unpacked = stage / 'publish'
            unpacked.mkdir()
            extract(archive, unpacked)
            root = 'ghc-' + version
            if not (unpacked / root / 'libraries').is_dir():
                raise ValueError('Unexpected GHC source layout')
            progress('Indexing GHC source modules')
            index = {}
            for path in (unpacked / root / 'libraries').rglob('*.hs'):
                check_cancel()
                match = re.search(r'^\s*module\s+([A-Z][\w.\']*)', path.read_text(encoding='utf-8'), re.M)
                if match:
                    index.setdefault(match[1], []).append(str(path.relative_to(unpacked)))
            (unpacked / 'modules.json').write_text(json.dumps(index), encoding='utf-8')
            (unpacked / 'manifest.json').write_text(json.dumps({
                'compiler': 'ghc-' + version, 'digest': digest, 'root': root,
                'origin': origin + archive_name,
            }), encoding='utf-8')
            check_cancel()
            destination = base / digest
            if destination.exists():
                raise ValueError('Incomplete cache entry exists; move it aside before retrying: ' + str(destination))
            unpacked.rename(destination)  # Same filesystem, atomic publication.
            return {'message': 'Installed verified GHC sources; invoke navigation again', 'path': str(destination)}
