import unittest
from typing import Dict

import core.utils
from test.common import *


def get_offset(code: str, target: str):
    pos = code.find(target)
    assert pos >= 0
    return pos


class JumpOtherFile(unittest.TestCase):
    def jump(self, files: List[SingleFile], cursor_file: str, cursor_offset: int) -> str:
        result = ""

        def expectation(method: str, args: List[Any]) -> bool:
            nonlocal result
            if method == "lsp-bridge-define--jump":
                result = args[0]
                return True
            return False

        @with_multiple_files(files)
        @interceptor(expectation)
        def go(filenames: Dict[str, str]):
            eval_sexp(file_buffer(filenames[cursor_file], f"""
            (setq-local major-mode '{files[0].mode})
            (lsp-bridge-mode 1)
            (goto-char (+ (point-min) {cursor_offset}))
            (lsp-bridge-find-def)
            """))

        go()

        return result

    def test_jump_project(self):
        files = [
            SingleFile(
                filename="b.py",
                code="from a import *\nprint(a)\n",
                mode="python-mode",
            ),
            SingleFile(
                filename="a.py",
                code="a = 1\n",
                mode="python-mode",
            ),
        ]
        result_file = self.jump(files, "b.py", get_offset(files[0].code, "a)"))
        self.assertIn("a.py", result_file)

    def test_jump_external(self):
        files = [
            SingleFile(
                filename="b.py",
                code="import os",
                mode="python-mode",
            ),
        ]
        result_file = self.jump(files, "b.py", get_offset(files[0].code, "os"))
        self.assertIn("os", result_file)
