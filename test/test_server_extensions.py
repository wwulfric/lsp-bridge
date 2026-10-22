import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.handler.completion import Completion
from core.lspserver import LspServer


class ConfigurationSection(unittest.TestCase):
    settings = {
        "java": {"format": {"enabled": True}},
        "jetbrains.kotlin.hints.parameters": True,
        "jetbrains.kotlin.hints.type.property": False,
        "jetbrains.java.hints.types.local variable": True,
    }

    def section(self, name):
        return LspServer.get_configuration_section(self.settings, name)

    def test_exact_and_nested(self):
        self.assertEqual(self.section("jetbrains.kotlin.hints.parameters"), True)
        self.assertEqual(self.section("java"), {"format": {"enabled": True}})
        self.assertEqual(self.section("java.format"), {"enabled": True})

    def test_flat_prefix(self):
        self.assertEqual(self.section("jetbrains.kotlin.hints"),
                         {"parameters": True, "type.property": False})

    def test_missing_and_whole(self):
        self.assertEqual(self.section("python.analysis"), {})
        self.assertIs(self.section(None), self.settings)


class CompletionCommands(unittest.TestCase):
    def run_completion(self, server_info, items):
        file_action = SimpleNamespace(
            last_change=(0, 0), completion_match_mode="prefix", completion_case_mode="ignore",
            completion_block_kind_list=False, enable_auto_import=False,
            display_label_max_length=100, completion_items={}, completion_items_limit=100,
            filepath="/work/app/A.kt", version=1,
            get_match_lsp_servers=lambda method: [],
            get_lsp_server_names=lambda: ["intellij-server"])
        completion = Completion(file_action)
        completion.method_server = SimpleNamespace(server_info=server_info,
                                                   completion_trigger_characters=["."])
        completion.method_server_name = server_info["name"]
        completion.prefix = "Ap"
        completion.position = {"line": 0, "character": 2}
        completion.version = 1
        with patch("core.handler.completion.eval_in_emacs") as eval_in_emacs:
            completion.process_response({"items": items})
        return eval_in_emacs.call_args.args[3]

    item = {"label": "App", "kind": 7,
            "textEdit": {"newText": "", "range": {"start": {"line": 0, "character": 2},
                                                  "end": {"line": 0, "character": 2}}},
            "command": {"title": "", "command": "jetbrains.kotlin.completion.apply", "arguments": [7]}}

    def test_configured_command_reaches_emacs(self):
        candidates = self.run_completion(
            {"name": "intellij-server",
             "completionItemCommands": [r"^jetbrains\..+\.completion\.apply$"]},
            [dict(self.item)])
        self.assertEqual(candidates[0]["command"]["command"], "jetbrains.kotlin.completion.apply")

    def test_other_servers_keep_ignoring_commands(self):
        candidates = self.run_completion({"name": "kotlin-language-server"}, [dict(self.item)])
        self.assertNotIn("command", candidates[0])


if __name__ == "__main__":
    unittest.main()
