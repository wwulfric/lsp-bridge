"""Explicit GHC source acquisition. Only this module may access the network."""
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

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


def download(url, destination, limit):
    digest, total = hashlib.sha256(), 0
    check_cancel()
    with urlopen(url, timeout=15) as response, destination.open('wb') as output:
        if not response.url.startswith('https://downloads.haskell.org/'):
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
            progress('Downloading GHC sources: %d MiB' % (total // 1048576))
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
            progress('Extracting verified GHC sources')


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
