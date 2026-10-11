"""El cerrojo SR: memoria de un bit, y el ejemplo que mejor muestra por qué
hace falta un tercer valor lógico.

Un cerrojo sin inicializar NO está en 0 ni en 1: está indeterminado. Un
simulador de dos valores se inventaría un estado al arrancar y todo lo que
viniera después estaría construido sobre una mentira.
"""

import os

import pytest

from voltio.core.logic import L
from voltio.dsl.loader import load_file
from voltio.sim import Engine

RUTA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples", "sr_latch.yaml")


def _eng():
    c, issues = load_file(RUTA)
    assert not [i for i in issues if i.kind == "error"]
    return Engine(c)


def _q(eng, t):
    return eng.waveform("Q").at(t)


def test_arranca_indeterminado():
    eng = _eng()
    eng.run(until=0.005)
    assert _q(eng, 0.005) is L.X
    assert eng.waveform("nQ").at(0.005) is L.X


def test_set_y_memoria():
    eng = _eng()
    eng.schedule_change(0.010, "S", 0)
    eng.schedule_change(0.020, "S", 1)
    eng.run(until=0.025)
    assert _q(eng, 0.015) is L.HIGH          # durante el pulso
    assert _q(eng, 0.025) is L.HIGH          # y después: MANTIENE


def test_reset_y_memoria():
    eng = _eng()
    eng.schedule_change(0.010, "S", 0)       # set primero...
    eng.schedule_change(0.020, "S", 1)
    eng.schedule_change(0.030, "R", 0)       # ...luego reset
    eng.schedule_change(0.040, "R", 1)
    eng.run(until=0.045)
    assert _q(eng, 0.035) is L.LOW
    assert _q(eng, 0.045) is L.LOW           # mantiene el 0


def test_la_memoria_es_historia_no_entrada():
    """Con las mismas entradas (1,1), Q vale 1 o 0 según lo que pasó antes.

    Esta es la definición operativa de memoria, y es lo que ningún circuito
    combinatorio puede hacer.
    """
    eng = _eng()
    eng.schedule_change(0.010, "S", 0)
    eng.schedule_change(0.020, "S", 1)
    eng.run(until=0.025)
    tras_set = _q(eng, 0.025)

    eng2 = _eng()
    eng2.schedule_change(0.010, "R", 0)
    eng2.schedule_change(0.020, "R", 1)
    eng2.run(until=0.025)
    tras_reset = _q(eng2, 0.025)

    assert tras_set is L.HIGH
    assert tras_reset is L.LOW
    assert tras_set is not tras_reset        # mismas entradas, distinta salida


def test_estado_prohibido_las_dos_a_uno():
    """Con /S=/R=0 las dos salidas valen 1: es la firma del estado prohibido."""
    eng = _eng()
    eng.schedule_change(0.010, "S", 0)
    eng.schedule_change(0.010, "R", 0)
    eng.run(until=0.02)
    assert _q(eng, 0.02) is L.HIGH
    assert eng.waveform("nQ").at(0.02) is L.HIGH


def test_ledes_siguen_al_cerrojo():
    eng = _eng()
    eng.schedule_change(0.010, "S", 0)
    eng.schedule_change(0.020, "S", 1)
    eng.run(until=0.025)
    assert eng.model("D1").lit()             # Q=1 enciende el rojo
    assert not eng.model("D2").lit()         # /Q=0 apaga el verde


def test_sin_problemas_electricos():
    eng = _eng()
    eng.schedule_change(0.010, "S", 0)
    eng.schedule_change(0.020, "S", 1)
    eng.schedule_change(0.030, "R", 0)
    eng.schedule_change(0.040, "R", 1)
    eng.run(until=0.05)
    assert not eng.problems, eng.problems
