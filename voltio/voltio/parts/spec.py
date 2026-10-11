"""Definición estática de un componente: pines, propiedades, categoría, símbolo.

Aquí **no** hay comportamiento eléctrico: eso vive en :mod:`voltio.sim.models`.
Separarlo a propósito permite listar/validar/dibujar un circuito sin cargar el
simulador, y añadir un componente nuevo sin tocar el motor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.ir import Pin, PinDir

__all__ = ["PropSpec", "PartSpec", "SYMBOLS"]


@dataclass
class PropSpec:
    """Una propiedad editable de un componente (lo que ves en el diálogo de Proteus)."""

    name: str
    label: str = ""
    kind: str = "string"  # string | int | float | bool | choice
    default: Any = None
    choices: List[Any] = field(default_factory=list)
    hidden: bool = False

    def __post_init__(self):
        self.label = self.label or self.name


# Símbolos esquemáticos disponibles en el renderizador.
# Cada nombre corresponde a un dibujo vectorial en voltio/render/symbols.py.
SYMBOLS = (
    "gate2",        # compuerta de 2 entradas genérica (AND/OR/NAND/NOR/XOR)
    "inverter",
    "buffer",
    "tribuf",
    "ic",           # rectángulo con pines (el comodín)
    "flipflop",
    "register",
    "counter",
    "memory",
    "mcu",
    "resistor",
    "led",
    "7seg",
    "lcd",
    "switch",
    "pushbutton",
    "clock",
    "vcc",
    "gnd",
    "battery",
    "motor",
    "buzzer",
    "probe",
    "logicstate",
    "terminal",
)


@dataclass
class PartSpec:
    """Plantilla de un componente."""

    name: str
    category: str = "misc"
    description: str = ""
    pins: List[Pin] = field(default_factory=list)
    props: List[PropSpec] = field(default_factory=list)
    symbol: str = "ic"
    # Geometría del símbolo en unidades de rejilla (grid = 0.1" = 10 thou).
    width: float = 6.0
    height: float = 4.0
    # Nombre del modelo de simulación en voltio.sim.models (None = sin simular).
    model: Optional[str] = None
    # Datos extra para los exportadores (p. ej. prefijo SPICE).
    spice_prefix: str = "U"
    package: str = "DIP"
    datasheet: str = ""
    tags: List[str] = field(default_factory=list)

    # -- pines ------------------------------------------------------------
    def pin(self, name: str) -> Optional[Pin]:
        """Busca un pin por nombre.

        La coincidencia **exacta gana siempre**, y eso es esencial: en un
        74LS74 conviven ``Q`` y ``/Q``, que son dos salidas distintas. Si se
        normalizara la barra antes de comparar, ``pin("/Q")`` devolvería ``Q``
        y el circuito acabaría con la misma salida conectada a dos redes.

        Solo si no hay coincidencia exacta se tolera la barra: escribir
        ``CLR`` cuando el pin se llama ``/CLR`` es un olvido razonable y
        conviene aceptarlo. Pero nunca al revés — pedir ``Q`` no debe resolver
        a ``/Q``, porque ahí sí se está nombrando otro pin que existe.
        """
        if name is None:
            return None
        target = name.strip()
        for p in self.pins:
            if p.name == target:
                return p
        if target.startswith("/"):
            bare = target.lstrip("/")
            for p in self.pins:
                if p.name == bare:
                    return p
        else:
            for p in self.pins:
                if p.name == "/" + target:
                    return p
        return None

    def pin_names(self) -> List[str]:
        return [p.name for p in self.pins]

    def inputs(self) -> List[Pin]:
        return [p for p in self.pins if p.direction == PinDir.IN]

    def outputs(self) -> List[Pin]:
        return [p for p in self.pins if p.direction == PinDir.OUT]

    def inout(self) -> List[Pin]:
        return [p for p in self.pins if p.direction == PinDir.INOUT]

    def pin_count(self) -> int:
        """Total de patillas físicas (sumando el ancho de los vectoriales)."""
        return sum(max(p.width, 1) for p in self.pins)

    # -- propiedades ------------------------------------------------------
    def prop_names(self) -> List[str]:
        return [p.name for p in self.props]

    def defaults(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.props if p.default is not None}

    # -- construcción cómoda ---------------------------------------------
    def clone(self, name: str, **overrides) -> "PartSpec":
        import copy

        c = copy.deepcopy(self)
        c.name = name
        for k, v in overrides.items():
            setattr(c, k, v)
        return c


def _pin(name, direction=PinDir.IN, width=1, side="left", order=0,
         active_low=False, number=None, tri_state=False) -> Pin:
    """Fábrica de pines.

    ``tri_state=True`` marca una salida que puede soltar el cable en vez de
    empujarlo siempre. Sin esa marca el validador no distingue un bus con
    varios dispositivos colgando (correcto) de dos salidas push-pull
    enfrentadas (cortocircuito).
    """
    return Pin(
        name=name,
        direction=direction,
        width=width,
        side=side,
        order=order,
        active_low=active_low or name.startswith("/"),
        number=number,
        tri_state=tri_state,
    )


def gate_spec(name: str, desc: str, n_inputs: int, symbol: str,
              model: str, in_prefix: str = "A", out_name: str = "Y",
              extra_pins: Optional[List[Pin]] = None) -> PartSpec:
    """Fábrica para la familia 74xx de compuertas repetidas (00, 02, 04, 32...)."""
    pins: List[Pin] = []
    for i in range(n_inputs):
        pins.append(_pin(f"{in_prefix}{i}", PinDir.IN, side="left", order=i))
    pins.append(_pin(out_name, PinDir.OUT, side="right", order=0))
    if extra_pins:
        pins.extend(extra_pins)
    return PartSpec(
        name=name,
        category="74xx",
        description=desc,
        pins=pins,
        symbol=symbol,
        model=model,
        spice_prefix="U",
        tags=["digital", "logica", "74xx"],
        width=4.0,
        height=max(3.0, 1.0 + n_inputs),
    )
