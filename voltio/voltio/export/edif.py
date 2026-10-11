"""Exportador a EDIF 2 0 0.

EDIF (Electronic Design Interchange Format) es un estándar abierto de
intercambio de netlists en forma de S-expressions. Interesa aquí por una razón
concreta: **Proteus lo importa**. En el menú de schematic capture de Proteus
hay importación de EDIF2, que es la puerta oficial para meter un diseño
generado por fuera sin tener que escribir el binario propietario.

El resultado es un netlist (no un dibujo): EDIF lleva la conectividad y los
componentes, pero no la posición de los símbolos. Es decir, importa la *lógica*
del circuito; la presentación hay que reorganizarla dentro de Proteus. Para un
esquemático ya colocado, la ruta es el escritor ``.pdsprj`` de
:mod:`voltio.export.proteus`.

Estructura que se genera::

    (edif NOMBRE
      (edifVersion 2 0 0) (edifLevel 0) (keywordMap (keywordLevel 0))
      (status (written (timeStamp ...) (author "Voltio") (program "Voltio")))
      (library VOLTIOLIB (edifLevel 0)
        (technology (numberDefinition))
        (cell C_74LS00 (cellType GENERIC) (view NETLIST ...))
        ...)
      (design NOMBRE (cellRef TOP (libraryRef VOLTIOLIB))
        (view NETLIST (viewType NETLIST)
          (interface (port ...))
          (contents (instance ...) (net ...)))))
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Set, Tuple

from ..core.ir import Circuit, ep_token
from .netlist import to_netlist

__all__ = ["to_edif", "save", "edif_rename"]

_RENAME_CACHE: Dict[str, str] = {}


def edif_rename(name: str) -> str:
    """Convierte un nombre de Voltio en un identificador EDIF legal.

    EDIF solo permite letras, dígitos y guion bajo, y no puede empezar por
    dígito. ``Q[3] → Q_3``, ``/MR → N_MR``, ``74LS00 → C_74LS00``.
    """
    if name in _RENAME_CACHE:
        return _RENAME_CACHE[name]
    s = (name or "X").replace("[", "_").replace("]", "").replace("/", "N_")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_") or "X"
    if s[0].isdigit():
        s = "C_" + s
    _RENAME_CACHE[name] = s
    return s


def _indent(level: int) -> str:
    return "  " * level


def _cell_name(part: str) -> str:
    """Nombre de celda EDIF. ``edif_rename`` ya antepone ``C_`` si hace falta,
    así que aquí no se añade otro o salen cosas como ``C_C_74LS193``."""
    n = edif_rename(part)
    return n if n.startswith("C_") else "C_" + n


def to_edif(circuit: Circuit, design_name: Optional[str] = None,
            library_name: str = "VOLTIOLIB", include_ports: bool = True) -> str:
    """Genera el documento EDIF 2 0 0 completo."""
    entries = to_netlist(circuit)
    design = edif_rename(design_name or circuit.name)
    lib = edif_rename(library_name)
    out: List[str] = []

    # -- cabecera ----------------------------------------------------------
    stamp = time.strftime("%Y %m %d %H %M %S", time.gmtime())
    out.append(f"(edif {design}")
    out.append("  (edifVersion 2 0 0)")
    out.append("  (edifLevel 0)")
    out.append("  (keywordMap (keywordLevel 0))")
    out.append("  (status")
    out.append(f"    (written (timeStamp {stamp})")
    out.append(f'      (author "Voltio") (program "Voltio") (version "0.1")))')

    # -- librería: una celda por tipo de componente ------------------------
    out.append(f"  (library {lib}")
    out.append("    (edifLevel 0)")
    out.append("    (technology (numberDefinition))")

    seen_cells: Dict[str, Dict[str, Dict[str, str]]] = {}
    for e in entries:
        cell = _cell_name(e["part"])
        if cell in seen_cells:
            continue
        ports: Dict[str, str] = {}
        part_obj = circuit.parts[e["ref"]]
        spec = part_obj.spec
        if spec is not None:
            for p in spec.pins:
                direction = {
                    "in": "INPUT", "out": "OUTPUT", "inout": "INOUT", "power": "INPUT",
                }.get(p.direction, "INOUT")
                for bit in range(max(p.width, 1)):
                    pname = p.name + (f"[{bit}]" if p.width > 1 else "")
                    ports[edif_rename(pname)] = direction
        seen_cells[cell] = ports

    for cell, ports in sorted(seen_cells.items()):
        out.append(f"    (cell {cell}")
        out.append("      (cellType GENERIC)")
        out.append(f"      (view NETLIST (viewType NETLIST)")
        out.append("        (interface")
        for pname, direction in sorted(ports.items()):
            out.append(f"          (port {pname} (direction {direction}))")
        out.append("        )")
        out.append("      )")
        out.append("    )")
    out.append("  )")

    # -- diseño: celda superior con las instancias y las redes -------------
    out.append(f"  (design {design}")
    out.append(f"    (cellRef TOPCELL (libraryRef {lib}))")
    out.append("    (view NETLIST")
    out.append("      (viewType NETLIST)")
    out.append("      (interface")

    # Puertos externos: los extremos de red que no pertenecen a ninguna parte
    # se exponen como puertos del diseño, que es como EDIF representa las
    # entradas y salidas del bloque.
    ext_ports: Set[str] = set()
    if include_ports:
        connected = set()
        for e in entries:
            for pname in e["pins"]:
                connected.add(pname)
        for net in circuit.nets.values():
            for ep in net.endpoints:
                if ep.part == "$TIE":
                    ext_ports.add(edif_rename(net.name or "TIE"))
    for pname in sorted(ext_ports):
        out.append(f"        (port {pname} (direction INOUT))")
    out.append("      )")

    out.append("      (contents")
    for e in entries:
        cell = _cell_name(e["part"])
        out.append(f"        (instance {edif_rename(e['ref'])} "
                   f"(viewRef NETLIST (cellRef {cell} (libraryRef {lib})))")
        props = {k: v for k, v in e["props"].items() if v not in (None, "")}
        if props:
            out.append("          (property VALUES (string \"")
            for k, v in sorted(props.items()):
                out.append(f"            {k}={_escape(str(v))}")
            out.append("          \"))")
        out.append("        )")

    for net in circuit.nets.values():
        nname = edif_rename(net.name or "N")
        joins: List[str] = []
        for ep in net.endpoints:
            if ep.part == "$TIE":
                joins.append(f"(portRef {edif_rename(net.name or 'TIE')})")
                continue
            tok = ep_token(ep, circuit)          # "U1.Q[3]" o "U1.A"
            pname = tok.split(".", 1)[1] if "." in tok else ep.pin
            joins.append(
                f"(portRef {edif_rename(pname)} "
                f"(instanceRef {edif_rename(ep.part)}))"
            )
        if not joins:
            continue
        out.append(f"        (net {nname}")
        out.append("          (joined")
        for j in joins:
            out.append(f"            {j}")
        out.append("          )")
        if net.kind == "bus" and net.bus:
            out.append(f'          (property BUS (string "{edif_rename(net.bus)}"))')
            out.append(f'          (property BIT (string "{net.bit}"))')
        out.append("        )")

    out.append("      )")
    out.append("    )")
    out.append("  )")
    out.append(")")
    return "\n".join(out) + "\n"


def _pin_is_wide(circuit: Circuit, ep) -> bool:
    part = circuit.parts.get(ep.part)
    if part is None or part.spec is None:
        return False
    p = part.spec.pin(ep.pin)
    return bool(p and p.width > 1)


def _escape(s: str) -> str:
    return s.replace('"', '\\"')


def save(circuit: Circuit, path, **kw) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(to_edif(circuit, **kw))
    return path
