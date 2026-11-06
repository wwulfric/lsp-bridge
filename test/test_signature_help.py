import unittest
from unittest.mock import patch
from types import SimpleNamespace

from core.handler.signature_help import SignatureHelp


class SignatureSelection(unittest.TestCase):
    def update(self, response):
        with patch("core.handler.signature_help.eval_in_emacs") as update:
            SignatureHelp(SimpleNamespace(last_change=(0, 0))).process_response(response)
            return update.call_args.args

    def test_ranges_and_active_parameter_use_selected_overload(self):
        result = self.update({
            "activeSignature": 1, "activeParameter": 0,
            "signatures": [
                {"label": "f(x)", "parameters": [{"label": [2, 3]}]},
                {"label": "f(long, value)", "activeParameter": 1,
                 "parameters": [{"label": [2, 6]}, {"label": [8, 13]}]},
            ],
        })
        self.assertEqual(result[1:3], (["long", "value"], 1))
        self.assertEqual(result[4], 1)
        self.assertEqual(result[3][1], ["f(long, value)", [[2, 6], [8, 13]], 1])

    def test_invalid_signature_falls_back_to_first(self):
        for active in (-1, 3, None):
            with self.subTest(active=active):
                result = self.update({"activeSignature": active, "activeParameter": None,
                                      "signatures": [{"label": "f(x)",
                                                      "parameters": [{"label": "x"}]}]})
                self.assertEqual(result[1:3], (["x"], 0))
                self.assertEqual(result[4], 0)
