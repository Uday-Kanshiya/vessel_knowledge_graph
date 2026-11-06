"""
Windowing of a casefile's pages: size 5, overlap 1 by default (config).

A window is a list of pages sorted by pagenum. Every window has at most
`size` pages — the run's hard cap on mails per LLM call — and consecutive
windows share `overlap` pages so a reply is read next to what it answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class WindowSpec:
    size: int
    overlap: int

    def __post_init__(self):
        if self.size < 1:
            raise ValueError("window size must be >= 1")
        if not 0 <= self.overlap < self.size:
            raise ValueError("overlap must be >= 0 and < size")

    @property
    def stride(self) -> int:
        return self.size - self.overlap


def windows(items: Sequence[T], spec: WindowSpec) -> list[list[T]]:
    """Cut `items` into windows of `spec.size` with `spec.overlap` shared items."""
    n = len(items)
    if n == 0:
        return []
    out: list[list[T]] = []
    start = 0
    while True:
        end = min(start + spec.size, n)
        out.append(list(items[start:end]))
        if end >= n:
            break
        start += spec.stride
    return out


def window_count(n_items: int, spec: WindowSpec) -> int:
    if n_items == 0:
        return 0
    if n_items <= spec.size:
        return 1
    return 1 + -(-(n_items - spec.size) // spec.stride)  # ceil


def windows_after(pages: Sequence[T], spec: WindowSpec, last_pagenum: int, pagenum_of) -> list[list[T]]:
    """
    Windows still to run when pages up to `last_pagenum` are already folded in.
    Windows are cut over the full page list so the boundaries are stable across
    resumes; the ones whose last page is <= last_pagenum are dropped.
    """
    return [w for w in windows(pages, spec) if pagenum_of(w[-1]) > last_pagenum]
