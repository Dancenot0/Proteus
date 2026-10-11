"""Librería de componentes.

Añade un componente nuevo así:

.. code-block:: python

   from voltio.parts.library import Library, register

   @register(category="74xx", description="Buffer tri-estado octal")
   def _74ls245(lib):
       ...
       return PartSpec(name="74LS245", pins=[...], model="tri_bus")

El decorador lo mete en la librería global por defecto, que es la que usa
todo el motor.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from .spec import PartSpec, PropSpec, gate_spec, _pin
from ..core.ir import Pin, PinDir

__all__ = ["Library", "LIBRARY", "register", "all_parts", "search", "part"]


class Library:
    """Registro de plantillas de componentes, indexado por nombre y alias."""

    def __init__(self, name: str = "default"):
        self.name = name
        self._specs: Dict[str, PartSpec] = {}
        self._alias: Dict[str, str] = {}

    # -- registro ---------------------------------------------------------
    def add(self, spec: PartSpec, *aliases: str) -> PartSpec:
        key = spec.name.upper()
        self._specs[key] = spec
        self._alias[key] = key
        for a in aliases:
            self._alias[a.upper()] = key
        # Alias implícitos: sin espacios, sin guiones, familia LS/HCT/HC.
        for a in _implicit_aliases(spec.name):
            self._alias.setdefault(a.upper(), key)
        return spec

    def get(self, name: str) -> Optional[PartSpec]:
        if name is None:
            return None
        return self._specs.get(self._alias.get(name.upper(), name.upper()))

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> List[str]:
        return sorted(self._specs)

    def specs(self) -> Iterable[PartSpec]:
        return (self._specs[k] for k in sorted(self._specs))

    def by_category(self) -> Dict[str, List[PartSpec]]:
        out: Dict[str, List[PartSpec]] = {}
        for s in self.specs():
            out.setdefault(s.category, []).append(s)
        return out

    def search(self, query: str) -> List[PartSpec]:
        """Búsqueda difusa por nombre, descripción o etiqueta."""
        q = query.lower().strip()
        if not q:
            return list(self.specs())
        scored = []
        for s in self.specs():
            hay = " ".join(
                [s.name, s.description, s.category, " ".join(s.tags)]
            ).lower()
            if q in hay:
                score = 0
                if q == s.name.lower():
                    score = 100
                elif q in s.name.lower():
                    score = 60
                elif q in s.tags:
                    score = 30
                else:
                    score = 10
                scored.append((score, s))
        scored.sort(key=lambda t: -t[0])
        return [s for _, s in scored]


def _implicit_aliases(name: str) -> List[str]:
    """``74LS00`` también responde a ``7400``, ``74HC00``, ``74HCT00``, ``LS00``."""
    out: List[str] = []
    up = name.upper().replace(" ", "").replace("-", "")
    out.append(up)
    if up.startswith("74"):
        tail = up[2:]
        for fam in ("LS", "HC", "HCT", "S", "F", "AC", "AHC", ""):
            if tail.startswith(fam) and fam:
                base = tail[len(fam):]
                out.append("74" + base)
                for f2 in ("LS", "HC", "HCT", "S", "F", "AC", "AHC"):
                    out.append("74" + f2 + base)
                out.append(fam + base)
                break
    return out


LIBRARY = Library("voltio")

_PENDING: List[Callable[[Library], None]] = []


def register(category: str = "misc", description: str = "", **kw):
    """Decorador: registra un componente en la librería global."""

    def deco(fn):
        def run(lib: Library) -> None:
            res = fn(lib)
            if res is not None:
                if isinstance(res, PartSpec):
                    res = [res]
                for spec in res:
                    if not spec.category or spec.category == "misc":
                        spec.category = category
                    if description and not spec.description:
                        spec.description = description
                    for k, v in kw.items():
                        setattr(spec, k, v)
                    lib.add(spec)

        _PENDING.append(run)
        return fn

    return deco


# ---------------------------------------------------------------------------
# Construcción de la librería
# ---------------------------------------------------------------------------

_P = PropSpec


def _build(lib: Library) -> None:
    _passives(lib)
    _sources(lib)
    _gates(lib)
    _combinational(lib)
    _sequential(lib)
    _memory(lib)
    _displays(lib)
    _mcu(lib)
    _instruments(lib)


# -- pasivos y fuentes ----------------------------------------------------


def _passives(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="RES",
            category="pasivos",
            description="Resistencia",
            pins=[_pin("1", PinDir.INOUT, side="left"), _pin("2", PinDir.INOUT, side="right")],
            props=[_P("R", "Resistencia", "string", "10k")],
            symbol="resistor",
            model="resistor",
            spice_prefix="R",
            width=3.0,
            height=1.0,
            tags=["analogico", "pasivo"],
        ),
        "R", "RESISTOR", "RESISTENCIA",
    )
    lib.add(
        PartSpec(
            name="LED",
            category="optoelectronica",
            description="Diodo emisor de luz",
            pins=[
                _pin("A", PinDir.IN, side="left", order=0),
                # El cátodo es una carga: el LED no drivea nada. Si se declara
                # INOUT, un cátodo al aire cuenta como "cable con driver" y el
                # validador se calla un fallo real.
                _pin("K", PinDir.IN, side="right", order=0),
            ],
            props=[_P("COLOR", "Color", "choice", "ROJO",
                      choices=["ROJO", "VERDE", "AMARILLO", "AZUL", "BLANCO"])],
            symbol="led",
            model="led",
            spice_prefix="D",
            width=2.0,
            height=2.0,
            tags=["indicador", "salida"],
        ),
        "D", "DIODO_LED",
    )
    lib.add(
        PartSpec(
            name="SWITCH",
            category="electromecanicos",
            description="Interruptor (conmutable por el usuario)",
            pins=[
                # 1 = lado que entrega el nivel (salida hacia el circuito)
                # 2 = lado que recibe el nivel (se ata a VCC o a GND)
                # Cerrado, el modelo pasa el nivel de 2 hacia 1; abierto, suelta.
                _pin("1", PinDir.OUT, side="left"),
                _pin("2", PinDir.IN, side="right"),
            ],
            props=[_P("STATE", "Estado inicial", "choice", "0", choices=["0", "1"])],
            symbol="switch",
            model="switch",
            spice_prefix="S",
            width=3.0,
            height=2.0,
            tags=["entrada", "interactivo"],
        ),
        "SW", "INTERRUPTOR",
    )
    lib.add(
        PartSpec(
            name="BUTTON",
            category="electromecanicos",
            description="Pulsador (momentáneo)",
            pins=[
                _pin("1", PinDir.OUT, side="left"),
                _pin("2", PinDir.IN, side="right"),
            ],
            props=[_P("STATE", "Estado", "choice", "0", choices=["0", "1"])],
            symbol="pushbutton",
            model="switch",
            spice_prefix="S",
            width=3.0,
            height=2.0,
            tags=["entrada", "interactivo"],
        ),
        "PB", "PULSADOR",
    )


def _sources(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="VCC",
            category="fuentes",
            description="Alimentación positiva (+5 V por defecto)",
            pins=[_pin("P", PinDir.OUT, side="bottom")],
            props=[_P("V", "Voltaje", "string", "5V")],
            symbol="vcc",
            model="rail",
            spice_prefix="V",
            width=1.0,
            height=1.0,
            tags=["power"],
        ),
        "V+", "POWER", "5V",
    )
    lib.add(
        PartSpec(
            name="GND",
            category="fuentes",
            description="Tierra (0 V)",
            pins=[_pin("P", PinDir.OUT, side="bottom")],
            symbol="gnd",
            model="ground",
            spice_prefix="V",
            width=1.0,
            height=1.0,
            tags=["power"],
        ),
        "V-", "TIERRA", "0V",
    )
    lib.add(
        PartSpec(
            name="LOGICSTATE",
            category="fuentes",
            description="Nivel lógico fijo, conmutable (0/1)",
            pins=[_pin("Q", PinDir.OUT, side="right")],
            props=[_P("STATE", "Estado", "choice", "0", choices=["0", "1"])],
            symbol="logicstate",
            model="logicstate",
            spice_prefix="V",
            width=2.0,
            height=2.0,
            tags=["entrada", "digital", "interactivo"],
        ),
        "LOGIC", "ESTADO_LOGICO",
    )
    lib.add(
        PartSpec(
            name="CLOCK",
            category="fuentes",
            description="Generador de reloj digital",
            pins=[_pin("Q", PinDir.OUT, side="right")],
            props=[
                _P("FREQ", "Frecuencia (Hz)", "float", 1.0),
                _P("DUTY", "Ciclo de trabajo (%)", "float", 50.0),
                _P("PHASE", "Fase inicial", "choice", "0", choices=["0", "1"]),
            ],
            symbol="clock",
            model="clock",
            spice_prefix="V",
            width=3.0,
            height=2.0,
            tags=["entrada", "digital", "reloj"],
        ),
        "CLK", "RELOJ", "OSC",
    )


# -- compuertas 74xx ------------------------------------------------------


def _gates(lib: Library) -> None:
    g = [
        ("74LS00", "Cuádruple NAND de 2 entradas", 2, "gate2", "nand"),
        ("74LS02", "Cuádruple NOR de 2 entradas", 2, "gate2", "nor"),
        ("74LS04", "Séxtuple inversor (NOT)", 1, "inverter", "not"),
        ("74LS08", "Cuádruple AND de 2 entradas", 2, "gate2", "and"),
        ("74LS32", "Cuádruple OR de 2 entradas", 2, "gate2", "or"),
        ("74LS86", "Cuádruple XOR de 2 entradas", 2, "gate2", "xor"),
        ("74LS10", "Triple NAND de 3 entradas", 3, "gate2", "nand"),
        ("74LS27", "Triple NOR de 3 entradas", 3, "gate2", "nor"),
        ("74LS11", "Triple AND de 3 entradas", 3, "gate2", "and"),
        ("74LS20", "Doble NAND de 4 entradas", 4, "gate2", "nand"),
        ("74LS30", "NAND de 8 entradas", 8, "gate2", "nand"),
        ("74LS21", "Doble AND de 4 entradas", 4, "gate2", "and"),
        ("74LS266", "Cuádruple XNOR de 2 entradas", 2, "gate2", "xnor"),
    ]
    for name, desc, n, sym, model in g:
        lib.add(gate_spec(name, desc, n, sym, model), name.replace("74LS", "74"))

    lib.add(
        gate_spec(
            "74LS07", "Séxtuple buffer de salida en colector abierto",
            1, "buffer", "buffer", in_prefix="A", out_name="Y",
        ),
        "7407",
    )
    lib.add(
        PartSpec(
            name="74LS125",
            category="74xx",
            description="Cuádruple buffer tri-estado (enable activo bajo)",
            pins=[
                _pin("/OE", PinDir.IN, side="left", order=0),
                _pin("A", PinDir.IN, side="left", order=1),
                _pin("Y", PinDir.OUT, side="right", order=0, tri_state=True),
            ],
            symbol="tribuf",
            model="tribuf_alow",
            width=3.0,
            height=3.0,
            tags=["digital", "bus", "tri-estado", "74xx"],
        ),
        "74125",
    )
    lib.add(
        PartSpec(
            name="74LS126",
            category="74xx",
            description="Cuádruple buffer tri-estado (enable activo alto)",
            pins=[
                _pin("OE", PinDir.IN, side="left", order=0),
                _pin("A", PinDir.IN, side="left", order=1),
                _pin("Y", PinDir.OUT, side="right", order=0, tri_state=True),
            ],
            symbol="tribuf",
            model="tribuf_ahigh",
            width=3.0,
            height=3.0,
            tags=["digital", "bus", "tri-estado", "74xx"],
        ),
        "74126",
    )


# -- combinacionales ------------------------------------------------------


def _combinational(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="74LS138",
            category="74xx",
            description="Decodificador/demultiplexor 3 a 8, salidas activas bajas",
            pins=[
                _pin("A", PinDir.IN, width=3, side="left", order=0),
                _pin("/E1", PinDir.IN, side="left", order=1),
                _pin("/E2", PinDir.IN, side="left", order=2),
                _pin("E3", PinDir.IN, side="left", order=3),
                _pin("/Y", PinDir.OUT, width=8, side="right", order=0),
            ],
            symbol="ic",
            model="dec138",
            width=6.0,
            height=8.0,
            tags=["digital", "decodificador", "74xx"],
        ),
        "74138",
    )
    lib.add(
        PartSpec(
            name="74LS47",
            category="74xx",
            description="Decodificador BCD a 7 segmentos, salidas activas bajas",
            pins=[
                _pin("A", PinDir.IN, width=4, side="left", order=0),
                _pin("/LT", PinDir.IN, side="left", order=1),
                _pin("/RBI", PinDir.IN, side="left", order=2),
                _pin("/BI", PinDir.IN, side="left", order=3),
                _pin("/a", PinDir.OUT, width=7, side="right", order=0),
            ],
            symbol="ic",
            model="dec47",
            width=6.0,
            height=7.0,
            tags=["digital", "display", "bcd", "74xx"],
        ),
        "7447",
    )
    lib.add(
        PartSpec(
            name="74LS48",
            category="74xx",
            description="Decodificador BCD a 7 segmentos, salidas activas altas",
            pins=[
                _pin("A", PinDir.IN, width=4, side="left", order=0),
                _pin("/LT", PinDir.IN, side="left", order=1),
                _pin("/RBI", PinDir.IN, side="left", order=2),
                _pin("/BI", PinDir.IN, side="left", order=3),
                _pin("a", PinDir.OUT, width=7, side="right", order=0),
            ],
            symbol="ic",
            model="dec48",
            width=6.0,
            height=7.0,
            tags=["digital", "display", "bcd", "74xx"],
        ),
        "7448",
    )
    lib.add(
        PartSpec(
            name="74LS151",
            category="74xx",
            description="Multiplexor/selecto de datos 8 a 1",
            pins=[
                _pin("D", PinDir.IN, width=8, side="left", order=0),
                _pin("S", PinDir.IN, width=3, side="left", order=1),
                _pin("/E", PinDir.IN, side="left", order=2),
                _pin("Y", PinDir.OUT, side="right", order=0),
                _pin("/W", PinDir.OUT, side="right", order=1),
            ],
            symbol="ic",
            model="mux151",
            width=6.0,
            height=8.0,
            tags=["digital", "mux", "74xx"],
        ),
        "74151",
    )
    lib.add(
        PartSpec(
            name="74LS157",
            category="74xx",
            description="Cuádruple multiplexor 2 a 1",
            pins=[
                _pin("A", PinDir.IN, width=4, side="left", order=0),
                _pin("B", PinDir.IN, width=4, side="left", order=1),
                _pin("S", PinDir.IN, side="left", order=2),
                _pin("/E", PinDir.IN, side="left", order=3),
                _pin("Y", PinDir.OUT, width=4, side="right", order=0, tri_state=True),
            ],
            symbol="ic",
            model="mux157",
            width=6.0,
            height=7.0,
            tags=["digital", "mux", "74xx"],
        ),
        "74157",
    )
    lib.add(
        PartSpec(
            name="74LS245",
            category="74xx",
            description="Transceptor de bus octal tri-estado, bidireccional",
            pins=[
                _pin("/E", PinDir.IN, side="left", order=0),
                _pin("DIR", PinDir.IN, side="left", order=1),
                _pin("A", PinDir.INOUT, width=8, side="left", order=2, tri_state=True),
                _pin("B", PinDir.INOUT, width=8, side="right", order=0, tri_state=True),
            ],
            symbol="ic",
            model="bus245",
            width=7.0,
            height=10.0,
            tags=["digital", "bus", "tri-estado", "74xx"],
        ),
        "74245",
    )
    lib.add(
        PartSpec(
            name="74LS273",
            category="74xx",
            description="Registro de 8 flip-flops D con reset (clock ascendente)",
            pins=[
                _pin("/MR", PinDir.IN, side="left", order=0, active_low=True),
                _pin("CP", PinDir.IN, side="left", order=1),
                _pin("D", PinDir.IN, width=8, side="left", order=2),
                _pin("Q", PinDir.OUT, width=8, side="right", order=0),
            ],
            symbol="register",
            model="reg273",
            width=7.0,
            height=10.0,
            tags=["digital", "registro", "secuencial", "74xx"],
        ),
        "74273",
    )
    lib.add(
        PartSpec(
            name="74LS244",
            category="74xx",
            description="Buffer/línea octal tri-estado",
            pins=[
                _pin("/1E", PinDir.IN, side="left", order=0),
                _pin("/2E", PinDir.IN, side="left", order=1),
                _pin("1A", PinDir.IN, width=4, side="left", order=2),
                _pin("2A", PinDir.IN, width=4, side="left", order=3),
                _pin("1Y", PinDir.OUT, width=4, side="right", order=0, tri_state=True),
                _pin("2Y", PinDir.OUT, width=4, side="right", order=1, tri_state=True),
            ],
            symbol="ic",
            model="buf244",
            width=7.0,
            height=9.0,
            tags=["digital", "bus", "tri-estado", "74xx"],
        ),
        "74244",
    )
    lib.add(
        PartSpec(
            name="74LS85",
            category="74xx",
            description="Comparador de magnitud de 4 bits",
            pins=[
                _pin("A", PinDir.IN, width=4, side="left", order=0),
                _pin("B", PinDir.IN, width=4, side="left", order=1),
                _pin("IA", PinDir.IN, side="left", order=2),
                _pin("IB", PinDir.IN, side="left", order=3),
                _pin("IE", PinDir.IN, side="left", order=4),
                _pin("QA", PinDir.OUT, side="right", order=0),
                _pin("QB", PinDir.OUT, side="right", order=1),
                _pin("QE", PinDir.OUT, side="right", order=2),
            ],
            symbol="ic",
            model="cmp85",
            width=7.0,
            height=9.0,
            tags=["digital", "alu", "comparador", "74xx"],
        ),
        "7485",
    )
    lib.add(
        PartSpec(
            name="74LS283",
            category="74xx",
            description="Sumador completo binario de 4 bits con acarreo rápido",
            pins=[
                _pin("A", PinDir.IN, width=4, side="left", order=0),
                _pin("B", PinDir.IN, width=4, side="left", order=1),
                _pin("C0", PinDir.IN, side="left", order=2),
                _pin("S", PinDir.OUT, width=4, side="right", order=0),
                _pin("C4", PinDir.OUT, side="right", order=1),
            ],
            symbol="ic",
            model="add283",
            width=7.0,
            height=7.0,
            tags=["digital", "alu", "sumador", "aritmetica", "74xx"],
        ),
        "74283",
    )
    lib.add(
        PartSpec(
            name="ALU74181",
            category="74xx",
            description="Unidad aritmético-lógica de 4 bits (74LS181)",
            pins=[
                _pin("A", PinDir.IN, width=4, side="left", order=0),
                _pin("B", PinDir.IN, width=4, side="left", order=1),
                _pin("S", PinDir.IN, width=4, side="left", order=2),
                _pin("M", PinDir.IN, side="left", order=3),
                _pin("/CN", PinDir.IN, side="left", order=4),
                _pin("F", PinDir.OUT, width=4, side="right", order=0),
                _pin("/CN4", PinDir.OUT, side="right", order=1),
                _pin("AeqB", PinDir.OUT, side="right", order=2),
            ],
            symbol="ic",
            model="alu181",
            width=8.0,
            height=9.0,
            tags=["digital", "alu", "cpu", "74xx"],
        ),
        "74181", "74LS181",
    )


# -- secuenciales ---------------------------------------------------------


def _sequential(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="74LS74",
            category="74xx",
            description="Doble flip-flop D con preset y clear, flanco ascendente",
            pins=[
                _pin("/CLR", PinDir.IN, side="left", order=0, active_low=True),
                _pin("CLK", PinDir.IN, side="left", order=1),
                _pin("D", PinDir.IN, side="left", order=2),
                _pin("/PRE", PinDir.IN, side="left", order=3, active_low=True),
                _pin("Q", PinDir.OUT, side="right", order=0),
                _pin("/Q", PinDir.OUT, side="right", order=1, active_low=True),
            ],
            symbol="flipflop",
            model="dff_async",
            width=6.0,
            height=6.0,
            tags=["digital", "flipflop", "secuencial", "74xx"],
        ),
        "7474", "DFF",
    )
    lib.add(
        PartSpec(
            name="74LS76",
            category="74xx",
            description="Doble flip-flop JK con preset y clear, flanco descendente",
            pins=[
                _pin("CLK", PinDir.IN, side="left", order=0),
                _pin("K", PinDir.IN, side="left", order=1),
                _pin("J", PinDir.IN, side="left", order=2),
                _pin("/CLR", PinDir.IN, side="left", order=3, active_low=True),
                _pin("/PRE", PinDir.IN, side="left", order=4, active_low=True),
                _pin("Q", PinDir.OUT, side="right", order=0),
                _pin("/Q", PinDir.OUT, side="right", order=1, active_low=True),
            ],
            symbol="flipflop",
            model="jkff_async",
            width=6.0,
            height=7.0,
            tags=["digital", "flipflop", "secuencial", "74xx"],
        ),
        "7476", "JKFF",
    )
    lib.add(
        PartSpec(
            name="DFF",
            category="primitivas",
            description="Flip-flop D genérico, flanco ascendente, sin asíncronos",
            pins=[
                _pin("CLK", PinDir.IN, side="left", order=0),
                _pin("D", PinDir.IN, side="left", order=1),
                _pin("Q", PinDir.OUT, side="right", order=0),
                _pin("/Q", PinDir.OUT, side="right", order=1, active_low=True),
            ],
            symbol="flipflop",
            model="dff",
            width=5.0,
            height=4.0,
            tags=["digital", "flipflop", "primitiva"],
        ),
    )
    lib.add(
        PartSpec(
            name="74LS193",
            category="74xx",
            description="Contador binario síncrono de 4 bits, arriba/abajo, carga paralela",
            pins=[
                _pin("/MR", PinDir.IN, side="left", order=0, active_low=True),
                _pin("/PL", PinDir.IN, side="left", order=1, active_low=True),
                _pin("CPU", PinDir.IN, side="left", order=2),
                _pin("CPD", PinDir.IN, side="left", order=3),
                _pin("P", PinDir.IN, width=4, side="left", order=4),
                _pin("Q", PinDir.OUT, width=4, side="right", order=0),
                _pin("/TCU", PinDir.OUT, side="right", order=1, active_low=True),
                _pin("/TCD", PinDir.OUT, side="right", order=2, active_low=True),
            ],
            symbol="counter",
            model="cnt193",
            width=7.0,
            height=9.0,
            tags=["digital", "contador", "secuencial", "74xx"],
        ),
        "74193",
    )
    lib.add(
        PartSpec(
            name="74LS161",
            category="74xx",
            description="Contador binario síncrono de 4 bits, carga paralela, reset síncrono",
            pins=[
                _pin("/MR", PinDir.IN, side="left", order=0, active_low=True),
                _pin("/PE", PinDir.IN, side="left", order=1, active_low=True),
                _pin("CP", PinDir.IN, side="left", order=2),
                _pin("CEP", PinDir.IN, side="left", order=3),
                _pin("CET", PinDir.IN, side="left", order=4),
                _pin("P", PinDir.IN, width=4, side="left", order=5),
                _pin("Q", PinDir.OUT, width=4, side="right", order=0),
                _pin("TC", PinDir.OUT, side="right", order=1),
            ],
            symbol="counter",
            model="cnt161",
            width=7.0,
            height=10.0,
            tags=["digital", "contador", "secuencial", "74xx"],
        ),
        "74161",
    )
    lib.add(
        PartSpec(
            name="74LS595",
            category="74xx",
            description="Registro de desplazamiento de 8 bits con latch de salida",
            pins=[
                _pin("DS", PinDir.IN, side="left", order=0),
                _pin("SHCP", PinDir.IN, side="left", order=1),
                _pin("STCP", PinDir.IN, side="left", order=2),
                _pin("/OE", PinDir.IN, side="left", order=3, active_low=True),
                _pin("/MR", PinDir.IN, side="left", order=4, active_low=True),
                _pin("Q", PinDir.OUT, width=8, side="right", order=0, tri_state=True),
                _pin("Q7S", PinDir.OUT, side="right", order=1),
            ],
            symbol="register",
            model="sr595",
            width=7.0,
            height=9.0,
            tags=["digital", "registro", "shift", "74xx"],
        ),
        "74595",
    )
    lib.add(
        PartSpec(
            name="74LS164",
            category="74xx",
            description="Registro de desplazamiento de 8 bits, entrada serie doble",
            pins=[
                _pin("DSA", PinDir.IN, side="left", order=0),
                _pin("DSB", PinDir.IN, side="left", order=1),
                _pin("CP", PinDir.IN, side="left", order=2),
                _pin("/MR", PinDir.IN, side="left", order=3, active_low=True),
                _pin("Q", PinDir.OUT, width=8, side="right", order=0),
            ],
            symbol="register",
            model="sr164",
            width=7.0,
            height=8.0,
            tags=["digital", "registro", "shift", "74xx"],
        ),
        "74164",
    )


# -- memoria --------------------------------------------------------------


def _memory(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="2764",
            category="memoria",
            description="EPROM 8K × 8 bits (64 kbit)",
            pins=[
                _pin("A", PinDir.IN, width=13, side="left", order=0),
                _pin("D", PinDir.INOUT, width=8, side="right", order=0, tri_state=True),
                _pin("/E", PinDir.IN, side="left", order=1),
                _pin("/G", PinDir.IN, side="left", order=2),
            ],
            props=[
                _P("FILE", "Archivo de contenido (hex/intel)", "string", ""),
                _P("INIT", "Contenido inicial en línea", "string", ""),
            ],
            symbol="memory",
            model="rom",
            width=8.0,
            height=12.0,
            tags=["memoria", "rom", "eprom", "cpu"],
        ),
        "EPROM2764",
    )
    lib.add(
        PartSpec(
            name="6264",
            category="memoria",
            description="RAM estática 8K × 8 bits (64 kbit)",
            pins=[
                _pin("A", PinDir.IN, width=13, side="left", order=0),
                _pin("D", PinDir.INOUT, width=8, side="right", order=0, tri_state=True),
                _pin("/E", PinDir.IN, side="left", order=1),
                _pin("/W", PinDir.IN, side="left", order=2),
            ],
            props=[_P("FILE", "Archivo de contenido inicial", "string", "")],
            symbol="memory",
            model="ram",
            width=8.0,
            height=12.0,
            tags=["memoria", "ram", "cpu"],
        ),
        "SRAM6264",
    )
    lib.add(
        PartSpec(
            name="RAM",
            category="primitivas",
            description="RAM genérica de tamaño configurable",
            pins=[
                _pin("A", PinDir.IN, width=16, side="left", order=0),
                _pin("D", PinDir.INOUT, width=8, side="right", order=0, tri_state=True),
                _pin("/WE", PinDir.IN, side="left", order=1, active_low=True),
                _pin("/OE", PinDir.IN, side="left", order=2, active_low=True),
            ],
            props=[
                _P("BITS", "Ancho de dato", "int", 8),
                _P("ADDR", "Ancho de dirección", "int", 8),
            ],
            symbol="memory",
            model="ram_generic",
            width=8.0,
            height=10.0,
            tags=["memoria", "ram", "primitiva"],
        ),
    )


# -- displays -------------------------------------------------------------


def _displays(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="7SEG-CC",
            category="displays",
            description="Display de 7 segmentos, cátodo común",
            pins=[
                _pin("a", PinDir.IN, width=7, side="left", order=0),
                _pin("dp", PinDir.IN, side="left", order=1),
                _pin("CC", PinDir.IN, side="bottom", order=0),
            ],
            props=[
                _P("COMMON", "Común (solo si el pin común queda flotante)",
                   "choice", "CATODO", choices=["CATODO", "ANODO"]),
                _P("COLOR", "Color", "choice", "ROJO",
                   choices=["ROJO", "VERDE", "AMARILLO", "AZUL"]),
            ],
            symbol="7seg",
            model="seven_seg",
            width=6.0,
            height=8.0,
            tags=["display", "salida", "indicador"],
        ),
        "7SEG", "7SEGMENTOS", "DISPLAY7",
    )
    lib.add(
        PartSpec(
            name="7SEG-CA",
            category="displays",
            description="Display de 7 segmentos, ánodo común",
            pins=[
                _pin("a", PinDir.IN, width=7, side="left", order=0),
                _pin("dp", PinDir.IN, side="left", order=1),
                _pin("CA", PinDir.IN, side="bottom", order=0),
            ],
            props=[_P("COLOR", "Color", "choice", "ROJO",
                      choices=["ROJO", "VERDE", "AMARILLO", "AZUL"])],
            symbol="7seg",
            model="seven_seg_ca",
            width=6.0,
            height=8.0,
            tags=["display", "salida", "indicador"],
        ),
    )
    lib.add(
        PartSpec(
            name="LCD1602",
            category="displays",
            description="Módulo LCD alfanumérico 16×2, interfaz paralela HD44780",
            pins=[
                _pin("RS", PinDir.IN, side="left", order=0),
                _pin("RW", PinDir.IN, side="left", order=1),
                _pin("E", PinDir.IN, side="left", order=2),
                _pin("D", PinDir.INOUT, width=8, side="left", order=3, tri_state=True),
                _pin("BL", PinDir.IN, side="left", order=4),
            ],
            props=[
                _P("COLS", "Columnas", "int", 16),
                _P("ROWS", "Filas", "int", 2),
            ],
            symbol="lcd",
            model="lcd1602",
            width=14.0,
            height=6.0,
            tags=["display", "salida", "lcd"],
        ),
        "LCD", "LCD16X2", "HD44780",
    )
    lib.add(
        PartSpec(
            name="MATRIX8X8",
            category="displays",
            description="Matriz de LEDs 8×8",
            pins=[
                _pin("ROW", PinDir.IN, width=8, side="left", order=0),
                _pin("COL", PinDir.IN, width=8, side="right", order=0),
            ],
            symbol="ic",
            model="led_matrix",
            width=8.0,
            height=8.0,
            tags=["display", "salida"],
        ),
    )
    lib.add(
        PartSpec(
            name="BUZZER",
            category="electromecanicos",
            description="Zumbador piezoeléctrico",
            pins=[_pin("P", PinDir.IN, side="left"), _pin("N", PinDir.IN, side="right")],
            props=[_P("FREQ", "Frecuencia resonante (Hz)", "float", 2000.0)],
            symbol="buzzer",
            model="buzzer",
            spice_prefix="B",
            width=3.0,
            height=3.0,
            tags=["salida", "audio"],
        ),
        "SPEAKER", "ALTAVOZ", "ZUMBADOR",
    )
    lib.add(
        PartSpec(
            name="MOTOR",
            category="electromecanicos",
            description="Motor DC simple",
            pins=[_pin("+", PinDir.IN, side="left"), _pin("-", PinDir.IN, side="right")],
            props=[_P("V", "Voltaje nominal", "string", "5V")],
            symbol="motor",
            model="motor",
            spice_prefix="M",
            width=4.0,
            height=4.0,
            tags=["salida", "actuador"],
        ),
        "DCMOTOR",
    )
    lib.add(
        PartSpec(
            name="L293D",
            category="electromecanicos",
            description="Driver de motor push-pull de 4 canales con diodos de protección",
            pins=[
                _pin("EN", PinDir.IN, width=2, side="left", order=0),
                _pin("IN", PinDir.IN, width=4, side="left", order=1),
                _pin("OUT", PinDir.OUT, width=4, side="right", order=0),
            ],
            symbol="ic",
            model="l293d",
            width=7.0,
            height=8.0,
            tags=["salida", "actuador", "driver"],
        ),
    )


# -- microcontroladores ---------------------------------------------------

_MCU_PINS_328 = [
    _pin("PB", PinDir.INOUT, width=8, side="right", order=0, tri_state=True),
    _pin("PC", PinDir.INOUT, width=7, side="right", order=1, tri_state=True),
    _pin("PD", PinDir.INOUT, width=8, side="right", order=2, tri_state=True),
    _pin("/RESET", PinDir.IN, side="left", order=0, active_low=True),
    _pin("XTAL1", PinDir.IN, side="left", order=1),
    _pin("XTAL2", PinDir.OUT, side="left", order=2),
]


def _mcu(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="ATMEGA328P",
            category="microcontroladores",
            description="AVR 8 bits, 32 KB flash, 2 KB SRAM (el de Arduino Uno)",
            pins=_MCU_PINS_328,
            props=[
                _P("PROGRAM", "Archivo de firmware (.hex/.bin)", "string", ""),
                _P("CLOCK", "Frecuencia de reloj (Hz)", "float", 16_000_000.0),
            ],
            symbol="mcu",
            model="avr",
            width=10.0,
            height=14.0,
            package="DIP28",
            datasheet="pdfs/avr/atmega328p.pdf",
            tags=["mcu", "avr", "arduino", "programable"],
        ),
        "ATMEGA328", "ARDUINO", "UNO",
    )
    lib.add(
        PartSpec(
            name="ATMEGA16",
            category="microcontroladores",
            description="AVR 8 bits, 16 KB flash, 4 puertos",
            pins=[
                _pin("PA", PinDir.INOUT, width=8, side="right", order=0),
                _pin("PB", PinDir.INOUT, width=8, side="right", order=1),
                _pin("PC", PinDir.INOUT, width=8, side="right", order=2),
                _pin("PD", PinDir.INOUT, width=8, side="right", order=3),
                _pin("/RESET", PinDir.IN, side="left", order=0, active_low=True),
                _pin("XTAL1", PinDir.IN, side="left", order=1),
                _pin("XTAL2", PinDir.OUT, side="left", order=2),
            ],
            props=[
                _P("PROGRAM", "Archivo de firmware", "string", ""),
                _P("CLOCK", "Frecuencia de reloj (Hz)", "float", 1_000_000.0),
            ],
            symbol="mcu",
            model="avr",
            width=12.0,
            height=14.0,
            package="DIP40",
            datasheet="pdfs/avr/atmega16.pdf",
            tags=["mcu", "avr", "programable"],
        ),
    )
    lib.add(
        PartSpec(
            name="PIC16F877A",
            category="microcontroladores",
            description="PIC 8 bits de Microchip, 40 patillas",
            pins=[
                _pin("PORTA", PinDir.INOUT, width=6, side="right", order=0),
                _pin("PORTB", PinDir.INOUT, width=8, side="right", order=1),
                _pin("PORTC", PinDir.INOUT, width=8, side="right", order=2),
                _pin("PORTD", PinDir.INOUT, width=8, side="right", order=3),
                _pin("PORTE", PinDir.INOUT, width=3, side="right", order=4),
                _pin("/MCLR", PinDir.IN, side="left", order=0, active_low=True),
                _pin("OSC1", PinDir.IN, side="left", order=1),
                _pin("OSC2", PinDir.OUT, side="left", order=2),
            ],
            props=[
                _P("PROGRAM", "Archivo de firmware", "string", ""),
                _P("CLOCK", "Frecuencia de reloj (Hz)", "float", 4_000_000.0),
            ],
            symbol="mcu",
            model="pic",
            width=12.0,
            height=16.0,
            package="DIP40",
            tags=["mcu", "pic", "microchip", "programable"],
        ),
        "PIC16F877", "PIC",
    )
    # Una CPU educativa, la estrella de "arquitectura de hardware".
    lib.add(
        PartSpec(
            name="CPU8",
            category="cpu",
            description="CPU de 8 bits educativa (conjunto de instrucciones configurable)",
            pins=[
                _pin("CLK", PinDir.IN, side="left", order=0),
                _pin("/RESET", PinDir.IN, side="left", order=1, active_low=True),
                _pin("ADDR", PinDir.OUT, width=16, side="right", order=0),
                _pin("DATA", PinDir.INOUT, width=8, side="right", order=1, tri_state=True),
                _pin("/RD", PinDir.OUT, side="right", order=2, active_low=True),
                _pin("/WR", PinDir.OUT, side="right", order=3, active_low=True),
                _pin("/MREQ", PinDir.OUT, side="right", order=4, active_low=True),
                _pin("/IOREQ", PinDir.OUT, side="right", order=5, active_low=True),
                _pin("HALT", PinDir.OUT, side="right", order=6),
                # Strobe de escritura de puerto: un pulso de UN ciclo completo,
                # que es lo que necesita un latch externo para capturar el bus.
                _pin("WSTROBE", PinDir.OUT, side="right", order=7),
            ],
            props=[
                _P("PROGRAM", "Programa (hex)", "string", ""),
                _P("ISA", "Conjunto de instrucciones", "choice", "SAP8",
                   choices=["SAP8", "MINIMAL8", "Z80SUBSET"]),
            ],
            symbol="mcu",
            model="cpu8",
            width=12.0,
            height=14.0,
            tags=["cpu", "arquitectura", "educativo"],
        ),
        "SAP8", "CPU",
    )


# -- instrumentos ---------------------------------------------------------


def _instruments(lib: Library) -> None:
    lib.add(
        PartSpec(
            name="PROBE",
            category="instrumentos",
            description="Sonda de tensión (muestra el nivel en el esquemático)",
            pins=[_pin("IN", PinDir.IN, side="left")],
            symbol="probe",
            model="probe",
            width=2.0,
            height=2.0,
            tags=["instrumento", "medida"],
        ),
        "VPROBE", "SONDA",
    )
    lib.add(
        PartSpec(
            name="LOGICPROBE",
            category="instrumentos",
            description="Punta lógica con LED de estado",
            pins=[_pin("IN", PinDir.IN, side="left")],
            symbol="probe",
            model="probe",
            width=2.0,
            height=2.0,
            tags=["instrumento", "medida", "digital"],
        ),
    )
    lib.add(
        PartSpec(
            name="TERMINAL",
            category="instrumentos",
            description="Terminal/etiqueta de red (para buses y jerarquía)",
            pins=[_pin("P", PinDir.INOUT, side="left")],
            props=[_P("LABEL", "Nombre de la red", "string", "")],
            symbol="terminal",
            width=3.0,
            height=1.0,
            tags=["conector", "etiqueta"],
        ),
        "TERM", "PORT",
    )


# ---------------------------------------------------------------------------

_build(LIBRARY)
for _fn in _PENDING:
    _fn(LIBRARY)
_PENDING.clear()


def all_parts() -> List[PartSpec]:
    return list(LIBRARY.specs())


def search(q: str) -> List[PartSpec]:
    return LIBRARY.search(q)


def part(name: str) -> Optional[PartSpec]:
    return LIBRARY.get(name)
