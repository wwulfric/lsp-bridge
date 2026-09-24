"""Explicit network smoke test; never included in offline unittest discovery."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from core.source.install import install


def main():
    with tempfile.TemporaryDirectory(prefix='lsp-source-smoke-') as temporary:
        cache = Path(temporary)
        result = install('9.12.2', cache)
        path = Path(result['path'])
        assert (path / 'ghc-9.12.2/VERSION').read_text().strip() == '9.12.2'
        index = json.loads((path / 'modules.json').read_text())
        assert index['Data.Traversable']
        assert index['GHC.Internal.Data.Traversable']
        with patch('core.source.install.download', side_effect=AssertionError('duplicate download')):
            assert install('9.12.2', cache)['path'] == str(path)
        print('Official GHC archive verified, indexed, atomically published and reused.')


if __name__ == '__main__':
    main()
