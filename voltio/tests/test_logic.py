"""Pruebas de la lógica de cuatro valores.

Aquí se verifica lo que sostiene todo lo demás: que las compuertas se comportan
correctamente con entradas indeterminadas, y que la resolución de bus detecta
las colisiones en vez de tragárselas.
"""

from voltio.core.logic import (
    AND,
    L,
    NAND,
    NOR,
    NOT,
    OR,
    TRIBUF,
    XOR,
    XNOR,
    bus_from_int,
    bus_to_int,
    bus_value,
    edge,
    fmt_bus,
)
from voltio.core.logic import is_contention


def test_valores_basicos():
    assert NOT(L.LOW) is L.HIGH
    assert NOT(L.HIGH) is L.LOW
    # Un nivel indeterminado no se puede invertir: sigue indeterminado.
    assert NOT(L.X) is L.X
    # Y una alta impedancia tampoco es un nivel: se convierte en X.
    assert NOT(L.Z) is L.X


def test_and_con_dominancia_del_cero():
    """Un 0 en una entrada AND determina el resultado aunque la otra sea X."""
    assert AND(L.LOW, L.X) is L.LOW
    assert AND(L.X, L.LOW) is L.LOW
    assert AND(L.HIGH, L.X) is L.X
    assert AND(L.HIGH, L.HIGH) is L.HIGH


def test_or_con_dominancia_del_uno():
    assert OR(L.HIGH, L.X) is L.HIGH
    assert OR(L.X, L.HIGH) is L.HIGH
    assert OR(L.LOW, L.X) is L.X
    assert OR(L.LOW, L.LOW) is L.LOW


def test_tablas_completas():
    vals = [L.LOW, L.HIGH, L.X, L.Z]
    for a in vals:
        for b in vals:
            assert NAND(a, b) is NOT(AND(a, b))
            assert NOR(a, b) is NOT(OR(a, b))
            assert XNOR(a, b) is NOT(XOR(a, b))


def test_xor_con_indeterminados():
    assert XOR(L.HIGH, L.LOW) is L.HIGH
    assert XOR(L.HIGH, L.HIGH) is L.LOW
    assert XOR(L.HIGH, L.X) is L.X
    assert XOR(L.LOW, L.Z) is L.X


def test_tristate():
    """El buffer tri-estado suelta el bus cuando no está habilitado."""
    assert TRIBUF(L.HIGH, L.HIGH) is L.HIGH
    assert TRIBUF(L.HIGH, L.LOW) is L.Z
    assert TRIBUF(L.HIGH, L.X) is L.X
    assert TRIBUF(L.LOW, L.HIGH) is L.LOW


def test_bus_un_solo_driver():
    assert bus_value([L.Z, L.HIGH, L.Z]) is L.HIGH
    assert bus_value([L.Z, L.LOW, L.Z]) is L.LOW


def test_bus_sin_drivers_lee_indeterminado():
    """Un cable del que nadie tira NO lee Z: lee X. Nadie lo está definiendo."""
    assert bus_value([]) is L.X
    assert bus_value([L.Z, L.Z]) is L.X


def test_bus_colision():
    """Un 0 y un 1 a la vez es un cortocircuito: se resuelve a X."""
    assert bus_value([L.HIGH, L.LOW]) is L.X
    assert bus_value([L.Z, L.LOW, L.HIGH, L.Z]) is L.X


def test_colision_no_con_indeterminados():
    """X contra 0 no es colisión: es indeterminación. Distinto de un corto."""
    assert not is_contention([L.X, L.LOW])
    assert not is_contention([L.Z, L.HIGH])
    assert is_contention([L.Z, L.LOW, L.HIGH])


def test_conversiones_de_bus():
    assert bus_from_int(5, 4) == [L.HIGH, L.LOW, L.HIGH, L.LOW]
    assert bus_to_int([L.HIGH, L.LOW, L.HIGH, L.LOW]) == 5
    assert bus_to_int(bus_from_int(0xAB, 8)) == 0xAB
    # Cualquier bit indeterminado se lee como 0 al convertir a entero.
    assert bus_to_int([L.X, L.HIGH]) == 2


def test_formato_de_bus():
    """Se imprime MSB a la izquierda, como en un esquema."""
    assert fmt_bus(bus_from_int(0b1010, 4)) == "1010"
    assert fmt_bus([L.X, L.HIGH, L.LOW]) == "01X"


def test_deteccion_de_flancos():
    assert edge(L.LOW, L.HIGH) == 1
    assert edge(L.HIGH, L.LOW) == -1
    assert edge(L.LOW, L.LOW) == 0
    # Un flanco que pasa por X no es un flanco: no debe disparar nada.
    assert edge(L.X, L.HIGH) == 0
    assert edge(L.LOW, L.X) == 0
    assert edge(L.Z, L.HIGH) == 0


def test_coercion_de_entradas():
    """Se aceptan bools, ints y cadenas, porque el DSL y la GUI los producen."""
    from voltio.core.logic import _l

    assert _l(True) is L.HIGH
    assert _l(0) is L.LOW
    assert _l("1") is L.HIGH
    assert _l("Z") is L.Z
    assert _l("hi-z") is L.Z
    assert _l("x") is L.X
