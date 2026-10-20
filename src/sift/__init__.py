"""Sift: bounded extractive compaction for chat histories."""

from .core import CompactionError, CompactionResult, compact_messages

__all__ = ["CompactionError", "CompactionResult", "compact_messages"]
