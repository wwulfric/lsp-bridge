"""One isolated task per helper; JSON on stdin/stdout, progress on stderr."""
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.source import Context, MANAGERS, provider
from core.source import install


def main():
    request = json.loads(sys.stdin.readline())
    install.CANCEL = request.pop('cancel', None)
    action = request.pop('action')
    context = Context(**request)
    instance = provider(context)
    try:
        if instance is None:
            output = {'message': MANAGERS.get(context.language, 'Sources are managed by the language server/toolchain')}
        elif action == 'install':
            output = instance.prepare(context)
        elif action == 'status':
            output = instance.probe(context)
            output['roots'] = [str(root) for root, _ in instance.roots(context)]
        else:
            result = instance.resolve(context)
            output = result.json() if result else {}
        install.check_cancel()
        print(json.dumps({'ok': True, 'result': output}), flush=True)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}), flush=True)


if __name__ == '__main__':
    main()
