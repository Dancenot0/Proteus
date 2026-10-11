"""Simulación digital por eventos con lógica de cuatro valores."""
from .models import Ctx, Model, make_model, register_model, registry
from . import parts as _parts  # registra todos los modelos en `registry`
from .engine import Engine, Problem, Waveform

__all__ = ["Engine", "Problem", "Waveform", "Model", "Ctx", "make_model",
           "register_model", "registry"]
