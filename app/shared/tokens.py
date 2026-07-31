"""Token counting, with the answering model's own tokenizer.

⚠️ Counted, never estimated — and the reason is a specific failure mode.

Exceed the context window and most serving layers truncate **from the start**,
which removes the instructions first. The model then behaves as though it was
never told what to do: no error, no warning, just an answer that ignores every
rule set for it. It is the most likely way this system misbehaves without anyone
understanding why, and explicit counting is the only guard against it.

A characters-per-token estimate is not good enough for that job. The ratio varies
with content — English prose runs about 4.5 chars/token, a table of fare codes
far less — so an estimate is most wrong exactly where the text is densest, which
is where the budget is tightest.

See doc/components/04-conversation-memory.md §5.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Protocol

logger = logging.getLogger(__name__)

#: Fallback ratio, used only when the real tokenizer cannot be loaded. It is
#: deliberately PESSIMISTIC (fewer chars per token than English prose actually
#: runs) so the estimate over-counts and the budget errs toward dropping
#: something rather than toward silent truncation.
_FALLBACK_CHARS_PER_TOKEN = 3.0


class Tokenizer(Protocol):
    def encode(self, text: str) -> list[int]: ...


@lru_cache(maxsize=4)
def _load(repo: str) -> Tokenizer | None:
    """One tokenizer per repo, per process. Loading is slow; counting is not."""
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(repo)
    except Exception as exc:
        # Degrade to estimating rather than failing the request — but say so
        # loudly, because from here on the budget is approximate.
        logger.warning(
            "tokenizer %s unavailable (%s); falling back to a character estimate, "
            "so context budgets are approximate from here on",
            repo,
            exc,
        )
        return None


def count_tokens(text: str, repo: str) -> int:
    """Tokens in `text` according to `repo`'s tokenizer."""
    if not text:
        return 0
    tokenizer = _load(repo)
    if tokenizer is None:
        return int(len(text) / _FALLBACK_CHARS_PER_TOKEN) + 1
    return len(tokenizer.encode(text))


def is_exact(repo: str) -> bool:
    """Whether counting is real or estimated — recorded on the span, so a run
    whose budgets were approximate is identifiable after the fact."""
    return _load(repo) is not None
