"""Constructor programático de circuitos.

Es la segunda puerta de entrada al motor, y la más potente: como es Python de
verdad, puedes usar bucles, funciones y condiciones. Un bus de 64 bits con sus
ocho transceptores se escribe en tres líneas, mientras que en YAML serían
doscientas.

La IA también puede generar código para esta API en vez de YAML; el resultado
es el mismo, porque :meth:`Circuit.build` produce exactamente el mismo grafo.

.. code-block:: python

   from voltio.builder import Circuit

   c = Circuit("parpadeo")
   clk = c.clock("CLK", freq=2)
   ff  = c.part("DFF", "U1")
   led = c.led("D1")

   c.net("C").join(clk.Q, ff.pin("CLK"))
   # Realimentación invertida: Q̄ → D, para que oscile a la mitad del reloj.
   c.net("FB").join(ff.pin("/Q"), ff.pin("D"))
   c.net("OUT").join(ff.pin("Q"), led.pin("A"))
   c.gnd(led.pin("K"))

   circ = c.build()
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .core.ir import BusDef, Circuit as IrCircuit, Endpoint, Net, Part
from .parts.library import LIBRARY, Library

__all__ = ["Circuit", "PartHandle", "PinRef", "PinGroup", "NetBuilder",
           "Tie", "ZERO", "ONE", "FLOAT", "UNKNOWN"]


class PinRef:
    """Referencia a un pin (o a un bit de un pin) de una parte."""

    __slots__ = ("ref", "pin", "lo", "hi")

    def __init__(self, ref: str, pin: str, lo: Optional[int] = None,
                 hi: Optional[int] = None):
        self.ref, self.pin, self.lo, self.hi = ref, pin, lo, hi

    def bit(self, i: int) -> "PinRef":
        return PinRef(self.ref, self.pin, i, i)

    def bits(self, lo: int, hi: int) -> "PinRef":
        return PinRef(self.ref, self.pin, lo, hi)

    def all(self) -> "PinRef":
        return PinRef(self.ref, self.pin, None, None)

    @property
    def width(self) -> Optional[int]:
        if self.lo is None:
            return None
        return self.hi - self.lo + 1

    def tokens(self) -> List[str]:
        """Forma textual, la que entiende el cargador del DSL."""
        if self.lo is None and self.hi is None:
            return [f"{self.ref}.{self.pin}"]
        if self.lo == self.hi:
            return [f"{self.ref}.{self.pin}[{self.lo}]"]
        return [f"{self.ref}.{self.pin}[{self.lo}:{self.hi}]"]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Pin {self.tokens()[0]}>"


class PinGroup:
    """Un pin completo, indexable: ``u1.Q[3]``, ``u1.Q[0:7]``, ``u1.Q.all()``.

    Existe porque la sintaxis que uno quiere escribir es la misma que en el
    DSL. Si ``u1.Q`` devolviera directamente un :class:`PinRef`, no se podría
    indexar; si devolviera un slice de Python, no se podría pasar a
    :meth:`Circuit.tie` tal cual.
    """

    __slots__ = ("ref", "pin", "width")

    def __init__(self, ref: str, pin: str, width: int = 1):
        self.ref, self.pin, self.width = ref, pin, width

    def __getitem__(self, key) -> "PinRef":
        if isinstance(key, slice):
            lo = key.start or 0
            hi = (key.stop - 1) if key.stop is not None else (self.width - 1)
            return PinRef(self.ref, self.pin, lo, hi)
        return PinRef(self.ref, self.pin, int(key), int(key))

    def bit(self, i: int) -> "PinRef":
        return PinRef(self.ref, self.pin, i, i)

    def bits(self, lo: int, hi: int) -> "PinRef":
        """Rango inclusivo, igual que en el DSL: ``bits(0, 7)`` son 8 bits."""
        return PinRef(self.ref, self.pin, lo, hi)

    def all(self) -> "PinRef":
        return PinRef(self.ref, self.pin)

    def __iter__(self):
        for i in range(self.width):
            yield PinRef(self.ref, self.pin, i, i)

    def __len__(self) -> int:
        return self.width

    def __repr__(self) -> str:  # pragma: no cover
        w = f"[0:{self.width - 1}]" if self.width > 1 else ""
        return f"<Pin {self.ref}.{self.pin}{w}>"


class Tie:
    """Un nivel lógico constante, para atar pines que el diseño no usa."""

    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value

    def tokens(self) -> List[str]:
        return [f"${self.value}"]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tie ${self.value}>"


ZERO = Tie("0")
ONE = Tie("1")
FLOAT = Tie("z")
UNKNOWN = Tie("x")


class PartHandle:
    """Una parte ya colocada. Sus pines se acceden como atributos.

    ``u1.Q`` devuelve un :class:`PinRef`; ``u1.Q[3]`` y ``u1.Q[0:7]`` también,
    gracias a ``__getitem__`` con slice.
    """

    def __init__(self, builder: "Circuit", part: Part):
        self._b = builder
        self.part = part

    @property
    def ref(self) -> str:
        return self.part.ref

    def pin(self, name: str, lo: Optional[int] = None,
            hi: Optional[int] = None) -> PinRef:
        return PinRef(self.part.ref, name, lo, hi)

    def group(self, name: str) -> "PinGroup":
        """El pin completo, indexable. Es lo que devuelve ``handle.NOMBRE``."""
        width = 1
        if self.part.spec is not None:
            p = self.part.spec.pin(name)
            if p is not None:
                width = max(p.width, 1)
        return PinGroup(self.part.ref, name, width)

    def __getattr__(self, name: str):
        if name.startswith("_") or name in ("part",):
            raise AttributeError(name)
        return self.group(name)

    def __getitem__(self, key: str) -> "PinGroup":
        """Acceso a pines cuyo nombre no es un identificador Python válido.

        Los activos en baja empiezan por barra (``/OE``, ``/MR``), que no se
        puede escribir como atributo. ``u1["/OE"]`` es la forma cómoda;
        ``u1.group("/OE")`` hace exactamente lo mismo.
        """
        return self.group(key)

    def prop(self, **kwargs) -> "PartHandle":
        for k, v in kwargs.items():
            self.part.props[k.upper()] = v
        return self

    def move(self, x: float, y: float) -> "PartHandle":
        self.part.x, self.part.y = x, y
        return self

    def rotate(self, deg: int) -> "PartHandle":
        self.part.rotation = deg % 360
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.part.ref}:{self.part.part}>"


class NetBuilder:
    """Un cable en construcción. Se le van añadiendo extremos."""

    def __init__(self, circuit: "Circuit", name: Optional[str],
                 bus_width: Optional[int] = None):
        self._c = circuit
        self.name = name
        self.bus_width = bus_width
        self.ends: List[Union[PinRef, Tie]] = []

    def join(self, *pins) -> "NetBuilder":
        """Añade extremos.

        Acepta :class:`PinRef`, :class:`PinGroup`, :class:`Tie`, cadenas
        (``"U1.A"``) y listas de cualquiera de ellas.
        """
        for p in pins:
            if isinstance(p, (list, tuple)):
                self.join(*p)
            else:
                self.ends.append(_coerce(p))
        return self

    def bus(self, width: int) -> "NetBuilder":
        self.bus_width = width
        return self

    def tie(self, level) -> "NetBuilder":
        """Ata todo el cable a un nivel fijo (además de los extremos)."""
        self.ends.append(_coerce(level))
        return self

    def to_dict(self) -> Dict[str, Any]:
        connect: List[str] = []
        tie = None
        for e in self.ends:
            if isinstance(e, Tie):
                tie = e.value
            else:
                connect.extend(e.tokens())
        d: Dict[str, Any] = {"name": self.name, "connect": connect}
        if tie is not None:
            d["tie"] = tie
        if self.bus_width:
            d["bus"] = self.bus_width
        return d


def _coerce(p) -> Union[PinRef, Tie]:
    if isinstance(p, (PinRef, Tie)):
        return p
    if isinstance(p, PinGroup):
        # Sin índices explícitos: significa "todos los bits del pin", que el
        # cargador del DSL expande usando el ancho real declarado.
        return PinRef(p.ref, p.pin)
    if isinstance(p, str):
        from .dsl.loader import parse_endpoint

        ref, pin, sel = parse_endpoint(p)
        if ref == "$TIE":
            return Tie(pin)
        if sel is None:
            return PinRef(ref, pin)
        if sel == "*":
            return PinRef(ref, pin)
        if isinstance(sel, int):
            return PinRef(ref, pin, sel, sel)
        return PinRef(ref, pin, sel[0], sel[1])
    if isinstance(p, bool):
        return Tie("1" if p else "0")
    if isinstance(p, int):
        return Tie(str(p))
    raise TypeError(f"no sé convertir {p!r} en un extremo de cable")


class Circuit:
    """Constructor de circuitos con sintaxis cómoda.

    No confundir con :class:`voltio.core.ir.Circuit`, que es el grafo ya
    construido. Esta clase acumula la definición y :meth:`build` la materializa.
    """

    def __init__(self, name: str = "sin_titulo", title: str = "",
                 library: Optional[Library] = None):
        self.name = name
        self.title = title or name
        self.library = library or LIBRARY
        self._parts: List[Part] = []
        self._nets: List[NetBuilder] = []
        self._buses: List[BusDef] = []
        self._refs: Dict[str, PartHandle] = {}
        self._auto = 0
        self.metadata: Dict[str, Any] = {}

    # -- partes -----------------------------------------------------------
    def part(self, part_name: str, ref: Optional[str] = None,
             x: float = 0, y: float = 0, rotation: int = 0,
             **props) -> PartHandle:
        """Coloca un componente. Si omite ``ref``, se genera uno automático."""
        spec = self.library.get(part_name)
        if spec is None:
            raise KeyError(
                f"componente '{part_name}' no está en la librería. "
                f"Búscalo con voltio.parts.search('{part_name}')"
            )
        if ref is None:
            ref = self._auto_ref(spec)
        if ref in self._refs:
            raise ValueError(f"referencia duplicada: {ref}")
        p = Part(ref=ref, part=spec.name, x=x, y=y, rotation=rotation,
                 props={k.upper(): v for k, v in props.items()})
        self._parts.append(p)
        h = PartHandle(self, p)
        self._refs[ref] = h
        return h

    def _auto_ref(self, spec) -> str:
        prefix = {
            "74xx": "U", "microcontroladores": "U", "cpu": "U",
            "memoria": "U", "displays": "DISP", "optoelectronica": "D",
            "pasivos": "R", "electromecanicos": "SW", "fuentes": "V",
            "instrumentos": "P", "primitivas": "U",
        }.get(spec.category, "U")
        if spec.name in ("GND", "VCC"):
            prefix = spec.name[:3]
        elif spec.name == "CLOCK":
            prefix = "CLK"
        elif spec.name == "LED":
            prefix = "D"
        elif spec.name in ("RES",):
            prefix = "R"
        while True:
            self._auto += 1
            cand = f"{prefix}{self._auto}"
            if cand not in self._refs:
                return cand

    def __getitem__(self, ref: str) -> PartHandle:
        return self._refs[ref]

    def __getattr__(self, name: str) -> PartHandle:
        # Permite `c.U1` además de `c["U1"]`.
        if name.startswith("_"):
            raise AttributeError(name)
        refs = self.__dict__.get("_refs", {})
        if name in refs:
            return refs[name]
        raise AttributeError(name)

    # -- atajos para lo más común -----------------------------------------
    def clock(self, name: str = "CLK1", freq: float = 1000.0,
              duty: float = 50.0, x: float = 0, y: float = 0) -> PartHandle:
        """Generador de reloj. Devuelve el handle; su salida es ``.Q``."""
        return self.part("CLOCK", ref=name, x=x, y=y, FREQ=freq, DUTY=duty)

    def led(self, ref: Optional[str] = None, color: str = "ROJO",
            x: float = 0, y: float = 0) -> PartHandle:
        return self.part("LED", ref=ref, x=x, y=y, COLOR=color)

    def resistor(self, ref: Optional[str] = None, value: str = "330",
                 x: float = 0, y: float = 0) -> PartHandle:
        return self.part("RES", ref=ref, x=x, y=y, R=value)

    def logic(self, ref: Optional[str] = None, state: int = 0,
              x: float = 0, y: float = 0) -> PartHandle:
        """Entrada lógica conmutable desde la GUI o con ``set_interactive``."""
        return self.part("LOGICSTATE", ref=ref, x=x, y=y, STATE=str(state))

    def button(self, ref: Optional[str] = None, x: float = 0,
               y: float = 0) -> PartHandle:
        return self.part("BUTTON", ref=ref, x=x, y=y)

    def vcc(self, ref: Optional[str] = None, x: float = 0, y: float = 0) -> PartHandle:
        return self.part("VCC", ref=ref, x=x, y=y)

    def gnd_part(self, ref: Optional[str] = None, x: float = 0,
                 y: float = 0) -> PartHandle:
        return self.part("GND", ref=ref, x=x, y=y)

    # -- cables -----------------------------------------------------------
    def net(self, name: Optional[str] = None, bus: Optional[int] = None,
            *pins) -> NetBuilder:
        """Crea un cable y le añade extremos de una vez."""
        nb = NetBuilder(self, name, bus)
        if pins:
            nb.join(*pins)
        self._nets.append(nb)
        return nb

    def connect(self, *pins, name: Optional[str] = None,
                bus: Optional[int] = None) -> NetBuilder:
        """Atajo: ``c.connect(u1.Y, led.A)``."""
        return self.net(name, bus, *pins)

    def gnd(self, *pins) -> NetBuilder:
        """Ata los pines a tierra creando (o reutilizando) la red GND."""
        nb = next((n for n in self._nets if n.name == "GND"), None)
        if nb is None:
            nb = NetBuilder(self, "GND")
            self._nets.append(nb)
        return nb.join(*pins)

    def vcc_net(self, *pins) -> NetBuilder:
        """Ata los pines a alimentación creando (o reutilizando) la red VCC."""
        nb = next((n for n in self._nets if n.name == "VCC"), None)
        if nb is None:
            nb = NetBuilder(self, "VCC")
            self._nets.append(nb)
        return nb.join(*pins)

    def tie(self, level, *pins, name: Optional[str] = None) -> NetBuilder:
        """Ata un conjunto de pines a un nivel fijo (``tie(1, u1./MR)``)."""
        nb = NetBuilder(self, name)
        nb.ends.append(level if isinstance(level, Tie) else _coerce(level))
        nb.join(*pins)
        self._nets.append(nb)
        return nb

    def bus(self, name: str, width: int, *pin_groups) -> List[NetBuilder]:
        """Crea un bus de ``width`` bits uniendo grupos de pines bit a bit.

        Cada elemento de ``pin_groups`` debe ser un :class:`PinRef` de exactamente
        ``width`` bits. Es la forma corta de escribir un bus de datos::

            c.bus("DATA", 8, rom.D.all(), ram.D.all(), cpu.DATA.bits(0, 7))
        """
        nets: List[NetBuilder] = []
        for bit in range(width):
            nb = NetBuilder(self, f"{name}[{bit}]")
            for g in pin_groups:
                pr = _coerce(g)
                if pr.lo is None:
                    nb.join(PinRef(pr.ref, pr.pin, bit, bit))
                else:
                    nb.join(PinRef(pr.ref, pr.pin, pr.lo + bit, pr.lo + bit))
            self._nets.append(nb)
            nets.append(nb)
        self._buses.append(BusDef(name=name, width=width,
                                  nets=[n.name for n in nets]))
        return nets

    # -- metadatos --------------------------------------------------------
    def simulate(self, duration: float = 1e-3, timescale: float = 1e-9) -> "Circuit":
        self.metadata["simulation"] = {"duration": duration, "timescale": timescale}
        return self

    def describe(self, text: str) -> "Circuit":
        self.metadata.setdefault("meta", {})["description"] = text
        return self

    # -- materialización --------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": self.name, "title": self.title}
        d.update(self.metadata)
        d["parts"] = []
        for p in self._parts:
            item: Dict[str, Any] = {"ref": p.ref, "part": p.part}
            if p.x or p.y:
                item["at"] = [p.x, p.y]
            if p.rotation:
                item["rotation"] = p.rotation
            if p.props:
                item["props"] = dict(p.props)
            d["parts"].append(item)
        d["nets"] = [n.to_dict() for n in self._nets]
        if self._buses:
            d["buses"] = [{"name": b.name, "width": b.width} for b in self._buses]
        return d

    def to_yaml(self) -> str:
        from .core import miniyaml

        return miniyaml.dumps(self.to_dict())

    def build(self) -> IrCircuit:
        """Construye el grafo. Lo que devuelven ``Engine`` y el renderizador."""
        from .dsl.loader import load_dict

        circ, issues = load_dict(self.to_dict(), library=self.library)
        errs = [i for i in issues if i.kind == "error"]
        if errs:
            raise ValueError(
                "el circuito construido no valida:\n  "
                + "\n  ".join(str(e) for e in errs)
            )
        circ.metadata = dict(self.metadata)
        return circ

    def build_lenient(self) -> Tuple[IrCircuit, List]:
        """Como :meth:`build` pero devuelve los problemas en vez de lanzar."""
        from .dsl.loader import load_dict

        circ, issues = load_dict(self.to_dict(), library=self.library)
        circ.metadata = dict(self.metadata)
        return circ, issues


