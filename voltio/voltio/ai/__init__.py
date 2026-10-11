"""Generación de circuitos con IA, en ambos sentidos."""
from .pipeline import Attempt, Result, describe, extract_yaml, generate
from .providers import (
    Message,
    Provider,
    ProviderError,
    available,
    get,
    names,
    pick_auto,
)
from .prompt import DSL_SPEC, catalog, repair_prompt, system_prompt, user_prompt

__all__ = [
    "generate", "describe", "extract_yaml", "Result", "Attempt",
    "Provider", "Message", "ProviderError", "get", "names", "available", "pick_auto",
    "system_prompt", "user_prompt", "repair_prompt", "catalog", "DSL_SPEC",
]
