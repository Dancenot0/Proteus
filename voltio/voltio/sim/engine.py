"""Motor de simulación digital por eventos.

Cómo funciona
-------------

El tiempo avanza a saltos, de evento en evento. Entre dos eventos el circuito
se **asienta**: se evalúan los modelos repetidamente hasta que ningún cable
cambia de valor (punto fijo). Eso resuelve las cascadas de compuertas sin
necesidad de retardo y detecta los lazos combinatorios.

Cada ciclo de asentamiento tiene dos fases, como en un simulador de eventos
clásico:

1. **Evaluación** — cada modelo lee sus entradas y escribe el valor *deseado*
   de sus salidas en un búfer privado. Nadie ve los cambios de los demás hasta
   que la fase termina, así el orden de evaluación no importa.
2. **Resolución** — cada cable combina todos los drivers que lo empujan usando
   la tabla de resolución de :mod:`voltio.core.logic`. Un 0 contra un 1 da
   ``X`` y se registra como **colisión de bus**.

Un lazo combinatorio se detecta cuando el asentamiento no converge tras
``max_settle`` pasadas, y se reporta en vez de colgar el programa.

Uso
---

.. code-block:: python

   eng = Engine(circuit)
   eng.run(until=0.01)
   eng.net_value("Q[3]")          # L.HIGH
   eng.waveform("CLK")            # [(0.0, L.LOW), (5e-4, L.HIGH), ...]
   eng.problems                   # colisiones y oscilaciones detectadas
"""

from __future__ import annotations

import heapq
import time as _wallclock
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..core.ir import Circuit, Endpoint, TIE_PART
from ..core.logic import L, bus_value, is_contention, to_int
from ..parts.library import LIBRARY
from .models import Ctx, Model, make_model

__all__ = ["Engine", "Problem", "Waveform"]

#: Pasadas máximas de asentamiento antes de declarar oscilación.
MAX_SETTLE = 200


@dataclass
class Problem:
    """Algo que fue mal durante la simulación."""

    kind: str  # collision | oscillation | unknown_model | floating
    message: str
    time: float = 0.0
    where: str = ""
    count: int = 1

    def __str__(self) -> str:  # pragma: no cover
        t = f" @ {self.time:g}s" if self.time else ""
        return f"{self.kind.upper()}{t} [{self.where}]: {self.message}"


@dataclass
class Waveform:
    """Historia de niveles de un cable a lo largo del tiempo."""

    name: str
    transitions: List[Tuple[float, L]] = field(default_factory=list)

    def at(self, t: float) -> L:
        """Nivel en el instante ``t`` (búsqueda lineal hacia atrás)."""
        val = L.X
        for ts, v in self.transitions:
            if ts > t:
                break
            val = v
        return val

    def edges(self, rising: bool = True) -> List[float]:
        out = []
        prev = L.X
        for ts, v in self.transitions:
            if prev is L.LOW and v is L.HIGH and rising:
                out.append(ts)
            if prev is L.HIGH and v is L.LOW and not rising:
                out.append(ts)
            prev = v
        return out

    def frequency(self) -> Optional[float]:
        """Frecuencia estimada a partir de los flancos de subida."""
        e = self.edges(True)
        if len(e) < 2:
            return None
        return (len(e) - 1) / (e[-1] - e[0])

    def to_int_samples(self, times: Sequence[float]) -> List[Optional[int]]:
        return [None if not self.at(t).determinate else to_int(self.at(t)) for t in times]

    def __len__(self) -> int:
        return len(self.transitions)


class _Event:
    """Un evento programado en el tiempo."""

    __slots__ = ("t", "seq", "fn", "tag")

    def __init__(self, t: float, seq: int, fn: Callable[[], None], tag: str = ""):
        self.t, self.seq, self.fn, self.tag = t, seq, fn, tag

    def __lt__(self, other: "_Event") -> bool:
        if self.t != other.t:
            return self.t < other.t
        return self.seq < other.seq


class Engine:
    """El simulador."""

    def __init__(
        self,
        circuit: Circuit,
        library=LIBRARY,
        timescale: float = 1e-9,
        max_settle: int = MAX_SETTLE,
        record: bool = True,
    ):
        self.circuit = circuit
        self.library = library
        self.timescale = timescale
        self.max_settle = max_settle
        self.record = record

        self.time = 0.0
        self.step_count = 0
        self.problems: List[Problem] = []
        self.halted = False

        self._net_value: Dict[str, L] = {}
        self._net_raw: Dict[str, List[L]] = {}
        # Dos tablas por modelo, y confundirlas es un bug (ver _settle):
        #   _model_prev     → referencia de flanco; lo que lee ctx.prev()
        #   _model_at_fire  → niveles en el momento del último disparo
        self._model_prev: Dict[str, Dict[str, L]] = {}
        self._model_at_fire: Dict[str, Dict[str, L]] = {}
        self._models: Dict[str, Model] = {}
        self._ctx: Dict[str, Ctx] = {}
        self._buffer: Dict[str, List[List[L]]] = {}
        self._widths: Dict[str, Dict[str, int]] = {}
        self._pin_nets: Dict[str, Dict[Tuple[str, int], str]] = {}
        self._waveforms: Dict[str, Waveform] = {}
        self._queue: List[_Event] = []
        self._seq = 0
        self._edge_models: List[str] = []
        self._comb_models: List[str] = []
        self._last_published: Dict[str, L] = {}
        self._fired_now: Set[str] = set()
        self._problem_index: Dict[Tuple[str, str], Problem] = {}
        self._wallclock_start = 0.0

        self._build(library)

    # -- construcción -----------------------------------------------------
    def _build(self, library) -> None:
        c = self.circuit
        for net in c.nets.values():
            self._net_value[net.name] = L.X
            self._net_raw[net.name] = []
            if self.record:
                self._waveforms[net.name] = Waveform(net.name)

        # Mapa pin → net(s) para cada parte.
        for net in c.nets.values():
            for ep in net.pin_endpoints():
                table = self._pin_nets.setdefault(ep.part, {})
                table[(ep.pin, ep.bit)] = net.name

        # Atados constantes ($TIE): fuentes ideales que empujan el cable.
        self._ties: Dict[str, List[L]] = {
            net.name: net.tie_levels()
            for net in c.nets.values()
            if net.tie_levels()
        }

        # Instancia los modelos.
        for ref, part in c.parts.items():
            if part.spec is None:
                part.spec = library.get(part.part)
                if part.spec is None:
                    self.problems.append(
                        Problem("unknown_model", f"componente '{part.part}' no está en "
                                                f"la librería; se omite", where=ref)
                    )
                    continue
            model = make_model(ref, part.spec, part.props)
            self._models[ref] = model
            widths = {p.name: max(p.width, 1) for p in part.spec.pins}
            self._widths[ref] = widths
            self._buffer[ref] = [
                [L.Z] * widths[p.name] for p in part.spec.pins
            ]
            self._pin_index = getattr(self, "_pin_index", {})

        # Contexto por modelo (se reutiliza, solo cambian tiempo y paso).
        for ref, model in self._models.items():
            self._ctx[ref] = Ctx(
                read=self._make_reader(ref),
                write=self._make_writer(ref),
                prev=self._make_prev_reader(ref),
                widths=self._widths[ref],
                time=0.0,
                step=0,
                engine=self,
            )

        # Los modelos secuenciales van aparte: se evalúan una sola vez por
        # evento para no re-dispararse con el mismo flanco.
        for ref, model in self._models.items():
            if getattr(model, "edge_sensitive", False):
                self._edge_models.append(ref)
            else:
                self._comb_models.append(ref)

        self._settle()
        self._schedule_initial()

    def _pin_slot(self, ref: str, pin: str) -> List[L]:
        spec = self._models[ref].spec
        names = [p.name for p in spec.pins]
        if pin not in names:
            raise KeyError(f"{ref} no tiene el pin {pin}")
        return self._buffer[ref][names.index(pin)]

    def _make_reader(self, ref: str) -> Callable[[str, int], L]:
        def read(pin: str, bit: int) -> L:
            nets = self._pin_nets.get(ref, {})
            name = nets.get((pin, bit))
            if name is None:
                return L.X  # entrada sin conectar → indeterminada
            return self._net_value.get(name, L.X)

        return read

    def _make_prev_reader(self, ref: str) -> Callable[[str, int], L]:
        """Lee la referencia de flanco PROPIA de este modelo.

        Es la foto de los niveles que el motor tomó justo antes de dispararlo
        por última vez. Comparar contra eso (y no contra una foto global del
        evento) es lo que permite los relojes en cascada.
        """

        def read(pin: str, bit: int) -> L:
            nets = self._pin_nets.get(ref, {})
            name = nets.get((pin, bit))
            if name is None:
                return L.X
            return self._model_prev.get(ref, {}).get(name, L.X)

        return read

    def _make_writer(self, ref: str) -> Callable[[str, int, L], None]:
        def write(pin: str, bit: int, value: L) -> None:
            slot = self._pin_slot(ref, pin)
            if 0 <= bit < len(slot):
                slot[bit] = value

        return write

    # -- asentamiento -----------------------------------------------------
    def _settle(self) -> bool:
        """Asienta el circuito tras un cambio. Devuelve False si no converge.

        Dos fases que se alternan:

        **Propagar** — los modelos combinatorios se reevalúan hasta que ningún
        cable cambia (punto fijo). Lleva los niveles nuevos hasta las entradas
        de los elementos de memoria.

        **Capturar** — los modelos secuenciales se evalúan comparando contra su
        referencia de flanco.

        Las dos reglas que hacen que esto funcione, y que costaron varios
        intentos falsos
        -------------------------------------------------------------------
        1. La referencia de flanco de la PRIMERA evaluación de un modelo en
           cada evento es el estado asentado **antes** del evento, no el actual.
           Si se congelara en el momento de dispararse, la fase de propagación
           ya habría publicado el nivel nuevo y el modelo no vería ningún
           flanco: un flip-flop dejaría de capturar para siempre.

        2. Después de la primera evaluación, un modelo puede volver a
           dispararse **solo si alguna de sus entradas cambió desde entonces**.
           Esto es lo que resuelve los relojes en cascada: cuando la salida de
           un dispositivo es el reloj de otro (el strobe de escritura de una CPU
           que alimenta el latch de su puerto, o el Q de un flip-flop que relojea
           al siguiente en un contador de rizado), el segundo ve aparecer un
           flanco nuevo a mitad del asentamiento y lo captura.

           La condición es essential: sin ella, el modelo se re-dispararía con
           el mismo flanco en cada vuelta y un contador contaría 200 veces por
           un solo pulso de reloj, que es el fallo contrario.
        """
        baseline = dict(self._net_value)

        # Siembra el estado inicial en t=0 la primera vez. Sin esto,
        # ``waveform.at(0)`` devolvería X en vez del nivel real con el que
        # arranca el circuito, y las formas de onda perderían su primer tramo.
        if not self._last_published:
            for name, value in baseline.items():
                self._last_published[name] = value
                if self.record:
                    wf = self._waveforms.get(name)
                    if wf is not None:
                        wf.transitions.append((self.time, value))

        # Modelos ya disparados en ESTE evento. Se limpia aquí, no se confunde
        # con _model_prev, que es la referencia de flanco y sí persiste.
        self._fired_now.clear()
        rounds = 0
        settled = False

        while not settled and rounds < self.max_settle:
            rounds += 1

            # ---- propagar hasta punto fijo -------------------------------
            passes = 0
            changed = True
            while changed and passes < self.max_settle:
                passes += 1
                for ref in self._comb_models:
                    self._eval_model(ref, self._models[ref])
                changed = self._resolve_nets()
            if changed:
                self._report_oscillation()
                self._publish()
                return False

            # ---- capturar flancos ----------------------------------------
            fired_any = False
            for ref in self._edge_models:
                model = self._models[ref]
                if ref not in self._fired_now:
                    # Primera evaluación del evento: se compara contra el
                    # estado asentado ANTERIOR, que es donde estaba el flanco.
                    self._model_prev[ref] = baseline
                    self._fired_now.add(ref)
                elif not self._inputs_changed(ref):
                    continue        # ya respondió exactamente a estos niveles

                before = self._snapshot_outputs(ref)
                self._eval_model(ref, model)

                # Las dos fotos se toman AHORA, tras evaluar, y son distintas:
                #
                #   _model_prev    → lo que ctx.prev() verá en el PRÓXIMO
                #                     disparo. Si no se actualizara, el modelo
                #                     volvería a ver el mismo flanco una y otra
                #                     vez (un contador contaría 200 por pulso).
                #   _model_at_fire → los niveles tal como quedan DESPUÉS de que
                #                     este modelo publique sus salidas. Sirve
                #                     para saber si algo cambió desde entonces,
                #                     que es lo que habilita los relojes en
                #                     cascada: la CPU sube su strobe, el latch
                #                     de puerto lo ve aparecer y se dispara.
                self._model_prev[ref] = dict(self._net_value)
                self._resolve_nets()
                self._model_at_fire[ref] = dict(self._net_value)

                if self._snapshot_outputs(ref) != before:
                    fired_any = True

            if self._resolve_nets():
                fired_any = True

            settled = not fired_any

        if not settled:
            self._report_oscillation()
        self._publish()
        return settled

    def _inputs_changed(self, ref: str) -> bool:
        """¿Ha cambiado alguna entrada de este modelo desde su último disparo?

        Compara contra :data:`_model_at_fire`, que es la foto de los niveles en
        el momento del último disparo. No contra ``_model_prev``, que es la
        referencia de flanco y puede ser más antigua a propósito.
        """
        prev = self._model_at_fire.get(ref)
        if not prev:
            return True
        nets = self._pin_nets.get(ref, {})
        for pin, bit in nets:
            name = nets[(pin, bit)]
            cur = self._net_value.get(name, L.X)
            if prev.get(name, L.X) != cur:
                return True
        return False

    def _snapshot_outputs(self, ref: str) -> tuple:
        """Estado de los búfers de salida de un modelo, para ver si cambió."""
        return tuple(tuple(slot) for slot in self._buffer[ref])

    def _report_oscillation(self) -> None:
        self._report(
            "oscillation",
            f"el circuito no converge tras {self.max_settle} pasadas: "
            f"hay un lazo combinatorio (una salida realimentada a su propia "
            f"entrada sin ningún elemento de memoria de por medio)",
            where=self.circuit.name,
        )

    def _eval_model(self, ref: str, model: Model) -> None:
        """Evalúa un modelo sobre su búfer privado, sin publicar nada aún."""
        for slot in self._buffer[ref]:
            for i in range(len(slot)):
                slot[i] = L.Z  # si el modelo no escribe, suelta el bus
        ctx = self._ctx[ref]
        ctx._time = self.time
        ctx._step = self.step_count
        try:
            model.evaluate(ctx)
        except Exception as e:  # un modelo roto no debe tumbar el motor
            self._report("unknown_model", f"el modelo de {ref} falló: {e!r}",
                         where=ref)

    def _resolve_nets(self, record: bool = False) -> bool:
        """Recalcula el nivel de cada cable. Devuelve True si algo cambió.

        Con ``record=False`` (el caso normal dentro del asentamiento) no toca
        las formas de onda ni denuncia colisiones: los valores intermedios de
        una pasada no son observables, son solo el camino hacia el punto fijo.
        """
        changed = False
        for net in self.circuit.nets.values():
            drivers: List[L] = list(self._ties.get(net.name, ()))
            for ep in net.pin_endpoints():
                model = self._models.get(ep.part)
                if model is None:
                    continue
                if ep.pin not in model.outputs:
                    continue
                try:
                    drivers.append(self._pin_slot(ep.part, ep.pin)[ep.bit])
                except (KeyError, IndexError):
                    continue
            value = bus_value(drivers)
            self._net_raw[net.name] = drivers
            if value != self._net_value[net.name]:
                self._net_value[net.name] = value
                changed = True
        return changed

    def _publish(self) -> None:
        """Publica el estado asentado: formas de onda y colisiones.

        Se llama UNA vez por evento, cuando el circuito ya convergió. Esto es
        lo que separa un fallo real de un glitch interno del asentamiento: con
        retardo cero, una pasada intermedia puede mostrar a la RAM driveando el
        bus un instante antes de que el inversor propague el ``/WR`` que la
        deshabilita. Ese intermedio no existe físicamente; el estado final sí.
        """
        for net in self.circuit.nets.values():
            drivers: List[L] = list(self._ties.get(net.name, ()))
            for ep in net.pin_endpoints():
                model = self._models.get(ep.part)
                if model is None or ep.pin not in model.outputs:
                    continue
                try:
                    drivers.append(self._pin_slot(ep.part, ep.pin)[ep.bit])
                except (KeyError, IndexError):
                    continue
            value = bus_value(drivers)
            self._net_raw[net.name] = drivers

            if is_contention(drivers):
                who = ", ".join(
                    str(ep)
                    for ep in net.pin_endpoints()
                    if ep.part in self._models
                    and ep.pin in self._models[ep.part].outputs
                )
                self._report(
                    "collision",
                    f"colisión en el cable '{net.label}': hay un driver en 0 y "
                    f"otro en 1 a la vez ({who or 'fuentes opuestas'}). Dos "
                    f"salidas push-pull enfrentadas, o un dispositivo que no "
                    f"suelta el bus cuando debería.",
                    where=net.label,
                )

            if self.record and value != self._last_published.get(net.name, value):
                wf = self._waveforms.get(net.name)
                if wf is not None:
                    wf.transitions.append((self.time, value))
                self._last_published[net.name] = value
            elif net.name not in self._last_published:
                self._last_published[net.name] = value
            self._net_value[net.name] = value

    def _report(self, kind: str, message: str, where: str = "") -> None:
        """Registra un problema, colapsando los repetidos en un contador."""
        key = (kind, where)
        existing = self._problem_index.get(key)
        if existing is not None:
            existing.count += 1
            existing.time = self.time
            return
        p = Problem(kind=kind, message=message, time=self.time, where=where)
        self.problems.append(p)
        self._problem_index[key] = p

    # -- agenda de eventos ------------------------------------------------
    def _schedule_initial(self) -> None:
        """Arranca los relojes y cualquier fuente dependiente del tiempo."""
        for ref, model in self._models.items():
            period = getattr(model, "period", 0.0)
            if period and period > 0 and hasattr(model, "state"):
                self._schedule_next_toggle(model, self.time)

    def _schedule_next_toggle(self, model: Model, t: float) -> None:
        """Programa la *siguiente* conmutación de un reloj (no todas de golpe).

        Programar perezosamente es lo que permite simular un reloj de 16 MHz
        durante 1 segundo sin meter 32 millones de eventos en la cola.
        """
        period = float(getattr(model, "period", 0.0))
        if period <= 0:
            return
        duty = float(getattr(model, "duty", 0.5))
        duty = min(max(duty, 0.01), 0.99)
        high = period * duty
        low = period - high
        is_low = model.state.get("v") is L.LOW
        delta = high if is_low else low
        if delta <= 0:
            delta = period

        def toggle() -> None:
            cur = model.state.get("v", L.LOW)
            model.state["v"] = L.LOW if cur is L.HIGH else L.HIGH
            self._schedule_next_toggle(model, self.time)

        self._push(t + delta, toggle, tag=f"{model.ref}:clock")

    def _push(self, t: float, fn: Callable[[], None], tag: str = "") -> None:
        self._seq += 1
        heapq.heappush(self._queue, _Event(t, self._seq, fn, tag))

    def schedule(self, t: float, fn: Callable[[], None], tag: str = "user") -> None:
        """Programa una acción arbitraria en el instante ``t`` (API pública)."""
        self._push(t, fn, tag)

    def schedule_change(self, t: float, ref: str, value: Any) -> None:
        """Cambia un componente interactivo en ``t`` (p. ej. pulsar un botón)."""
        model = self._models.get(ref)
        if model is None:
            raise KeyError(f"no existe la parte '{ref}'")
        self._push(t, lambda: self._set_interactive(model, value), tag=f"{ref}:user")

    def _set_interactive(self, model: Model, value: Any) -> None:
        model.set_interactive(value)
        self._settle()

    def next_event_time(self) -> Optional[float]:
        """Instante del próximo evento programado, o ``None`` si no queda nada."""
        return self._queue[0].t if self._queue else None

    # -- ejecución --------------------------------------------------------
    def run(self, until: float, wall_limit: float = 30.0,
            on_step: Optional[Callable[[float], None]] = None) -> None:
        """Avanza hasta el instante ``until`` (segundos).

        ``wall_limit`` corta la simulación si tarda demasiado en tiempo real,
        para que un reloj de 16 MHz no cuelgue el programa.
        """
        self._wallclock_start = _wallclock.time()
        deadline_events = 0
        while self._queue and not self.halted:
            ev = self._queue[0]
            if ev.t > until:
                break
            heapq.heappop(self._queue)
            self.time = ev.t
            self.step_count += 1
            ev.fn()
            self._settle()
            if on_step is not None:
                on_step(self.time)
            deadline_events += 1
            if deadline_events % 512 == 0:
                if _wallclock.time() - self._wallclock_start > wall_limit:
                    self._report(
                        "oscillation",
                        f"simulación abortada: {deadline_events} eventos y más de "
                        f"{wall_limit:.0f}s de cómputo. El reloj es demasiado "
                        f"rápido para este intervalo; baja FREQ o acorta 'duration'.",
                        where=self.circuit.name,
                    )
                    self.halted = True
                    break
        self.time = max(self.time, min(until, self.time))

    def step_to(self, t: float) -> None:
        """Alias de :meth:`run` para avanzar a un instante concreto."""
        self.run(until=t)

    def settle(self) -> None:
        """Re-asienta el circuito tras un cambio manual (lo usa la GUI)."""
        self._settle()

    def poke(self, net_name: str, value: L) -> None:
        """Fuerza el nivel de un cable (inyección de fallos / depuración)."""
        if net_name not in self._net_value:
            raise KeyError(f"no existe el cable '{net_name}'")
        self._net_value[net_name] = value
        if self.record:
            self._waveforms[net_name].transitions.append((self.time, value))
        self._settle()

    def reset(self) -> None:
        """Reinicia la simulación desde cero."""
        for net in self.circuit.nets.values():
            self._net_value[net.name] = L.X
            self._net_raw[net.name] = []
        self._model_prev.clear()
        self._model_at_fire.clear()
        self._fired_now.clear()
        for wf in self._waveforms.values():
            wf.transitions.clear()
        for model in self._models.values():
            model.setup()
        self.time = 0.0
        self.step_count = 0
        self.problems.clear()
        self._problem_index.clear()
        self._queue.clear()
        self.halted = False
        # Los modelos secuenciales van aparte: se evalúan una sola vez por
        # evento para no re-dispararse con el mismo flanco.
        for ref, model in self._models.items():
            if getattr(model, "edge_sensitive", False):
                self._edge_models.append(ref)
            else:
                self._comb_models.append(ref)

        self._settle()
        self._schedule_initial()

    # -- consultas --------------------------------------------------------
    def net_value(self, name: str) -> L:
        """Nivel actual de un cable. Acepta ``Q[3]`` o el nombre de una red."""
        if name in self._net_value:
            return self._net_value[name]
        net = self.circuit.nets.get(name)
        if net is not None:
            return self._net_value.get(net.name, L.X)
        return L.X

    def pin_value(self, ref: str, pin: str, bit: int = 0) -> L:
        """Nivel visto por un pin concreto de una parte."""
        name = self._pin_nets.get(ref, {}).get((pin, bit))
        if name is None:
            return L.X
        return self._net_value.get(name, L.X)

    def pin_bus(self, ref: str, pin: str) -> List[L]:
        model = self._models.get(ref)
        if model is None:
            return []
        w = self._widths.get(ref, {}).get(pin, 1)
        return [self.pin_value(ref, pin, i) for i in range(w)]

    def bus_int(self, ref: str, pin: str) -> Optional[int]:
        bits = self.pin_bus(ref, pin)
        if any(not b.determinate for b in bits):
            return None
        v = 0
        for i, b in enumerate(bits):
            v |= to_int(b) << i
        return v

    def model(self, ref: str) -> Optional[Model]:
        return self._models.get(ref)

    def models(self) -> Dict[str, Model]:
        return dict(self._models)

    def interactive_parts(self) -> List[str]:
        return [r for r, m in self._models.items() if m.interactive()]

    def waveform(self, name: str) -> Optional[Waveform]:
        return self._waveforms.get(name)

    def waveforms(self) -> Dict[str, Waveform]:
        return dict(self._waveforms)

    def sample(self, times: Sequence[float]) -> Dict[str, List[Optional[int]]]:
        """Muestrea todos los cables en una serie de instantes (para trazas)."""
        out: Dict[str, List[Optional[int]]] = {}
        for name, wf in self._waveforms.items():
            out[name] = [
                None if not wf.at(t).determinate else to_int(wf.at(t)) for t in times
            ]
        return out

    def ascii_wave(self, names: Sequence[str], samples: int = 64,
                   until: Optional[float] = None) -> str:
        """Diagrama de tiempos en texto. Muy útil en terminal y en tests."""
        t_end = until if until is not None else max(self.time, 1e-9)
        times = [t_end * i / max(samples - 1, 1) for i in range(samples)]
        rows: List[str] = []
        width = max((len(n) for n in names), default=8) + 2
        for n in names:
            wf = self._waveforms.get(n)
            if wf is None:
                rows.append(f"{n:>{width}}" + "?" * samples)
                continue
            line = []
            for t in times:
                v = wf.at(t)
                line.append(str(v))
            rows.append(f"{n:>{width}}" + "".join(line))
        header = " " * width + "".join(
            "|" if i % 8 == 0 else " " for i in range(samples)
        )
        scale = " " * width + f"0{'' : <{samples-16}}{t_end:g}s"
        return "\n".join([header, *rows, scale])

    def summary(self) -> str:
        c = self.circuit
        s = c.stats()
        lines = [
            f"circuito : {s['name']}",
            f"partes   : {s['parts']}  ({len(s['by_kind'])} tipos distintos)",
            f"cables   : {s['nets']}   buses: {s['buses']}",
            f"tiempo   : {self.time:g} s en {self.step_count} eventos",
            f"modelos  : {len(self._models)} instanciados",
        ]
        stubs = [r for r, m in self._models.items()
                 if type(m).__name__ == "_Stub"]
        if stubs:
            lines.append(f"sin modelo eléctrico: {', '.join(sorted(stubs))}")
        if self.problems:
            lines.append(f"problemas: {len(self.problems)}")
            for p in self.problems:
                lines.append(f"  · {p}")
        else:
            lines.append("problemas: ninguno")
        return "\n".join(lines)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Engine '{self.circuit.name}' t={self.time:g}s "
                f"{self.step_count} eventos, {len(self.problems)} problemas>")
