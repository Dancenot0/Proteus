"""Lógica de cuatro valores: la base de todo el simulador.

En electrónica digital no basta con 0 y 1. Necesitamos:

* ``X`` — desconocido. Un flip-flop recién encendido no está en 0 ni en 1:
  está en un estado indeterminado. Si no modelamos esto, el simulador miente.
* ``Z`` — alta impedancia. Una salida tri-estado deshabilitada no emite 0,
  "se suelta". Esto es lo que permite que varios dispositivos compartan un bus.

Con estos cuatro valores podemos simular arquitecturas de hardware reales
(buses de datos compartidos, buffers tri-estado, resets asíncronos) y, sobre
todo, **detectar colisiones**: dos drivers peleando por el mismo cable.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Iterable, Iterator, Sequence

__all__ = [
    "L",
    "ZERO",
    "ONE",
    "X",
    "Z",
    "NOT",
    "AND",
    "NAND",
    "OR",
    "NOR",
    "XOR",
    "XNOR",
    "BUF",
    "TRIBUF",
    "tri",
    "bus_value",
    "to_int",
    "to_bool",
    "from_int",
    "bus_from_int",
    "bus_to_int",
    "is_determinate",
    "edge",
]


class L(IntEnum):
    """Un nivel lógico.

    Se ordena ``Z < X < 0 < 1`` únicamente para tener un orden estable al
    depurar; el orden **no** tiene significado eléctrico.
    """

    Z = 0  # alta impedancia (salida soltada)
    X = 1  # desconocido / indeterminado
    LOW = 2
    HIGH = 3

    # -- conveniencias ---------------------------------------------------
    def __str__(self) -> str:  # pragma: no cover - trivial
        return _SYM[self]

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return _SYM[self]

    def __invert__(self) -> "L":
        return NOT(self)

    def __and__(self, other: "L") -> "L":
        return AND(self, other)

    def __or__(self, other: "L") -> "L":
        return OR(self, other)

    def __xor__(self, other: "L") -> "L":
        return XOR(self, other)

    @property
    def determinate(self) -> bool:
        """True si es un 0 o un 1 inequívoco."""
        return self in (L.LOW, L.HIGH)

    @property
    def as_bit(self) -> int:
        """1 si es HIGH, 0 en cualquier otro caso (X/Z/LOW colapsan a 0)."""
        return 1 if self is L.HIGH else 0


_SYM = {L.Z: "Z", L.X: "X", L.LOW: "0", L.HIGH: "1"}

ZERO = L.LOW
ONE = L.HIGH

# Alias cortos: en el código de un simulador se lee mucho mejor ``X`` que ``L.X``.
globals()["X"] = L.X
globals()["Z"] = L.Z
X = L.X
Z = L.Z


def _l(v) -> L:
    """Coerce cualquier cosa razonable a :class:`L`."""
    if isinstance(v, L):
        return v
    if isinstance(v, bool):
        return L.HIGH if v else L.LOW
    if isinstance(v, int):
        return L.HIGH if v else L.LOW
    if isinstance(v, str):
        s = v.strip().upper()
        if s in ("1", "H", "HIGH", "VCC", "TRUE"):
            return L.HIGH
        if s in ("0", "L", "LOW", "GND", "FALSE"):
            return L.LOW
        if s in ("Z", "HI-Z", "HIGHZ", "FLOAT"):
            return L.Z
        if s in ("X", "U", "UNK", "UNKNOWN", "?"):
            return L.X
    raise TypeError(f"no se puede interpretar {v!r} como nivel lógico")


# ---------------------------------------------------------------------------
# Compuertas básicas
#
# Cada tabla está definida para los 16 casos posibles de dos entradas de 2 bits
# lógicos (0/1/X/Z). La regla general: cualquier entrada X propaga X, salvo que
# el otro lado ya determine el resultado (0 AND X = 0, 1 OR X = 1).
# ---------------------------------------------------------------------------


def NOT(a) -> L:
    """Inversor. ``NOT(X) = X``, ``NOT(Z) = X`` (un Z no es un nivel válido)."""
    a = _l(a)
    if a is L.HIGH:
        return L.LOW
    if a is L.LOW:
        return L.HIGH
    return L.X


def AND(a, b) -> L:
    a, b = _l(a), _l(b)
    # Un 0 domina: 0 AND cualquier cosa = 0.
    if a is L.LOW or b is L.LOW:
        return L.LOW
    if a is L.HIGH and b is L.HIGH:
        return L.HIGH
    return L.X


def NAND(a, b) -> L:
    return NOT(AND(a, b))


def OR(a, b) -> L:
    a, b = _l(a), _l(b)
    # Un 1 domina: 1 OR cualquier cosa = 1.
    if a is L.HIGH or b is L.HIGH:
        return L.HIGH
    if a is L.LOW and b is L.LOW:
        return L.LOW
    return L.X


def NOR(a, b) -> L:
    return NOT(OR(a, b))


def XOR(a, b) -> L:
    a, b = _l(a), _l(b)
    if not a.determinate or not b.determinate:
        return L.X
    return L.HIGH if (a is L.HIGH) != (b is L.HIGH) else L.LOW


def XNOR(a, b) -> L:
    return NOT(XOR(a, b))


def BUF(a, en=True) -> L:
    """Buffer. Si ``en`` es falso la salida se suelta (Z)."""
    a = _l(a)
    if not _l(en).determinate:
        return L.X
    return a if _l(en) is L.HIGH else L.Z


def TRIBUF(a, en) -> L:
    """Buffer tri-estado: el ladrillo básico de cualquier bus."""
    en = _l(en)
    if en is L.X:
        return L.X  # enable indeterminado → salida indeterminada
    if en is L.Z:
        return L.Z
    return _l(a) if en is L.HIGH else L.Z


def tri(a, en) -> L:
    """Alias de :func:`TRIBUF`."""
    return TRIBUF(a, en)


# ---------------------------------------------------------------------------
# Resolución de bus
# ---------------------------------------------------------------------------

# Matriz de resolución "wired": qué resulta cuando dos drivers empujan el mismo
# cable. 0 contra 1 es un cortocircuito → X (y el simulador lo reporta).
_RESOLVE = {
    (L.Z, L.Z): L.Z,
    (L.Z, L.X): L.X,
    (L.X, L.Z): L.X,
    (L.Z, L.LOW): L.LOW,
    (L.LOW, L.Z): L.LOW,
    (L.Z, L.HIGH): L.HIGH,
    (L.HIGH, L.Z): L.HIGH,
    (L.X, L.X): L.X,
    (L.X, L.LOW): L.X,
    (L.LOW, L.X): L.X,
    (L.X, L.HIGH): L.X,
    (L.HIGH, L.X): L.X,
    (L.LOW, L.LOW): L.LOW,
    (L.HIGH, L.HIGH): L.HIGH,
    (L.LOW, L.HIGH): L.X,  # ← COLISIÓN
    (L.HIGH, L.LOW): L.X,  # ← COLISIÓN
}


def bus_value(drivers: Iterable) -> L:
    """Resuelve el nivel de un cable con varios drivers conectados.

    >>> bus_value([L.Z, L.HIGH, L.Z])
    1
    >>> bus_value([L.LOW, L.HIGH])   # colisión
    X
    >>> bus_value([])                # cable flotante
    X
    """
    acc = L.Z
    for d in drivers:
        acc = _RESOLVE[(acc, _l(d))]
    # Un cable del que nadie tira no se queda en Z: se lee como indeterminado.
    return L.X if acc is L.Z else acc


def is_contention(drivers: Sequence) -> bool:
    """True si en el cable hay un 0 y un 1 peleando simultáneamente."""
    seen_low = seen_high = False
    for d in drivers:
        d = _l(d)
        seen_low |= d is L.LOW
        seen_high |= d is L.HIGH
    return seen_low and seen_high


# ---------------------------------------------------------------------------
# Conversiones
# ---------------------------------------------------------------------------


def to_int(v) -> int:
    """``HIGH → 1``, todo lo demás → ``0``."""
    return 1 if _l(v) is L.HIGH else 0


def to_bool(v) -> bool:
    return _l(v) is L.HIGH


def from_int(n: int) -> L:
    return L.HIGH if n else L.LOW


def is_determinate(v) -> bool:
    return _l(v).determinate


def bus_from_int(value: int, width: int) -> list:
    """``5, 4 → [LOW, HIGH, LOW, HIGH]`` (LSB primero)."""
    return [L.HIGH if (value >> i) & 1 else L.LOW for i in range(width)]


def bus_to_int(bits: Iterable) -> int:
    """``[LOW, HIGH, LOW, HIGH] → 5`` (LSB primero). Cualquier X/Z se lee 0."""
    out = 0
    for i, b in enumerate(bits):
        out |= to_int(b) << i
    return out


def edge(old, new) -> int:
    """Detecta flanco entre dos niveles.

    Devuelve ``+1`` (subida), ``-1`` (bajada) o ``0`` (sin cambio útil).
    Cualquier transición que involucre X/Z cuenta como ``0``: un flanco
    indeterminado no dispara nada.
    """
    old, new = _l(old), _l(new)
    if old is L.LOW and new is L.HIGH:
        return 1
    if old is L.HIGH and new is L.LOW:
        return -1
    return 0


def fmt_bus(bits: Iterable) -> str:
    """Representación legible de un bus, MSB a la izquierda: ``10X1``."""
    b = list(bits)
    return "".join(str(x) for x in reversed(b))


def scan(bits: Iterable) -> Iterator[L]:
    for b in bits:
        yield _l(b)
