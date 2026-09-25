"""Small, source-preserving checks for parser text and Markdown wrappers."""
from __future__ import annotations

import re


EMPTY_CODE_FENCE = re.compile(r"\A\s*```[^\n]*\n\s*```\s*\Z")


def local_content_empty(text: str, markdown: str) -> bool:
    """An empty LiteParse code block is formatting, not visible page content."""
    return not text.strip() and (not markdown.strip() or bool(EMPTY_CODE_FENCE.fullmatch(markdown)))
