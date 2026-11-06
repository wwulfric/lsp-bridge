from core.handler import Handler
from core.utils import eval_in_emacs


def parameter_ranges(signature: dict) -> list:
    """Return where each parameter is in the signature's label, as [start, end]."""
    label = signature.get("label", "")
    ranges = []
    # Past the function's name, which may contain a parameter's.
    search_from = label.find("(") + 1
    for parameter in signature.get("parameters") or []:
        parameter_label = parameter.get("label")
        if isinstance(parameter_label, list) and len(parameter_label) == 2:
            start, end = parameter_label
        elif isinstance(parameter_label, str):
            start = label.find(parameter_label, search_from)
            if start < 0:
                continue
            end = start + len(parameter_label)
        else:
            continue
        ranges.append([start, end])
        search_from = end
    return ranges


class SignatureHelp(Handler):
    name = "signature_help"
    method = "textDocument/signatureHelp"
    cancel_on_change = True
    provider = "signature_help_provider"
    provider_message = "Current server not support signature help."

    def process_request(self, position) -> dict:
        return dict(position=position)

    def process_response(self, response: dict) -> None:
        if response is not None:
            if len(response.get("signatures") or []) > 0:
                active_signature = response.get("activeSignature") or 0
                if not 0 <= active_signature < len(response["signatures"]):
                    active_signature = 0
                selected = response["signatures"][active_signature]
                parameters = selected.get("parameters") or []

                arguments = []
                active_argument_index = selected.get("activeParameter", response.get("activeParameter"))
                if active_argument_index is None:
                    active_argument_index = 0
                for index, parameter in enumerate(parameters):
                    label = parameter["label"]
                    if isinstance(label, str): # most lsp server return string
                        argument = label if index == active_argument_index else label.split(":")[0]
                        arguments.append(argument)
                    elif isinstance(label, list) and len(label) == 2: # ccls return list
                        signatures_label = selected["label"]
                        arguments.append(signatures_label[label[0]:label[1]])

                # Every signature in full, for a popup that shows them all:
                # (LABEL PARAMETER-RANGES ACTIVE-PARAMETER), -1 for none.
                signatures = []
                for signature in response["signatures"]:
                    active_parameter = signature.get("activeParameter", response.get("activeParameter"))
                    signatures.append([signature.get("label", ""),
                                       parameter_ranges(signature),
                                       -1 if active_parameter is None else active_parameter])

                eval_in_emacs("lsp-bridge-signature-help--update", arguments, active_argument_index,
                              signatures, active_signature)
