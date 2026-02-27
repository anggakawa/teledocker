"""Utilities for splitting long messages to fit Telegram's 4096-character limit.

Splitting strategy (in priority order):
1. Never split inside a Markdown code block (between ``` fences).
2. Prefer splitting at code block boundaries (after a closing ```).
3. Otherwise split at the last newline before the limit.
4. Hard-cut at max_length only if no newline is available.
"""

_CODE_FENCE = "```"
_DEFAULT_MAX_LENGTH = 4096


def split_message(text: str, max_length: int = _DEFAULT_MAX_LENGTH) -> list[str]:
    """Split a long text into Telegram-safe chunks.

    Args:
        text: The full response text, potentially containing Markdown code blocks.
        max_length: Maximum character length per chunk (Telegram limit is 4096).

    Returns:
        A list of strings, each under max_length characters.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_length:
        candidate = remaining[:max_length]
        split_index = _find_safe_split_index(candidate)
        chunk = remaining[:split_index].rstrip()
        chunks.append(chunk)
        remaining = remaining[split_index:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks


def _find_safe_split_index(candidate: str) -> int:
    """Find the best position to split candidate text without breaking code blocks.

    We scan the candidate for ``` fences. If we would end up inside an open
    code block, we walk backwards to the last fence that closes a block.
    Otherwise we split at the last newline, or hard-cut at the end.
    """
    # Determine whether we are inside an open code block at candidate's end.
    fence_count = candidate.count(_CODE_FENCE)
    is_inside_code_block = (fence_count % 2) == 1

    if is_inside_code_block:
        # Walk backwards to find the opening ``` of the current block.
        last_fence_index = candidate.rfind(_CODE_FENCE)
        # Split just before that opening fence so we don't split mid-block.
        newline_before_fence = candidate.rfind("\n", 0, last_fence_index)
        if newline_before_fence > 0:
            return newline_before_fence
        # No newline before the fence — we have to hard-cut before the fence.
        return max(last_fence_index, 1)

    # Prefer splitting after the last closing fence (clean boundary).
    last_fence = candidate.rfind(_CODE_FENCE)
    if last_fence > 0:
        after_fence = last_fence + len(_CODE_FENCE)
        newline_after_fence = candidate.find("\n", after_fence)
        if newline_after_fence > 0:
            return newline_after_fence + 1

    # Fall back to splitting at the last newline in the candidate.
    last_newline = candidate.rfind("\n")
    if last_newline > 0:
        return last_newline + 1

    # Hard cut — no natural boundary found.
    return len(candidate)
