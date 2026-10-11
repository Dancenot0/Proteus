"""Interfaz de línea de comandos.

.. code-block:: text

   python -m voltio CIRCUITO.yaml                 # valida, simula y dibuja
   python -m voltio CIRCUITO.yaml --svg out.svg   # guarda el esquemático
   python -m voltio CIRCUITO.yaml --trace         # formas de onda en texto
   python -m voltio --generate "un semáforo..."   # de lenguaje natural a circuito
   python -m voltio CIRCUITO.yaml --spice out.cir # exporta netlist
   python -m voltio --gui CIRCUITO.yaml           # canvas PySide6
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

from . import __version__
from .core.ir import Circuit
from .core.logic import L
from .dsl.loader import DslError, dump_file, load_file, load_text, to_dict
from .core import miniyaml


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="voltio",
        description="Voltio — motor de circuitos digitales accionado por IA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("archivo", nargs="?", help="DSL YAML del circuito")
    p.add_argument("--version", action="version", version=f"voltio {__version__}")

    g = p.add_argument_group("salida gráfica")
    g.add_argument("--svg", metavar="RUTA", help="guarda el esquemático en SVG")
    g.add_argument("--no-sim-color", action="store_true",
                   help="no colorear los cables según el nivel simulado")
    g.add_argument("--no-grid", action="store_true", help="sin rejilla de fondo")

    g = p.add_argument_group("simulación")
    g.add_argument("--sim", metavar="DURACIÓN",
                   help="cuánto simular (p. ej. 10ms, 1us). Por defecto usa "
                        "simulation.duration del DSL")
    g.add_argument("--trace", action="store_true",
                   help="imprime las formas de onda en texto")
    g.add_argument("--trace-nets", metavar="A,B,C",
                   help="qué redes mostrar en la traza (por defecto, todas)")
    g.add_argument("--samples", type=int, default=72,
                   help="columnas de la traza ASCII (defecto 72)")
    g.add_argument("--set", metavar="REF=0|1", action="append", default=[],
                   help="fuerza el estado de un componente interactivo antes de simular")

    g = p.add_argument_group("exportación")
    g.add_argument("--spice", metavar="RUTA", help="exporta netlist SPICE")
    g.add_argument("--edif", metavar="RUTA", help="exporta EDIF 2 0 0")
    g.add_argument("--bom", metavar="RUTA", help="exporta la lista de materiales en CSV")
    g.add_argument("--netlist", metavar="RUTA", help="exporta el netlist plano en texto")
    g.add_argument("--proteus", metavar="RUTA",
                   help="exporta un .pdsprj/.dsn de Proteus (experimental)")
    g.add_argument("--plantilla", metavar="PDSPRJ",
                   help="proyecto real de Proteus usado como donante de "
                        "familias, PINOUTs y CDB para --proteus")
    g.add_argument("--dsl-out", metavar="RUTA",
                   help="re-serializa el DSL normalizado (útil tras editar en la GUI)")

    g = p.add_argument_group("verificación")
    g.add_argument("--check", action="store_true",
                   help="solo valida; no simula ni dibuja")
    g.add_argument("--strict", action="store_true",
                   help="los avisos cuentan como errores")
    g.add_argument("--stats", action="store_true", help="estadísticas del circuito")
    g.add_argument("--analyze", action="store_true",
                   help="análisis estructural del circuito, sin llamar a ninguna IA")

    g = p.add_argument_group("IA")
    g.add_argument("--generate", metavar="DESCRIPCIÓN",
                   help="pide a una IA que construya el circuito descrito")
    g.add_argument("--provider", default="clipboard",
                   help="openai | anthropic | gemini | ollama | clipboard (defecto)")
    g.add_argument("--model", help="modelo concreto dentro del proveedor")
    g.add_argument("--attempts", type=int, default=3,
                   help="rondas máximas de autocorrección (defecto 3)")
    g.add_argument("--data", metavar="TEXTO",
                   help="datos o restricciones extra para la IA")
    g.add_argument("--explain", action="store_true",
                   help="sentido inverso: pide a la IA que explique el circuito")
    g.add_argument("--question", default="", help="pregunta concreta para --explain")
    g.add_argument("--providers", action="store_true",
                   help="lista los proveedores de IA disponibles y sale")

    g = p.add_argument_group("otros")
    g.add_argument("--gui", action="store_true", help="abre el canvas PySide6")
    g.add_argument("--parts", metavar="BUSQUEDA", nargs="?", const="",
                   help="lista los componentes de la librería")
    g.add_argument("--quiet", action="store_true", help="menos chatter")
    return p


# ---------------------------------------------------------------------------


def _parse_duration(text: Optional[str], circuit: Circuit) -> float:
    """Duración de la simulación: la de la CLI manda sobre la del DSL."""
    if text:
        from .core.miniyaml import _parse_scalar

        v = _parse_scalar(text)
        if isinstance(v, (int, float)):
            return float(v)
        raise SystemExit(f"--sim: no entiendo la duración {text!r} (usa p. ej. 10ms)")
    sim = (circuit.metadata or {}).get("simulation") or {}
    d = sim.get("duration")
    if isinstance(d, (int, float)):
        return float(d)
    return 1e-3  # 1 ms por defecto


def _apply_overrides(engine, overrides: Sequence[str]) -> None:
    for item in overrides:
        if "=" not in item:
            raise SystemExit(f"--set: formato inválido {item!r}, usa REF=0 o REF=1")
        ref, val = item.split("=", 1)
        model = engine.model(ref.strip())
        if model is None:
            raise SystemExit(f"--set: no existe el componente {ref.strip()!r}")
        if not model.interactive():
            print(f"aviso: {ref.strip()} no es interactivo; se ignora", file=sys.stderr)
            continue
        model.set_interactive(val.strip())
    if overrides:
        engine.settle()


def _cmd_parts(query: str) -> int:
    from .parts.library import LIBRARY

    specs = LIBRARY.search(query) if query else list(LIBRARY.specs())
    by_cat: dict = {}
    for s in specs:
        by_cat.setdefault(s.category, []).append(s)
    total = len(LIBRARY)
    print(f"{len(specs)} de {total} componentes\n")
    for cat in sorted(by_cat):
        print(f"[{cat}]")
        for s in sorted(by_cat[cat], key=lambda x: x.name):
            pins = ", ".join(
                p.display_name() + (f"[0:{p.width-1}]" if p.width > 1 else "")
                for p in s.pins
            )
            print(f"  {s.name:<12} {s.description}")
            print(f"  {'':<12} pines: {pins}")
        print()
    return 0


def _cmd_providers() -> int:
    from .ai.providers import available

    print("Proveedor     Estado")
    print("-" * 60)
    for name, reason in available().items():
        print(f"{name:<13} {'LISTO' if reason is None else reason}")
    print("\nUsa --provider NOMBRE para elegir uno.")
    print("Con 'clipboard' no hace falta ninguna clave: te imprime el prompt,")
    print("lo pegas en tu chat de IA favorito y pegas la respuesta de vuelta.")
    return 0


def _cmd_generate(args) -> int:
    from .ai.pipeline import generate
    from .ai.providers import ProviderError, get

    def show(att):
        mark = "✓" if att.ok else "✗"
        print(f"  {mark} intento {att.index}: {len(att.errors)} errores, "
              f"{len(att.warnings)} avisos ({att.seconds:.1f}s)"
              + (f" — {att.note}" if att.note else ""))
        for e in att.errors[:5]:
            print(f"      · {e}")

    print(f"Generando circuito para:\n  «{args.generate}»\n")
    print(f"Proveedor: {args.provider}" + (f" / {args.model}" if args.model else ""))
    try:
        res = generate(
            args.generate,
            provider_name=args.provider,
            model=args.model,
            extra_data=args.data or "",
            max_attempts=args.attempts,
            on_attempt=None if args.quiet else show,
        )
    except ProviderError as e:
        print(f"\nError del proveedor: {e}", file=sys.stderr)
        return 2

    print()
    if not args.quiet:
        print(res.report())
        print()

    if res.yaml_text:
        out = args.archivo or "generado.yaml"
        with open(out, "w", encoding="utf-8") as f:
            f.write(res.yaml_text if res.yaml_text.endswith("\n") else res.yaml_text + "\n")
        print(f"DSL guardado en: {out}")

    if res.ok and res.circuit is not None:
        base = os.path.splitext(args.archivo or "generado")[0]
        _render(args, res.circuit, base, quiet=args.quiet)
        return 0

    print("\nNo se consiguió un circuito válido.", file=sys.stderr)
    return 1


def _cmd_explain(args, circuit: Circuit) -> int:
    from .ai.pipeline import describe
    from .ai.providers import ProviderError

    try:
        text = describe(circuit, question=args.question,
                        provider_name=args.provider, model=args.model)
    except ProviderError as e:
        print(f"\nError del proveedor: {e}", file=sys.stderr)
        return 2
    print("\n" + "=" * 72)
    print(text.strip())
    print("=" * 72)
    return 0


def _render(args, circuit: Circuit, base: str, quiet: bool = False) -> Optional[object]:
    """Simula (si procede) y guarda el SVG."""
    from .render import save_svg
    from .sim import Engine

    engine = Engine(circuit)
    _apply_overrides(engine, args.set)
    duration = _parse_duration(args.sim, circuit)
    engine.run(until=duration)

    svg_path = args.svg or f"{base}.svg"
    save_svg(
        circuit, svg_path, engine=engine,
        show_grid=not args.no_grid,
        show_values=not args.no_sim_color,
    )
    if not quiet:
        size = os.path.getsize(svg_path)
        print(f"Esquemático  : {svg_path} ({size // 1024} KB)")
        print(f"Simulación   : t={engine.time:g} s, {engine.step_count} eventos")
        if engine.problems:
            print(f"Problemas    : {len(engine.problems)}")
            for p in engine.problems:
                print(f"  · {p}")
        else:
            print("Problemas    : ninguno")

        # Indicadores en vivo: lo más útil de ver de un vistazo.
        live = _live_readout(engine)
        if live:
            print("\nEstado final:")
            for line in live:
                print(f"  {line}")

    if args.trace:
        nets = ([n.strip() for n in args.trace_nets.split(",")]
                if args.trace_nets else _default_trace_nets(circuit))
        print()
        print(engine.ascii_wave(nets, samples=args.samples, until=duration))

    if args.spice:
        from .export.spice import save as save_spice

        save_spice(circuit, args.spice)
        print(f"Netlist SPICE: {args.spice}")
    if args.edif:
        from .export.edif import save as save_edif

        save_edif(circuit, args.edif)
        print(f"EDIF 2 0 0   : {args.edif}")
    if args.bom:
        from .export.netlist import to_bom_csv

        with open(args.bom, "w", encoding="utf-8") as f:
            f.write(to_bom_csv(circuit))
        print(f"Lista de mat.: {args.bom}")
    if args.netlist:
        from .export.netlist import to_netlist

        with open(args.netlist, "w", encoding="utf-8") as f:
            for e in to_netlist(circuit):
                f.write(f"{e['ref']:<8} {e['part']:<12} "
                        f"{', '.join(f'{k}={v}' for k, v in sorted(e['pins'].items()))}\n")
        print(f"Netlist      : {args.netlist}")
    if args.proteus and args.plantilla:
        from .proteus.writer import synthesize

        info = synthesize(circuit, args.plantilla, args.proteus)
        print(f"Proteus DSN  : {info['dsn']} ({info['bytes']:,} bytes)")
        print(f"               {info['componentes']} componentes, "
              f"{info['cables']} registros de cable, sintetizados por el motor")
        print("               (dialecto de la plantilla; ábrelo con doble clic "
              "y cuéntame qué ver:")
        print("                piezas sueltas, cables desconectados, o el "
                "circuito funcionando)")
        return 0
    if args.proteus:
        from .export.proteus import save as save_proteus

        r = save_proteus(circuit, args.proteus)
        print(f"Proteus      : {args.proteus}")
        for nombre, tam in r.entries.items():
            print(f"               · {nombre} ({tam:,} bytes)")
        print(f"               {r.message}")
    return engine


def _live_readout(engine) -> List[str]:
    """Resumen de los indicadores: displays, LEDs, LCD, sondas, CPU."""
    out: List[str] = []
    for ref, model in sorted(engine.models().items()):
        spec = model.spec
        if spec is None:
            continue
        sym = spec.symbol
        if sym == "7seg":
            lit = model.segments_lit() if hasattr(model, "segments_lit") else []
            bar = "".join("#" if x else "." for x in lit)
            out.append(f"{ref:<7} display 7 segmentos = {model.state.get('digit','?')!r}  [{bar}]")
        elif sym == "led":
            out.append(f"{ref:<7} LED {'ENCENDIDO' if model.state.get('on') else 'apagado'}")
        elif sym == "lcd":
            text = model.text() if hasattr(model, "text") else []
            out.append(f"{ref:<7} LCD: " + " | ".join(t.rstrip() for t in text))
        elif sym == "probe":
            out.append(f"{ref:<7} sonda = {model.state.get('v', L.X)}")
        elif sym == "logicstate":
            out.append(f"{ref:<7} estado = {model.state.get('v', L.X)}")
        elif sym == "switch":
            out.append(f"{ref:<7} {'CERRADO' if model.state.get('closed') else 'abierto'}")
        elif spec.model in ("cpu8", "counter", "register"):
            probe = model.on_probe(None) if hasattr(model, "on_probe") else ""
            if probe:
                out.append(f"{ref:<7} {probe}")
        elif spec.symbol in ("counter", "register"):
            probe = model.on_probe(None) if hasattr(model, "on_probe") else ""
            if probe:
                out.append(f"{ref:<7} {probe}")
    return out


def _default_trace_nets(circuit: Circuit, limit: int = 16) -> List[str]:
    """Redes que merece la pena trazar: buses con nombre y redes nombradas."""
    named = [n for n in circuit.nets.values()
             if n.name and not n.name.startswith("N$")]
    named.sort(key=lambda n: ((n.bit or 0), n.bus or "", n.name))
    out = [n.name for n in named]
    return out[:limit]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)

    if args.providers:
        return _cmd_providers()
    if args.parts is not None:
        return _cmd_parts(args.parts)
    if args.generate:
        return _cmd_generate(args)
    if not args.archivo:
        _parser().print_help()
        return 0

    # -- cargar ------------------------------------------------------------
    try:
        circuit, issues = load_file(args.archivo)
    except DslError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"ERROR: no existe el archivo {args.archivo}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"Circuito: {circuit.title}  ({args.archivo})")
        print(f"  {len(circuit.parts)} componentes, {len(circuit.nets)} redes, "
              f"{len(circuit.buses)} buses")

    errs = [i for i in issues if i.kind == "error"]
    warns = [i for i in issues if i.kind == "warning"]
    infos = [i for i in issues if i.kind == "info"]
    for i in issues:
        if args.quiet and i.kind == "info":
            continue
        print(f"  {i}")

    if errs or (args.strict and warns):
        print(f"\n{len(errs)} errores de validación.", file=sys.stderr)
        return 1
    if args.check:
        print("\nValidación correcta." + (f" ({len(warns)} avisos)" if warns else ""))
        return 0

    if args.explain:
        return _cmd_explain(args, circuit)

    if args.analyze:
        from .reverse import describe_local

        print()
        print(describe_local(circuit))
        return 0

    if args.stats:
        s = circuit.stats()
        print("\nEstadísticas:")
        for k, v in s["by_kind"].items():
            print(f"  {k:<14} ×{v}")

    if args.dsl_out:
        dump_file(circuit, args.dsl_out)
        print(f"DSL normalizado: {args.dsl_out}")

    base = os.path.splitext(args.archivo)[0]
    if args.gui:
        return _cmd_gui(args, circuit)

    # Si no se pidió ninguna salida concreta, al menos dibuja.
    _render(args, circuit, base, quiet=args.quiet)
    return 0


def _cmd_gui(args, circuit: Circuit) -> int:
    try:
        from .gui.main import run as gui_run
    except ImportError as e:
        print(
            "\nLa GUI necesita PySide6:\n\n    pip install PySide6\n\n"
            f"(detalle: {e})",
            file=sys.stderr,
        )
        return 3
    gui_run(circuit, duration=_parse_duration(args.sim, circuit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
