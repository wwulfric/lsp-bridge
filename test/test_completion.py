import unittest

from test.common import *


def try_complete(file: SingleFile, label: str):
    def must_include_completion(method: str, args: List[Any]):
        if method == "lsp-bridge-completion--record-items":
            # NOTE:
            # We need pick `candidates` from `lsp-bridge-completion--record-items`
            # if you change API of lsp-bridge-completion--record-items
            # you need replace below index of `args` with index of `candidates`
            items = args[2]
            for item in items:
                if item['label'] == label:
                    return True
        return False

    @with_file(file)
    @interceptor(must_include_completion)
    def complete_file(filename: str):
        eval_sexp(file_buffer(filename, f"""
            (setq-local major-mode '{file.mode})
            (lsp-bridge-mode 1)
            (goto-char (point-max))
            (delete-char -1)
            (sleep-for 5) ;; sleep here to give lsp server enough time to compute the *complete* completion list
            (insert "{file.code[-1]}")
        """))

    return complete_file()


class SimpleCompletion(unittest.TestCase):
    def test_python(self):
        try_complete(SingleFile(
            filename="test.py",
            code="import os\n\nos.system",
            mode="python-mode",
        ), label="system")

    def test_python_with_utf8(self):
        try_complete(SingleFile(
            filename="test.py",
            code="import os\n\ndef 测试():\n    os.system",
            mode="python-mode",
        ), label="system")
