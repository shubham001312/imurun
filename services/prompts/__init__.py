"""Prompt queue: user messages waiting while a task runs."""
from .queue import PromptQueue, QueuedPrompt

__all__ = ["PromptQueue", "QueuedPrompt"]
