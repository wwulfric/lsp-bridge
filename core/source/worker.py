"""Serial local resolver session; installations remain isolated one-shot tasks."""
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.source import Context, MANAGERS, provider
from core.source import install, session


def dispatch(request, cache):
    install.CANCEL = request.pop('cancel', None)
    action = request.pop('action')
    context = Context(**request)
    try:
        if action == 'resolve':
            cache.begin(context)
        else:
            session.ACTIVE = None
        instance = provider(context)
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
        return {'ok': True, 'result': output}
    except Exception as error:
        cache.commands.clear()  # Do not retain failures or cancellations.
        return {'ok': False, 'error': str(error)}
    finally:
        session.ACTIVE = None


def main():
    cache = session.Session()
    for line in sys.stdin:
        try:
            output = dispatch(json.loads(line), cache)
        except Exception as error:
            output = {'ok': False, 'error': str(error)}
        print(json.dumps(output), flush=True)
        if '--server' not in sys.argv:
            break


if __name__ == '__main__':
    main()
