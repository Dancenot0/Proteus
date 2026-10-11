"""Cargador del DSL: YAML → :class:`~voltio.core.ir.Circuit`.

Este es el punto de entrada de todo. La IA produce el YAML, este módulo lo
convierte en el grafo, y de ahí salen el simulador, el render y los exportadores.

Formato
-------

.. code-block:: yaml

   name: contador_4bit
   title: "Contador con display de 7 segmentos"
   meta: { author: tú, description: "..." }

   simulation:
     timescale: 1ns
     duration: 10ms

   parts:
     - ref: U1              # referencia única en el esquemático
       part: 74LS193        # nombre en la librería (acepta alias)
       at: [120, 200]       # posición en unidades de rejilla
       rotation: 0          # 0 | 90 | 180 | 270
       props: { ... }       # propiedades del componente

   nets:
     - name: CLK            # nombre de red (aparece como etiqueta)
       connect: [CLK1.Q, U1.CPU]

     - name: Q              # bus de 4 bits: genera Q[0]..Q[3]
       bus: 4
       connect: [U1.Q[0:3], SEG.A[0:3]]

     - name: GND
       tie: 0               # atajo: todos los extremos quedan a nivel 0
       connect: [U1./MR]

Sintaxis de extremos
--------------------

===========================  ==========================================
``U1.A``                     pin simple
``U1.Q[3]``                  un bit de un pin vectorial
``U1.Q[0:7]``                rango inclusivo (solo dentro de un bus)
``U1.Q[*]``                  todos los bits del pin
``$0`` / ``$1``              atado a nivel lógico constante
``$X`` / ``$Z``              atado a indeterminado / alta impedancia
===========================  ==========================================

Las claves de red ``GND``, ``VCC``, ``GND$`` y ``VCC$`` se reconocen y se
tratan como rieles de alimentación.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core import miniyaml
from ..core.ir import BusDef, Circuit, Endpoint, Issue, Net, Part, ep_token
from ..core.logic import L
from ..parts.library import LIBRARY, Library

__all__ = ["load_file", "load_text", "load_dict", "to_dict", "dump_file",
           "DslError", "parse_endpoint", "TIE"]


class DslError(ValueError):
    """El DSL no se puede cargar. El mensaje está pensado para devolvérselo a
    la IA y que se autocorrija (ver :mod:`voltio.ai`)."""

    def __init__(self, message: str, path: str = ""):
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


TIE = {
    "0": L.LOW, "1": L.HIGH, "x": L.X, "z": L.Z,
    "gnd": L.LOW, "vcc": L.HIGH, "low": L.LOW, "high": L.HIGH,
    "false": L.LOW, "true": L.HIGH,
}

_EP_RE = re.compile(
    r"""^\s*
    (?P<ref>[A-Za-z_][A-Za-z0-9_]*)
    \.
    (?P<slash>/?)
    (?P<pin>[A-Za-z0-9_][A-Za-z0-9_]*)
    (?:\[(?P<idx>[0-9]+|\*|[0-9]+\s*:\s*[0-9]+)\])?
    \s*$""",
    re.VERBOSE,
)


def parse_endpoint(token: Any) -> Tuple[str, str, Any]:
    """Descompone ``"U1.Q[0:3]"`` en ``("U1", "Q", (0, 3))``.

    El tercer elemento es ``None`` (pin completo), un ``int`` (un bit),
    ``"*"`` (todos los bits) o una tupla ``(lo, hi)`` inclusiva.
    Los pines activos en baja conservan su barra: ``"U1./MR" → ("U1", "/MR", None)``.
    """
    if isinstance(token, bool):
        return "$TIE", "1" if token else "0", None
    if isinstance(token, (int, float)):
        # Un número suelto en una lista de conexiones = atado a nivel constante.
        return "$TIE", str(token), None

    if not isinstance(token, str):
        raise DslError(
            f"extremo inválido: {token!r} (se esperaba texto tipo 'U1.A', "
            f"'U1.Q[3]' o '$0')"
        )

    s = token.strip()
    if not s:
        raise DslError("extremo vacío")

    if s.startswith("$"):
        return "$TIE", s[1:].strip().lower(), None

    m = _EP_RE.match(s)
    if not m:
        raise DslError(
            f"no entiendo el extremo {s!r}. Formato esperado: "
            f"'REF.PIN', 'REF.PIN[3]', 'REF.PIN[0:7]' o '$0'/'$1'/'$X'/'$Z'",
            s,
        )

    pin = m.group("slash") + m.group("pin")
    idx = m.group("idx")

    if idx is None:
        return m.group("ref"), pin, None
    if idx == "*":
        return m.group("ref"), pin, "*"
    if ":" in idx:
        lo, hi = idx.split(":")
        return m.group("ref"), pin, (int(lo), int(hi))
    return m.group("ref"), pin, int(idx)


# ---------------------------------------------------------------------------


def load_file(path, library: Optional[Library] = None) -> Tuple[Circuit, List[Issue]]:
    with open(path, "r", encoding="utf-8") as f:
        return load_text(f.read(), library=library, origin=str(path))


def load_text(text: str, library: Optional[Library] = None,
              origin: str = "<texto>") -> Tuple[Circuit, List[Issue]]:
    try:
        data = miniyaml.loads(text)
    except Exception as e:  # YamlError u otro
        raise DslError(f"YAML inválido: {e}", origin) from e
    if data is None:
        raise DslError("el documento está vacío", origin)
    return load_dict(data, library=library, origin=origin)


def load_dict(data: Any, library: Optional[Library] = None,
              origin: str = "<dict>") -> Tuple[Circuit, List[Issue]]:
    lib = library or LIBRARY
    if not isinstance(data, dict):
        raise DslError(f"la raíz del documento debe ser un mapa, no {type(data).__name__}", origin)

    _check_unknown_top(data, origin)

    circuit = Circuit(
        name=str(data.get("name") or "sin_titulo"),
        title=str(data.get("title") or data.get("name") or "sin_titulo"),
        author=str((data.get("meta") or {}).get("author") or ""),
    )
    circuit.metadata = {
        k: v for k, v in data.items()
        if k in ("meta", "simulation", "description", "notes", "ai")
    }

    # -- partes -----------------------------------------------------------
    parts_raw = data.get("parts") or []
    if not isinstance(parts_raw, list):
        raise DslError("'parts' debe ser una lista", origin + ".parts")
    for i, pr in enumerate(parts_raw):
        circuit.add_part(_load_part(pr, i, origin))

    # -- engancha con la librería ----------------------------------------
    issues = circuit.link(lib)

    # -- buses ------------------------------------------------------------
    for i, b in enumerate(data.get("buses") or []):
        if not isinstance(b, dict):
            issues.append(Issue("error", f"bus #{i} debe ser un mapa", origin))
            continue
        name = b.get("name")
        width = b.get("width")
        if not name or not isinstance(width, int) or width < 1:
            issues.append(
                Issue("error", f"el bus #{i} necesita 'name' y 'width' entero > 0", origin)
            )
            continue
        circuit.add_bus(BusDef(name=str(name), width=int(width)))

    # -- nets -------------------------------------------------------------
    nets_raw = data.get("nets") or []
    if not isinstance(nets_raw, list):
        raise DslError("'nets' debe ser una lista", origin + ".nets")
    for i, nr in enumerate(nets_raw):
        issues.extend(_load_net(nr, i, circuit, origin))

    issues.extend(circuit.validate())
    return circuit, issues


_TOP_KEYS = {
    "name", "title", "meta", "description", "notes", "simulation", "parts",
    "nets", "buses", "ai", "proteus",
}


def _check_unknown_top(data: Dict[str, Any], origin: str) -> None:
    for k in data:
        if k not in _TOP_KEYS:
            near = difflib.get_close_matches(k, _TOP_KEYS, n=1, cutoff=0.6)
            hint = f" (¿quisiste decir '{near[0]}'?)" if near else ""
            raise DslError(
                f"clave desconocida '{k}'{hint}. Válidas: "
                + ", ".join(sorted(_TOP_KEYS)),
                origin,
            )


def _load_part(pr: Any, index: int, origin: str) -> Part:
    where = f"{origin}.parts[{index}]"
    if not isinstance(pr, dict):
        raise DslError(f"cada parte debe ser un mapa, no {type(pr).__name__}", where)

    ref = pr.get("ref") or pr.get("name") or pr.get("id")
    if not ref:
        raise DslError("falta 'ref' (la referencia, p. ej. U1)", where)
    ref = str(ref)

    pname = pr.get("part") or pr.get("type") or pr.get("device")
    if not pname:
        raise DslError(f"la parte '{ref}' no declara 'part'", where)
    pname = str(pname)

    pos = pr.get("at") or pr.get("pos") or pr.get("position")
    x = y = 0.0
    if isinstance(pos, (list, tuple)) and len(pos) >= 2:
        x, y = float(pos[0]), float(pos[1])
    elif isinstance(pos, dict):
        x, y = float(pos.get("x", 0)), float(pos.get("y", 0))

    rot = pr.get("rotation", pr.get("rot", 0)) or 0
    try:
        rot = int(rot) % 360
    except (TypeError, ValueError):
        raise DslError(f"'rotation' de {ref} debe ser un número (0/90/180/270), no {rot!r}", where)
    if rot not in (0, 90, 180, 270):
        raise DslError(f"'rotation' de {ref} vale {rot}; usa 0, 90, 180 o 270", where)

    props = pr.get("props") or pr.get("properties") or {}
    if not isinstance(props, dict):
        raise DslError(f"'props' de {ref} debe ser un mapa", where)

    for k in pr:
        if k not in ("ref", "name", "id", "part", "type", "device", "at", "pos",
                     "position", "rotation", "rot", "props", "properties",
                     "mirror", "label", "value"):
            near = difflib.get_close_matches(
                k, ["ref", "part", "at", "rotation", "props", "mirror", "label", "value"],
                n=1, cutoff=0.6,
            )
            hint = f" (¿quisiste decir '{near[0]}'?)" if near else ""
            raise DslError(f"clave desconocida '{k}' en la parte {ref}{hint}", where)

    if "value" in pr:
        props = dict(props)
        props.setdefault("VALUE", pr["value"])

    return Part(
        ref=ref,
        part=pname,
        x=x,
        y=y,
        rotation=rot,
        mirror=bool(pr.get("mirror", False)),
        props=props,
        label=pr.get("label"),
    )


def _load_net(nr: Any, index: int, circuit: Circuit, origin: str) -> List[Issue]:
    where = f"{origin}.nets[{index}]"
    issues: List[Issue] = []
    if not isinstance(nr, dict):
        raise DslError(f"cada net debe ser un mapa, no {type(nr).__name__}", where)

    name = nr.get("name")
    connects = nr.get("connect") or nr.get("pins") or nr.get("to") or []
    tie = nr.get("tie")
    bus_width = nr.get("bus")

    if isinstance(connects, str):
        connects = [connects]
    if not isinstance(connects, list):
        raise DslError(f"'connect' del net '{name}' debe ser una lista", where)
    if not connects and tie is None:
        raise DslError(f"el net '{name}' no conecta nada", where)

    # Normaliza los extremos a (ref, pin, selector)
    ends: List[Tuple[str, str, Any]] = []
    for tok in connects:
        ref, pin, sel = parse_endpoint(tok)
        ends.append((ref, pin, sel))

    for k in nr:
        if k not in ("name", "connect", "pins", "to", "tie", "bus", "kind", "label"):
            raise DslError(f"clave desconocida '{k}' en el net '{name}'", where)

    if bus_width:
        try:
            width = int(bus_width)
        except (TypeError, ValueError):
            raise DslError(f"'bus' del net '{name}' debe ser un entero, no {bus_width!r}", where)
        issues.extend(_build_bus(name, width, ends, tie, circuit, where))
    else:
        issues.extend(_build_single(name, ends, tie, circuit, where))
    return issues


def _resolve_selector(sel: Any, width: int, where: str) -> List[int]:
    if sel is None or sel == "*":
        return list(range(width))
    if isinstance(sel, int):
        if not 0 <= sel < width:
            raise DslError(f"índice [{sel}] fuera de rango para un pin de {width} bits", where)
        return [sel]
    lo, hi = sel
    if not (0 <= lo <= hi < width):
        raise DslError(f"rango [{lo}:{hi}] inválido para un pin de {width} bits", where)
    return list(range(lo, hi + 1))


def _suggest(spec, pin: str) -> str:
    near = difflib.get_close_matches(pin.lstrip("/"), [p.name.lstrip("/") for p in spec.pins], n=2, cutoff=0.5)
    return f" ¿Quisiste decir {near}?" if near else f" Pines válidos: {spec.pin_names()}"


def _add_endpoint(circuit: Circuit, net: Net, ref: str, pin: str, bit: int,
                  issues: List[Issue], where: str) -> None:
    if ref == "$TIE":
        net.add(Endpoint("$TIE", pin, 0))  # pin guarda el literal ("0","1","x","z")
        return
    part = circuit.parts.get(ref)
    if part is None:
        issues.append(Issue("error", f"el net '{net.label}' referencia la parte "
                                     f"inexistente '{ref}'", where))
        return
    if part.spec is None:
        return  # ya se reportó como componente desconocido
    pspec = part.spec.pin(pin)
    if pspec is None:
        issues.append(Issue("error", f"{ref} ({part.part}) no tiene el pin '{pin}'."
                                     + _suggest(part.spec, pin), where))
        return
    if not 0 <= bit < max(pspec.width, 1):
        issues.append(Issue("error", f"{ref}.{pin} tiene {pspec.width} bits; "
                                     f"el índice {bit} se sale", where))
        return
    net.add(Endpoint(ref, pspec.name, bit))


def _tie_net(net: Net, tie: Any, where: str) -> None:
    if tie is None:
        return
    key = str(tie).strip().lower().lstrip("$")
    if key not in TIE:
        raise DslError(f"'tie: {tie}' no es un nivel válido. Usa 0, 1, X o Z", where)
    net.add(Endpoint("$TIE", key, 0))


def _has_rail_part(circuit: Circuit, ends: List[Tuple[str, str, Any]]) -> bool:
    """True si entre los extremos del net hay un símbolo de tierra o de VCC.

    El DSL ata solo los nets llamados GND/VCC por comodidad, pero si el diseño
    ya puso su propio símbolo de tierra en ese net, añadir además el atado
    automático duplica la fuente (y el renderizador dibujaba dos símbolos de
    tierra encima). Un solo nodo de tierra, una sola fuente, un solo símbolo.
    """
    for ref, _pin, _sel in ends:
        if ref == "$TIE":
            continue
        part = circuit.parts.get(ref)
        if part is not None and part.spec is not None:
            if part.spec.model in ("ground", "rail"):
                return True
    return False


def _rail_endpoint(name: Optional[str]) -> Optional[Endpoint]:
    """Los nets llamados GND/VCC se atan automáticamente a su riel."""
    if not name:
        return None
    up = name.upper().rstrip("$").replace("/", "")
    if up in ("GND", "GROUND", "0V", "VSS", "TIERRA"):
        return Endpoint("$TIE", "0", 0)
    if up in ("VCC", "VDD", "5V", "3V3", "3.3V", "+5V", "ALIMENTACION"):
        return Endpoint("$TIE", "1", 0)
    return None


def _make_net(name: Optional[str], where: str, circuit: Circuit) -> Net:
    if name and name in circuit.nets:
        raise DslError(f"net '{name}' definido dos veces", where)
    return circuit.add_net(Net(name=name, kind="wire"))


def _build_single(name, ends, tie, circuit, where) -> List[Issue]:
    issues: List[Issue] = []
    net = _make_net(name, where, circuit)
    _tie_net(net, tie, where)
    rail = _rail_endpoint(name)
    if rail is not None and not _has_rail_part(circuit, ends):
        net.add(rail)
    for ref, pin, sel in ends:
        if ref == "$TIE":
            net.add(Endpoint("$TIE", pin, 0))
            continue
        part = circuit.parts.get(ref)
        width = 1
        if part and part.spec:
            pspec = part.spec.pin(pin)
            if pspec:
                width = max(pspec.width, 1)
        bits = _resolve_selector(sel, width, where)
        for b in bits:
            _add_endpoint(circuit, net, ref, pin, b, issues, where)
    if len(net.endpoints) < 2:
        issues.append(Issue("warning", f"el net '{net.label}' tiene menos de 2 extremos", where))
    return issues


def _build_bus(name, width, ends, tie, circuit, where) -> List[Issue]:
    """Un net con ``bus: N`` se expande a N nets ``name[0]..name[N-1]``."""
    issues: List[Issue] = []
    if not name:
        raise DslError("un net con 'bus' necesita 'name'", where)
    bus = circuit.buses.get(name)
    if bus is None:
        bus = circuit.add_bus(BusDef(name=name, width=width))
    elif bus.width != width:
        issues.append(Issue("error", f"el bus '{name}' ya estaba declarado con ancho "
                                     f"{bus.width} y aquí con {width}", where))
        width = bus.width

    # Fase 1: resolver qué bits aporta cada extremo, ANTES de crear ningún net.
    # Si se hiciera dentro del bucle por bit, un error de ancho se reportaría N
    # veces (una por bit), que es justo el ruido que menos le sirve a la IA
    # cuando está intentando autocorregirse.
    plan: List[Tuple[str, Optional[str], List[int]]] = []
    for ref, pin, sel in ends:
        if ref == "$TIE":
            plan.append(("$TIE", pin, []))
            continue
        part = circuit.parts.get(ref)
        if part is None:
            issues.append(Issue("error", f"el bus '{name}' referencia la parte "
                                         f"inexistente '{ref}'", where))
            return issues
        if part.spec is None:
            return issues  # ya se reportó como componente desconocido
        pspec = part.spec.pin(pin)
        if pspec is None:
            issues.append(Issue("error", f"{ref} ({part.part}) no tiene el pin "
                                         f"'{pin}'." + _suggest(part.spec, pin), where))
            return issues
        pwidth = max(pspec.width, 1)
        bits = _resolve_selector(sel, pwidth, where)
        if sel is None and pwidth == 1 and width > 1:
            # Un pin de 1 bit en un bus de N se replica en los N cables. Es lo
            # que se quiere para un enable compartido, pero casi siempre es un
            # olvido, así que se avisa.
            issues.append(Issue("info", f"{ref}.{pin} es de 1 bit y está en el bus "
                                        f"'{name}' de {width} bits: se conecta a todos",
                                where))
            bits = [0] * width
        if len(bits) != width:
            issues.append(Issue(
                "error",
                f"{ref}.{pin}" + _fmt_sel(sel) + f" aporta {len(bits)} bits pero el "
                f"bus '{name}' tiene {width}. Los rangos son inclusivos y deben "
                f"cubrir exactamente el ancho del bus.",
                where))
            return issues
        plan.append((ref, pspec.name, bits))

    # Fase 2: materializar los N cables.
    for bit in range(width):
        net_name = f"{name}[{bit}]"
        net = _make_net(net_name, where, circuit)
        net.bus = name
        net.bit = bit
        net.kind = "bus"
        bus.nets.append(net_name)
        if tie is not None:
            _tie_net(net, tie, where)
        for ref, pin, bits in plan:
            if ref == "$TIE":
                net.add(Endpoint("$TIE", pin, 0))
            else:
                net.add(Endpoint(ref, pin, bits[bit]))
    return issues


def _fmt_sel(sel) -> str:
    """Representa el selector de un extremo para los mensajes de error."""
    if sel is None:
        return ""
    if sel == "*":
        return "[*]"
    if isinstance(sel, int):
        return f"[{sel}]"
    return f"[{sel[0]}:{sel[1]}]"


# ---------------------------------------------------------------------------
# Sentido inverso: Circuit → dict YAML
# ---------------------------------------------------------------------------


def _agrupar_en_rangos(pares: List[Tuple[str, str, List[int]]],
                       width: int) -> List[str]:
    """Recompone tokens de extremos para un bus, agrupando bits contiguos.

    ``to_dict`` recibe los bits sueltos (``U1.Q[0]``, ``U1.Q[1]``, ...) pero el
    DSL de un bus exige que cada extremo aporte exactamente ``width`` bits, así
    que hay que volver a escribirlos como rangos (``U1.Q[0:3]``). Sin esto el
    round-trip rompe: al recargar, cada token suelto aportaría un solo bit y el
    validador rechazaría el bus entero.

    También detecta el caso de un pin de 1 bit replicado en todos los cables del
    bus (un enable compartido) y lo emite sin índice, que es como se escribió.
    """
    # Agrupa por (ref, pin) conservando el orden en que aparecen por bit.
    orden: List[Tuple[str, str]] = []
    mapa: Dict[Tuple[str, str], List[int]] = {}
    for ref, pin, bits in pares:
        if ref == "$TIE":
            continue
        clave = (ref, pin)
        if clave not in mapa:
            mapa[clave] = []
            orden.append(clave)
        mapa[clave] = bits

    out: List[str] = []
    # Primero los atados, que van al principio del net.
    for ref, pin, bits in pares:
        if ref == "$TIE":
            tok = "$" + pin.upper()
            if tok not in out:
                out.append(tok)

    for ref, pin in orden:
        bits = mapa[(ref, pin)]
        if len(bits) == width and len(set(bits)) == 1:
            # Un pin de 1 bit conectado a los `width` cables del bus.
            out.append(f"{ref}.{pin}")
            continue
        if len(bits) != width:
            # No cubre el bus entero: se emite bit a bit y que el validador
            # decida. No se puede inventar un rango que no existe.
            for b in bits:
                out.append(f"{ref}.{pin}[{b}]")
            continue
        # Trocea en tramos estrictamente ascendentes y contiguos.
        i = 0
        while i < len(bits):
            j = i
            while j + 1 < len(bits) and bits[j + 1] == bits[j] + 1:
                j += 1
            if i == j:
                out.append(f"{ref}.{pin}[{bits[i]}]")
            else:
                out.append(f"{ref}.{pin}[{bits[i]}:{bits[j]}]")
            i = j + 1
    return out


def to_dict(circuit: Circuit) -> Dict[str, Any]:
    """Serializa un circuito de vuelta al DSL (lo usa :mod:`voltio.reverse`).

    Es la otra mitad del round-trip: cargar un YAML, modificarlo y volver a
    escribirlo tiene que producir un YAML que cargue de nuevo dando el mismo
    circuito. Por eso los buses se reescriben como rangos y no como bits
    sueltos.
    """
    parts = []
    for p in circuit.parts.values():
        d: Dict[str, Any] = {"ref": p.ref, "part": p.part, "at": [p.x, p.y]}
        if p.rotation:
            d["rotation"] = p.rotation
        if p.props:
            d["props"] = dict(p.props)
        if p.label:
            d["label"] = p.label
        parts.append(d)

    nets: List[Dict[str, Any]] = []
    consumidos: set = set()

    # Primero los buses, agrupando los cables que pertenecen a cada uno.
    for bname, bus in circuit.buses.items():
        miembros = [n for n in circuit.nets.values() if n.bus == bname]
        if not miembros:
            continue
        miembros.sort(key=lambda n: (n.bit if n.bit is not None else 0))
        anchos = {max(_ancho_pin(circuit, ep), 1) for n in miembros for ep in n.endpoints
                  if ep.part != "$TIE"}
        pares: List[Tuple[str, str, List[int]]] = []
        visto: set = set()
        for n in miembros:
            por_extremo: Dict[Tuple[str, str], List[int]] = {}
            orden_local: List[Tuple[str, str]] = []
            for ep in n.endpoints:
                if ep.part == "$TIE":
                    clave = ("$TIE", ep.pin)
                else:
                    clave = (ep.part, ep.pin)
                if clave not in por_extremo:
                    por_extremo[clave] = []
                    orden_local.append(clave)
                por_extremo[clave].append(ep.bit)
            for clave in orden_local:
                ref, pin = clave
                if (ref, pin) in visto:
                    # Ya se emitió para otro bit del bus: ampliar su lista.
                    for k, (r2, p2, bits) in enumerate(pares):
                        if (r2, p2) == (ref, pin):
                            bits.extend(por_extremo[clave])
                            pares[k] = (r2, p2, bits)
                            break
                else:
                    visto.add((ref, pin))
                    pares.append((ref, pin, list(por_extremo[clave])))
        connect = _agrupar_en_rangos(pares, bus.width)
        entry: Dict[str, Any] = {"name": bname, "bus": bus.width, "connect": connect}
        nets.append(entry)
        consumidos.update(n.name for n in miembros)

    # Y después las redes sueltas.
    for n in circuit.nets.values():
        if n.name in consumidos:
            continue
        connect = []
        for ep in n.endpoints:
            connect.append("$" + ep.pin.upper() if ep.part == "$TIE"
                           else ep_token(ep, circuit))
        entry = {"name": n.name, "connect": connect}
        nets.append(entry)

    out: Dict[str, Any] = {"name": circuit.name, "title": circuit.title}
    if circuit.metadata.get("meta"):
        out["meta"] = circuit.metadata["meta"]
    if circuit.metadata.get("simulation"):
        out["simulation"] = circuit.metadata["simulation"]
    out["parts"] = parts
    out["nets"] = nets
    if circuit.buses:
        out["buses"] = [
            {"name": b.name, "width": b.width} for b in circuit.buses.values()
        ]
    return out


def _ancho_pin(circuit: Circuit, ep) -> int:
    part = circuit.parts.get(ep.part)
    if part is None or part.spec is None:
        return 1
    p = part.spec.pin(ep.pin)
    return max(p.width, 1) if p else 1


def dump_file(circuit: Circuit, path) -> None:
    miniyaml.dump(to_dict(circuit), path)
