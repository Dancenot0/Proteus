"""GUI PySide6: el canvas interactivo.

Requiere ``pip install PySide6``. El resto de Voltio (motor, simulador,
renderizador SVG, exportadores, CLI) funciona sin ella.

Organización:

* :mod:`voltio.gui.canvas` — los ítems gráficos y el dibujado de símbolos.
* :mod:`voltio.gui.main`   — la ventana, los paneles y la barra de acciones.

Si PySide6 no está instalado, importar :mod:`voltio.gui` no revienta: expone
:func:`disponible` y :func:`motivo` para que la CLI pueda explicar qué falta.
"""

from __future__ import annotations

__all__ = ["disponible", "motivo", "run"]

_MOTIVO = ""
try:  # pragma: no cover - depende del entorno
    from PySide6 import QtWidgets  # noqa: F401
except Exception as _e:  # pragma: no cover
    _MOTIVO = f"{type(_e).__name__}: {_e}"


def disponible() -> bool:
    """True si PySide6 está importable."""
    return not _MOTIVO


def motivo() -> str:
    """Por qué no está disponible (cadena vacía si sí lo está)."""
    return _MOTIVO


def run(circuit=None, dsl_path: str = "", duration: float = 1e-3) -> int:
    """Arranca la aplicación. Devuelve el código de salida."""
    if not disponible():
        raise ImportError(
            "La GUI necesita PySide6:\n\n"
            "    pip install PySide6\n\n"
            f"(detalle: {_MOTIVO})"
        )
    from .main import run as _run

    return _run(circuit=circuit, dsl_path=dsl_path, duration=duration)
