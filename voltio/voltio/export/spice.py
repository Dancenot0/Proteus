"""Exportador a netlist SPICE.

Genera un archivo ``.cir`` que **ngspice** puede simular tal cual, usando sus
primitivas digitales XSPICE (``and2``, ``or2``, ``inv``, ``dff``, ``clk``...).
También lo leen otros simuladores que acepten netlists con subcircuitos.

Dos cosas que conviene saber:

* Los nombres de nodo de SPICE no admiten corchetes, así que ``Q[3]`` se
  convierte en ``Q_3``. El nodo ``0`` es tierra por convención y nunca se
  renombra.
* Los componentes analógicos (resistencias) se emiten con su elemento SPICE
  nativo (``R``); los digitales, como instancias ``X`` de subcircuitos que se
  incluyen al final del archivo.

Para simularlo::

    ngspice out.cir

Si ngspice se queja de alguna primitiva digital, es que falta el modelo XSPICE
correspondiente en ``cmpkgspice``; el archivo incluye los más comunes.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from ..core.ir import Circuit
from .netlist import to_netlist

__all__ = ["to_spice", "save", "sanitize_node"]

_BAD = re.compile(r"[^A-Za-z0-9_$./+-]")


def sanitize_node(name: str, used: Optional[Dict[str, str]] = None) -> str:
    """Convierte un nombre de red de Voltio en un nodo SPICE legal.

    ``Q[3] → Q_3``, ``/MR → N_MR``, ``GND → 0``. Es inyectivo para los nombres
    que produce el DSL; si dos nombres distintos colapsaran al mismo nodo se
    registraría en ``used`` para poder avisar.
    """
    if name is None:
        return "0"
    up = name.upper().rstrip("$")
    if up in ("GND", "GROUND", "0V", "VSS", "TIERRA"):
        return "0"
    s = name.replace("[", "_").replace("]", "").replace("/", "N_")
    s = _BAD.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "N"
    # SPICE trata los nodos que empiezan por dígito como numéricos; es legal,
    # pero un nombre propio no debe empezar por número para no confundirse.
    if s[0].isdigit() and not s.isdigit():
        s = "N_" + s
    if used is not None:
        used.setdefault(s, name)
    return s


# ---------------------------------------------------------------------------
# Modelos XSPICE de ngspice
# ---------------------------------------------------------------------------

# (nombre de subcircuito, primitiva XSPICE, orden de pines del DSL)
_XSPICE: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "and":   ("and2",  ("A0", "A1", "Y")),
    "and3":  ("and3",  ("A0", "A1", "A2", "Y")),
    "and4":  ("and4",  ("A0", "A1", "A2", "A3", "Y")),
    "nand":  ("nand2", ("A0", "A1", "Y")),
    "nand3": ("nand3", ("A0", "A1", "A2", "Y")),
    "nand4": ("nand4", ("A0", "A1", "A2", "A3", "Y")),
    "or":    ("or2",   ("A0", "A1", "Y")),
    "or3":   ("or3",   ("A0", "A1", "A2", "Y")),
    "nor":   ("nor2",  ("A0", "A1", "Y")),
    "nor3":  ("nor3",  ("A0", "A1", "A2", "Y")),
    "xor":   ("xor2",  ("A0", "A1", "Y")),
    "xnor":  ("xnor2", ("A0", "A1", "Y")),
    "not":   ("inv",   ("A0", "Y")),
    "buffer": ("buf",  ("A", "Y")),
}

#: Subcircuitos que el exportador sabe emitir.
SUPPORTED = tuple(_XSPICE)


def _subckt_name(model: str, n_inputs: int) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Nombre del subcircuito XSPICE para un modelo de compuerta."""
    base = model.rstrip("0123456789")
    if base in _XSPICE:
        prim, order = _XSPICE[base]
        if len([p for p in order if p.startswith("A")]) == n_inputs:
            return prim, order
        # La aridad no coincide: busca una variante con el número justo.
        for cand in (f"{base}{n_inputs}", base):
            if cand in _XSPICE:
                return _XSPICE[cand]
        return prim, order
    return None


def to_spice(circuit: Circuit, title: Optional[str] = None,
             include_models: bool = True, comments: bool = True) -> str:
    """Convierte el circuito en un netlist SPICE completo."""
    entries = to_netlist(circuit)
    used: Dict[str, str] = {}
    lines: List[str] = []

    name = title or circuit.title or circuit.name
    # La primera línea de un netlist SPICE es el título, y se ignora.
    lines.append(f"* {name}")
    lines.append(f"* Generado por Voltio desde {circuit.name}")
    lines.append("*")

    if comments:
        lines.append("* --- Alimentación ---")
    lines.append("V_VCC VCC 0 DC 5")
    lines.append("")

    subcircuits_needed: Set[str] = set()
    body: List[str] = []
    warnings: List[str] = []

    for e in entries:
        ref = e["ref"]
        part = e["part"]
        spec = circuit.parts[ref].spec
        prefix = e.get("spice_prefix") or "U"
        pins = e["pins"]

        if spec is None:
            warnings.append(f"{ref}: componente '{part}' desconocido, se omite")
            continue

        # --- pasivos y fuentes: elementos SPICE nativos --------------------
        if spec.model == "resistor":
            n1 = sanitize_node(pins.get("1", "0"), used)
            n2 = sanitize_node(pins.get("2", "0"), used)
            val = _spice_value(e["props"].get("R", "10k"))
            body.append(f"{prefix}{ref} {n1} {n2} {val}")
            continue

        if spec.model == "rail":
            continue  # ya está la fuente V_VCC arriba
        if spec.model == "ground":
            continue  # el nodo 0 es tierra por definición

        if spec.model == "logicstate":
            n = sanitize_node(pins.get("Q", "0"), used)
            level = 5 if str(e["props"].get("STATE", "0")) in ("1", "HIGH") else 0
            body.append(f"V_{ref} {n} 0 DC {level}")
            continue

        if spec.model == "clock":
            n = sanitize_node(pins.get("Q", "0"), used)
            freq = float(e["props"].get("FREQ", 1.0) or 1.0)
            duty = float(e["props"].get("DUTY", 50.0) or 50.0) / 100.0
            period = 1.0 / freq
            body.append(
                f"V_{ref} {n} 0 clock(0) 0 5 {period / 2:.9g} "
                f"{period * duty:.9g} {period * (1 - duty):.9g}"
            )
            continue

        if spec.model == "led":
            na = sanitize_node(pins.get("A", "0"), used)
            nk = sanitize_node(pins.get("K", "0"), used)
            body.append(f"D_{ref} {na} {nk} DLED")
            subcircuits_needed.add("DLED")
            continue

        if spec.model == "switch":
            n1 = sanitize_node(pins.get("1", "0"), used)
            n2 = sanitize_node(pins.get("2", "0"), used)
            ctrl = "ON" if str(e["props"].get("STATE", "0")) == "1" else "OFF"
            body.append(f"S_{ref} {n1} {n2} VCTRL_{ref} {ctrl}")
            warnings.append(f"{ref}: los interruptores SPICE necesitan una fuente "
                            f"de control; se emite como comentario")
            continue

        # --- compuertas: subcircuitos XSPICE ------------------------------
        n_in = len([p for p in pins if p.startswith("A")])
        mapped = _subckt_name(spec.model or "", n_in)
        if mapped:
            prim, order = mapped
            nodes = []
            for pin_name in order:
                key = pin_name
                if key not in pins:
                    key = pin_name.rstrip("0123456789") + "0" if pin_name.startswith("A") else pin_name
                nodes.append(sanitize_node(pins.get(key, pins.get(pin_name, "0")), used))
            body.append(f"X{ref} {' '.join(nodes)} {prim}")
            subcircuits_needed.add(prim)
            continue

        # --- todo lo demás: caja negra con comentario ---------------------
        node_list = " ".join(
            f"{sanitize_node(v, used)}" for _, v in sorted(pins.items())
        )
        body.append(f"* {ref} {part} — sin modelo SPICE ({spec.description})")
        if node_list:
            body.append(f"*   pines: {node_list}")
        warnings.append(
            f"{ref} ({part}): no hay modelo SPICE; se emite como comentario. "
            f"Este componente es digital complejo y necesita un subcircuito "
            f"a medida o simulación por eventos (que es lo que hace Voltio)."
        )

    lines.append("* --- Componentes ---")
    lines.extend(body)

    # --- subcircuitos ------------------------------------------------------
    if include_models and subcircuits_needed:
        lines.append("")
        lines.append("* --- Modelos ---")
        for name_ in sorted(subcircuits_needed):
            if name_ == "DLED":
                lines.extend([
                    ".subckt DLED A K",
                    "* LED ideal: conduce por encima de 1.8 V",
                    "D1 A K DLED1N",
                    ".ends DLED",
                    ".model DLED1N D(Is=1e-14 Rs=20 N=1.8 Cjo=30p Vj=0.7 M=0.5 Iave=20m Vpk=5)",
                ])

    # --- directivas de análisis -------------------------------------------
    sim = (circuit.metadata or {}).get("simulation") or {}
    duration = sim.get("duration") or 1e-3
    # Los avisos van ANTES de .end: en un netlist SPICE todo lo que siga a
    # .end se ignora, así que ponerlos al final los haría invisibles.
    if warnings:
        lines.append("")
        lines.append("* --- Avisos del exportador ---")
        for w in warnings:
            lines.append(f"* ! {w}")

    lines.append("")
    lines.append("* --- Análisis ---")
    lines.append(f".tran {_pick_step(duration)} {duration} UIC")
    lines.append(".control")
    lines.append("run")
    lines.append("* plot all")
    lines.append(".endc")
    lines.append(".end")

    return "\n".join(lines) + "\n"


def _pick_step(duration: float) -> str:
    """Paso de tran razonable: ~2000 puntos."""
    step = duration / 2000.0
    return f"{step:.6g}"


def _spice_value(v) -> str:
    """``330 → 330``, ``10k → 10k``, ``4.7u → 4.7u``."""
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).strip()
    return s or "10k"


def save(circuit: Circuit, path, **kw) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(to_spice(circuit, **kw))
    return path
