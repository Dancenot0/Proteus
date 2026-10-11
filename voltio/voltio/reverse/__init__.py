"""Sentido inverso: circuito → DSL → descripción en lenguaje natural.

Complementa a :mod:`voltio.ai`. Sirve para tres cosas:

* **Documentar** un esquemático heredado del que nadie sabe nada.
* **Revisar** lo que generó la IA en el otro sentido, con ojos frescos.
* **Round-trip**: cargar un circuito, editarlo y volver a escribirlo.

La serialización a DSL vive en :func:`voltio.dsl.loader.to_dict`, que es la
misma función que usa ``--dsl-out``. Aquí se añade el análisis estructural, que
es lo que hace que la descripción no sea una simple lista de componentes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.ir import Circuit, Net, TIE_PART
from ..core.logic import L
from ..dsl.loader import to_dict
from ..core import miniyaml

__all__ = ["to_yaml", "analyze", "blocks", "describe_local", "summary_lines"]


def to_yaml(circuit: Circuit) -> str:
    """El circuito como texto DSL."""
    return miniyaml.dumps(to_dict(circuit))


# ---------------------------------------------------------------------------
# Análisis estructural
# ---------------------------------------------------------------------------


def blocks(circuit: Circuit) -> List[Dict[str, Any]]:
    """Agrupa los componentes en bloques funcionales por categoría.

    Es el primer paso para describir un circuito con sentido: en vez de
    "hay un 74LS193, un 74LS48 y un display", decir "hay una etapa de conteo,
    una de decodificación y una de indicación".
    """
    groups: Dict[str, List[str]] = {}
    for ref, part in circuit.parts.items():
        cat = part.spec.category if part.spec else "desconocidos"
        groups.setdefault(cat, []).append(ref)

    orden = [
        ("fuentes", "Generación de señal y alimentación"),
        ("electromecanicos", "Entradas manuales"),
        ("instrumentos", "Instrumentos y sondas"),
        ("74xx", "Lógica 74xx"),
        ("primitivas", "Primitivas lógicas"),
        ("cpu", "Procesador"),
        ("microcontroladores", "Microcontrolador"),
        ("memoria", "Memoria"),
        ("displays", "Indicación"),
        ("optoelectronica", "Indicación"),
        ("pasivos", "Pasivos"),
    ]
    out: List[Dict[str, Any]] = []
    vistos: Set[str] = set()
    for cat, titulo in orden:
        if cat in groups and cat not in vistos:
            vistos.add(cat)
            out.append({"categoria": cat, "titulo": titulo,
                        "refs": sorted(groups[cat])})
    for cat in sorted(groups):
        if cat not in vistos:
            out.append({"categoria": cat, "titulo": cat.title(),
                        "refs": sorted(groups[cat])})
    return out


def analyze(circuit: Circuit) -> Dict[str, Any]:
    """Radiografía del circuito: qué hay, cómo está conectado, qué sospechoso hay."""
    nets = list(circuit.nets.values())
    named = [n for n in nets if n.name and not n.name.startswith("N$")]

    buses = [
        {"nombre": b.name, "ancho": b.width}
        for b in circuit.buses.values()
    ]

    # Abanico de cada red: cuántos driver y cuántas cargas.
    fan: List[Dict[str, Any]] = []
    for n in named:
        fan.append({
            "red": n.label,
            "extremos": len(n.pin_endpoints()),
            "drivers": len([e for e in n.drivers]),
            "cargas": len([e for e in n.loads]),
            "atado": None if n.tie_value() is None else str(n.tie_value()),
            "bus": n.bus,
        })

    # Señales de control: redes cuyo nombre sugiere enable/reset/etc.
    claves = ("EN", "OE", "MR", "CLR", "PRE", "PL", "RESET", "CS", "WE", "RD",
              "WR", "LT", "RBI", "BI", "DIR")
    controles = []
    for n in named:
        base = n.label.upper().lstrip("/").split("[")[0]
        if any(k in base for k in claves):
            controles.append({
                "red": n.label,
                "atado": None if n.tie_value() is None else str(n.tie_value()),
                "extremos": [str(e) for e in n.pin_endpoints()],
                # `drivers` lo rellena Circuit.validate(); son los pines de
                # salida o bidireccionales enganchados a esta red. Que no haya
                # ninguno es lo que de verdad significa "flotando", y es
                # distinto de "no atada a un nivel fijo": una señal puede estar
                # perfectamente driveada por una salida lógica.
                "driveada": bool(n.drivers),
            })

    relojes = [
        ref for ref, p in circuit.parts.items()
        if p.spec and p.spec.model == "clock"
    ]

    return {
        "nombre": circuit.name,
        "titulo": circuit.title,
        "partes": len(circuit.parts),
        "redes": len(nets),
        "redes_con_nombre": len(named),
        "buses": buses,
        "relojes": relojes,
        "bloques": blocks(circuit),
        "abanico": fan,
        "controles": controles,
        "memorias": [
            ref for ref, p in circuit.parts.items()
            if p.spec and p.spec.category == "memoria"
        ],
    }


# ---------------------------------------------------------------------------
# Descripción sin IA
# ---------------------------------------------------------------------------


def summary_lines(circuit: Circuit) -> List[str]:
    """Descripción generada localmente, sin llamar a ningún modelo.

    No sustituye a :func:`voltio.ai.describe` (que explica el *propósito*), pero
    funciona sin conexión ni claves y es suficiente para orientarse en un
    circuito desconocido.
    """
    a = analyze(circuit)
    out: List[str] = []
    out.append(f"{a['titulo']}  ({a['partes']} componentes, {a['redes']} redes)")
    if a["relojes"]:
        freqs = []
        for ref in a["relojes"]:
            f = circuit.parts[ref].props.get("FREQ")
            freqs.append(f"{ref}" + (f" a {_hz(f)}" if f else ""))
        out.append("Reloj: " + ", ".join(freqs))
    if a["buses"]:
        out.append("Buses: " + ", ".join(
            f"{b['nombre']}[{b['ancho']} bits]" for b in a["buses"]
        ))
    out.append("")
    for b in a["bloques"]:
        out.append(f"  {b['titulo']} ({b['categoria']})")
        for ref in b["refs"]:
            p = circuit.parts[ref]
            desc = p.spec.description if p.spec else "?"
            props = ", ".join(f"{k}={v}" for k, v in p.props.items())
            out.append(f"    {ref:<7} {p.part:<12} {desc}"
                       + (f"   [{props}]" if props else ""))
    if a["controles"]:
        out.append("")
        out.append("  Señales de control:")
        for ctrl in a["controles"]:
            if ctrl["atado"] is not None:
                estado = f"atada a {ctrl['atado']}"
            elif ctrl["driveada"]:
                estado = "driveada"
            else:
                estado = "SIN DRIVER"
            out.append(f"    {ctrl['red']:<12} {estado:<12} → "
                       f"{', '.join(ctrl['extremos'][:4])}"
                       + (" ..." if len(ctrl["extremos"]) > 4 else ""))
    sospechas = _suspicious(circuit, a)
    if sospechas:
        out.append("")
        out.append("  Cosas que merecen una revisión:")
        for s in sospechas:
            out.append(f"    ! {s}")
    return out


def _hz(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    for div, suf in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= div:
            return f"{f / div:g} {suf}"
    return f"{f:g} Hz"


def _suspicious(circuit: Circuit, a: Dict[str, Any]) -> List[str]:
    """Heurísticas de revisión: lo que un ingeniero miraría con lupa."""
    out: List[str] = []

    # 1. Señales de control sin nada que las defina. Una entrada de enable al
    #    aire lee X y deja el dispositivo fuera de juego de forma impredecible.
    #    Ojo con el criterio: lo que importa es que no haya NINGÚN driver, no
    #    que falte un `tie`. Una señal de write manejada por una salida lógica
    #    está perfectamente definida y no debe dar aviso.
    for ctrl in a["controles"]:
        if not ctrl["driveada"] and ctrl["atado"] is None:
            out.append(
                f"{ctrl['red']} parece una señal de control y no la drivea "
                f"ninguna salida ni está atada a un nivel: leerá X"
            )

    # 2. Buses con varios drivers tri-estado y ninguna señal de enable visible.
    for b in a["buses"]:
        nombre = b["nombre"]
        nets = [n for n in circuit.nets.values() if n.bus == nombre]
        drivers = set()
        for n in nets:
            for ep in n.drivers:
                drivers.add(ep.part)
        if len(drivers) > 1:
            hay_enable = any(
                k in (n.name or "").upper()
                for n in circuit.nets.values()
                for k in ("EN", "OE", "DIR")
            )
            if not hay_enable:
                out.append(
                    f"el bus {nombre} tiene {len(drivers)} dispositivos "
                    f"({', '.join(sorted(drivers))}) y no se ve ninguna señal de "
                    f"enable: comprueba que solo uno drivea a la vez"
                )

    # 3. Display con decodificador de polaridad opuesta.
    decodificadores = {
        ref for ref, p in circuit.parts.items()
        if p.spec and p.spec.model in ("dec47", "dec48")
    }
    displays = {
        ref for ref, p in circuit.parts.items()
        if p.spec and p.spec.symbol == "7seg"
    }
    if decodificadores and displays:
        for dref in decodificadores:
            activo_bajo = circuit.parts[dref].spec.model == "dec47"
            for sref in displays:
                comun = circuit.parts[sref].props.get("COMMON", "CATODO")
                anodo = str(comun).upper().startswith("ANOD") or \
                    circuit.parts[sref].part.upper().endswith("CA")
                if activo_bajo != anodo:
                    out.append(
                        f"{dref} ({circuit.parts[dref].part}) tiene salidas "
                        f"activas en {'baja' if activo_bajo else 'alta'} pero "
                        f"{sref} es de {'ánodo' if anodo else 'cátodo'} común: "
                        f"el display mostrará segmentos invertidos"
                    )

    # 4. Memorias sin contenido.
    for ref in a["memorias"]:
        p = circuit.parts[ref]
        if not p.props.get("FILE") and not p.props.get("INIT"):
            out.append(
                f"{ref} ({p.part}) está en el circuito pero no tiene contenido "
                f"cargado (FILE o INIT): leerá ceros o FF"
            )

    # 5. Microcontrolador sin firmware.
    for ref, p in circuit.parts.items():
        if p.spec and p.spec.category == "microcontroladores" and not p.props.get("PROGRAM"):
            out.append(f"{ref} ({p.part}) no tiene PROGRAM asignado")

    return out


def describe_local(circuit: Circuit) -> str:
    """La descripción local como texto corrido."""
    return "\n".join(summary_lines(circuit))
