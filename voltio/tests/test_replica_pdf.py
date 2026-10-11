"""Pruebas de la réplica de 'Compuertas NAND.PDF'.

El PDF es un esquemático de Proteus exportado. Se reconstruyó por geometría
(segmentos, cuerpos, burbujas, textos con posición), no por sus rótulos, y
estas pruebas fijan lo que el cableado computa de verdad.

El hallazgo importante: los rótulos booleanos que el autor escribió sobre los
cables NO coinciden con la lógica del cableado. Es un error de De Morgan
sistemático: al author ve una NAND con entradas ya complementadas y cree que
hace AND de los términos directos, cuando hace OR (NAND(x', y') = x + y).

Estas pruebas documentan ambas cosas: la función real (que es la que se
replica) y la discrepancia con los rótulos (para que nadie "corrija" la
réplica hacia los rótulos pensando que el motor se equivoca).
"""

import itertools
import os

from voltio.core.logic import L
from voltio.dsl.loader import load_file
from voltio.sim import Engine

RUTA = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                    "examples", "compuertas_nand_pdf.yaml")


def _tabla():
    """Las 16 combinaciones de S, V, R, D con los dos cables de salida."""
    filas = []
    for s, v, r, d in itertools.product((0, 1), repeat=4):
        c, issues = load_file(RUTA)
        assert not [i for i in issues if i.kind == "error"]
        eng = Engine(c)
        for ref, val in (("S", s), ("V", v), ("R", r), ("D", d)):
            eng.model(ref).set_interactive(val)
        eng.settle()
        u1d = 1 if eng.net_value("SAL_U1D") is L.HIGH else 0
        sal = 1 if eng.net_value("SALIDA") is L.HIGH else 0
        filas.append(((s, v, r, d), u1d, sal))
    return filas


def test_la_replica_carga_y_valida():
    c, issues = load_file(RUTA)
    assert not [i for i in issues if i.kind == "error"]
    # 4 entradas + 6 puertas, como en el original (sin sondas añadidas)
    assert len(c.parts) == 10


def test_inversores():
    """U1:A y U1:C están cableados como inversores: ambas entradas al mismo net."""
    c, _ = load_file(RUTA)
    s_net = c.net_of("U1A", "A0")
    assert s_net is not None and s_net is c.net_of("U1A", "A1")
    r_net = c.net_of("U1C", "A0")
    assert r_net is not None and r_net is c.net_of("U1C", "A1")


def test_funcion_real_del_cableado():
    """U1:D = (S'·V) + (R'+D)  y  U3:A = su complementario.

    Es lo que dice el cableado extraído del PDF. Nótese el OR: una NAND con
    las dos entradas ya complementadas suma, no multiplica. Ese es justo el
    paso que el autor original aplicó mal en sus rótulos.
    """
    for (s, v, r, d), u1d, sal in _tabla():
        esperado_u1d = int(((not s) and v) or ((not r) or d))
        esperado_sal = 1 - esperado_u1d
        assert u1d == esperado_u1d, (s, v, r, d, u1d)
        assert sal == esperado_sal, (s, v, r, d, sal)


def test_los_rotulos_del_pdf_no_coinciden_con_el_cableado():
    """Documenta la discrepancia: con los rótulos, habría otra tabla.

    Si algún día alguien "arregla" el ejemplo para que coincida con los
    rótulos del PDF, esta prueba fallará y obligará a decidir a conciencia
    cuál de las dos versiones es la especificación.
    """
    fa = lambda s, v, r, d: int(((not s) and v) and ((not r) or d))   # (S'.V).(R'+D)
    fb = lambda s, v, r, d: int((s and (not v)) and (r or (not d)))  # (S.V').(R+D')
    discrepancias_u1d = discrepancias_sal = 0
    for (s, v, r, d), u1d, sal in _tabla():
        if u1d != fa(s, v, r, d):
            discrepancias_u1d += 1
        if sal != fb(s, v, r, d):
            discrepancias_sal += 1
    # Deben existir discrepancias: esa es la finding. Si desaparecen, algo
    # cambió el circuito o mi lectura del PDF, y hay que mirarlo.
    assert discrepancias_u1d > 0
    assert discrepancias_sal > 0


def test_con_entradas_en_cero_como_en_el_pdf():
    """El PDF muestra los cuatro LOGICSTATE en 0. Con eso, la salida vale 1."""
    c, _ = load_file(RUTA)   # los STATE del YAML ya son 0
    eng = Engine(c)
    eng.settle()
    assert eng.net_value("SAL_U1D") is L.HIGH     # (0+1) = 1... ver fórmula
    assert eng.net_value("SALIDA") is L.LOW
