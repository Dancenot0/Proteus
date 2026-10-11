"""Representación intermedia (IR): el grafo del circuito.

Tres conceptos y nada más:

* :class:`Part` — una instancia de un componente, con referencia (``U1``),
  posición y propiedades.
* :class:`Net`  — un cable. Conecta *puntos* (pin de una parte + índice de bit).
  Es la unidad que el simulador resuelve.
* :class:`Circuit` — el conjunto, más validación.

Los pines pueden ser **vectoriales** (``Q[0:7]``), que es como se escribe
naturalmente un registro o un bus. Internamente siempre se expanden a bits
individuales: el simulador nunca ve un vector, ve ocho cables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .logic import L, bus_value, is_contention

__all__ = [
    "ep_token",
    "TIE_PART",
    "TIE_LEVELS",
    "PinDir",
    "Pin",
    "Part",
    "Endpoint",
    "Net",
    "BusDef",
    "Issue",
    "Circuit",
]


#: Referencia pseudo-parte que marca un atado a nivel constante (``$0``, ``$1``...).
#: No es un componente: es una fuente ideal que el DSL y el motor entienden.
TIE_PART = "$TIE"

#: Niveles que puede tomar un atado.
TIE_LEVELS = {"0": L.LOW, "1": L.HIGH, "x": L.X, "z": L.Z}


#: Modelos que son fuentes ideales: empujan su net a un nivel fijo sin pelearse
#: con otras fuentes iguales. Varios símbolos GND en el mismo cable es normal.
_IDEAL_SOURCE_MODELS = frozenset({"ground", "rail", "logicstate"})


class PinDir:
    IN = "in"
    OUT = "out"
    INOUT = "inout"  # tri-estado: puede soltar el bus (Z)
    POWER = "power"

    ALL = (IN, OUT, INOUT, POWER)


@dataclass
class Pin:
    """Definición de un pin dentro de una :class:`PartSpec` (la plantilla)."""

    name: str
    direction: str = PinDir.IN
    width: int = 1
    active_low: bool = False
    #: True si la salida puede soltar el cable (alta impedancia) en vez de
    #: empujarlo siempre a un nivel. Es lo que distingue un buffer tri-estado
    #: de una salida push-pull, y es la diferencia entre un bus legítimo con
    #: ocho dispositivos colgando y un cortocircuito.
    tri_state: bool = False
    number: Optional[str] = None  # número físico del paquete (para SPICE/PCB)
    side: str = "left"  # left | right | top | bottom — para el renderizador
    order: int = 0  # posición vertical/horizontal dentro de su lado

    def display_name(self) -> str:
        """Nombre como se escribe en el DSL y en el esquemático.

        La barra de activo-en-baja forma parte del nombre del pin (``/MR``), así
        que aquí solo se añade si no viene ya puesta. Duplicarla (``//MR``)
        rompería la validación y confundiría a la IA.
        """
        if self.active_low and not self.name.startswith("/"):
            return f"/{self.name}"
        return self.name


@dataclass
class Part:
    """Una instancia concreta de un componente en el esquemático."""

    ref: str
    part: str  # nombre en la librería, p. ej. "74LS00"
    x: float = 0.0
    y: float = 0.0
    rotation: int = 0  # 0, 90, 180, 270
    mirror: bool = False
    props: Dict[str, Any] = field(default_factory=dict)
    label: Optional[str] = None
    # Resuelto por Circuit.link() contra la librería de partes:
    spec: Any = None

    # -- pines ------------------------------------------------------------
    def pin_names(self) -> List[str]:
        if self.spec is None:
            return []
        return [p.name for p in self.spec.pins]

    def pin(self, name: str) -> Optional[Pin]:
        if self.spec is None:
            return None
        return self.spec.pin(name)

    def has_pin(self, name: str) -> bool:
        return self.pin(name) is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Part {self.ref}:{self.part} @({self.x:g},{self.y:g})>"


def ep_token(ep: "Endpoint", circuit: "Circuit" = None) -> str:
    """Forma canónica de escribir un extremo: ``U1.A``, ``U1.Q[3]``.

    El índice se incluye si el pin es vectorial **o** si el bit no es el 0.
    Omitirlo cuando ``bit == 0`` parece inocente pero rompe el round-trip:
    ``U1.Q[0]`` se serializaría como ``U1.Q``, que al recargarse significa
    "todos los bits del pin", y un extremo acaba duplicado en dos redes.
    """
    width = 1
    if circuit is not None:
        part = circuit.parts.get(ep.part)
        if part is not None and part.spec is not None:
            pin = part.spec.pin(ep.pin)
            if pin is not None:
                width = max(pin.width, 1)
    suffix = f"[{ep.bit}]" if (ep.bit or width > 1) else ""
    return f"{ep.part}.{ep.pin}{suffix}"


@dataclass(frozen=True)
class Endpoint:
    """Un extremo de cable: el bit ``bit`` del pin ``pin`` de la parte ``part``.

    Se hace *hashable* e inmutable a propósito, porque los metemos en sets.
    """

    part: str
    pin: str
    bit: int = 0

    def __str__(self) -> str:
        return f"{self.part}.{self.pin}" + (f"[{self.bit}]" if self.bit else "")


@dataclass
class Net:
    """Un cable eléctrico.

    ``name`` es opcional: si se omite, el motor inventa ``N$12``. Darle nombre
    es lo que produce etiquetas legibles en el esquemático y en el netlist.
    """

    name: Optional[str]
    endpoints: List[Endpoint] = field(default_factory=list)
    bus: Optional[str] = None  # a qué bus pertenece (solo informativo)
    bit: Optional[int] = None  # qué bit del bus es
    kind: str = "wire"  # wire | bus
    # Poblado por el simulador:
    drivers: List[Endpoint] = field(default_factory=list)
    loads: List[Endpoint] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.name:
            if self.bus is not None and self.bit is not None:
                return f"{self.bus}[{self.bit}]"
            return self.name
        return f"N${id(self) & 0xFFFF:04X}"

    def add(self, ep: Endpoint) -> None:
        if ep not in self.endpoints:
            self.endpoints.append(ep)

    def tie_levels(self) -> List[L]:
        """Niveles de los atados constantes (``$0``, ``$1``, ``$X``, ``$Z``).

        Un riel de alimentación y un atado explícito son la misma cosa: fuentes
        ideales que empujan el cable a un nivel fijo.
        """
        out: List[L] = []
        for ep in self.endpoints:
            if ep.part == TIE_PART:
                out.append(TIE_LEVELS.get(ep.pin.lower(), L.X))
        return out

    def pin_endpoints(self) -> List["Endpoint"]:
        """Extremos que son pines reales (excluye los atados)."""
        return [ep for ep in self.endpoints if ep.part != TIE_PART]

    def tie_value(self) -> Optional[L]:
        """Nivel resuelto de los atados: ``None`` si no hay, ``X`` si se pelean."""
        levels = self.tie_levels()
        if not levels:
            return None
        return bus_value(levels)


@dataclass
class BusDef:
    """Un bus con nombre, p. ej. ``DATA[0:7]``. Solo agrupa nets para dibujar."""

    name: str
    width: int
    nets: List[str] = field(default_factory=list)


@dataclass
class Issue:
    """Un problema encontrado al validar. ``kind`` decide si es fatal."""

    kind: str  # error | warning | info
    message: str
    where: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover
        loc = f" [{self.where}]" if self.where else ""
        return f"{self.kind.upper()}{loc}: {self.message}"


# ---------------------------------------------------------------------------


class Circuit:
    """El circuito completo: partes + cables + validación.

    Es deliberadamente tonto: no simula ni dibuja. Solo sostiene el grafo y
    lo verifica. Todo lo demás (motor, render, exportadores) lo consume.
    """

    def __init__(self, name: str = "sin_titulo", title: str = "", author: str = ""):
        self.name = name
        self.title = title or name
        self.author = author
        self.parts: Dict[str, Part] = {}
        self.nets: Dict[str, Net] = {}
        self.buses: Dict[str, BusDef] = {}
        self._anon = 0
        self.metadata: Dict[str, Any] = {}

    # -- construcción -----------------------------------------------------
    def add_part(self, part: Part) -> Part:
        if part.ref in self.parts:
            raise ValueError(f"referencia duplicada: {part.ref}")
        self.parts[part.ref] = part
        return part

    def new_net_name(self) -> str:
        self._anon += 1
        while f"N${self._anon}" in self.nets:
            self._anon += 1
        return f"N${self._anon}"

    def add_net(self, net: Net) -> Net:
        if net.name is None:
            net.name = self.new_net_name()
        if net.name in self.nets:
            raise ValueError(f"net duplicado: {net.name}")
        self.nets[net.name] = net
        return net

    def net(self, name: str) -> Net:
        return self.nets[name]

    def net_of(self, part_ref: str, pin_name: str, bit: int = 0) -> Optional[Net]:
        """Busca el net al que está conectado un pin (lineal, O(n·m))."""
        ep = Endpoint(part_ref, pin_name, bit)
        for n in self.nets.values():
            if ep in n.endpoints:
                return n
        return None

    def add_bus(self, bus: BusDef) -> BusDef:
        self.buses[bus.name] = bus
        return bus

    # -- consultas --------------------------------------------------------
    def __iter__(self) -> Iterator[Part]:
        return iter(self.parts.values())

    def __len__(self) -> int:
        return len(self.parts)

    def parts_of_kind(self, part_name: str) -> List[Part]:
        return [p for p in self.parts.values() if p.part == part_name]

    def endpoints(self) -> Iterator[Endpoint]:
        for n in self.nets.values():
            yield from n.endpoints

    # -- resolución -------------------------------------------------------
    def link(self, library) -> List[Issue]:
        """Engancha cada :class:`Part` con su plantilla en ``library``.

        Devuelve los errores de partes desconocidas. Hay que llamarlo antes de
        :meth:`validate` o de simular.
        """
        issues: List[Issue] = []
        for p in self.parts.values():
            spec = library.get(p.part)
            if spec is None:
                issues.append(
                    Issue("error", f"componente desconocido '{p.part}'", p.ref)
                )
                continue
            p.spec = spec
            for key, val in p.props.items():
                if key not in spec.prop_names():
                    issues.append(
                        Issue(
                            "warning",
                            f"propiedad '{key}' no existe en {p.part}",
                            p.ref,
                        )
                    )
        return issues

    def validate(self, strict: bool = False) -> List[Issue]:
        """Revisa el circuito y devuelve la lista de problemas.

        Con ``strict=True`` los warnings también se tratan como errores.
        """
        issues: List[Issue] = []

        # 1. ¿Todos los endpoints apuntan a pines que existen?
        for net in self.nets.values():
            for ep in net.pin_endpoints():
                part = self.parts.get(ep.part)
                if part is None:
                    issues.append(
                        Issue("error", f"el net '{net.label}' referencia la parte "
                                       f"inexistente '{ep.part}'", net.label)
                    )
                    continue
                pin = part.pin(ep.pin)
                if pin is None:
                    issues.append(
                        Issue(
                            "error",
                            f"{part.ref} ({part.part}) no tiene el pin '{ep.pin}'",
                            net.label,
                        )
                    )
                    continue
                if not (0 <= ep.bit < max(pin.width, 1)):
                    issues.append(
                        Issue(
                            "error",
                            f"{part.ref}.{ep.pin} tiene ancho {pin.width}, "
                            f"índice {ep.bit} fuera de rango",
                            net.label,
                        )
                    )

        # 2. Un mismo punto no puede estar en dos nets distintos.
        #    (Los atados quedan fuera: el mismo $1 puede alimentar muchos cables.)
        seen: Dict[Endpoint, str] = {}
        for net in self.nets.values():
            for ep in net.pin_endpoints():
                if ep in seen:
                    issues.append(
                        Issue(
                            "error",
                            f"{ep} está conectado a dos nets: "
                            f"'{seen[ep]}' y '{net.label}'",
                            str(ep),
                        )
                    )
                else:
                    seen[ep] = net.label

        # 3. Clasifica drivers/cargas y busca problemas eléctricos.
        #
        #    Una fuente ideal (GND, VCC, un atado $0/$1) no compite con otra
        #    fuente del MISMO nivel: es normal tener varios símbolos de tierra
        #    en el mismo cable. Lo que sí es un error es mezclar niveles
        #    opuestos, o conectar una salida lógica a un riel.
        for net in self.nets.values():
            net.drivers, net.loads = [], []
            logic_drivers: List[str] = []
            # Los pines INOUT (resistencias, transceptores de bus) pueden o no
            # driverear según el estado; cuentan como "driver potencial" para no
            # avisar en falso de un cable que sí acaba teniendo nivel.
            potential_drivers: List[str] = []
            for ep in net.pin_endpoints():
                part = self.parts.get(ep.part)
                if part is None or part.spec is None:
                    continue
                pin = part.pin(ep.pin)
                if pin is None:
                    continue
                d = pin.direction
                if d == PinDir.OUT:
                    net.drivers.append(ep)
                    if pin.tri_state:
                        # Puede soltar el bus: cuenta como driver potencial, no duro.
                        potential_drivers.append(f"{part.ref}.{pin.display_name()}")
                    elif (part.spec.model or "") not in _IDEAL_SOURCE_MODELS:
                        logic_drivers.append(f"{part.ref}.{pin.display_name()}")
                elif d == PinDir.INOUT:
                    # Puede driverear, pero también soltar el bus.
                    net.drivers.append(ep)
                    net.loads.append(ep)
                    potential_drivers.append(f"{part.ref}.{pin.display_name()}")
                elif d in (PinDir.IN, PinDir.POWER):
                    net.loads.append(ep)

            tie = net.tie_value()
            tie_is_determinate = tie is not None and tie.determinate

            # Fuentes ideales conectadas al cable (símbolos GND, VCC, LOGICSTATE).
            # Se distinguen de los atados `tie:` porque son componentes con un
            # nivel propio, y dos de niveles opuestos son un cortocircuito de
            # verdad, no una redundancia.
            ideal_levels: Dict[L, List[str]] = {}
            for ep in net.pin_endpoints():
                p_ = self.parts.get(ep.part)
                if p_ is None or p_.spec is None:
                    continue
                pin_ = p_.pin(ep.pin)
                if pin_ is None or pin_.direction != PinDir.OUT:
                    continue
                if (p_.spec.model or "") not in _IDEAL_SOURCE_MODELS:
                    continue
                if p_.spec.model == "logicstate":
                    lv = (L.HIGH if str(p_.props.get("STATE", "0")) in ("1", "HIGH")
                          else L.LOW)
                elif p_.spec.model == "ground":
                    lv = L.LOW
                else:  # "rail" → VCC
                    lv = L.HIGH
                ideal_levels.setdefault(lv, []).append(p_.ref)

            opposing: Dict[L, List[str]] = {}
            if tie_is_determinate:
                opposing[tie] = ["tie"]
            for lv, refs in ideal_levels.items():
                opposing.setdefault(lv, []).extend(refs)

            if len(logic_drivers) > 1:
                issues.append(
                    Issue(
                        "error",
                        f"el net '{net.label}' tiene {len(logic_drivers)} salidas "
                        f"lógicas conectadas entre sí ({', '.join(logic_drivers)}): "
                        f"cortocircuito garantizado. Usa buffers tri-estado.",
                        net.label,
                    )
                )
            elif len(opposing) > 1:
                detalle = "; ".join(
                    f"{lv} ← {', '.join(refs)}" for lv, refs in sorted(opposing.items(), key=lambda kv: str(kv[0]))
                )
                issues.append(
                    Issue(
                        "error",
                        f"el net '{net.label}' enfrenta alimentaciones opuestas "
                        f"({detalle}): cortocircuito entre VCC y GND",
                        net.label,
                    )
                )
            elif logic_drivers and (tie_is_determinate or ideal_levels):
                other = tie if tie_is_determinate else next(iter(ideal_levels))
                issues.append(
                    Issue(
                        "warning",
                        f"el net '{net.label}' conecta la salida "
                        f"{logic_drivers[0]} directamente a un nivel fijo "
                        f"({other}): si la salida intenta el nivel opuesto, hay "
                        f"cortocircuito",
                        net.label,
                    )
                )

            if not net.loads:
                issues.append(
                    Issue("info", f"el net '{net.label}' no alimenta ninguna entrada",
                          net.label)
                )

            # Cable sin nada que lo defina: leerá X para siempre. Se comprueba
            # así (y no mirando solo `net.drivers`) porque un pin INOUT sin
            # conectar —el cátodo de un LED al aire— cuenta como "driver" pero
            # en realidad no empuja nada.
            if (not tie_is_determinate and not ideal_levels and not logic_drivers
                    and not potential_drivers):
                if net.loads:
                    issues.append(
                        Issue(
                            "warning",
                            f"el net '{net.label}' tiene entradas pero ningún "
                            f"driver (leerá X permanentemente)",
                            net.label,
                        )
                    )
                elif net.endpoints:
                    issues.append(
                        Issue(
                            "warning",
                            f"el net '{net.label}' queda flotante: ningún "
                            f"componente lo define",
                            net.label,
                        )
                    )

        # 4. Pines de entrada sin conectar.
        for part in self.parts.values():
            if part.spec is None:
                continue
            for pin in part.spec.pins:
                if pin.direction != PinDir.IN:
                    continue
                for bit in range(max(pin.width, 1)):
                    if self.net_of(part.ref, pin.name, bit) is None:
                        issues.append(
                            Issue(
                                "warning",
                                f"entrada {part.ref}.{pin.display_name()}"
                                + (f"[{bit}]" if pin.width > 1 else "")
                                + " sin conectar",
                                part.ref,
                            )
                        )

        if strict:
            issues = [
                Issue("error", i.message, i.where) if i.kind == "warning" else i
                for i in issues
            ]
        return issues

    def errors(self) -> List[Issue]:
        return [i for i in self.validate() if i.kind == "error"]

    # -- análisis de colisiones (lo usa el simulador) ---------------------
    def contention(self, net_name: str, values: Sequence) -> bool:
        return is_contention(values)

    def resolve(self, net_name: str, values: Iterable) -> L:
        return bus_value(values)

    # -- utilidades -------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        kinds: Dict[str, int] = {}
        for p in self.parts.values():
            kinds[p.part] = kinds.get(p.part, 0) + 1
        return {
            "name": self.name,
            "parts": len(self.parts),
            "nets": len(self.nets),
            "buses": len(self.buses),
            "by_kind": kinds,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Circuit '{self.name}' {len(self.parts)} partes, "
            f"{len(self.nets)} nets>"
        )
