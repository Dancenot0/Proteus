"""Escritor de proyectos de Proteus.

**Estado: investigación activa.** Este módulo NO genera todavía un ``.pdsprj``
que Proteus abra con tu circuito dentro. Genera el contenedor correcto con los
archivos de texto que sí sabemos escribir, y deja el ``ROOT.DSN`` binario
pendiente. Está aquí para que el camino esté trazado y el código de apoyo
(ZIP, XML, firmware) ya exista y probado.

Por qué no está terminado
-------------------------

El ``.pdsprj`` es un ZIP. Dentro, ``PROJECT.XML`` y ``FIRMWARE.XML`` son texto
plano y los escribimos sin problema. El esquemático vive en ``ROOT.DSN``, que
empieza por ``ISIS SCHEMATIC FILE`` y es un formato binario de registros **no
documentado**. De él ya sabemos bastante (ver ``docs/PROTEUS_DSN.md``):

* las cadenas llevan prefijo de longitud de 1 byte;
* hay una tabla de estilos al principio (COMPONENT, WIRE, BUS WIRE, PORT...);
* las propiedades de cada dispositivo son bloques de texto plano
  ``{CLAVE=VALOR}`` delimitados por NUL, con secciones ``{*DEVICE}``,
  ``{*PROPDEFS}``, ``{*INDEX}`` y ``{*COMPONENT}``.

Lo que falta es el *framing*: cómo se codifican las coordenadas de cada
símbolo, los identificadores que unen un cable con sus extremos, y la tabla de
instancias. Eso no se deduce leyendo un solo archivo; se deduce **comparando
muchos**.

El plan, en orden de coste creciente
------------------------------------

1. **Diseño asistido** (funciona hoy): Voltio dibuja el esquemático en SVG con
   los símbolos colocados y los cables encaminados, y tú lo reproduces en
   Proteus mirándolo. No es automático, pero elimina todo el trabajo de
   pensar la topología.

2. **EDIF 2 0 0** (implementado en :mod:`voltio.export.edif`): Proteus importa
   EDIF2 en su captura de esquemático. Lleva la conectividad completa, no las
   posiciones. Es la puerta oficial y no requiere ingeniería inversa.

3. **Netlist + instrucciones**: generar la lista de materiales y de conexiones
   y una secuencia ordenada de acciones ("coloca U1, une U1.3 con U2.11") que
   una persona —o un script de UI— ejecuta en Proteus.

4. **Escritor binario del DSN**: el objetivo final. Requiere el estudio
   comparativo descrito más abajo.

Cómo avanzar con el punto 4
---------------------------

El método que funciona con formatos binarios no documentados:

1. Crear en Proteus un proyecto con UN solo componente. Guardar.
2. Mover ese componente 10 unidades a la derecha. Guardar como otro archivo.
3. :func:`voltio.proteus.dsn.compare` sobre los dos archivos muestra exactamente
   qué bytes cambiaron. Esos bytes son la coordenada X.
4. Repetir con Y, con la rotación, con el nombre del componente, con una
   propiedad.
5. Luego con dos componentes, para ver cómo se asignan los identificadores.
6. Después añadir un cable y repetir: qué bytes aparecen, cómo referencian los
   extremos.
7. Con eso ya se puede escribir un generador.

Cada paso es media hora de trabajo. Lo caro no es ninguno, es hacerlos todos.
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core.ir import Circuit
from ..proteus.dsn import DSN_MAGIC, ProteusProject, open_project

__all__ = ["save", "write_container", "Result", "STATUS"]

STATUS = (
    "experimental: se genera el contenedor ZIP con PROJECT.XML y FIRMWARE.XML "
    "correctos, pero el ROOT.DSN binario todavía no lleva el circuito dibujado"
)


@dataclass
class Result:
    path: str
    ok: bool
    message: str
    entries: Dict[str, int]


def _project_xml(circuit: Circuit, release: str = "800") -> str:
    now = int(time.time())
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
        "<PROJECT>\n"
        f' <TIMESTAMP RELEASE="{release}" MODIFIED="{now}" FILEVER="{release}" '
        f'CUSTID="{abs(hash(circuit.name)) % 2**31}" CREATED="{now}"/>\n'
        " <SCRIPTS>\n"
        "  <STRING>PWRRAILS.DAT</STRING>\n"
        " </SCRIPTS>\n"
        "</PROJECT>\n"
    )


def _firmware_xml(circuit: Circuit) -> tuple:
    """Devuelve ``(FIRMWARE.XML, {ruta: contenido})`` con el firmware declarado.

    Los proyectos de Proteus con microcontrolador guardan aquí qué controlador
    usa cada uno y dónde está su código. Esto sí lo sabemos escribir: es XML y
    texto plano, y es lo que hace que un proyecto lleve su firmware dentro.
    """
    entries: Dict[str, str] = {}
    controllers: List[str] = []
    for ref, part in circuit.parts.items():
        spec = part.spec
        if spec is None or spec.category not in ("microcontroladores", "cpu"):
            continue
        program = str(part.props.get("PROGRAM", "") or "")
        if not program:
            continue
        dev = spec.name.upper()
        folder = f"FIRMWARE/{dev}"
        if os.path.exists(program):
            with open(program, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        else:
            src = program  # se permite pegar el código directamente en la propiedad
        fname = f"{folder}/main.asm"
        entries[fname] = src
        entries[f"{folder}.XML"] = (
            "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
            f"<{dev}>\n"
            f" <SOURCE>{os.path.basename(fname)}</SOURCE>\n"
            f" <CODEGEN>AVRASM2</CODEGEN>\n"
            f" <DEVICE>{ref}</DEVICE>\n"
            f"</{dev}>\n"
        )
        controllers.append((dev, ref, f"{folder}.XML"))

    if not controllers:
        return (
            "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
            "<FIRMWARE/>\n",
            entries,
        )

    body = "".join(
        f" <CONTROLLER NAME=\"{dev}\" REF=\"{ref}\" CONFIG=\"{cfg}\"/>\n"
        for dev, ref, cfg in controllers
    )
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
        "<FIRMWARE>\n" + body + "</FIRMWARE>\n",
        entries,
    )


def _pwrrails_dat() -> bytes:
    """El script de rieles de alimentación. Contenido observado en archivos reales."""
    return b"PWRRAILS\x00\x00\x00\x00\x00\x00\x00\x00\x00"


def _placeholder_dsn(circuit: Circuit, template: Optional[bytes] = None) -> bytes:
    """El ``ROOT.DSN``.

    Si se pasa una plantilla (un DSN real de un proyecto de Proteus), se usa
    tal cual: el proyecto se abrirá mostrando la plantilla, no tu circuito,
    pero al menos será un archivo válido y podrás comparar.

    Sin plantilla se emite una cabecera mínima. **Proteus no va a abrir esto**:
    falta todo el cuerpo binario. Se genera de todos modos para que las
    herramientas de comparación tengan algo con lo que trabajar y para dejar
    explícito hasta dónde llega hoy el escritor.
    """
    if template:
        return template
    title = (circuit.title or circuit.name or "Design")[:30]
    head = bytearray()
    head += DSN_MAGIC
    head += b"\x1a\x00\x00"
    head += title.encode("latin-1", "replace") + b"\x00"
    head += b"plate\x00"
    head += b"Voltio\x00"
    head += b"\x00" * 200
    return bytes(head)


def write_container(circuit: Circuit, path, template_dsn: Optional[bytes] = None,
                    release: str = "800") -> Result:
    """Escribe el contenedor ``.pdsprj``."""
    firmware_xml, firmware_files = _firmware_xml(circuit)
    entries: Dict[str, bytes] = {
        "PROJECT.XML": _project_xml(circuit, release).encode("utf-8"),
        "FIRMWARE.XML": firmware_xml.encode("utf-8"),
        "ROOT.DSN": _placeholder_dsn(circuit, template_dsn),
        "ROOT.CDB": b"",
        "SCRIPTS/PWRRAILS.DAT": _pwrrails_dat(),
    }
    for name, text in firmware_files.items():
        entries[name] = text.encode("utf-8")

    # El ZIP de Proteus guarda los nombres en mayúsculas y sin compresión
    # llamativa; replicamos eso para que la estructura coincida.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in entries.items():
            z.writestr(name, blob)

    sizes = {k: len(v) for k, v in entries.items()}
    if template_dsn:
        msg = (
            "contenedor escrito con un ROOT.DSN de plantilla: Proteus lo abrirá, "
            "pero mostrará el proyecto plantilla, no este circuito"
        )
        ok = True
    else:
        msg = (
            "contenedor escrito, pero el ROOT.DSN es un marcador de posición: "
            "Proteus NO lo abrirá. Ver el docstring de voltio/export/proteus.py "
            "y docs/PROTEUS_DSN.md para el plan de ataque."
        )
        ok = False
    return Result(path=str(path), ok=ok, message=msg, entries=sizes)


def save(circuit: Circuit, path, template: Optional[str] = None,
         release: str = "800", sidecars: bool = True) -> Result:
    """Exporta el circuito al mundo Proteus.

    Genera, junto al ``.pdsprj``, los archivos que SÍ se pueden meter en
    Proteus hoy por la vía oficial:

    * ``<nombre>.edf``  — EDIF 2 0 0. Proteus 8 lo importa en la captura de
      esquemáticos (File → Import → EDIF). Lleva la conectividad completa:
      todos los componentes y todos los cables, con sus nets. No lleva las
      posiciones (EDIF de netlist no las tiene), así que Proteus colocará las
      piezas y tú las ordenas; pero el circuito entra entero y correcto.
    * ``<nombre>.net``  — netlist plano legible, por si prefieres reconstruir
      a mano siguiendo la lista.
    * ``LEEME_PROTEUS.txt`` — los tres pasos exactos de importación.

    El ``.pdsprj`` en sí lleva el contenedor ZIP correcto (PROJECT.XML,
    FIRMWARE.XML) pero un ROOT.DSN marcador de posición: escribir el binario
    del esquemático exige tener descodificados los registros de cables y la
    base de componentes ROOT.CDB, que es la investigación en curso de
    ``docs/PROTEUS_DSN.md``. Este módulo lo dice en claro en vez de entregar
    un archivo que Proteus rechazaría sin explicación.

    ``template`` puede ser la ruta a un proyecto real de Proteus cuyo
    ``ROOT.DSN`` se reutilizará, para comprobar que el contenedor que
    generamos es aceptado por el programa.
    """
    tpl: Optional[bytes] = None
    if template:
        proj = open_project(template)
        tpl = next((b for n, b in proj.raw.items() if n.upper().endswith(".DSN")), None)
    res = write_container(circuit, path, template_dsn=tpl, release=release)

    if sidecars:
        base = os.path.splitext(str(path))[0]
        from .edif import to_edif
        from .netlist import to_netlist

        with open(base + ".edf", "w", encoding="utf-8") as f:
            f.write(to_edif(circuit))
        with open(base + ".net", "w", encoding="utf-8") as f:
            f.write(_netlist_plano(circuit))
        with open(base + "_LEEME_PROTEUS.txt", "w", encoding="utf-8") as f:
            f.write(_leeme(circuit, base))
        res.entries[os.path.basename(base) + ".edf"] = os.path.getsize(base + ".edf")
        res.entries[os.path.basename(base) + ".net"] = os.path.getsize(base + ".net")
    return res


def _netlist_plano(circuit: Circuit) -> str:
    """Netlist en texto plano, orientado a reconstrucción manual."""
    from .netlist import to_netlist

    NL = chr(10)
    out = ["* " + circuit.title,
           "* " + str(len(circuit.parts)) + " componentes", ""]
    for e in to_netlist(circuit):
        out.append(f"{e['ref']:<8} {e['part']:<12} {e.get('description','')}")
        for pin, node in sorted(e["pins"].items()):
            out.append(f"         pin {pin:<10} -> nodo {node}")
        out.append("")
    return NL.join(out) + NL


_LEEME = """Cómo meter "{titulo}" en Proteus 8 (probado contra 8.6 SP2)
============================================================

Lo que tu Proteus 8.6 acepta HOY por la interfaz:

  · File → Open Project            → archivos .pdsprj
  · File → Import Legacy Project   → esquemáticos .dsn / .DSN antiguos
  · (NO hay importación EDIF en el menú de 8.6: esa vía existe en la
     documentación de Labcenter pero no está expuesta en esta versión)

Estado de este paquete:

  {net}   netlist plano componente a componente y pin a pin.
             Con esto puedes reconstruir el esquema a mano en media
             hora, o esperar a que el escritor nativo esté listo.

  {edf}   EDIF 2 0 0. Inútil en tu 8.6 por lo dicho arriba, pero lo
            留 dejan otros programas (KiCad lo abre como netlist).

  {pdsprj}  contenedor de proyecto con ROOT.DSN marcador de
             posición: NO lo abras, se queda cargando para siempre
             (comprobado en tu máquina). Es el hueco que falta por
             rellenar: el binario del esquemático.

Por qué falta el .dsn bueno, en una frase: el formato guarda los
componentes como registros con cabecera y un blob de propiedades en
texto plano (eso ya se lee y se escribe), pero los CABLES viven en
registros cuya gramática aún no está cerrada (no aparecen como
registros de tipo 14 con coordenadas u32; se sospecha coordenadas u16
o un enum propio). Cada ronda de experimentos con tu Proteus como
oráculo cierra un campo; el protocolo está en
salida/proteus/experimentos/LEEME_EXPERIMENTOS.txt y los hallazgos en
docs/PROTEUS_DSN.md.

Generado por Voltio.
"""


def _leeme(circuit: Circuit, base: str) -> str:
    return _LEEME.format(
        titulo=circuit.title,
        edf=os.path.basename(base) + ".edf",
        net=os.path.basename(base) + ".net",
        pdsprj=os.path.basename(base) + ".pdsprj",
    )
