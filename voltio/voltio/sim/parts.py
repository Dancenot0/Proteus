"""Modelos de simulación de la librería digital.

Organizados por familias. Cada modelo es corto a propósito: la complejidad
interesante (resolución de buses, propagación, colisiones) vive en el motor,
no aquí.

Convención de nombres: el decorador usa el campo ``model`` de la
:class:`~voltio.parts.spec.PartSpec`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from ..core.logic import (
    AND,
    L,
    NAND,
    NOR,
    NOT,
    OR,
    TRIBUF,
    X,
    XOR,
    Z,
    bus_from_int,
    from_int,
    to_int,
)
from .models import Ctx, Model, register_model

# ---------------------------------------------------------------------------
# Compuertas combinacionales
# ---------------------------------------------------------------------------

_GATE_FUNCS = {
    "and": AND, "nand": NAND, "or": OR, "nor": NOR, "xor": XOR,
    "xnor": lambda a, b: NOT(XOR(a, b)),
}


def _make_gate(kind: str, n_inputs: int):
    """Genera la clase para una compuerta de ``n_inputs`` entradas."""
    fn = _GATE_FUNCS[kind]

    class _Gate(Model):
        name = kind + str(n_inputs)

        def evaluate(self, ctx: Ctx) -> None:
            if n_inputs == 1:
                ctx.set("Y", fn(ctx.get("A0"), ctx.get("A0")))
                return
            acc = ctx.get("A0")
            for i in range(1, n_inputs):
                acc = fn(acc, ctx.get(f"A{i}"))
            ctx.set("Y", acc)

    _Gate.__name__ = f"Gate_{kind}_{n_inputs}"
    _Gate.__qualname__ = _Gate.__name__
    _Gate.__doc__ = f"Compuerta {kind.upper()} de {n_inputs} entrada(s)."
    return _Gate


# Registramos los nombres que usan las PartSpec de la librería.
register_model("and")(_make_gate("and", 2))
register_model("nand")(_make_gate("nand", 2))
register_model("or")(_make_gate("or", 2))
register_model("nor")(_make_gate("nor", 2))
register_model("xor")(_make_gate("xor", 2))
register_model("xnor")(_make_gate("xnor", 2))
register_model("and3")(_make_gate("and", 3))
register_model("nand3")(_make_gate("nand", 3))
register_model("or3")(_make_gate("or", 3))
register_model("nor3")(_make_gate("nor", 3))
register_model("and4")(_make_gate("and", 4))
register_model("nand4")(_make_gate("nand", 4))


# Las compuertas de 3 y 4 entradas usan el mismo nombre de modelo que las de 2
# en la librería; las re-registramos con clases que miran cuántos pines A hay.
class _GateAuto(Model):
    """Compuerta cuya aridad se deduce de los pines reales de la parte."""

    fn = staticmethod(AND)

    def setup(self) -> None:
        self._n = len([p for p in self.inputs if p.startswith("A")])

    def evaluate(self, ctx: Ctx) -> None:
        n = self._n or 2
        if n == 1:
            ctx.set("Y", NOT(ctx.get("A0")))
            return
        acc = ctx.get("A0")
        for i in range(1, n):
            acc = self.fn(acc, ctx.get(f"A{i}"))
        ctx.set("Y", acc)


def _auto(kind: str):
    fn = _GATE_FUNCS[kind]

    class _G(_GateAuto):
        pass

    _G.fn = staticmethod(fn)
    _G.__name__ = f"Auto_{kind}"
    _G.__qualname__ = _G.__name__
    return _G


for _k in ("and", "nand", "or", "nor", "xor", "xnor"):
    register_model(_k)(_auto(_k))


@register_model("not")
class NotGate(Model):
    """Inversor."""

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("Y", NOT(ctx.get("A0")))


@register_model("buffer")
class Buffer(Model):
    """Buffer no inversor."""

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("Y", ctx.get("A"))


@register_model("tribuf_ahigh")
class TriBufHigh(Model):
    """Buffer tri-estado con enable activo en alta (74LS126)."""

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("Y", TRIBUF(ctx.get("A"), ctx.get("OE")))


@register_model("tribuf_alow")
class TriBufLow(Model):
    """Buffer tri-estado con enable activo en baja (74LS125)."""

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("Y", TRIBUF(ctx.get("A"), NOT(ctx.get("/OE"))))


# ---------------------------------------------------------------------------
# Fuentes y entradas
# ---------------------------------------------------------------------------


@register_model("rail")
class Rail(Model):
    """Riel de alimentación positiva. Emite 1 siempre."""

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("P", L.HIGH)


@register_model("ground")
class Ground(Model):
    """Tierra. Emite 0 siempre."""

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("P", L.LOW)


@register_model("logicstate")
class LogicState(Model):
    """Nivel lógico conmutable por el usuario (el cuadrito 0/1 de Proteus)."""

    def setup(self) -> None:
        self.state["v"] = L.HIGH if str(self.prop("STATE", "0")) in ("1", "HIGH") else L.LOW

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("Q", self.state["v"])

    def interactive(self) -> bool:
        return True

    def set_interactive(self, value: Any) -> None:
        self.state["v"] = L.HIGH if to_int(value) else L.LOW


@register_model("clock")
class ClockGen(Model):
    """Generador de reloj. El motor lo trata aparte (agenda sus flancos)."""

    def setup(self) -> None:
        self.freq = float(self.num_prop("FREQ", 1.0)) or 1.0
        self.duty = min(max(float(self.num_prop("DUTY", 50.0)), 1.0), 99.0) / 100.0
        self.state["v"] = L.HIGH if str(self.prop("PHASE", "0")) == "1" else L.LOW

    @property
    def period(self) -> float:
        return 1.0 / self.freq

    def next_edges(self, t: float) -> List[float]:
        """Próximos instantes de conmutación a partir de ``t``."""
        p = self.period
        hi = p * self.duty
        lo = p - hi
        phase = t % p
        if self.state["v"] is L.LOW:
            return [t + (hi - phase if phase < hi else lo - (phase - hi))]
        return [t + (lo - phase if phase >= hi else hi - phase)]

    def evaluate(self, ctx: Ctx) -> None:
        ctx.set("Q", self.state["v"])

    def set_interactive(self, value: Any) -> None:
        self.state["v"] = L.HIGH if to_int(value) else L.LOW


@register_model("switch")
class Switch(Model):
    """Interruptor / pulsador. Cierra (deja pasar el nivel de 2) según STATE."""

    def setup(self) -> None:
        self.state["closed"] = str(self.prop("STATE", "0")) == "1"
        self.state["level"] = L.LOW  # nivel que entrega cuando está cerrado

    def evaluate(self, ctx: Ctx) -> None:
        # Pin 1 = salida que se puede atar a VCC/GND por el otro extremo del net.
        # Modelamos el interruptor como un buffer tri-estado: cerrado → pasa el
        # nivel del net "2"; abierto → suelta.
        ctx.set("1", ctx.get("2") if self.state["closed"] else L.Z)

    def interactive(self) -> bool:
        return True

    def set_interactive(self, value: Any) -> None:
        self.state["closed"] = bool(to_int(value))


# ---------------------------------------------------------------------------
# Secuenciales
# ---------------------------------------------------------------------------


@register_model("dff")
class DFlipFlop(Model):
    """Flip-flop D, flanco de subida, sin asíncronos. Arranca en X."""

    edge_sensitive = True

    def setup(self) -> None:
        self.state["q"] = L.X

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.rose("CLK"):
            self.state["q"] = ctx.get("D")
        q = self.state["q"]
        ctx.set("Q", q)
        ctx.set("/Q", NOT(q))


@register_model("dff_async")
class DFlipFlopAsync(Model):
    """Flip-flop D con /CLR y /PRE asíncronos (74LS74). /CLR gana a /PRE."""

    edge_sensitive = True

    def setup(self) -> None:
        self.state["q"] = L.X

    def evaluate(self, ctx: Ctx) -> None:
        clr = ctx.get("/CLR")
        pre = ctx.get("/PRE")
        if clr is L.LOW:
            self.state["q"] = L.LOW
        elif pre is L.LOW:
            self.state["q"] = L.HIGH
        elif clr is L.X or pre is L.X:
            self.state["q"] = L.X
        elif ctx.rose("CLK"):
            self.state["q"] = ctx.get("D")
        q = self.state["q"]
        ctx.set("Q", q)
        ctx.set("/Q", NOT(q))


@register_model("jkff_async")
class JKFlipFlop(Model):
    """Flip-flop JK con /CLR y /PRE, flanco de bajada (74LS76)."""

    edge_sensitive = True

    def setup(self) -> None:
        self.state["q"] = L.X

    def evaluate(self, ctx: Ctx) -> None:
        clr, pre = ctx.get("/CLR"), ctx.get("/PRE")
        if clr is L.LOW:
            self.state["q"] = L.LOW
        elif pre is L.LOW:
            self.state["q"] = L.HIGH
        elif ctx.fell("CLK"):
            j, k = ctx.get("J"), ctx.get("K")
            q = self.state["q"]
            if not j.determinate or not k.determinate:
                self.state["q"] = L.X
            elif j is L.HIGH and k is L.HIGH:
                self.state["q"] = NOT(q)
            elif j is L.HIGH:
                self.state["q"] = L.HIGH
            elif k is L.HIGH:
                self.state["q"] = L.LOW
        q = self.state["q"]
        ctx.set("Q", q)
        ctx.set("/Q", NOT(q))


@register_model("cnt193")
class Counter193(Model):
    """Contador síncrono 4 bits arriba/abajo con carga paralela (74LS193).

    ``CPU`` cuenta arriba por flanco de subida cuando ``CPD`` está alto;
    ``CPD`` cuenta abajo por flanco de subida cuando ``CPU`` está alto.
    """

    edge_sensitive = True

    def setup(self) -> None:
        self.state["n"] = 0
        self.state["init"] = False

    def evaluate(self, ctx: Ctx) -> None:
        if not self.state.get("init"):
            self.state["n"] = 0
            self.state["init"] = True

        mr = ctx.get("/MR")
        # Los tres casos son mutuamente excluyentes y van en este orden de
        # prioridad: reset asíncrono > carga paralela > contar. Escribirlos
        # como `if` encadenados en vez de `elif` es un bug sutil: el reset
        # ponía n=0 y acto seguido el flanco de reloj lo incrementaba a 1.
        if mr is L.X:
            self._emit(ctx, None)
        elif mr is L.LOW:
            self.state["n"] = 0
            self._emit(ctx, 0)
        elif ctx.get("/PL") is L.LOW:
            v = ctx.bus_int("P", 0, 3)
            self.state["n"] = 0 if v is None else v & 0xF
            self._emit(ctx, self.state["n"])
        else:
            up = ctx.rose("CPU") and ctx.get("CPD") is L.HIGH
            down = ctx.rose("CPD") and ctx.get("CPU") is L.HIGH
            if up:
                self.state["n"] = (self.state["n"] + 1) & 0xF
            elif down:
                self.state["n"] = (self.state["n"] - 1) & 0xF
            self._emit(ctx, self.state["n"])

    def _emit(self, ctx: Ctx, n: Optional[int]) -> None:
        if n is None:
            ctx.release("Q")
            ctx.set("/TCU", L.X)
            ctx.set("/TCD", L.X)
            return
        ctx.set_bus("Q", bus_from_int(n, 4))
        # Terminal count: pulso bajo al llegar al límite en esa dirección.
        ctx.set("/TCU", L.LOW if (n == 0xF and ctx.get("CPD") is L.HIGH) else L.HIGH)
        ctx.set("/TCD", L.LOW if (n == 0x0 and ctx.get("CPU") is L.HIGH) else L.HIGH)

    def on_probe(self, ctx: Ctx) -> str:
        return str(self.state.get("n", 0))


@register_model("cnt161")
class Counter161(Model):
    """Contador síncrono 4 bits, carga paralela, reset síncrono (74LS161)."""

    edge_sensitive = True

    def setup(self) -> None:
        self.state["n"] = 0

    def evaluate(self, ctx: Ctx) -> None:
        mr = ctx.get("/MR")
        if not mr.determinate:
            self._emit(ctx, None)
            return
        if not ctx.rose("CP"):
            # Sin flanco no hay nada que hacer; el 161 resetea en sincronía.
            self._emit(ctx, self.state["n"])
            return
        if mr is L.LOW:
            self.state["n"] = 0
        elif ctx.get("/PE") is L.LOW:
            v = ctx.bus_int("P", 0, 3)
            self.state["n"] = 0 if v is None else v & 0xF
        elif ctx.get("CEP") is L.HIGH and ctx.get("CET") is L.HIGH:
            self.state["n"] = (self.state["n"] + 1) & 0xF
        self._emit(ctx, self.state["n"])

    def _emit(self, ctx: Ctx, n: Optional[int]) -> None:
        if n is None:
            ctx.release("Q")
            ctx.set("TC", L.X)
            return
        ctx.set_bus("Q", bus_from_int(n, 4))
        ctx.set("TC", L.HIGH if (n == 0xF and ctx.get("CET") is L.HIGH) else L.LOW)


@register_model("reg273")
class Register273(Model):
    """Registro octal de flip-flops D con reset asíncrono (74LS273)."""

    edge_sensitive = True

    def setup(self) -> None:
        self.state["q"] = 0

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/MR") is L.LOW:
            self.state["q"] = 0
        elif ctx.rose("CP"):
            v = ctx.bus_int("D", 0, 7)
            if v is not None:
                self.state["q"] = v & 0xFF
            else:
                self.state["q"] = None  # datos indeterminados
        q = self.state["q"]
        if q is None:
            ctx.release("Q")
        else:
            ctx.set_bus("Q", bus_from_int(q, 8))

    def on_probe(self, ctx: Ctx) -> str:
        q = self.state.get("q")
        return "XX" if q is None else f"{q:02X}"


@register_model("sr595")
class ShiftRegister595(Model):
    """Registro de desplazamiento con latch de salida (74LS595)."""

    edge_sensitive = True

    def setup(self) -> None:
        self.state["shift"] = 0
        self.state["out"] = 0

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/MR") is L.LOW:
            self.state["shift"] = 0
            self.state["out"] = 0
        else:
            if ctx.rose("SHCP"):
                self.state["shift"] = ((self.state["shift"] << 1) | ctx.get("DS").as_bit) & 0xFF
            if ctx.rose("STCP"):
                self.state["out"] = self.state["shift"]
        if ctx.get("/OE") is L.HIGH:
            ctx.release("Q")
        else:
            ctx.set_bus("Q", bus_from_int(self.state["out"], 8))
        ctx.set("Q7S", from_int((self.state["shift"] >> 7) & 1))


@register_model("sr164")
class ShiftRegister164(Model):
    """Registro de desplazamiento de entrada serie doble (74LS164)."""

    edge_sensitive = True

    def setup(self) -> None:
        self.state["q"] = 0

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/MR") is L.LOW:
            self.state["q"] = 0
        elif ctx.rose("CP"):
            ds = AND(ctx.get("DSA"), ctx.get("DSB"))
            self.state["q"] = ((self.state["q"] << 1) | ds.as_bit) & 0xFF
        ctx.set_bus("Q", bus_from_int(self.state["q"], 8))


# ---------------------------------------------------------------------------
# Combinacionales MSI
# ---------------------------------------------------------------------------


@register_model("dec138")
class Decoder138(Model):
    """Decodificador 3→8 con salidas activas en baja (74LS138)."""

    def evaluate(self, ctx: Ctx) -> None:
        enabled = (
            ctx.get("/E1") is L.LOW
            and ctx.get("/E2") is L.LOW
            and ctx.get("E3") is L.HIGH
        )
        if not enabled:
            ctx.set_bus("/Y", [L.HIGH] * 8)
            return
        sel = ctx.bus_int("A", 0, 2)
        if sel is None:
            ctx.set_bus("/Y", [L.X] * 8)
            return
        ctx.set_bus("/Y", [L.LOW if i == sel else L.HIGH for i in range(8)])


_SEVEN = {
    #     a     b     c     d     e     f     g
    0:  (1, 1, 1, 1, 1, 1, 0),
    1:  (0, 1, 1, 0, 0, 0, 0),
    2:  (1, 1, 0, 1, 1, 0, 1),
    3:  (1, 1, 1, 1, 0, 0, 1),
    4:  (0, 1, 1, 0, 0, 1, 1),
    5:  (1, 0, 1, 1, 0, 1, 1),
    6:  (1, 0, 1, 1, 1, 1, 1),
    7:  (1, 1, 1, 0, 0, 0, 0),
    8:  (1, 1, 1, 1, 1, 1, 1),
    9:  (1, 1, 1, 1, 0, 1, 1),
    10: (0, 0, 1, 1, 1, 0, 1),  # b minuscula estilizada
    11: (0, 0, 1, 1, 1, 1, 1),
    12: (0, 1, 0, 1, 1, 0, 1),
    13: (0, 0, 1, 1, 0, 1, 1),
    14: (0, 0, 0, 1, 1, 0, 1),
    15: (0, 0, 0, 1, 1, 1, 0),
}


@register_model("dec47")
class Decoder47(Model):
    """BCD → 7 segmentos, salidas activas en baja (74LS47)."""

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/BI") is L.LOW:  # blanking forzado
            ctx.set_bus("/a", [L.HIGH] * 7)
            return
        if ctx.get("/LT") is L.LOW:  # prueba de lámpara: todo encendido
            ctx.set_bus("/a", [L.LOW] * 7)
            return
        bcd = ctx.bus_int("A", 0, 3)
        if bcd is None:
            ctx.set_bus("/a", [L.X] * 7)
            return
        seg = _SEVEN[bcd & 0xF]
        ctx.set_bus("/a", [L.LOW if s else L.HIGH for s in seg])


@register_model("dec48")
class Decoder48(Model):
    """BCD → 7 segmentos, salidas activas en alta (74LS48)."""

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/BI") is L.LOW:
            ctx.set_bus("a", [L.LOW] * 7)
            return
        if ctx.get("/LT") is L.LOW:
            ctx.set_bus("a", [L.HIGH] * 7)
            return
        bcd = ctx.bus_int("A", 0, 3)
        if bcd is None:
            ctx.set_bus("a", [L.X] * 7)
            return
        ctx.set_bus("a", [from_int(s) for s in _SEVEN[bcd & 0xF]])


@register_model("mux151")
class Mux151(Model):
    """Multiplexor 8→1 (74LS151)."""

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/E") is L.HIGH:
            ctx.set("Y", L.LOW)
            ctx.set("/W", L.HIGH)
            return
        sel = ctx.bus_int("S", 0, 2)
        if sel is None:
            ctx.set("Y", L.X)
            ctx.set("/W", L.X)
            return
        d = ctx.get("D", sel)
        ctx.set("Y", d)
        ctx.set("/W", NOT(d))


@register_model("mux157")
class Mux157(Model):
    """Cuádruple multiplexor 2→1 (74LS157)."""

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/E") is L.HIGH:
            ctx.release("Y")
            return
        s = ctx.get("S")
        src = "A" if s is L.LOW else ("B" if s is L.HIGH else None)
        if src is None:
            ctx.set_bus("Y", [L.X] * 4)
            return
        ctx.set_bus("Y", [ctx.get(src, i) for i in range(4)])


@register_model("buf244")
class Buffer244(Model):
    """Buffer octal tri-estado dividido en dos mitades (74LS244)."""

    def evaluate(self, ctx: Ctx) -> None:
        for half, en in ((1, "/1E"), (2, "/2E")):
            src, dst = f"{half}A", f"{half}Y"
            if ctx.get(en) is L.LOW:
                ctx.set_bus(dst, [ctx.get(src, i) for i in range(4)])
            else:
                ctx.release(dst)


@register_model("bus245")
class BusTransceiver245(Model):
    """Transceptor de bus octal bidireccional (74LS245).

    ``/E`` bajo habilita; ``DIR`` alto manda A→B, bajo manda B→A.
    El lado que no drivea queda en alta impedancia.
    """

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/E") is not L.LOW:
            ctx.release("A")
            ctx.release("B")
            return
        dirn = ctx.get("DIR")
        if dirn is L.HIGH:  # A → B
            ctx.release("A")
            ctx.set_bus("B", [ctx.get("A", i) for i in range(8)])
        elif dirn is L.LOW:  # B → A
            ctx.release("B")
            ctx.set_bus("A", [ctx.get("B", i) for i in range(8)])
        else:
            ctx.release("A")
            ctx.release("B")


@register_model("add283")
class Adder283(Model):
    """Sumador completo de 4 bits con acarreo (74LS283)."""

    def evaluate(self, ctx: Ctx) -> None:
        a, b = ctx.bus_int("A", 0, 3), ctx.bus_int("B", 0, 3)
        cin = ctx.get("C0")
        if a is None or b is None or not cin.determinate:
            ctx.release("S")
            ctx.set("C4", L.X)
            return
        total = a + b + to_int(cin)
        ctx.set_bus("S", bus_from_int(total & 0xF, 4))
        ctx.set("C4", from_int((total >> 4) & 1))


@register_model("cmp85")
class Comparator85(Model):
    """Comparador de magnitud de 4 bits con cascada (74LS85)."""

    def evaluate(self, ctx: Ctx) -> None:
        a, b = ctx.bus_int("A", 0, 3), ctx.bus_int("B", 0, 3)
        if a is None or b is None:
            ctx.set("QA", L.X); ctx.set("QB", L.X); ctx.set("QE", L.X)
            return
        if a > b:
            ctx.set("QA", L.HIGH); ctx.set("QB", L.LOW); ctx.set("QE", L.LOW)
        elif a < b:
            ctx.set("QA", L.LOW); ctx.set("QB", L.HIGH); ctx.set("QE", L.LOW)
        else:
            # Empate: manda la cascada.
            ia, ib, ie = ctx.get("IA"), ctx.get("IB"), ctx.get("IE")
            ctx.set("QA", ia); ctx.set("QB", ib); ctx.set("QE", ie)


# Tabla de funciones de la 74181 (M=0 → aritmética, M=1 → lógica).
_ALU181_ARITH = {
    0x0: lambda a, b, c: a + c,
    0x1: lambda a, b, c: (a | b) + c,
    0x2: lambda a, b, c: (a | (~b & 0xF)) + c,
    0x3: lambda a, b, c: 0xF + c,
    0x4: lambda a, b, c: a + (~b & 0xF) + c,
    0x5: lambda a, b, c: (a | b) + (~b & 0xF) + c,
    0x6: lambda a, b, c: a + (~b & 0xF) + (a & (~b & 0xF)) + c,
    0x7: lambda a, b, c: (a | (~b & 0xF)) + c,
    0x8: lambda a, b, c: (a & b) + c,
    0x9: lambda a, b, c: a + c,
    0xA: lambda a, b, c: (a | b) + (a & b) + c,
    0xB: lambda a, b, c: (a | b) + c,
    0xC: lambda a, b, c: (a | (~b & 0xF)) + (a & b) + c,
    0xD: lambda a, b, c: (a | b) + (a & b) + c,
    0xE: lambda a, b, c: a + (a & b) + c,
    0xF: lambda a, b, c: a + c,
}
_ALU181_LOGIC = {
    0x0: lambda a, b: ~a & 0xF,
    0x1: lambda a, b: ~(a | b) & 0xF,
    0x2: lambda a, b: (~a & 0xF) & b,
    0x3: lambda a, b: 0,
    0x4: lambda a, b: ~(a & b) & 0xF,
    0x5: lambda a, b: ~b & 0xF,
    0x6: lambda a, b: a ^ b,
    0x7: lambda a, b: a & (~b & 0xF),
    0x8: lambda a, b: (~a & 0xF) | b,
    0x9: lambda a, b: ~(a ^ b) & 0xF,
    0xA: lambda a, b: b,
    0xB: lambda a, b: a & b,
    0xC: lambda a, b: 1,
    0xD: lambda a, b: a | b,
    0xE: lambda a, b: (~a & 0xF) | b,
    0xF: lambda a, b: a,
}


@register_model("alu181")
class ALU74181(Model):
    """ALU de 4 bits (74LS181). 16 funciones aritméticas y 16 lógicas."""

    def evaluate(self, ctx: Ctx) -> None:
        a = ctx.bus_int("A", 0, 3)
        b = ctx.bus_int("B", 0, 3)
        s = ctx.bus_int("S", 0, 3)
        m = ctx.get("M")
        cn = ctx.get("/CN")  # carry-in activo en baja
        if a is None or b is None or s is None or not m.determinate or not cn.determinate:
            ctx.release("F"); ctx.set("/CN4", L.X); ctx.set("AeqB", L.X)
            return
        carry_in = 1 if cn is L.LOW else 0
        if m is L.LOW:  # modo aritmético
            total = _ALU181_ARITH[s](a, b, carry_in)
            f = total & 0xF
            ctx.set("/CN4", L.LOW if (total >> 4) & 1 else L.HIGH)
        else:  # modo lógico
            f = _ALU181_LOGIC[s](a, b) & 0xF
            ctx.set("/CN4", L.LOW if not (f == 0xF) else L.HIGH)
        ctx.set_bus("F", bus_from_int(f, 4))
        ctx.set("AeqB", L.HIGH if (a == b and f == 0xF) else L.LOW)


# ---------------------------------------------------------------------------
# Memoria
# ---------------------------------------------------------------------------


def _parse_int(tok: str) -> Optional[int]:
    """Convierte un token en entero tolerando todas las formas habituales.

    Ojo con ``int(tok, 0)``: en base 0 un ``0`` inicial significa *octal*, así
    que ``int("01", 0)`` y ``int("08", 0)`` lanzan ``ValueError``. Eso rompía
    en silencio cualquier volcado de bytes con ceros a la izquierda (``"01 42
    08 00 09"``), que es exactamente como se escribe un programa. Se intenta
    base 0 solo si hay prefijo explícito, y si no, decimal y luego hexadecimal.
    """
    t = tok.strip().replace("_", "")
    if not t:
        return None
    bajo = t.lower()
    # Prefijo explícito: se respeta tal cual.
    if bajo.startswith(("0x", "0b", "0o")):
        try:
            return int(t, 0)
        except ValueError:
            return None
    # Sin prefijo se interpreta en HEXADECIMAL. Es la convención universal de
    # los volcados de firmware (Intel HEX, ``objdump``, cualquier monitor):
    # ``"01 42 08 FF"`` son bytes, no números decimales. Intentar decimal
    # primero haría que ``42`` valiera 42 en vez de 0x42, y que ``09`` diera
    # error por octal inválido. Los dos fallos a la vez.
    try:
        return int(t, 16)
    except ValueError:
        return None


def _parse_init(text: str) -> Dict[int, int]:
    """Parsea el contenido inicial de una memoria o el programa de una CPU.

    Formatos aceptados:

    ``"01 42 08 00 09"``
        Volcado plano de bytes, empezando en la dirección 0.
    ``"0:0xAA, 5:0x12"``
        Pares dirección:valor, para inicializar solo unas pocas posiciones.
        Las direcciones también van en hexadecimal.
    ``"AA,FF,03"``
        Lista separada por comas, desde la dirección 0.
    ``"@0010 AA FF"``
        Como el volcado plano pero fijando la dirección de inicio.

    Los tokens que no se pueden interpretar se **ignoran sin desplazar las
    direcciones**: si se colara un token inválido en medio de un firmware, lo
    último que quieres es que todo lo que viene detrás quede desalineado.
    """
    out: Dict[int, int] = {}
    if not text:
        return out
    t = str(text).strip()
    if not t:
        return out

    # Forma clave:valor — se reconoce por el patrón, no por la mera presencia
    # de dos puntos (que aparecen en rutas de Windows y en horas).
    if re.search(r"(?<![\w:])\d+\s*:\s*[0-9A-Fa-fxX]+", t) and "," in t:
        for chunk in t.split(","):
            if ":" not in chunk:
                continue
            k, v = chunk.split(":", 1)
            kk, vv = _parse_int(k), _parse_int(v)
            if kk is not None and vv is not None:
                out[kk] = vv & 0xFF
        return out

    addr = 0
    for tok in t.replace(",", " ").split():
        if tok.startswith("@"):
            a = _parse_int(tok[1:])
            if a is not None:
                addr = a
            continue
        v = _parse_int(tok)
        if v is None:
            continue  # se ignora, pero NO se salta la dirección
        out[addr] = v & 0xFF
        addr += 1
    return out


class _MemoryBase(Model):
    """Comportamiento común de ROM/RAM con bus tri-estado."""

    addr_pin = "A"
    data_pin = "D"

    def setup(self) -> None:
        self.mem: Dict[int, int] = {}
        self.size = 1 << self.width_of(self.addr_pin)
        self.mem.update(_parse_init(self.prop("INIT", "")))
        path = self.prop("FILE", "")
        if path:
            self.mem.update(_load_hex_file(str(path)))

    def _addr(self, ctx: Ctx) -> Optional[int]:
        a = ctx.bus_int(self.addr_pin, 0, self.width_of(self.addr_pin) - 1)
        return None if a is None else a % self.size


def _load_hex_file(path: str) -> Dict[int, int]:
    """Carga Intel HEX o un volcado plano de bytes."""
    out: Dict[int, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    if text.lstrip().startswith(":"):  # Intel HEX
        base = 0
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(":"):
                continue
            try:
                raw = bytes.fromhex(line[1:])
            except ValueError:
                continue
            if len(raw) < 5:
                continue
            count, addr, rectype = raw[0], (raw[1] << 8) | raw[2], raw[3]
            data = raw[4:4 + count]
            if rectype == 0x00:
                for i, b in enumerate(data):
                    out[base + addr + i] = b
            elif rectype == 0x04 and count == 2:
                base = ((data[0] << 8) | data[1]) << 16
            elif rectype == 0x02 and count == 2:
                base = ((data[0] << 8) | data[1]) << 4
            elif rectype == 0x01:
                break
        return out
    addr = 0
    for tok in text.split():
        try:
            out[addr] = int(tok, 0) & 0xFF
        except ValueError:
            continue
        addr += 1
    return out


@register_model("rom")
class ROM(_MemoryBase):
    """EPROM: solo lectura, salida tri-estado por /E y /G."""

    def evaluate(self, ctx: Ctx) -> None:
        if ctx.get("/E") is not L.LOW or ctx.get("/G") is not L.LOW:
            ctx.release("D")
            return
        a = self._addr(ctx)
        if a is None:
            ctx.release("D")
            return
        ctx.set_bus("D", bus_from_int(self.mem.get(a, 0xFF), 8))

    def on_probe(self, ctx: Ctx) -> str:
        a = self._addr(ctx)
        return "" if a is None else f"[{a:04X}]={self.mem.get(a, 0xFF):02X}"


@register_model("ram")
class RAM6264(_MemoryBase):
    """RAM estática: escribe con /W bajo, lee con /E bajo y /W alto."""

    def evaluate(self, ctx: Ctx) -> None:
        w = ctx.get("/W")
        e = ctx.get("/E")
        a = self._addr(ctx)
        if e is L.LOW and w is L.LOW and a is not None:
            v = ctx.bus_int("D", 0, 7)
            if v is not None:
                self.mem[a] = v
            ctx.release("D")
            return
        if e is L.LOW and w is L.HIGH and a is not None:
            ctx.set_bus("D", bus_from_int(self.mem.get(a, 0x00), 8))
            return
        ctx.release("D")

    def on_probe(self, ctx: Ctx) -> str:
        a = self._addr(ctx)
        return "" if a is None else f"[{a:04X}]={self.mem.get(a, 0):02X}"


@register_model("ram_generic")
class RAMGeneric(_MemoryBase):
    """RAM de tamaño configurable, con /WE y /OE activos en baja."""

    def evaluate(self, ctx: Ctx) -> None:
        a = self._addr(ctx)
        we, oe = ctx.get("/WE"), ctx.get("/OE")
        if we is L.LOW and a is not None:
            v = ctx.bus_int("D", 0, self.width_of("D") - 1)
            if v is not None:
                self.mem[a] = v
            ctx.release("D")
            return
        if oe is L.LOW and a is not None:
            w = self.width_of("D")
            ctx.set_bus("D", bus_from_int(self.mem.get(a, 0), w))
            return
        ctx.release("D")


# ---------------------------------------------------------------------------
# Salidas e indicadores
# ---------------------------------------------------------------------------


@register_model("led")
class Led(Model):
    """LED digital: enciende cuando el ánodo está alto y el cátodo bajo/Z."""

    def evaluate(self, ctx: Ctx) -> None:
        a, k = ctx.get("A"), ctx.get("K")
        self.state["on"] = a is L.HIGH and k is not L.HIGH

    def on_probe(self, ctx: Ctx) -> str:
        return "ON" if self.state.get("on") else ""

    def lit(self) -> bool:
        return bool(self.state.get("on"))


@register_model("resistor")
class Resistor(Model):
    """Resistencia como elemento bidireccional transparente.

    En simulación digital una resistencia es poco menos que un trozo de cable,
    pero **no es un buffer direccional**. Modelarla como "pasa el nivel del
    pin 2 al pin 1" hace que un lado flotante inyecte ``X`` en el otro y
    contamine todo el circuito: es el fallo clásico al poner un LED con su
    resistencia en serie.

    Tampoco vale "si ambos extremos están definidos, me suelto". Eso no es
    monótono: el cable pasaría de ``X`` a ``1`` y de vuelta a ``X`` en la
    pasada siguiente, y el motor reportaría un lazo combinatorio que no existe.

    La regla correcta y convergente es propagar siempre el nivel definido a
    ambos lados. Si los dos extremos tienen niveles opuestos, la resistencia
    drivea cada uno con el del otro y el cable cae en ``X`` por colisión, que
    es precisamente lo que pasa en la realidad cuando cortocircuitas VCC a
    tierra a través de un resistor.
    """

    def evaluate(self, ctx: Ctx) -> None:
        v1 = ctx.get("1")
        v2 = ctx.get("2")
        if v1.determinate and v2.determinate:
            # Ambos definidos: cruzamos los niveles. Si coinciden, no cambia
            # nada (idempotente); si se oponen, se detecta como colisión.
            ctx.set("1", v2)
            ctx.set("2", v1)
        elif v1.determinate:
            ctx.set("2", v1)
            ctx.release("1")
        elif v2.determinate:
            ctx.set("1", v2)
            ctx.release("2")
        else:
            ctx.release("1")
            ctx.release("2")


@register_model("buzzer")
class Buzzer(Model):
    """Zumbador: suena cuando hay nivel alto entre P y N."""

    def evaluate(self, ctx: Ctx) -> None:
        self.state["on"] = ctx.get("P") is L.HIGH and ctx.get("N") is not L.HIGH

    def on_probe(self, ctx: Ctx) -> str:
        return "♪" if self.state.get("on") else ""


@register_model("motor")
class Motor(Model):
    """Motor DC: gira cuando hay tensión entre + y -."""

    def evaluate(self, ctx: Ctx) -> None:
        self.state["on"] = ctx.get("+") is L.HIGH and ctx.get("-") is not L.HIGH

    def on_probe(self, ctx: Ctx) -> str:
        return "GIRA" if self.state.get("on") else ""


@register_model("seven_seg")
class SevenSegment(Model):
    """Display de 7 segmentos. Solo indica; no drivea nada.

    **Polaridad autodetectada.** Un display de cátodo común enciende un
    segmento con nivel ALTO; uno de ánodo común, con nivel BAJO. En vez de
    exigir que el usuario lo declare bien (y es un error facilísimo de
    cometer: conectar un 74LS47 —activo bajo— a un display de cátodo común),
    el modelo mira a qué nivel está el pin común:

    * común a tierra  → cátodo común → segmentos activos en alta
    * común a VCC     → ánodo común  → segmentos activos en baja
    * común flotante  → se usa la propiedad ``COMMON``
    * común con más de un driver en niveles opuestos → todo ``X``

    Así el circuito "se da cuenta" solo de cómo está cableado.
    """

    COMMON_PINS = ("CC", "CA", "COM", "COMMON")

    def setup(self) -> None:
        self.state["segs"] = [L.X] * 7
        self.state["lit"] = [False] * 7
        self.state["digit"] = "?"
        self.state["dp"] = L.X
        self.state["active_low"] = str(self.prop("COMMON", "CATODO")).upper().startswith("ANOD")
        self.state["polarity"] = "propiedad"
        self.state["common"] = L.X

    def _common_level(self, ctx: Ctx) -> L:
        for name in self.COMMON_PINS:
            if self.spec.pin(name) is not None:
                return ctx.get(name)
        return L.X

    def evaluate(self, ctx: Ctx) -> None:
        common = self._common_level(ctx)
        self.state["common"] = common
        if common is L.HIGH:
            active_low, why = True, "ánodo común (pin común en 1)"
        elif common is L.LOW:
            active_low, why = False, "cátodo común (pin común en 0)"
        else:
            active_low = self.state["active_low"]
            why = "propiedad COMMON" if common is L.X else "común indeterminado"
        self.state["polarity"] = why

        raw = [ctx.get("a", i) for i in range(7)]
        lit: List[bool] = []
        for v in raw:
            if not v.determinate:
                lit.append(False)
            else:
                lit.append((NOT(v) if active_low else v) is L.HIGH)
        self.state["segs"] = raw
        self.state["lit"] = lit
        self.state["dp"] = ctx.get("dp") if "dp" in self._widths else L.X

        if not common.determinate:
            self.state["digit"] = "?" if any(not v.determinate for v in raw) else " "
        else:
            self.state["digit"] = _decode_segments(lit)

    def on_probe(self, ctx: Ctx) -> str:
        return self.state.get("digit", "?")

    def segments_lit(self) -> List[bool]:
        """Segmentos encendidos, en el orden estándar a, b, c, d, e, f, g."""
        return list(self.state.get("lit", [False] * 7))


@register_model("seven_seg_ca")
class SevenSegmentCA(SevenSegment):
    """Idéntico, pero con la propiedad por defecto en ánodo común."""

    def setup(self) -> None:
        super().setup()
        self.state["active_low"] = True


def _decode_segments(lit: Sequence[bool]) -> str:
    """Identifica el dígito a partir de los segmentos encendidos."""
    key = tuple(1 if x else 0 for x in lit)
    for val, pat in _SEVEN.items():
        if tuple(pat) == key:
            return format(val, "X")
    if not any(lit):
        return " "
    return "?"


@register_model("led_matrix")
class LedMatrix(Model):
    """Matriz de LEDs 8×8 (filas activas en alta, columnas en baja)."""

    def evaluate(self, ctx: Ctx) -> None:
        rows = [ctx.get("ROW", i) for i in range(8)]
        cols = [ctx.get("COL", i) for i in range(8)]
        self.state["grid"] = [
            [rows[r] is L.HIGH and cols[c] is L.LOW for c in range(8)]
            for r in range(8)
        ]


def _decode_segments(lit: Sequence[bool]) -> str:
    """Identifica el dígito mostrado a partir de los segmentos encendidos."""
    key = tuple(1 if x else 0 for x in lit)
    for val, pat in _SEVEN.items():
        if tuple(pat) == key:
            return format(val, "X")
    if not any(lit):
        return " "
    return "?"


@register_model("buzzer")
class Buzzer(Model):
    """Zumbador: suena cuando hay nivel alto en P."""

    def evaluate(self, ctx: Ctx) -> None:
        self.state["on"] = ctx.get("P") is L.HIGH and ctx.get("N") is not L.HIGH


@register_model("motor")
class Motor(Model):
    """Motor DC: gira cuando hay tensión entre + y -."""

    def evaluate(self, ctx: Ctx) -> None:
        self.state["on"] = ctx.get("+") is L.HIGH and ctx.get("-") is not L.HIGH


@register_model("l293d")
class L293D(Model):
    """Driver cuádruple half-bridge. Un enable gobierna dos canales."""

    def evaluate(self, ctx: Ctx) -> None:
        for ch in range(4):
            en = ctx.get("EN", 0 if ch < 2 else 1)
            inp = ctx.get("IN", ch)
            ctx.set("OUT", inp if en is L.HIGH else L.LOW, ch)


@register_model("lcd1602")
class LCD1602(Model):
    """LCD alfanumérico HD44780 en modo 8 bits.

    Implementa el subconjunto que importa: escribir datos/órdenes, cursor,
    limpieza y desplazamiento. Guarda el texto en ``state["screen"]`` para que
    la GUI y las trazas lo muestren.
    """

    edge_sensitive = True

    def __init__(self, ref, spec, props=None):
        super().__init__(ref, spec, props)

    def setup(self) -> None:
        self.cols = int(self.num_prop("COLS", 16))
        self.rows = int(self.num_prop("ROWS", 2))
        self.state["screen"] = [[" "] * self.cols for _ in range(self.rows)]
        self.state["ddram"] = 0
        self.state["row"] = 0
        self.state["col"] = 0
        self.state["busy"] = L.HIGH
        self.state["display_on"] = False
        self.state["cursor_on"] = False
        self.state["increment"] = True
        self.state["shift"] = False
        self.state["init_count"] = 0

    def evaluate(self, ctx: Ctx) -> None:
        rw = ctx.get("RW")
        rs = ctx.get("RS")
        # Lectura: solo el bit de busy (el resto lo soltamos).
        if rw is L.HIGH:
            ctx.release("D")
            ctx.set("D", self.state["busy"], 7)
            return
        ctx.release("D")
        if not ctx.rose("E"):
            return
        data = ctx.bus_int("D", 0, 7)
        if data is None:
            return
        if rs is L.LOW:
            self._command(data)
        else:
            self._write_char(data)
        self.state["busy"] = L.LOW  # modelo ideal: nunca está ocupado

    # -- protocolo HD44780 ------------------------------------------------
    def _command(self, cmd: int) -> None:
        if cmd & 0x80:  # Set DDRAM address
            addr = cmd & 0x7F
            self.state["ddram"] = addr
            self.state["row"] = 1 if addr >= 0x40 else 0
            self.state["col"] = (addr - 0x40) if addr >= 0x40 else addr
        elif cmd == 0x01:  # Clear display
            self.state["screen"] = [[" "] * self.cols for _ in range(self.rows)]
            self.state["row"] = self.state["col"] = 0
        elif cmd == 0x02:  # Return home
            self.state["row"] = self.state["col"] = 0
        elif cmd & 0x08:  # Display on/off
            self.state["display_on"] = bool(cmd & 0x04)
            self.state["cursor_on"] = bool(cmd & 0x02)
        elif cmd & 0x04:  # Entry mode
            self.state["increment"] = bool(cmd & 0x02)
            self.state["shift"] = bool(cmd & 0x01)
        elif cmd & 0x10:  # Cursor/display shift
            move_display = bool(cmd & 0x08)
            right = bool(cmd & 0x04)
            if not move_display:
                self._move_cursor(1 if right else -1)
        elif cmd & 0x20:  # Function set (solo lo registramos)
            self.state["init_count"] += 1

    def _write_char(self, code: int) -> None:
        row, col = self.state["row"], self.state["col"]
        if 0 <= row < self.rows and 0 <= col < self.cols:
            self.state["screen"][row][col] = chr(code) if 32 <= code < 127 else "?"
        self._move_cursor(1 if self.state["increment"] else -1)

    def _move_cursor(self, delta: int) -> None:
        col = self.state["col"] + delta
        row = self.state["row"]
        if col >= self.cols:
            col = 0
            row = (row + 1) % self.rows
        elif col < 0:
            col = self.cols - 1
            row = (row - 1) % self.rows
        self.state["col"] = col
        self.state["row"] = row
        self.state["ddram"] = (0x40 if row else 0x00) + col

    def on_probe(self, ctx: Ctx) -> str:
        return " | ".join("".join(r).rstrip() for r in self.state["screen"])

    def text(self) -> List[str]:
        return ["".join(r) for r in self.state["screen"]]


# ---------------------------------------------------------------------------
# Instrumentos
# ---------------------------------------------------------------------------


@register_model("probe")
class Probe(Model):
    """Sonda: no drivea nada, solo reporta."""

    def evaluate(self, ctx: Ctx) -> None:
        self.state["v"] = ctx.get("IN")

    def on_probe(self, ctx: Ctx) -> str:
        return str(self.state.get("v", L.X))


@register_model("terminal")
class Terminal(Model):
    """Terminal de red: transparente, sirve para etiquetar y jerarquía."""

    def evaluate(self, ctx: Ctx) -> None:
        ctx.release("P")


# ---------------------------------------------------------------------------
# CPU educativa de 8 bits
# ---------------------------------------------------------------------------

# Conjunto de instrucciones SAP8 ampliado. Cada opcode: (mnemónico, operandos).
_SAP8_ISA: Dict[int, tuple] = {
    0x00: ("NOP", 0),
    0x01: ("LDA #", 1),   # A ← inmediato
    0x02: ("LDA", 1),     # A ← MEM[dir]
    0x03: ("STA", 1),     # MEM[dir] ← A
    0x04: ("ADD #", 1),   # A ← A + inmediato
    0x05: ("ADD", 1),     # A ← A + MEM[dir]
    0x06: ("SUB #", 1),
    0x07: ("SUB", 1),
    0x08: ("OUT", 1),     # puerto ← A
    0x09: ("HLT", 0),
    0x0A: ("JMP", 1),
    0x0B: ("JZ", 1),      # salta si el flag Z está activo
    0x0C: ("JC", 1),      # salta si hubo acarreo
    0x0D: ("IN", 1),      # A ← puerto
    0x0E: ("MOV A,B", 0),
    0x0F: ("MOV B,A", 0),
}


@register_model("cpu8")
class CPU8(Model):
    """CPU de 8 bits educativa con bus de direcciones y de datos tri-estado.

    Ejecuta una instrucción por ciclo de reloj. El ciclo se divide en fases
    internas (fetch / decode / execute) que se pueden observar con
    ``state["phase"]`` — útil para enseñar arquitectura de hardware.
    """

    edge_sensitive = True

    def setup(self) -> None:
        self.state.update(
            pc=0, a=0, b=0, flags={"Z": False, "C": False},
            halted=False, phase="FETCH", ir=0, operand=0,
            port_out=0, port_in=0, cycles=0, trace=[],
            # Cuenta atrás de ciclos con la escritura de puerto activa. Hace
            # falta que sobreviva AL MENOS un ciclo de reloj entero: si el
            # pulso empieza y termina dentro del mismo evento, ningún latch
            # externo llega a ver un flanco de subida y el dato se pierde sin
            # que nada parezca fallar. Es un error de temporización que solo un
            # simulador de este tipo deja ver.
            wr_count=0,
        )
        self.program: Dict[int, int] = _parse_init(self.prop("PROGRAM", ""))
        path = self.prop("FILE", "")
        if path:
            self.program.update(_load_hex_file(str(path)))

    # -- bus --------------------------------------------------------------
    def evaluate(self, ctx: Ctx) -> None:
        rst = ctx.get("/RESET")
        if rst is L.LOW:
            program = self.program
            self.setup()
            self.program = program
            st = self.state
            st["pc"] = 0
        st = self.state

        if not ctx.rose("CLK") or st["halted"]:
            self._drive(ctx, drive_data=False)
            return

        st["cycles"] += 1
        pc = st["pc"]

        # -- FETCH: leer el opcode -----------------------------------------
        if st["phase"] == "FETCH":
            opcode = self.program.get(pc, 0x00)
            st["ir"] = opcode
            st["phase"] = "FETCH2"
            st["pc"] = (pc + 1) & 0xFFFF
            self._advance_wr(st)
            self._drive(ctx, addr=pc, drive_data=False)
            st["trace"].append((st["cycles"], pc, "FETCH", f"{opcode:02X}"))
            self._trim(st)
            return

        mnem, nops = _SAP8_ISA.get(st["ir"], ("???", 0))

        # -- FETCH2: leer el operando si la instrucción lo lleva -----------
        if st["phase"] == "FETCH2":
            if nops:
                st["operand"] = self.program.get(st["pc"], 0)
                st["pc"] = (st["pc"] + 1) & 0xFFFF
                st["phase"] = "EXEC"
                self._advance_wr(st)
                self._drive(ctx, addr=pc, drive_data=False)
                return
            st["phase"] = "EXEC"

        # -- EXEC: ejecutar -------------------------------------------------
        if st["phase"] == "EXEC":
            wrote = self._execute(ctx, mnem)
            st["phase"] = "FETCH"
            if wrote:
                # La instrucción acaba de pedir escritura de puerto: el pulso
                # empieza AHORA y durará lo que queda de ciclo más el siguiente.
                st["wr_count"] = 2
            else:
                self._advance_wr(st)
            self._drive(ctx, drive_data=False)

    @staticmethod
    def _advance_wr(st: Dict[str, Any]) -> None:
        """Consume un ciclo de la cuenta atrás del strobe de escritura."""
        if st.get("wr_count", 0) > 0:
            st["wr_count"] -= 1

    @staticmethod
    def _trim(st: Dict[str, Any]) -> None:
        """Recorta la traza para que una simulación larga no se coma la RAM."""
        if len(st["trace"]) > 4096:
            del st["trace"][:2048]

    def _drive(self, ctx: Ctx, addr: Optional[int] = None,
               drive_data: bool = False, data: int = 0) -> None:
        st = self.state
        a = st["pc"] if addr is None else addr
        ctx.set_bus("ADDR", bus_from_int(a & 0xFFFF, 16))
        ctx.set("/MREQ", L.LOW)
        # Temporización de la escritura de puerto. `wr_count` vale 2 durante el
        # ciclo en que se ejecuta OUT y 1 durante el siguiente:
        #
        #   wr_count == 2 → /WR bajo y dato en el bus. Empieza la escritura.
        #   wr_count == 1 → /WR y el dato SIGUEN activos, y sube WSTROBE.
        #
        # Dos detalles que importan, y los dos se descubrieron a base de ver
        # formas de onda mal:
        #
        # * El dato debe mantenerse durante los DOS ciclos. Si solo se driveara
        #   en el primero, el bus volvería a X justo cuando sube el strobe y el
        #   latch capturaría indeterminación.
        # * El strobe va UN CICLO POR DETRÁS de /WR. Si subieran a la vez, el
        #   latch se dispararía en el mismo instante en que el dato aparece y,
        #   con semántica de retardo cero, capturaría el valor anterior.
        #
        # Es exactamente el error de temporización que se comete al diseñar un
        # puerto de salida a mano, y el simulador lo enseña en la traza.
        n = st.get("wr_count", 0)
        writing = drive_data or n >= 1
        if writing:
            ctx.set("/WR", L.LOW)
            ctx.set("/IOREQ", L.LOW)
            ctx.set("/RD", L.HIGH)
            ctx.set_bus("DATA", bus_from_int(st.get("port_out", data) & 0xFF, 8))
        else:
            ctx.set("/WR", L.HIGH)
            ctx.set("/IOREQ", L.HIGH)
            ctx.set("/RD", L.LOW)
            ctx.release("DATA")
        if drive_data or n == 1:
            ctx.set("WSTROBE", L.HIGH)
        else:
            ctx.set("WSTROBE", L.LOW)
        ctx.set("HALT", L.HIGH if st["halted"] else L.LOW)

    def _execute(self, ctx: Ctx, mnem: str) -> bool:
        """Ejecuta la instrucción en curso. Devuelve True si escribió en puerto."""
        st = self.state
        op = st["operand"]
        wrote_port = False

        def set_flags(v: int, carry: bool) -> None:
            st["a"] = v & 0xFF
            st["flags"]["Z"] = (v & 0xFF) == 0
            st["flags"]["C"] = carry

        if mnem == "NOP":
            pass
        elif mnem == "LDA #":
            set_flags(op, False)
        elif mnem == "LDA":
            set_flags(self._peek_mem(op), False)
        elif mnem == "STA":
            self._poke_mem(op, st["a"])
        elif mnem == "ADD #":
            v = st["a"] + op
            set_flags(v, v > 0xFF)
        elif mnem == "ADD":
            v = st["a"] + self._peek_mem(op)
            set_flags(v, v > 0xFF)
        elif mnem == "SUB #":
            v = st["a"] - op
            set_flags(v, v < 0)
        elif mnem == "SUB":
            v = st["a"] - self._peek_mem(op)
            set_flags(v, v < 0)
        elif mnem == "OUT":
            st["port_out"] = st["a"]
            wrote_port = True
            self._drive(ctx, drive_data=True, data=st["a"])
        elif mnem == "IN":
            set_flags(st["port_in"], False)
        elif mnem == "JMP":
            st["pc"] = op & 0xFFFF
        elif mnem == "JZ":
            if st["flags"]["Z"]:
                st["pc"] = op & 0xFFFF
        elif mnem == "JC":
            if st["flags"]["C"]:
                st["pc"] = op & 0xFFFF
        elif mnem == "MOV A,B":
            set_flags(st["b"], False)
        elif mnem == "MOV B,A":
            st["b"] = st["a"]
        elif mnem == "HLT":
            st["halted"] = True
        st["trace"].append((st["cycles"], st["pc"], mnem, f"{op:02X}"))
        self._trim(st)
        return wrote_port

    # Memoria interna de trabajo (para que la CPU sea autocontenida en la demo).
    def _peek_mem(self, addr: int) -> int:
        return self.mem.get(addr & 0xFFFF, 0)

    def _poke_mem(self, addr: int, value: int) -> None:
        self.mem[addr & 0xFFFF] = value & 0xFF

    @property
    def mem(self) -> Dict[int, int]:
        m = self.state.get("_mem")
        if m is None:
            m = self.state["_mem"] = {}
        return m

    def on_probe(self, ctx: Ctx) -> str:
        st = self.state
        return (f"PC={st['pc']:04X} A={st['a']:02X} B={st['b']:02X} "
                f"{'Z' if st['flags']['Z'] else '-'}{'C' if st['flags']['C'] else '-'}"
                + (" HLT" if st["halted"] else ""))
