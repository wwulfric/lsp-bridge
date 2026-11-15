from core.handler import Handler
from core.utils import *


class DocumentHighlight(Handler):
    name = "document_highlight"
    method = "textDocument/documentHighlight"
    # Emacs validates the document tick and symbol, allowing movement inside it.
    cancel_on_change = False
    provider = "document_highlight_provider"

    def process_request(self, position, serial) -> dict:
        self.serial = serial
        # textDocument will be injected by FileAction.send_server_request
        return {"position": position}

    def process_response(self, response: dict) -> None:
        if response is not None:
            eval_in_emacs("lsp-bridge-document-highlight-render", self.serial, response)
