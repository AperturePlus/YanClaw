"""Repair mojibake introduced at the human-bridge HTTP boundary.

A correctly-decoded Unicode page (the browser renders Chinese faculty pages
fine) can be re-corrupted when the userscript transport serializes the JSON
body: the page's UTF-8 bytes get reinterpreted as Latin-1 (ISO-8859-1), so a
correct heading like the Chinese for "talent recruitment" arrives as
byte-for-byte garble (every byte maps to one U+0080-U+00FF character). That
transcode is always losslessly reversible. ``repair_mojibake_text`` reverses it
only when the result is confidently real (the reversal yields CJK) and otherwise
returns the input unchanged, so clean ASCII, clean CJK, and genuine
Latin-accented text are all left untouched.

Note: UTF-8-bytes-misread-as-*GBK* is deliberately NOT auto-repaired -- that
transcode produces (wrong) CJK characters, so it is indistinguishable from
correct CJK and cannot be detected without risking real text.
"""

from __future__ import annotations

import re

# Real CJK (CJK Unified Ideographs, U+4E00-U+9FFF) signals a genuine recovery.
_CJK_RE = re.compile("[一-鿿]")
# Characters typical of UTF-8-bytes-misread-as-Latin-1 mojibake (Latin-1
# supplement + Latin Extended-A/B), used as a cheap "looks corrupted" gate.
_MOJIBAKE_HINT_RE = re.compile("[-ɏ]")


def repair_mojibake_text(text: str) -> str:
    if not text:
        return text
    # Already contains real CJK -> not corrupted in this way.
    if _CJK_RE.search(text):
        return text
    # No mojibake-shaped characters -> nothing to do (plain ASCII/Latin stays put).
    if not _MOJIBAKE_HINT_RE.search(text):
        return text
    try:
        recovered = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    # Only accept the recovery if it actually produced CJK text.
    if _CJK_RE.search(recovered):
        return recovered
    return text
