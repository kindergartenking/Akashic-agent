from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """A model-visible schema backed by server-side Python code."""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    async def execute(self, **arguments: Any) -> str:
        """Execute validated arguments and return model-readable text."""
