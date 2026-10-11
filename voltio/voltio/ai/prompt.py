"""Construcción del prompt: el "manual" que se le entrega a la IA.

La idea central del proyecto es esta: en vez de que la IA te dé un tutorial de
doce pasos, se le entrega un manual preciso del formato que entiende el motor y
devuelve el circuito completo de una vez. La calidad del resultado depende
casi por entero de la calidad de este prompt, así que se genera
**dinámicamente desde la librería**: si mañana añades un componente nuevo, el
prompt se entera solo, sin que tengas que tocar nada aquí.

Tres piezas:

* :func:`catalog`   — el catálogo de componentes disponibles con sus pines.
* :func:`dsl_spec`  — la gramática del DSL con ejemplos.
* :func:`build`     — el prompt de sistema completo.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ..parts.library import LIBRARY, Library
from ..parts.spec import PartSpec

__all__ = ["build", "catalog", "dsl_spec", "system_prompt", "user_prompt",
           "repair_prompt", "reverse_prompt"]


def catalog(library: Optional[Library] = None, include_props: bool = True,
            only: Optional[Sequence[str]] = None) -> str:
    """Lista compacta de componentes con sus pines y propiedades.

    Formato de cada línea, pensado para que un LLM lo lea sin ambigüedad::

        74LS193  contador síncrono 4 bits arriba/abajo, carga paralela
                 in:  /MR, /PL, CPU, CPD, P[0:3]
                 out: Q[0:3], /TCU, /TCD
                 props: (ninguna)
    """
    lib = library or LIBRARY
    specs = list(lib.specs())
    if only:
        wanted = {w.upper() for w in only}
        specs = [s for s in specs if s.name.upper() in wanted or lib.get(s.name).name.upper() in wanted]

    by_cat: dict = {}
    for s in specs:
        by_cat.setdefault(s.category, []).append(s)

    lines: List[str] = []
    for cat in sorted(by_cat):
        lines.append(f"### {cat}")
        for s in sorted(by_cat[cat], key=lambda x: x.name):
            lines.append(_spec_block(s, include_props))
        lines.append("")
    return "\n".join(lines).strip()


def _spec_block(s: PartSpec, include_props: bool = True) -> str:
    ins, outs, ios = [], [], []
    for p in s.pins:
        tok = _pin_token(p)
        if p.direction == "in" or p.direction == "power":
            ins.append(tok)
        elif p.direction == "out":
            outs.append(tok)
        else:
            ios.append(tok)
    out = [f"`{s.name}` — {s.description}"]
    if ins:
        out.append(f"    entradas: {', '.join(ins)}")
    if ios:
        out.append(f"    bidireccional (bus tri-estado): {', '.join(ios)}")
    if outs:
        out.append(f"    salidas: {', '.join(outs)}")
    if include_props and s.props:
        props = []
        for p in s.props:
            d = f"={p.default!r}" if p.default is not None else ""
            ch = f" ∈ {{{', '.join(str(c) for c in p.choices)}}}" if p.choices else ""
            props.append(f"{p.name}{d}{ch}")
        out.append(f"    props: {', '.join(props)}")
    return "\n".join(out)


def _pin_token(p) -> str:
    name = p.display_name()
    if p.width > 1:
        return f"{name}[0:{p.width - 1}]"
    return name


DSL_SPEC = """
# Gramática del DSL

Un circuito es un documento YAML con estas claves de nivel superior (y SOLO
estas): `name`, `title`, `meta`, `simulation`, `parts`, `nets`, `buses`.

```yaml
name: identificador_sin_espacios        # obligatorio
title: "Título legible"                 # opcional
meta:                                   # opcional
  author: ...
  description: >
    Texto libre explicando qué hace el circuito y por qué está cableado así.
simulation:                             # opcional
  timescale: 1us                        # resolución temporal
  duration: 10ms                        # cuánto simular por defecto

parts:
  - ref: U1                             # OBLIGATORIO y único en el circuito
    part: 74LS193                       # OBLIGATORIO, del catálogo
    at: [200, 80]                       # posición en unidades (1 unidad = 10 px)
    rotation: 0                         # 0 | 90 | 180 | 270 (opcional)
    props:                              # propiedades del componente (opcional)
      FREQ: 1000

nets:
  # Cable simple
  - name: CLK
    connect: [CLK1.Q, U1.CPU]

  # Bus de N bits: genera N cables llamados NOMBRE[0]..NOMBRE[N-1]
  - name: DATA
    bus: 8
    connect: [U1.D[0:7], U2.D[0:7]]

  # Varios pines atados al mismo nivel fijo (muy usado para deshabilitar)
  - name: DISABLED
    tie: 1
    connect: [U1./OE, U2./MR]
```

## Sintaxis de los extremos de `connect`

| Forma            | Significado                                    |
|------------------|------------------------------------------------|
| `U1.A`           | pin simple                                     |
| `U1.Q[3]`        | un bit concreto de un pin vectorial            |
| `U1.Q[0:7]`      | rango INCLUSIVO; solo dentro de un net con `bus` |
| `U1.Q[*]`        | todos los bits del pin                         |
| `U1./MR`         | pin activo en baja: la barra es parte del nombre |
| `$0` `$1` `$X` `$Z` | atado a un nivel lógico constante           |

## Reglas que el validador va a comprobar

1. Todo `ref` es único. Todo `part` existe en el catálogo.
2. Todo pin referenciado existe en ese componente, con ese nombre exacto,
   incluida la barra inicial si es activo en baja.
3. Un pin no puede estar en dos nets distintos.
4. **Dos salidas push-pull nunca se conectan al mismo net.** Para compartir un
   cable entre varios dispositivos se usan salidas tri-estado (`INOUT`), que
   sueltan el bus en Z cuando no les toca.
5. Un net llamado `GND`, `VCC`, `GND$` o `VCC$` se ata automáticamente a nivel
   0 o 1 respectivamente; no hace falta añadir un símbolo de tierra.
6. En un net con `bus: N`, cada extremo debe aportar exactamente N bits.
   Excepción: un pin de 1 bit se replica en los N cables (útil para un enable
   compartido), y el validador lo avisa.
7. Los pines vectoriales se indexan LSB primero: `Q[0]` es el bit menos
   significativo.
8. Toda entrada debería estar conectada. Si un componente tiene entradas que el
   circuito no usa, **atálas explícitamente** al nivel inactivo con `tie`
   (p. ej. un `/MR` activo en baja se ata a 1 para deshabilitar el reset).
   Dejarlas al aire hace que lean X y el circuito entero se vuelva
   indeterminado.
9. Las posiciones `at` deben evitar que los símbolos se solapen. Deja al menos
   12 unidades libres alrededor de cada componente. Si no las indicas, el
   motor las coloca automáticamente en rejilla.

## Semántica de la simulación

Lógica de cuatro valores: `0`, `1`, `X` (indeterminado), `Z` (alta
impedancia). Un cable sin driver lee `X`, no 0. Un flip-flop sin resetear
empieza en `X`. Las colisiones (un 0 y un 1 en el mismo cable) dan `X` y el
motor las reporta como problema.

Los elementos secuenciales (flip-flops, contadores, registros) disparan por
flanco, y el motor los evalúa una sola vez por evento para que no cuenten de
más.
"""

EXAMPLES = """
# Ejemplos de referencia

## Ejemplo mínimo: NAND con dos entradas manuales y un LED

```yaml
name: compuerta_nand
title: "NAND de 2 entradas con indicadores"
simulation: {timescale: 1ns, duration: 1us}

parts:
  - {ref: S1, part: LOGICSTATE, at: [40, 60],  props: {STATE: "0"}}
  - {ref: S2, part: LOGICSTATE, at: [40, 140], props: {STATE: "0"}}
  - {ref: U1, part: 74LS00,     at: [200, 90]}
  - {ref: R1, part: RES,        at: [340, 90], props: {R: 330}}
  - {ref: D1, part: LED,        at: [440, 90], props: {COLOR: ROJO}}
  - {ref: GND1, part: GND,      at: [520, 160]}

nets:
  - {name: A,     connect: [S1.Q, U1.A0]}
  - {name: B,     connect: [S2.Q, U1.A1]}
  - {name: Y,     connect: [U1.Y, R1.1]}
  - {name: LED_A, connect: [R1.2, D1.A]}
  - {name: GND,   connect: [D1.K, GND1.P]}
```

Fíjate en por qué funciona: la resistencia es bidireccional y deja pasar el
nivel de `Y` hasta el ánodo del LED, y el cátodo va a tierra. El LED enciende
cuando la NAND saca 1.

## Ejemplo con bus y decodificador: contador de 4 bits con display

```yaml
name: contador_7seg
title: "Contador de 4 bits con display de 7 segmentos"
simulation: {timescale: 1us, duration: 20ms}

parts:
  - {ref: CLK1,  part: CLOCK,   at: [30, 120], props: {FREQ: 1000, DUTY: 50}}
  - {ref: U1,    part: 74LS193, at: [200, 80]}
  - {ref: U2,    part: 74LS48,  at: [420, 80]}
  - {ref: DISP1, part: 7SEG-CC, at: [620, 60], props: {COLOR: ROJO}}
  - {ref: GND1,  part: GND,     at: [660, 300]}

nets:
  - {name: CLK, connect: [CLK1.Q, U1.CPU]}
  - {name: Q,   bus: 4, connect: [U1.Q[0:3], U2.A[0:3]]}
  - {name: SEG, bus: 7, connect: [U2.a[0:6], DISP1.a[0:6]]}
  # /MR y /PL activos en baja -> atados a 1 para deshabilitarlos.
  # CPD alto -> el contador solo cuenta hacia arriba.
  - {name: CTRL_CNT, tie: 1, connect: [U1./MR, U1.CPD, U1./PL]}
  - {name: P_BUS, bus: 4, tie: 0, connect: [U1.P[0:3]]}
  - {name: CTRL_DEC, tie: 1, connect: [U2./LT, U2./RBI, U2./BI]}
  - {name: GND, connect: [GND1.P, DISP1.CC, DISP1.dp]}
```

El detalle que más se olvida: el 74LS48 tiene salidas activas en ALTA y va con
un display de cátodo común (`7SEG-CC`). El 74LS47 es el gemelo con salidas
activas en BAJA y pide ánodo común. Mezclarlos y el display muestra basura.

## Ejemplo con bus compartido tri-estado

```yaml
nets:
  - name: DBUS
    bus: 8
    connect: [U_ROM.D[0:7], U_RAM.D[0:7], U_ALU.B[0:7]]
```

Los tres dispositivos tienen `D` bidireccional. Mientras solo uno esté
habilitado, los otros dos sueltan el bus (Z) y no hay conflicto. Si dos
escriben a la vez con niveles distintos, el motor reporta colisión.
"""


def system_prompt(library: Optional[Library] = None,
                  style: str = "digital",
                  include_examples: bool = True) -> str:
    """El prompt de sistema completo: rol + gramática + catálogo + ejemplos."""
    parts = [
        ROLE,
        DSL_SPEC,
    ]
    if include_examples:
        parts.append(EXAMPLES)
    parts.append("# Catálogo de componentes disponibles\n")
    parts.append(
        "Usa SOLO estos componentes, con estos nombres de pin exactos. "
        "Si necesitas algo que no está, aproxima con lo más cercano y "
        "explícalo en `meta.description`.\n"
    )
    parts.append(catalog(library))
    parts.append(OUTPUT_RULES)
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


ROLE = """
Eres un ingeniero de diseño digital que traduce descripciones en lenguaje
natural a esquemáticos completos y funcionales.

Tu salida no es una explicación ni un tutorial paso a paso: es **el documento
YAML completo del circuito**, listo para que un motor lo valide, lo dibuje y lo
simule sin intervención humana. Nadie va a colocar componentes a mano siguiendo
tus instrucciones; el motor lo hace todo a partir de tu YAML.

Trabajas así:

1. Identifica la función pedida y descomponla en bloques (entrada → lógica →
   salida).
2. Elige componentes del catálogo. Respeta los nombres de pines al carácter,
   incluidas las barras de los activos en baja.
3. Cablea TODO, incluidas las entradas que el diseño no usa: van atadas a su
   nivel inactivo con `tie`. Una entrada al aire lee X y contamina el circuito.
4. Comprueba mentalmente que no hay dos salidas push-pull en el mismo cable.
5. Elige posiciones `at` que no solapen los símbolos.
6. Devuelve SOLO el YAML, en un único bloque de código.
""".strip()

OUTPUT_RULES = """
# Formato de salida

Devuelve **únicamente** un bloque de código YAML, sin prosa antes ni después:

```yaml
name: ...
...
```

No expliques el circuito fuera del YAML: para eso está `meta.description`, que
debe ser generosa (qué hace, por qué elegiste esos componentes, qué trampas
evitaste). El motor extrae el bloque ```yaml``` automáticamente, así que un
párrafo de más no rompe nada, pero ensucia la salida.

Si el pedido es ambiguo o imposible con el catálogo disponible, **no te
quedes callado ni preguntes**: entrega el circuito más razonable y documenta la
suposición en `meta.description`.
""".strip()


def user_prompt(description: str, extra: Optional[str] = None) -> str:
    """El mensaje del usuario: la descripción del problema."""
    out = [description.strip()]
    if extra:
        out.append("\nDatos y restricciones adicionales:\n" + extra.strip())
    out.append("\nGenera el YAML del circuito completo.")
    return "\n".join(out)


def repair_prompt(bad_yaml: str, errors: Sequence[str]) -> str:
    """Prompt de autocorrección: se le devuelven los errores del validador.

    Este es el mecanismo que hace fiable la generación con IA. El validador de
    Voltio produce mensajes pensados para ser leídos por un modelo
    (``"U2 no tiene el pin '/a'. ¿Quisiste decir ['a']?"``), y se le piden
    tantas correcciones como haga falta hasta que el circuito carga limpio.
    """
    lines = [
        "El YAML que generaste no pasa la validación. Corrígelo.",
        "",
        "Errores:",
    ]
    lines += [f"  - {e}" for e in errors]
    lines += [
        "",
        "YAML que falló:",
        "```yaml",
        bad_yaml.strip(),
        "```",
        "",
        "Devuelve el YAML COMPLETO corregido (no un parche, no un diff), "
        "en un único bloque de código.",
    ]
    return "\n".join(lines)


def reverse_prompt(dsl_yaml: str, question: str = "") -> str:
    """Prompt del sentido inverso: circuito → explicación en lenguaje natural."""
    return (
        "Este es el DSL de un circuito. Descríbelo en lenguaje natural: qué "
        "hace, cómo está organizado en bloques, qué hace cada componente y "
        "cómo se propagan las señales. Señala también cualquier cosa que te "
        "parezca sospechosa o mejorable.\n\n"
        + (f"Pregunta concreta del usuario: {question}\n\n" if question else "")
        + "```yaml\n" + dsl_yaml.strip() + "\n```"
    )
