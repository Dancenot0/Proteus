"""Pruebas del parser YAML propio y del cargador del DSL.

El parser incorporado existe para que el núcleo funcione sin instalar nada, así
que se prueba **forzándolo** (``native=True``) incluso cuando PyYAML está
disponible. Si no, los tests pasarían por PyYAML y no probarían nada.
"""

import pytest

from voltio.core import miniyaml
from voltio.dsl.loader import DslError, load_dict, load_text, parse_endpoint, to_dict


# ---------------------------------------------------------------------------
# Parser YAML
# ---------------------------------------------------------------------------


def _nativo(texto):
    return miniyaml._loads(texto, native=True)


def test_mapa_simple():
    assert _nativo("a: 1\nb: dos\n") == {"a": 1, "b": "dos"}


def test_anidado():
    d = _nativo("a:\n  b:\n    c: 3\n")
    assert d == {"a": {"b": {"c": 3}}}


def test_lista_de_mapas():
    d = _nativo("parts:\n  - ref: U1\n    part: 74LS00\n  - ref: U2\n    part: LED\n")
    assert d == {"parts": [{"ref": "U1", "part": "74LS00"},
                           {"ref": "U2", "part": "LED"}]}


def test_lista_en_linea():
    assert _nativo("c: [a, b, 3]\n") == {"c": ["a", "b", 3]}


def test_mapa_en_linea():
    assert _nativo("c: {a: 1, b: x}\n") == {"c": {"a": 1, "b": "x"}}


def test_lista_bajo_clave_al_mismo_nivel():
    """YAML legal y muy común: la lista alineada con la clave."""
    d = _nativo("c:\n- a\n- b\n")
    assert d == {"c": ["a", "b"]}


def test_lista_de_listas():
    d = _nativo("c:\n  - [1, 2]\n  - [3, 4]\n")
    assert d == {"c": [[1, 2], [3, 4]]}


def test_comentarios():
    d = _nativo("# cabecera\na: 1  # al margen\n# otra\nb: 2\n")
    assert d == {"a": 1, "b": 2}


def test_comillas_preservan_contenido():
    d = _nativo('a: "con # almohadilla"\nb: \'simple\'\n')
    assert d == {"a": "con # almohadilla", "b": "simple"}


def test_unidades_de_ingenieria():
    """`10k`, `100ns`, `1MHz`: lo que cualquiera escribe en electrónica."""
    d = _nativo("r: 10k\nt: 100ns\nf: 1MHz\nc: 4.7u\n")
    assert d["r"] == 10000
    assert d["t"] == pytest.approx(1e-7)
    assert d["f"] == pytest.approx(1e6)
    assert d["c"] == pytest.approx(4.7e-6)


def test_hexadecimal_y_binario():
    d = _nativo("a: 0xFF\nb: 0b1010\n")
    assert d == {"a": 255, "b": 10}


def test_booleanos_y_nulos():
    d = _nativo("a: true\nb: false\nc: null\nd:\n")
    assert d == {"a": True, "b": False, "c": None, "d": None}


def test_bloque_plegado():
    d = _nativo("desc: >\n  una linea\n  otra linea\n")
    assert d["desc"].strip() == "una linea otra linea"


def test_bloque_literal():
    d = _nativo("desc: |\n  linea1\n  linea2\n")
    assert "linea1\nlinea2" in d["desc"]


def test_indentacion_mala_da_error_con_linea():
    with pytest.raises(miniyaml.YamlError) as ei:
        _nativo("a: 1\n    b: 2\n")
    assert "línea" in str(ei.value) or "linea" in str(ei.value).lower()


def test_clave_duplicada_da_error():
    with pytest.raises(miniyaml.YamlError):
        _nativo("a: 1\na: 2\n")


def test_roundtrip():
    """Lo que se serializa se vuelve a parsear igual. Importa para --dsl-out
    y para el sentido inverso (circuito → YAML)."""
    original = {
        "name": "x",
        "parts": [{"ref": "U1", "part": "74LS00", "at": [1, 2],
                   "props": {"A": 1}}],
        "nets": [{"name": "N", "connect": ["U1.A0", "U1.A1"]}],
    }
    texto = miniyaml.dumps(original)
    assert miniyaml._loads(texto, native=True) == original


# ---------------------------------------------------------------------------
# Extremos del DSL
# ---------------------------------------------------------------------------


def test_parse_endpoint_formas():
    assert parse_endpoint("U1.A") == ("U1", "A", None)
    assert parse_endpoint("U1.Q[3]") == ("U1", "Q", 3)
    assert parse_endpoint("U1.Q[0:7]") == ("U1", "Q", (0, 7))
    assert parse_endpoint("U1.Q[*]") == ("U1", "Q", "*")
    assert parse_endpoint("U1./MR") == ("U1", "/MR", None)
    assert parse_endpoint("R1.1") == ("R1", "1", None)  # pines numéricos


def test_parse_endpoint_atados():
    assert parse_endpoint("$0") == ("$TIE", "0", None)
    assert parse_endpoint("$1") == ("$TIE", "1", None)
    assert parse_endpoint(0) == ("$TIE", "0", None)
    assert parse_endpoint(1) == ("$TIE", "1", None)


def test_parse_endpoint_invalido():
    with pytest.raises(DslError):
        parse_endpoint("garbage")
    with pytest.raises(DslError):
        parse_endpoint("")


# ---------------------------------------------------------------------------
# Cargador del DSL
# ---------------------------------------------------------------------------

MINIMO = """
name: minimo
parts:
  - ref: S1
    part: LOGICSTATE
  - ref: D1
    part: LED
nets:
  - name: N
    connect: [S1.Q, D1.A]
"""


def test_carga_minima():
    c, issues = load_text(MINIMO)
    assert len(c.parts) == 2
    assert len(c.nets) == 1
    assert not [i for i in issues if i.kind == "error"]


def test_referencia_duplicada():
    with pytest.raises(ValueError):
        load_text(MINIMO.replace("- ref: D1", "- ref: S1"))


def test_componente_desconocido_da_error_util():
    _, issues = load_text(MINIMO.replace("part: LED", "part: LED_MAGICO"))
    msgs = " ".join(str(i) for i in issues)
    assert "LED_MAGICO" in msgs


def test_pin_inexistente_sugiere_alternativa():
    """El mensaje debe proponer el pin correcto: la IA lo usa para autocorregirse."""
    bad = MINIMO.replace("D1.A", "D1.ANODO")
    _, issues = load_text(bad)
    msgs = " ".join(str(i) for i in issues)
    assert "ANODO" in msgs
    assert "Quisiste decir" in msgs or "Pines válidos" in msgs


def test_clave_desconocida_en_la_raiz():
    with pytest.raises(DslError) as ei:
        load_text("name: x\nparts: []\nnets: []\npatrs: []\n")
    assert "patrs" in str(ei.value)


def test_bus_genera_nets_por_bit():
    doc = """
name: bus
parts:
  - {ref: U1, part: 74LS273}
  - {ref: U2, part: 74LS273}
nets:
  - name: D
    bus: 8
    connect: [U1.Q[0:7], U2.D[0:7]]
"""
    c, issues = load_text(doc)
    assert len(c.nets) == 8
    assert all(f"D[{i}]" in c.nets for i in range(8))
    assert "D" in c.buses
    assert c.buses["D"].width == 8
    assert not [i for i in issues if i.kind == "error"]


def test_bus_con_ancho_incompatible_da_error():
    doc = """
name: bus_mal
parts:
  - {ref: U1, part: 74LS273}
nets:
  - name: D
    bus: 8
    connect: [U1.Q[0:3]]
"""
    _, issues = load_text(doc)
    errs = [i for i in issues if i.kind == "error" and "8" in i.message]
    assert errs, "debe haber un error de ancho de bus"
    # Y se reporta UNA sola vez, no una por bit: el ruido no le sirve a nadie,
    # y menos a la IA cuando está intentando autocorregirse.
    assert len(errs) == 1


def test_gnd_y_vcc_se_atan_solas():
    """Un net llamado GND se ata a 0 sin pedirlo: es lo que todo el mundo espera."""
    doc = """
name: rieles
parts:
  - {ref: U1, part: 74LS00}
nets:
  - {name: GND, connect: [U1.A0]}
  - {name: VCC, connect: [U1.A1]}
"""
    c, _ = load_text(doc)
    assert c.nets["GND"].tie_value().name == "LOW"
    assert c.nets["VCC"].tie_value().name == "HIGH"


def test_dos_salidas_push_pull_es_error():
    doc = """
name: corto
parts:
  - {ref: U1, part: 74LS00}
  - {ref: U2, part: 74LS00}
nets:
  - {name: A, connect: [U1.Y, U2.Y]}
  - {name: B, tie: 0, connect: [U1.A0, U1.A1, U2.A0, U2.A1]}
"""
    _, issues = load_text(doc)
    errs = [i for i in issues if i.kind == "error" and "salidas lógicas" in i.message]
    assert errs, "dos salidas en el mismo cable deben dar error"
    assert "U1.Y" in errs[0].message and "U2.Y" in errs[0].message


def test_dos_riales_opuestos_es_error():
    doc = """
name: rieles_opuestos
parts:
  - {ref: G1, part: GND}
  - {ref: V1, part: VCC}
nets:
  - {name: PELEA, connect: [G1.P, V1.P]}
"""
    _, issues = load_text(doc)
    assert any(i.kind == "error" and "alimentaciones opuestas" in i.message
               for i in issues)


def test_dos_salidas_tristate_en_el_mismo_bus_no_es_error():
    """Lo anterior es un corto; esto en cambio es un bus legítimo.

    La diferencia está en que las salidas son tri-estado: solo una drivea a la
    vez y las demás sueltan el cable. El validador no debe quejarse.
    """
    doc = """
name: bus_ok
parts:
  - {ref: U1, part: 74LS245}
  - {ref: U2, part: 74LS245}
nets:
  - {name: BUS, bus: 8, connect: [U1.B[0:7], U2.B[0:7]]}
  - {name: E1, tie: 1, connect: [U1./E]}
  - {name: E2, tie: 1, connect: [U2./E]}
  - {name: D1, tie: 0, connect: [U1.DIR]}
  - {name: D2, tie: 0, connect: [U2.DIR]}
"""
    _, issues = load_text(doc)
    assert not [i for i in issues if i.kind == "error"]


def test_varios_simbolos_de_tierra_no_son_cortocircuito():
    """Tener tres GND en el mismo cable es normalísimo y no debe dar error."""
    doc = """
name: muchas_tierras
parts:
  - {ref: G1, part: GND}
  - {ref: G2, part: GND}
  - {ref: U1, part: 74LS00}
nets:
  - {name: GND, connect: [G1.P, G2.P, U1.A0]}
"""
    _, issues = load_text(doc)
    assert not [i for i in issues if i.kind == "error"]


def test_entrada_sin_conectar_avis():
    _, issues = load_text(MINIMO)
    msgs = " ".join(str(i) for i in issues)
    assert "sin conectar" in msgs


def test_serializacion_inversa():
    """cargar → to_dict debe conservar las partes y las conexiones."""
    c, _ = load_text(MINIMO)
    d = to_dict(c)
    assert d["name"] == "minimo"
    refs = [p["ref"] for p in d["parts"]]
    assert set(refs) == {"S1", "D1"}
    conns = [x for n in d["nets"] for x in n["connect"]]
    assert "S1.Q" in conns and "D1.A" in conns


def test_el_dsl_serializado_vuelve_a_cargar():
    """Round-trip completo: texto → circuito → dict → texto → circuito."""
    c1, _ = load_text(MINIMO)
    texto = miniyaml.dumps(to_dict(c1))
    c2, _ = load_text(texto)
    assert set(c1.parts) == set(c2.parts)
    assert len(c1.nets) == len(c2.nets)


def test_tierra_no_se_duplica_si_hay_simbolo():
    """Un net llamado GND con su propio símbolo no lleva atado automático.

    Sin esto, el cargador añadía una fuente ideal ADEMÁS del componente GND,
    y el renderizador dibujaba dos símbolos de tierra en el mismo cable.
    """
    doc = """
name: tierra_una
parts:
  - {ref: G1, part: GND}
  - {ref: D1, part: 7SEG-CC}
nets:
  - {name: GND, connect: [G1.P, D1.CC, D1.dp]}
"""
    c, issues = load_text(doc)
    net = c.nets["GND"]
    assert net.tie_value() is None, "no debe haber atado automático junto al símbolo"
    assert not [i for i in issues if i.kind == "error"]


def test_tierra_automatica_sin_simbolo():
    """Y sin símbolo, el atado automático sigue existiendo: es la comodidad."""
    doc = """
name: tierra_auto
parts:
  - {ref: D1, part: 7SEG-CC}
nets:
  - {name: GND, connect: [D1.CC, D1.dp]}
"""
    c, issues = load_text(doc)
    net = c.nets["GND"]
    assert net.tie_value() is not None
    assert str(net.tie_value()) == "0"
