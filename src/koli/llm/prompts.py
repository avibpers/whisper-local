# llm/prompts.py
# The messages each feature sends, written once. The Rephrase hotkey and the
# Rephrase voice command used to each carry a copy of the rewrite prompt.

REWRITE_RULE = 'Output ONLY the rewritten text with no preamble, no quotes, no commentary.'

DEFAULT_POLISH = ("Polish this dictation. Fix punctuation and capitalization only. Do not change wording "
                  "or add anything. Output ONLY the polished text:\n\n{text}")


# Polish every dictation: the user's instruction with {text} replaced (or
# followed by the text when it has no {text}).
def polish(template: str, text: str) -> list:
    template = template or DEFAULT_POLISH
    prompt = template.replace('{text}', text) if '{text}' in template else f"{template}\n\n{text}"
    return [{'role': 'user', 'content': prompt}]


# Rephrase (hotkey or voice command): a spoken or written instruction applied
# to the selected text.
def rewrite(instruction: str, selection: str) -> list:
    return [{'role': 'system', 'content': f"{instruction}\n\n{REWRITE_RULE}"},
            {'role': 'user', 'content': f"Input:\n{selection}"}]


# A transform: its instruction, then the selection.
def transform(instruction: str, selection: str) -> list:
    return [{'role': 'system', 'content': instruction},
            {'role': 'user', 'content': selection}]
