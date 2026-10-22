from core.handler import Handler
from core.handler.find_define_base import find_define_response
from core.utils import *


class FindTypeDefine(Handler):
    name = "find_type_define"
    method = "textDocument/typeDefinition"
    cancel_on_change = True

    def process_request(self, position) -> dict:
        return dict(position=position)

    def process_response(self, response: dict) -> None:
        if not response:
            message_emacs("No type define found.")
            return

        file_info = response[0]
        # volar return only LocationLink (using targetUri)
        fileuri = file_info["uri"] if "uri" in file_info else file_info["targetUri"]
        if not fileuri.startswith("file:"):
            # Library sources the server has to read for us.
            find_define_response(self, response, "lsp-bridge-define--jump")
            return
        filepath = uri_to_path(fileuri)
        range = file_info["range"] if "range" in file_info else file_info["targetRange"]
        startpos = range["start"]
        eval_in_emacs("lsp-bridge-define--jump", filepath, get_lsp_file_host(), startpos)
