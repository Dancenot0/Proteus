"""Interfaz de los modelos de simulación y utilidades comunes.

Un *modelo* es el comportamiento eléctrico de un componente. No sabe nada de
cables ni de tiempo: solo lee sus entradas y escribe sus salidas. El motor
(:mod:`voltio.sim.engine`) es quien lo orquesta.

Reglas del contrato
-------------------

* Toda salida que el modelo no escriba explícitamente queda en ``Z``
  (alta impedancia). Así un buffer tri-estado "suelta" el bus con solo no
  escribir nada.
* Las lecturas devuelven ``L`` (0/1/X/Z). Un bus flotante se lee ``X``.
* ``edge(nombre)`` informa de la transición del ciclo actual en esa entrada,
  que es lo que necesitan los elementos secuenciales.
* El estado interno vive en ``self.state`` y **persiste** entre ciclos.

Modelo de retardo: por defecto delta-cero (ideal). Cada parte puede declarar
``TPD`` (segundos) para que sus salidas se propaguen con retardo real.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..core.logic import L, X, Z, edge, from_int, to_int

__all__ = ["Model", "Ctx", "registry", "register_model", "make_model"]


class Ctx:
    """Contexto que el motor le pasa al modelo en cada evaluación."""

    __slots__ = ("_read", "_write", "_prev", "_widths", "_time", "_step",
                 "_engine", "extra")

    def __init__(self, read, write, prev, widths, time: float, step: int, engine=None):
        self._read = read
        self._write = write
        self._prev = prev
        self._widths = widths
        self._time = time
        self._step = step
        self._engine = engine
        self.extra: Dict[str, Any] = {}

    # -- lecturas ---------------------------------------------------------
    def get(self, pin: str, bit: int = 0) -> L:
        """Nivel de un bit de una entrada."""
        return self._read(pin, bit)

    def get_bus(self, pin: str, lo: int = 0, hi: Optional[int] = None) -> List[L]:
        """Bits de un pin vectorial, LSB primero. ``hi`` es inclusivo."""
        width = self.width(pin)
        if hi is None:
            hi = width - 1
        hi = min(hi, width - 1)
        lo = max(0, lo)
        return [self._read(pin, i) for i in range(lo, hi + 1)]

    def bus_int(self, pin: str, lo: int = 0, hi: Optional[int] = None) -> Optional[int]:
        """Valor entero de un bus, o ``None`` si algún bit es X/Z."""
        bits = self.get_bus(pin, lo, hi)
        if any(not b.determinate for b in bits):
            return None
        v = 0
        for i, b in enumerate(bits):
            v |= to_int(b) << i
        return v

    def width(self, pin: str) -> int:
        """Ancho en bits de un pin."""
        return self._widths.get(pin, 1)

    def all_settled(self, pin: str) -> bool:
        """True si todos los bits del pin son 0 o 1 inequívocos."""
        return all(self._read(pin, i).determinate for i in range(self.width(pin)))

    # -- escrituras -------------------------------------------------------
    def set(self, pin: str, value: L, bit: int = 0) -> None:
        self._write(pin, bit, value if isinstance(value, L) else from_int(int(value)))

    def set_bus(self, pin: str, bits: Sequence, lo: int = 0) -> None:
        for i, b in enumerate(bits):
            self._write(pin, lo + i, b if isinstance(b, L) else from_int(int(b)))

    def set_int(self, pin: str, value: Optional[int], width: Optional[int] = None,
                lo: int = 0) -> None:
        """Escribe un entero en un bus. ``None`` → todo a Z (suelta el bus)."""
        w = width if width is not None else self.width(pin)
        if value is None:
            for i in range(w):
                self._write(pin, lo + i, L.Z)
            return
        for i in range(w):
            self._write(pin, lo + i, L.HIGH if (value >> i) & 1 else L.LOW)

    def release(self, pin: str) -> None:
        """Suelta todas las patillas de un pin (alta impedancia)."""
        for i in range(self.width(pin)):
            self._write(pin, i, L.Z)

    # -- flancos ----------------------------------------------------------
    def prev(self, pin: str, bit: int = 0) -> L:
        """Nivel de la entrada en el ciclo anterior."""
        return self._prev(pin, bit)

    def edge(self, pin: str, bit: int = 0) -> int:
        """``+1`` flanco de subida, ``-1`` de bajada, ``0`` sin transición."""
        return edge(self._prev(pin, bit), self._read(pin, bit))

    def rose(self, pin: str, bit: int = 0) -> bool:
        return self.edge(pin, bit) > 0

    def fell(self, pin: str, bit: int = 0) -> bool:
        return self.edge(pin, bit) < 0

    # -- entorno ----------------------------------------------------------
    @property
    def time(self) -> float:
        return self._time

    @property
    def step(self) -> int:
        return self._step

    @property
    def engine(self):
        return self._engine


class Model:
    """Comportamiento de un componente.

    Subclase esto y sobreescribe :meth:`evaluate`. ``inputs`` y ``outputs`` se
    derivan solos de la plantilla de la parte.
    """

    #: nombre bajo el que se registra en :data:`registry`
    name: str = ""
    #: retardo de propagación por defecto (segundos)
    tpd: float = 0.0
    #: True si el modelo tiene memoria y reacciona a flancos. El motor lo
    #: evalúa una sola vez por evento, fuera del bucle de asentamiento, para
    #: que no se dispare varias veces con la misma transición.
    edge_sensitive: bool = False

    def __init__(self, ref: str, spec, props: Optional[Dict[str, Any]] = None):
        self.ref = ref
        self.spec = spec
        self.props: Dict[str, Any] = dict(props or {})
        self.state: Dict[str, Any] = {}
        self.inputs: List[str] = []
        self.outputs: List[str] = []
        self.inout: List[str] = []
        self._widths: Dict[str, int] = {}
        for p in spec.pins:
            self._widths[p.name] = max(p.width, 1)
            if p.direction == "in" or p.direction == "power":
                self.inputs.append(p.name)
            elif p.direction == "out":
                self.outputs.append(p.name)
            elif p.direction == "inout":
                self.inout.append(p.name)
                self.inputs.append(p.name)
                self.outputs.append(p.name)
        self.setup()

    # -- ciclo de vida ----------------------------------------------------
    def setup(self) -> None:
        """Se llama una vez al construir el modelo. Inicializa ``state``."""

    def evaluate(self, ctx: Ctx) -> None:
        """Calcula las salidas a partir de las entradas. **Sobreescribir.**"""
        # Por defecto: todo a alta impedancia (no drivea nada).
        for pin in self.outputs:
            ctx.release(pin)

    def on_probe(self, ctx: Ctx) -> Optional[str]:
        """Texto opcional que se muestra junto al símbolo (p. ej. el valor de un LCD)."""
        return None

    def interactive(self) -> bool:
        """True si el usuario puede cambiarlo en caliente desde la GUI."""
        return False

    def set_interactive(self, value: Any) -> None:
        """La GUI llama a esto cuando el usuario pulsa un interruptor."""

    # -- utilidades -------------------------------------------------------
    def prop(self, key: str, default: Any = None) -> Any:
        return self.props.get(key, default)

    def num_prop(self, key: str, default: float = 0.0) -> float:
        v = self.props.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def width_of(self, pin: str) -> int:
        return self._widths.get(pin, 1)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.ref}>"


# ---------------------------------------------------------------------------
# Registro de modelos
# ---------------------------------------------------------------------------

registry: Dict[str, type] = {}


def register_model(name: str):
    """Decorador: ``@register_model("nand")``."""

    def deco(cls):
        cls.name = name
        registry[name] = cls
        return cls

    return deco


class _Stub(Model):
    """Modelo comodín: componentes sin comportamiento eléctrico conocido.

    No simula nada (deja todo en Z) pero permite **dibujar** el circuito, que
    es lo que se necesita mientras se amplía la librería.
    """

    name = "_stub"


def make_model(ref: str, spec, props: Optional[Dict[str, Any]] = None) -> Model:
    """Instancia el modelo que corresponde a una plantilla."""
    key = spec.model or "_stub"
    cls = registry.get(key) or registry.get(spec.name.upper()) or _Stub
    return cls(ref, spec, props)
