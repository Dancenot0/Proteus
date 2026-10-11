"""Netlist genérico: la vista "plana" del circuito.

Es la representación que todos los exportadores comparten. Un netlist no sabe
de dibujos ni de coordenadas: solo de qué pin está unido a qué nodo. Es
también lo que Proteus usa internamente para pasar del esquemático al PCB, así
que tenerlo bien resuelto es medio camino para cualquier integración.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..core.ir import Circuit, TIE_PART

__all__ = ["to_netlist", "to_bom", "nodes_of"]


def nodes_of(circuit: Circuit) -> Dict[str, str]:
    """Mapa ``etiqueta de extremo → nombre de nodo``.

    Las claves son ``REF.PIN`` o ``REF.PIN[bit]``. Los atados constantes se
    resuelven a los nodos canónicos ``0`` (tierra) y ``VCC``.
    """
    out: Dict[str, str] = {}
    for net in circuit.nets.values():
        tie = net.tie_value()
        # Un cable llamado GND/VCC ya es un riel; un `tie` también lo crea.
        canonical = net.name.upper().rstrip("$") if net.name else ""
        if tie is not None or canonical in ("GND", "VCC", "GND$", "VCC$"):
            node = "0" if (str(tie) == "0" or canonical.startswith("GND")) else "VCC"
        else:
            node = net.name or f"N${id(net) & 0xFFFF:04X}"
        for ep in net.endpoints:
            if ep.part == TIE_PART:
                continue
            key = f"{ep.part}.{ep.pin}" + (f"[{ep.bit}]" if ep.bit else "")
            out[key] = node
    return out


def to_netlist(circuit: Circuit) -> List[Dict[str, Any]]:
    """Lista de entradas ``{ref, part, props, pins: {pin: nodo}}``.

    Es la forma más cómoda de recorrer el circuito desde fuera: no depende de
    ningún formato concreto y sirve igual para SPICE, EDIF, BOM o el escritor
    de Proteus.
    """
    nodes = nodes_of(circuit)
    entries: List[Dict[str, Any]] = []
    for ref, part in circuit.parts.items():
        pins: Dict[str, str] = {}
        if part.spec is None:
            entries.append({"ref": ref, "part": part.part, "props": dict(part.props),
                            "pins": pins, "package": "", "spice_prefix": "U"})
            continue
        for p in part.spec.pins:
            for bit in range(max(p.width, 1)):
                key = f"{ref}.{p.name}" + (f"[{bit}]" if bit else "")
                if key in nodes:
                    pname = p.name + (f"[{bit}]" if bit else "")
                    pins[pname] = nodes[key]
        entries.append({
            "ref": ref,
            "part": part.part,
            "props": dict(part.props),
            "pins": pins,
            "package": part.spec.package,
            "spice_prefix": part.spec.spice_prefix,
            "category": part.spec.category,
            "description": part.spec.description,
        })
    return entries


def to_bom(circuit: Circuit) -> List[Dict[str, Any]]:
    """Lista de materiales agrupada: qué hay que comprar, y cuántos."""
    groups: Dict[str, Dict[str, Any]] = {}
    for ref, part in circuit.parts.items():
        spec = part.spec
        key = part.part
        g = groups.setdefault(key, {
            "part": part.part,
            "description": spec.description if spec else "",
            "package": spec.package if spec else "",
            "category": spec.category if spec else "",
            "refs": [],
            "qty": 0,
            "props": set(),
        })
        g["refs"].append(ref)
        g["qty"] += 1
        for k, v in part.props.items():
            if k in ("VALUE", "R", "C", "L", "COLOR", "FREQ"):
                g["props"].add(f"{k}={v}")
    out = []
    for g in groups.values():
        g["props"] = ", ".join(sorted(g["props"]))
        g["refs"] = ", ".join(sorted(g["refs"]))
        out.append(g)
    out.sort(key=lambda g: (g["category"], g["part"]))
    return out


def to_bom_csv(circuit: Circuit) -> str:
    """La BOM en CSV, lista para un Excel o para el departamento de compras."""
    rows = to_bom(circuit)
    lines = ["Cantidad,Componente,Descripción,Encapsulado,Categoría,Referencias,Propiedades"]
    for r in rows:
        lines.append(
            ",".join(_csv(x) for x in [
                r["qty"], r["part"], r["description"], r["package"],
                r["category"], r["refs"], r["props"],
            ])
        )
    return "\n".join(lines) + "\n"


def _csv(v: Any) -> str:
    s = str(v)
    if any(c in s for c in ',"\n'):
        return '"' + s.replace('"', '""') + '"'
    return s
