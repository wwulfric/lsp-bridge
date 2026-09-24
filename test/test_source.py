"""Offline provider, integrity and definition routing regression tests."""
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.source import Context, provider, MANAGERS
from core.source.haskell import Haskell, import_target, source_link, source_location, cache_root
from core.source import install


class Imports(unittest.TestCase):
    def target(self, text, word, occurrence=0):
        start = text.find(word, occurrence)
        return import_target(text, start)

    def test_module_qualified_alias_multiline(self):
        for text, word, expected in [
            ('import Data.Traversable', 'Data', ('Data.Traversable', None)),
            ("import Data.Foo'", 'Data', ("Data.Foo'", None)),
            ('import qualified Data.Traversable as T', 'T', ('Data.Traversable', None)),
            ('import Data.Traversable qualified as T\n  (for, traverse)', 'for', ('Data.Traversable', 'for')),
            ('import Data.Traversable\n  ( for\n  , traverse\n  )', 'traverse', ('Data.Traversable', 'traverse')),
            ('-- 😀\nimport Data.Traversable (for)', 'for', ('Data.Traversable', 'for')),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.target(text, word), expected)

    def test_exclusions(self):
        for text, word in [
            ('-- import Data.Traversable (for)', 'for'),
            ('{- outer {- nested -} import Data.Traversable (for) -}', 'for'),
            ('import Data.Traversable hiding (for)', 'for'),
            ('import "base" Data.Traversable (for)', 'for'),
            ('#if FOO\nimport Data.Traversable (for)\n#endif', 'for'),
            ('import Data.Traversable\nf = for x y', 'for'),
            ('import Data.Traversable ((<$>))', '<$>'),
            ('import Data.Traversable ({- for -} traverse)', 'for'),
        ]:
            with self.subTest(text=text):
                self.assertIsNone(self.target(text, word))


class Mapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / 'docs'
        self.docs.mkdir()
        (self.docs / 'src').mkdir()
        self.document = self.docs / 'Data-Traversable.html'
        self.document.write_text('<p><a id="v:for">for</a><a href="src/GHC.Internal.Data.Traversable.html#for">Source</a></p>')
        self.html = self.docs / 'src/GHC.Internal.Data.Traversable.html'
        self.html.write_text('<pre><span id="line-1"></span>module GHC.Internal.Data.Traversable where\n'
                             '<span id="line-2"></span><span id="for"><span class="annottext">ignored\n tooltip</span>for</span> = flip traverse\n</pre>')
        self.src = self.root / 'source'
        file = self.src / 'GHC/Internal/Data/Traversable.hs'
        file.parent.mkdir(parents=True)
        file.write_text('module GHC.Internal.Data.Traversable where\nfor = flip traverse\n')
        self.file = file
        self.context = Context(language='haskell', text='import Data.Traversable (for)', point=25,
                               config={'roots': [{'compiler': 'ghc-9.12.2', 'unit': 'internal-unit', 'path': str(self.src)}],
                                       'cache': str(self.root / 'cache')})
        self.provider = Haskell()
        def probe(context):
            self.provider.version = '9.12.2'
            self.provider.units = {'base-unit': ('base','4.21'), 'internal-unit': ('ghc-internal','9.1202')}
            return {}
        self.provider.probe = probe
        def pkg(*args):
            if args[0] == 'find-module':
                return 'base-unit' if args[1] == 'Data.Traversable' else 'internal-unit'
            return str(self.docs)
        self.provider.pkg_run = pkg

    def tearDown(self):
        self.tmp.cleanup()

    def test_reexport_and_no_network(self):
        with patch.object(install, 'download', side_effect=AssertionError('network')):
            result = self.provider.resolve(self.context)
        self.assertEqual(result.kind, 'source')
        self.assertEqual(result.line, 1)
        self.assertEqual(result.path, str(self.file.resolve()))
        self.assertFalse(result.reusable_server)

    def test_installed_matching_root_prevents_download(self):
        with patch.object(install, 'install', side_effect=AssertionError('network')):
            self.assertIn('no download', self.provider.prepare(self.context)['message'])

    def test_unmatched_line_module_version(self):
        for text in ['module GHC.Internal.Data.Traversable where\nfor = other\n',
                     'module Other where\nfor = flip traverse\n']:
            self.file.write_text(text)
            self.assertEqual(self.provider.resolve(self.context).kind, 'documentation')
        self.context.config['roots'][0]['compiler'] = 'ghc-9.10.1'
        self.assertEqual(self.provider.resolve(self.context).kind, 'documentation')

    def test_missing_docs(self):
        self.document.unlink()
        with self.assertRaisesRegex(ValueError, 'No exact'):
            self.provider.resolve(self.context)

    def test_ambiguous_packages(self):
        self.provider.probe(self.context)
        self.provider.pkg_run = lambda *args: 'base-unit internal-unit' if args[0] == 'find-module' else str(self.docs)
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.provider.documents('Data.Traversable')

    def test_missing_anchor_and_online_link(self):
        self.assertIsNone(source_location(self.html, 'not-found'))
        self.document.write_text('<a href="https://example.com/source">Source</a>')
        self.assertIsNone(source_link(self.document, None))

    def test_duplicate_source_rejected(self):
        other = self.src / 'another/GHC/Internal/Data/Traversable.hs'
        other.parent.mkdir(parents=True)
        other.write_text(self.file.read_text())
        self.assertEqual(self.provider.resolve(self.context).kind, 'documentation')


class Archive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        install.CANCEL = None

    def tearDown(self):
        install.CANCEL = None
        self.tmp.cleanup()

    def archive(self, name, link=None):
        path = self.root / 'test.tar'
        with tarfile.open(path, 'w') as tar:
            item = tarfile.TarInfo(name)
            if link is not None:
                item.type = tarfile.SYMTYPE
                item.linkname = link
                tar.addfile(item)
            else:
                item.size = 4
                tar.addfile(item, io.BytesIO(b'test'))
        return path

    def test_paths_and_links(self):
        for name in ['../escape', '/absolute', 'C:/escape', 'a\\..\\b', 'aux.txt', 'x.']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                install.extract(self.archive(name), self.root / 'out')
        with self.assertRaises(ValueError):
            install.extract(self.archive('nested/link', '../../escape'), self.root / 'out')
        install.extract(self.archive('safe/file.hs'), self.root / 'out')
        self.assertEqual((self.root / 'out/safe/file.hs').read_text(), 'test')

    def test_cancel(self):
        install.CANCEL = str(self.root / 'cancel')
        Path(install.CANCEL).touch()
        with self.assertRaises(InterruptedError):
            install.extract(self.archive('safe.hs'), self.root / 'out')

    def test_checksum_failure_cleanup_and_retry(self):
        def wrong(url, path, limit):
            path.write_text('0' * 64 + '  ./ghc-9.12.2-src.tar.xz\n')
            return '1' * 64
        with patch.object(install, 'download', side_effect=wrong):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                    install.install('9.12.2', self.root)
        self.assertEqual(list((self.root / 'haskell/ghc/9.12.2').glob('.staging-*')), [])
        self.assertEqual(list(self.root.rglob('manifest.json')), [])

    def test_publication_version_isolation_and_reuse(self):
        import hashlib
        def fake(url, path, limit):
            if url.endswith('SHA256SUMS'):
                path.write_text(self.digest + '  ./ghc-9.12.2-src.tar.xz\n')
            else:
                path.write_bytes(self.archive_data)
            return hashlib.sha256(path.read_bytes()).hexdigest()
        archive = self.root / 'fixture.tar.xz'
        with tarfile.open(archive, 'w:xz') as tar:
            item = tarfile.TarInfo('ghc-9.12.2/libraries/base/Test.hs')
            data = b'module Test where\n'
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
        self.archive_data = archive.read_bytes()
        self.digest = hashlib.sha256(self.archive_data).hexdigest()
        with patch.object(install, 'download', side_effect=fake) as download:
            first = install.install('9.12.2', self.root)
            second = install.install('9.12.2', self.root)
            self.assertEqual(download.call_count, 2)
        self.assertEqual(first['path'], second['path'])
        self.assertIn(self.digest, first['path'])
        self.assertFalse((self.root / 'haskell/ghc/9.10.1').exists())
        self.assertTrue((Path(first['path']) / 'modules.json').is_file())


class Probe(unittest.TestCase):
    def test_toolchain_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'dist-newstyle/cache'
            p.mkdir(parents=True)
            (p / 'plan.json').write_text(json.dumps({'compiler-id':'ghc-9.10.1','install-plan':[]}))
            c = Context(file=str(Path(root) / 'Main.hs'))
            def run(cmd, *args):
                return {'--numeric-version':'9.12.2','--version':'GHC package manager version 9.12.2',
                        '--print-global-package-db':root, '--global':root}[args[0]]
            with patch('core.source.haskell.run', side_effect=run):
                with self.assertRaisesRegex(ValueError, 'Cabal plan and GHC differ'):
                    Haskell().probe(c)

    def test_explicit_environment_uses_configured_database(self):
        with tempfile.TemporaryDirectory() as root:
            context = Context(file=str(Path(root) / 'Main.hs'), config={
                'ghc': 'project-ghc', 'ghc_pkg': 'project-ghc-pkg',
                'packages': ['exact-unit'], 'package_dbs': [root]})
            calls = []
            def run(command, *args):
                calls.append(args)
                if args == ('--numeric-version',):
                    return '9.12.2'
                if args == ('--version',):
                    return 'GHC package manager version 9.12.2'
                if args in [('--print-global-package-db',), ('--global', 'list')]:
                    return root
                if 'field' in args:
                    self.assertIn('--package-db', args)
                    return 'pkg' if 'name' in args else '1.0'
                self.fail(str(args))
            with patch('core.source.haskell.run', side_effect=run):
                result = Haskell().probe(context)
                self.assertEqual(result['packages'], ['exact-unit'])

    def test_other_ecosystems_have_no_installer(self):
        for language in MANAGERS:
            self.assertIsNone(provider(Context(language=language)))


class Routing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.handler import find_define_base
        cls.routing = find_define_base

    def obj(self, name='find_define', token='request'):
        return SimpleNamespace(name=name, source_context=token, pos={'line':0,'character':0},
                               file_action=SimpleNamespace(filepath='/project/Main.hs', single_server=Mock(),
                                                           create_external_file_action=Mock(), send_server_request=Mock()))

    def test_existing_files_for_six_languages(self):
        with tempfile.TemporaryDirectory() as root:
            for extension in ('hs','java','rs','go','py','ts','js'):
                path = Path(root) / ('File.' + extension)
                path.touch()
                obj = self.obj()
                with patch.object(self.routing, 'eval_in_emacs') as send, patch.object(self.routing, 'get_lsp_file_host', return_value=''):
                    self.routing.find_define_response(obj, {'uri':path.as_uri(),'range':{'start':obj.pos}}, 'jump')
                    send.assert_called_once_with('jump', path.as_posix(), '', obj.pos)
                    obj.file_action.create_external_file_action.assert_called_once_with(path.as_posix())

    def test_virtual_uri_routes(self):
        for uri, resolver in [('jdt://contents/Foo','jdt_uri_resolver'), ('deno:asset/foo','deno_uri_resolver'),
                              ('csharp:/metadata/Foo','csharp_uri_resolver')]:
            obj = self.obj()
            with patch.object(self.routing, 'message_emacs'), patch.object(self.routing, 'eval_in_emacs') as send:
                self.routing.find_define_response(obj, {'targetUri':uri,'targetRange':{'start':obj.pos}}, 'jump')
                obj.file_action.send_server_request.assert_called_once_with(obj.file_action.single_server,resolver,uri,obj.pos,'jump')
                send.assert_not_called()

    def test_empty_missing_and_disabled(self):
        for response in [None, {'uri':'file:///missing-source.hs','range':{'start':{'line':0,'character':0}}}]:
            obj = self.obj()
            with patch.object(self.routing, 'eval_in_emacs') as send:
                self.routing.find_define_response(obj, response, 'jump')
                send.assert_called_once_with('lsp-bridge-source--fallback','/project/Main.hs','request',obj.pos)
        for name in ['find_type_define', 'find_implementation', 'find_define']:
            obj = self.obj(name=name, token=None)
            with patch.object(self.routing, 'eval_in_emacs') as send:
                self.routing.find_define_response(obj, None, 'jump')
                send.assert_called_once_with('lsp-bridge-find-def-fallback',obj.pos)


if __name__ == '__main__':
    unittest.main()
