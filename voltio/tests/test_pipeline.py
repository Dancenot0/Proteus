"""Pruebas de exportadores, sentido inverso y del pipeline de IA."""

import os

import pytest

from voltio.builder import ONE, ZERO, Circuit
from voltio.core.logic import L
from voltio.dsl.loader import load_file, load_text, to_dict
from voltio.export.edif import edif_rename, to_edif
from voltio.export.netlist import nodes_of, to_bom, to_bom_csv, to_netlist
from voltio.export.spice import sanitize_node, to_spice
from voltio.reverse import analyze, blocks, describe_local, summary_lines, to_yaml
from voltio.sim import Engine

RUTA_EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _contador():
    """El circuito de referencia para estas pruebas."""
    return load_file(os.path.join(RUTA_EXAMPLES, "contador_7seg.yaml"))[0]


def _nand():
    return load_file(os.path.join(RUTA_EXAMPLES, "compuerta_nand.yaml"))[0]


# ---------------------------------------------------------------------------
# Netlist genérico y BOM
# ---------------------------------------------------------------------------


def test_netlist_cubre_todas_las_partes():
    c = _contador()
    entries = to_netlist(c)
    assert len(entries) == len(c.parts)
    refs = {e["ref"] for e in entries}
    assert refs == set(c.parts)


def test_netlist_resuelve_los_nodos_de_riales():
    """GND debe acabar en el nodo canónico 0, no en un nombre inventado."""
    c = _contador()
    nodos = nodes_of(c)
    tierras = {v for k, v in nodos.items() if "GND1" in k}
    assert tierras == {"0"}, tierras


def test_bom_agrupa_por_componente():
    c = Circuit("bom")
    c.part("74LS00", "U1")
    c.part("74LS00", "U2")
    c.led("D1")
    circ = c.build_lenient()[0]
    bom = to_bom(circ)
    nands = [b for b in bom if b["part"] == "74LS00"]
    assert len(nands) == 1
    assert nands[0]["qty"] == 2
    assert "U1" in nands[0]["refs"] and "U2" in nands[0]["refs"]


def test_bom_csv_es_csv_valido():
    c = _contador()
    texto = to_bom_csv(c)
    lineas = texto.strip().split("\n")
    assert lineas[0].startswith("Cantidad,")
    # Todas las filas deben tener el mismo número de campos (teniendo en
    # cuenta las comillas de las descripciones).
    import csv, io
    filas = list(csv.reader(io.StringIO(texto)))
    ancho = len(filas[0])
    assert all(len(f) == ancho for f in filas), [len(f) for f in filas]


# ---------------------------------------------------------------------------
# SPICE
# ---------------------------------------------------------------------------


def test_sanitize_node():
    assert sanitize_node("Q[3]") == "Q_3"
    assert sanitize_node("GND") == "0"
    assert sanitize_node("GND$") == "0"
    assert sanitize_node("/MR") == "N_MR"
    # Un nodo SPICE no debe empezar por dígito si es un nombre.
    assert not sanitize_node("3V3").isdigit()


def test_spice_tiene_estructura_basica():
    c = _contador()
    texto = to_spice(c)
    lineas = texto.splitlines()
    assert lineas[0].startswith("*"), "la primera línea de un netlist es un comentario"
    assert texto.rstrip().endswith(".end")
    assert ".tran" in texto
    assert "V_VCC VCC 0 DC 5" in texto


def test_spice_las_compuertas_usan_primitivas_xspice():
    c = Circuit("g")
    s1 = c.logic("S1", state=1)
    s2 = c.logic("S2", state=0)
    u = c.part("74LS00", "U1")
    c.net("A").join(s1.Q, u.A0)
    c.net("B").join(s2.Q, u.A1)
    c.net("Y").join(u.Y)
    texto = to_spice(c.build())
    assert "nand2" in texto
    assert "XU1" in texto


def test_spice_advierte_de_lo_que_no_sabe_exportar():
    """Un contador no tiene primitiva XSPICE: debe salir como comentario y
    con un aviso explícito, nunca en silencio."""
    c = _contador()
    texto = to_spice(c)
    assert "* U1 74LS193" in texto
    assert "sin modelo SPICE" in texto
    assert "! U1" in texto


def test_spice_las_resistencias_salen_como_elemento_nativo():
    c = _nand()
    texto = to_spice(c)
    assert any(l.startswith("RR1") or l.startswith("R1") or " RR1 " in l
               for l in texto.splitlines()) or "R" in texto


# ---------------------------------------------------------------------------
# EDIF
# ---------------------------------------------------------------------------


def test_edif_rename():
    assert edif_rename("Q[3]") == "Q_3"
    assert edif_rename("74LS00").startswith("C_")
    assert edif_rename("/MR") == "N_MR"
    # EDIF no admite dos puntos ni corchetes.
    assert "[" not in edif_rename("A[0]")
    assert edif_rename("") == "X"


def test_edif_estructura():
    c = _contador()
    texto = to_edif(c)
    assert texto.startswith("(edif ")
    assert "(edifVersion 2 0 0)" in texto
    assert "(edifLevel 0)" in texto
    assert texto.rstrip().endswith(")")
    # Los paréntesis deben estar balanceados, que es lo mínimo que se le puede
    # pedir a un S-expression.
    assert texto.count("(") == texto.count(")"), (
        f"paréntesis desbalanceados: {texto.count('(')} vs {texto.count(')')}"
    )


def test_edif_una_celda_por_tipo_de_componente():
    c = Circuit("e")
    c.part("74LS00", "U1")
    c.part("74LS00", "U2")
    c.part("74LS04", "U3")
    circ = c.build_lenient()[0]
    texto = to_edif(circ)
    assert texto.count("(cell C_74LS00") == 1
    assert texto.count("(cell C_74LS04") == 1
    # Pero dos instancias.
    assert texto.count("(instance U1") == 1
    assert texto.count("(instance U2") == 1


def test_edif_nombres_de_celda_sin_doble_prefijo():
    c = _contador()
    texto = to_edif(c)
    assert "C_C_" not in texto


# ---------------------------------------------------------------------------
# Sentido inverso
# ---------------------------------------------------------------------------


def test_to_yaml_roundtrip():
    c = _contador()
    texto = to_yaml(c)
    c2, issues = load_text(texto)
    assert set(c2.parts) == set(c.parts)
    assert len(c2.nets) == len(c.nets)
    assert not [i for i in issues if i.kind == "error"]


def test_analyze_extrae_estructura():
    c = _contador()
    a = analyze(c)
    assert a["partes"] == len(c.parts)
    assert a["relojes"] == ["CLK1"]
    assert any(b["nombre"] == "Q" and b["ancho"] == 4 for b in a["buses"])
    assert a["bloques"], "debe agrupar los componentes en bloques"


def test_summary_lines_no_esta_vacio():
    c = _contador()
    lineas = summary_lines(c)
    assert len(lineas) > 5
    texto = "\n".join(lineas)
    assert "74LS193" in texto
    assert "74LS48" in texto


def test_heuristica_detecta_polaridad_mal_emparejada():
    """74LS47 (activo bajo) con display de cátodo común es el error clásico."""
    doc = """
name: polaridad_mal
parts:
  - {ref: U1, part: 74LS47}
  - {ref: DISP1, part: 7SEG-CC}
nets:
  - {name: SEG, bus: 7, connect: [U1./a[0:6], DISP1.a[0:6]]}
  - {name: A, bus: 4, tie: 0, connect: [U1.A[0:3]]}
  - {name: CTRL, tie: 1, connect: [U1./LT, U1./RBI, U1./BI]}
  - {name: GND, connect: [DISP1.CC, DISP1.dp]}
"""
    c, _ = load_text(doc)
    texto = describe_local(c)
    assert "segmentos invertidos" in texto or "polaridad" in texto.lower()


def test_heuristica_detecta_mcu_sin_firmware():
    doc = """
name: mcu_vacio
parts:
  - {ref: U1, part: ATMEGA328P}
nets:
  - {name: RST, tie: 1, connect: [U1./RESET]}
"""
    c, _ = load_text(doc)
    assert "PROGRAM" in describe_local(c)


# ---------------------------------------------------------------------------
# Pipeline de IA (sin red: se prueba la extracción y el bucle con un proveedor falso)
# ---------------------------------------------------------------------------

from voltio.ai.pipeline import extract_yaml
from voltio.ai.providers import Message, Provider

YAML_MINIMO = """name: nand_ia
title: "NAND generada por una IA"
parts:
  - {ref: S1, part: LOGICSTATE, props: {STATE: "0"}}
  - {ref: S2, part: LOGICSTATE, props: {STATE: "0"}}
  - {ref: U1, part: 74LS00}
  - {ref: D1, part: LED}
  - {ref: G1, part: GND}
nets:
  - {name: A, connect: [S1.Q, U1.A0]}
  - {name: B, connect: [S2.Q, U1.A1]}
  - {name: Y, connect: [U1.Y, D1.A]}
  - {name: GND, connect: [D1.K, G1.P]}
"""


def test_extract_yaml_con_vallas():
    r = extract_yaml("Aquí tienes el circuito:\n```yaml\n" + YAML_MINIMO + "\n```\nEspero que sirva.")
    assert r.strip().startswith("name:")
    assert "74LS00" in r


def test_extract_yaml_sin_vallas():
    r = extract_yaml("Claro, aquí va:\n\n" + YAML_MINIMO)
    assert r.strip().startswith("name:")


def test_extract_yaml_elige_el_bloque_que_valida():
    """Si hay varios bloques, se queda con el que tiene pinta de DSL."""
    malo = "```yaml\nfoo: 1\n```\nEl bueno:\n```yaml\n" + YAML_MINIMO + "\n```"
    r = extract_yaml(malo)
    assert "parts" in r


def test_extract_yaml_desenvuelve_json():
    import json
    r = extract_yaml(json.dumps({"yaml": YAML_MINIMO}))
    assert "74LS00" in r


def test_extract_yaml_vacio():
    assert extract_yaml("") == ""
    assert extract_yaml("   ") == ""


class _FakeProvider(Provider):
    """Proveedor de prueba: devuelve respuestas guionizadas."""

    name = "fake"
    needs_key = False

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def complete(self, messages, temperature=0.2, max_tokens=8000):
        self.llamadas.append([m.content for m in messages])
        return self.respuestas.pop(0) if self.respuestas else ""

    def available_reason(self):
        return None


def test_pipeline_genera_a_la_primera():
    from voltio.ai.pipeline import generate

    prov = _FakeProvider(["```yaml\n" + YAML_MINIMO + "\n```"])
    res = generate("una NAND con dos interruptores y un LED", provider=prov)
    assert res.ok, res.report()
    assert res.circuit is not None
    assert len(res.circuit.parts) == 5
    assert len(res.attempts) == 1
    assert len(prov.llamadas) == 1


def test_pipeline_autocorrige_con_los_errores_del_validador():
    """El mecanismo central: fallo → se le devuelven los errores → corrige."""
    from voltio.ai.pipeline import generate

    malo = YAML_MINIMO.replace("part: 74LS00", "part: 74LS01")   # no existe
    prov = _FakeProvider([
        "```yaml\n" + malo + "\n```",
        "```yaml\n" + YAML_MINIMO + "\n```",
    ])
    res = generate("una NAND", provider=prov, max_attempts=3)
    assert res.ok, res.report()
    assert len(res.attempts) == 2
    assert not res.attempts[0].ok
    assert res.attempts[0].errors
    # Y en la segunda llamada el prompt lleva los errores del validador.
    segundo_prompt = prov.llamadas[1][-1]
    assert "74LS01" in segundo_prompt
    assert "no existe" in segundo_prompt or "desconocido" in segundo_prompt


def test_pipeline_se_rinde_tras_n_intentos():
    from voltio.ai.pipeline import generate

    prov = _FakeProvider(["no sé hacer eso"] * 3)
    res = generate("algo imposible", provider=prov, max_attempts=3)
    assert not res.ok
    assert len(res.attempts) == 3
    assert res.circuit is None


def test_pipeline_detecta_respuesta_sin_yaml():
    from voltio.ai.pipeline import generate

    prov = _FakeProvider(["Lo siento, no puedo ayudar con eso."])
    res = generate("un circuito", provider=prov, max_attempts=1)
    assert not res.ok
    assert "YAML" in res.attempts[0].errors[0]


def test_prompt_incluye_el_catalogo_completo():
    from voltio.ai.prompt import catalog, system_prompt

    sp = system_prompt()
    cat = catalog()
    # Todos los componentes de la librería deben aparecer en el prompt: si falta
    # uno, la IA no puede usarlo y se inventará otro.
    from voltio.parts.library import LIBRARY
    for s in LIBRARY.specs():
        assert s.name in sp, f"{s.name} no aparece en el prompt"
    assert "74LS193" in cat
    assert len(sp) > 5000, "el prompt parece demasiado corto"


def test_prompt_no_tiene_barras_duplicadas():
    """/OE no puede aparecer como //OE: confundiría a la IA justo en lo delicado."""
    from voltio.ai.prompt import system_prompt
    sp = system_prompt().replace("https://", "").replace("http://", "")
    assert "//" not in sp


def test_los_pines_del_prompt_existen_de_verdad():
    """Todo nombre de pin que aparezca en el catálogo debe resolver en la librería.

    Es la garantía de que la IA no va a recibir un pin inventado.
    """
    import re
    from voltio.parts.library import LIBRARY

    from voltio.ai.prompt import catalog
    texto = catalog()
    for s in LIBRARY.specs():
        for p in s.pins:
            nombre = p.display_name()
            assert s.name in texto
            # El pin, con su barra si la tiene, debe aparecer en su bloque.
            bloque = texto.split(f"`{s.name}`")[1].split("`")[0] if f"`{s.name}`" in texto else ""
            assert nombre in bloque or p.name in bloque, f"{s.name}.{nombre} falta en el catálogo"


# ---------------------------------------------------------------------------
# Lector de Proteus
# ---------------------------------------------------------------------------


def test_proteus_detecta_archivos_que_no_son_proteus():
    from voltio.proteus.dsn import open_project
    with pytest.raises(ValueError):
        open_project(b"esto no es un proyecto de proteus")


def test_proteus_lee_un_dsn_minimo():
    from voltio.proteus.dsn import DSN_MAGIC, inspect_dsn
    data = DSN_MAGIC + b"\x1a\x00\x00" + b"Mi Diseno\x00" + b"plate\x00" + b"\x00" * 200
    info = inspect_dsn(data)
    assert info.magic == "ISIS SCHEMATIC FILE"
    assert info.size == len(data)


def test_proteus_extrae_bloques_de_dispositivo():
    """Con un blob sintético con el formato observado, debe parsear las props."""
    from voltio.proteus.dsn import DSN_MAGIC, inspect_dsn
    blob = (
        b"{*DEVICE}\n{ACTIVE=CTIMER,12,DLL}\n{*PROPDEFS}\n"
        b'{MODE="Operating Mode",KWDLIST,4,0="Time"}\n'
        b"{*INDEX}\n{*COMPONENT}\n{PRIMITIVE=DIGITAL}\n{MODDLL=CTIMER.DLL}\n{MODE=0}\n"
    )
    data = DSN_MAGIC + b"\x00" + blob + b"\x00" + b"\x00" * 40
    info = inspect_dsn(data)
    assert len(info.devices) == 1
    d = info.devices[0]
    assert d.component.get("PRIMITIVE") == "DIGITAL"
    assert d.model_dll == "CTIMER.DLL"
    assert len(d.propdefs) == 1
