"""Símbolos esquemáticos vectoriales.

Cada función devuelve una lista de trozos SVG en **coordenadas locales** de la
parte, con el origen en la esquina superior izquierda del cuerpo del símbolo y
el eje Y hacia abajo. El renderizador se encarga de rotar, trasladar y agrupar.

Las unidades son "unidades de esquema" (u); el render las multiplica por su
escala. Un paso de rejilla son 10 u.

La convención de pines es la misma que en :mod:`voltio.parts.spec`: cada lado
(left/right/top/bottom) reparte sus pines uniformemente.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

__all__ = ["pin_anchors", "draw_symbol", "BODY"]

#: Tamaño por defecto del cuerpo de un símbolo, si la parte no declara otro.
BODY = (40.0, 30.0)

#: Longitud del rabito de cable que sale de cada patilla hacia afuera.
PIN_STUB = 12.0


def pin_anchors(width: float, height: float, spec) -> Dict[str, List[Tuple[float, float]]]:
    """Posición de cada patilla en coordenadas locales.

    Devuelve ``{nombre_de_pin: [(x, y), ...]}`` con una entrada por bit en los
    pines vectoriales. Los pines se reparten a partes iguales por su lado.
    """
    sides: Dict[str, List] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in spec.pins:
        sides.get(p.side, sides["left"]).append(p)

    out: Dict[str, List[Tuple[float, float]]] = {}
    for side, pins in sides.items():
        pins = sorted(pins, key=lambda p: p.order)
        n = len(pins)
        if n == 0:
            continue
        for i, p in enumerate(pins):
            frac = (i + 0.5) / n
            bits = max(p.width, 1)
            pts: List[Tuple[float, float]] = []
            for b in range(bits):
                if side == "left":
                    # Los bits de un pin vectorial van de abajo (LSB) hacia arriba.
                    sub = (b + 0.5) / bits if bits > 1 else 0.5
                    y = height * (frac + (sub - 0.5) * (1.0 / n) * 0.8)
                    pts.append((0.0, y))
                elif side == "right":
                    sub = (b + 0.5) / bits if bits > 1 else 0.5
                    y = height * (frac + (sub - 0.5) * (1.0 / n) * 0.8)
                    pts.append((width, y))
                elif side == "top":
                    sub = (b + 0.5) / bits if bits > 1 else 0.5
                    x = width * (frac + (sub - 0.5) * (1.0 / n) * 0.8)
                    pts.append((x, 0.0))
                else:  # bottom
                    sub = (b + 0.5) / bits if bits > 1 else 0.5
                    x = width * (frac + (sub - 0.5) * (1.0 / n) * 0.8)
                    pts.append((x, height))
            out[p.name] = pts
    return out


def _stub_points(side: str, x: float, y: float) -> Tuple[float, float]:
    """Extremo exterior del rabito de una patilla."""
    if side == "left":
        return (x - PIN_STUB, y)
    if side == "right":
        return (x + PIN_STUB, y)
    if side == "top":
        return (x, y - PIN_STUB)
    return (x, y + PIN_STUB)


# ---------------------------------------------------------------------------
# Cuerpos de símbolo
# ---------------------------------------------------------------------------


def _gate_body(w: float, h: float, kind: str, n_inputs: int) -> List[str]:
    """AND / OR / NAND / NOR / XOR con la forma IEC-ish clásica."""
    out: List[str] = []
    r = h / 2.0
    if kind in ("and", "nand"):
        body_w = w - r
        out.append(
            f'<path d="M0,0 L{body_w:.1f},0 A{r:.1f},{r:.1f} 0 0 1 {body_w:.1f},{h:.1f} '
            f'L0,{h:.1f} Z" class="body"/>'
        )
    elif kind in ("or", "nor"):
        out.append(
            f'<path d="M0,0 Q{w * 0.35:.1f},{h / 2:.1f} 0,{h:.1f} '
            f'Q{w * 0.6:.1f},{h:.1f} {w:.1f},{h / 2:.1f} '
            f'Q{w * 0.6:.1f},0 0,0 Z" class="body"/>'
        )
    elif kind in ("xor", "xnor"):
        out.append(
            f'<path d="M{w * 0.12:.1f},0 Q{w * 0.47:.1f},{h / 2:.1f} {w * 0.12:.1f},{h:.1f} '
            f'Q{w * 0.72:.1f},{h:.1f} {w:.1f},{h / 2:.1f} '
            f'Q{w * 0.72:.1f},0 {w * 0.12:.1f},0 Z" class="body"/>'
        )
        out.append(
            f'<path d="M0,0 Q{w * 0.35:.1f},{h / 2:.1f} 0,{h:.1f}" class="wire-only"/>'
        )
    else:  # buffer
        out.append(f'<path d="M0,0 L{w:.1f},{h / 2:.1f} L0,{h:.1f} Z" class="body"/>')

    if kind in ("nand", "nor", "xnor", "not"):
        cx = w + 3.0 if kind != "not" else w * 0.8 + 3.0
        out.append(f'<circle cx="{cx:.1f}" cy="{h / 2:.1f}" r="3" class="wire-only"/>')
    return out


def _inverter_body(w: float, h: float) -> List[str]:
    return [
        f'<path d="M0,0 L{w * 0.75:.1f},{h / 2:.1f} L0,{h:.1f} Z" class="body"/>',
        f'<circle cx="{w * 0.75 + 3.5:.1f}" cy="{h / 2:.1f}" r="3.5" class="wire-only"/>',
    ]


def _tribuf_body(w: float, h: float) -> List[str]:
    return [
        f'<path d="M0,0 L{w:.1f},{h / 2:.1f} L0,{h:.1f} Z" class="body"/>',
        f'<line x1="{w * 0.3:.1f}" y1="{h:.1f}" x2="{w * 0.3:.1f}" y2="{h * 0.55:.1f}" class="wire-only"/>',
    ]


def _ic_body(w: float, h: float) -> List[str]:
    return [f'<rect x="0" y="0" width="{w:.1f}" height="{h:.1f}" class="body"/>']


def _resistor_body(w: float, h: float) -> List[str]:
    """Resistencia en zigzag, estilo americano."""
    mid = h / 2.0
    seg = (w - 12.0) / 6.0
    pts = [(0.0, mid), (6.0, mid)]
    for i in range(6):
        x = 6.0 + seg * (i + 0.5)
        y = mid + (-5.0 if i % 2 == 0 else 5.0)
        pts.append((x, y))
    pts += [(w - 6.0, mid), (w, mid)]
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    return [f'<path d="{d}" class="wire-only"/>']


def _led_body(w: float, h: float) -> List[str]:
    mid = h / 2.0
    tri = w * 0.5
    return [
        f'<line x1="0" y1="{mid:.1f}" x2="{tri:.1f}" y2="{mid:.1f}" class="wire-only"/>',
        f'<path d="M{tri:.1f},{mid - 6:.1f} L{tri + 10:.1f},{mid:.1f} L{tri:.1f},{mid + 6:.1f} Z" class="body"/>',
        f'<line x1="{tri + 10:.1f}" y1="{mid - 6:.1f}" x2="{tri + 10:.1f}" y2="{mid + 6:.1f}" class="wire-only"/>',
        f'<line x1="{tri + 10:.1f}" y1="{mid:.1f}" x2="{w:.1f}" y2="{mid:.1f}" class="wire-only"/>',
        # flechitas de emisión
        f'<path d="M{tri + 6:.1f},{mid - 8:.1f} l5,-5 m-5,0 l0,3 m0,-3 l-3,0" class="emit"/>',
        f'<path d="M{tri + 10:.1f},{mid - 5:.1f} l5,-5 m-5,0 l0,3 m0,-3 l-3,0" class="emit"/>',
    ]


_SEVEN_LAYOUT = {
    #  segmento: (x1, y1, x2, y2) en fracciones del cuerpo
    "a": (0.22, 0.10, 0.78, 0.10),
    "b": (0.80, 0.13, 0.80, 0.46),
    "c": (0.80, 0.54, 0.80, 0.87),
    "d": (0.22, 0.90, 0.78, 0.90),
    "e": (0.20, 0.54, 0.20, 0.87),
    "f": (0.20, 0.13, 0.20, 0.46),
    "g": (0.22, 0.50, 0.78, 0.50),
}


def _seven_seg_body(w: float, h: float) -> List[str]:
    out = [f'<rect x="0" y="0" width="{w:.1f}" height="{h:.1f}" class="body"/>']
    for name, (x1, y1, x2, y2) in _SEVEN_LAYOUT.items():
        out.append(
            f'<line data-seg="{name}" x1="{w * x1:.1f}" y1="{h * y1:.1f}" '
            f'x2="{w * x2:.1f}" y2="{h * y2:.1f}" class="seg"/>'
        )
    out.append(f'<circle data-seg="dp" cx="{w * 0.9:.1f}" cy="{h * 0.9:.1f}" r="2.5" class="seg"/>')
    return out


def _lcd_body(w: float, h: float) -> List[str]:
    return [
        f'<rect x="0" y="0" width="{w:.1f}" height="{h:.1f}" class="body"/>',
        f'<rect x="{w * 0.07:.1f}" y="{h * 0.15:.1f}" width="{w * 0.86:.1f}" '
        f'height="{h * 0.7:.1f}" class="lcd-screen"/>',
    ]


def _switch_body(w: float, h: float) -> List[str]:
    mid = h / 2.0
    return [
        f'<line x1="0" y1="{mid:.1f}" x2="{w * 0.35:.1f}" y2="{mid:.1f}" class="wire-only"/>',
        f'<circle cx="{w * 0.35:.1f}" cy="{mid:.1f}" r="2.5" class="contact"/>',
        f'<circle cx="{w * 0.72:.1f}" cy="{mid:.1f}" r="2.5" class="contact"/>',
        f'<line data-blade="1" x1="{w * 0.35:.1f}" y1="{mid:.1f}" x2="{w * 0.72:.1f}" '
        f'y2="{mid - 7:.1f}" class="wire-only"/>',
        f'<line x1="{w * 0.72:.1f}" y1="{mid:.1f}" x2="{w:.1f}" y2="{mid:.1f}" class="wire-only"/>',
    ]


def _gnd_body(w: float, h: float) -> List[str]:
    mid = w / 2.0
    return [
        f'<line x1="{mid:.1f}" y1="0" x2="{mid:.1f}" y2="{h * 0.45:.1f}" class="wire-only"/>',
        f'<line x1="{mid - 10:.1f}" y1="{h * 0.45:.1f}" x2="{mid + 10:.1f}" y2="{h * 0.45:.1f}" class="wire-only"/>',
        f'<line x1="{mid - 6:.1f}" y1="{h * 0.68:.1f}" x2="{mid + 6:.1f}" y2="{h * 0.68:.1f}" class="wire-only"/>',
        f'<line x1="{mid - 2.5:.1f}" y1="{h * 0.9:.1f}" x2="{mid + 2.5:.1f}" y2="{h * 0.9:.1f}" class="wire-only"/>',
    ]


def _vcc_body(w: float, h: float) -> List[str]:
    mid = w / 2.0
    return [
        f'<line x1="{mid:.1f}" y1="{h:.1f}" x2="{mid:.1f}" y2="{h * 0.35:.1f}" class="wire-only"/>',
        f'<path d="M{mid - 8:.1f},{h * 0.35:.1f} L{mid:.1f},{h * 0.05:.1f} L{mid + 8:.1f},{h * 0.35:.1f}" class="wire-only"/>',
    ]


def _clock_body(w: float, h: float) -> List[str]:
    mid = h / 2.0
    return [
        f'<rect x="0" y="0" width="{w * 0.75:.1f}" height="{h:.1f}" class="body"/>',
        f'<path d="M{w * 0.1:.1f},{mid + 5:.1f} L{w * 0.22:.1f},{mid + 5:.1f} '
        f'L{w * 0.22:.1f},{mid - 5:.1f} L{w * 0.4:.1f},{mid - 5:.1f} '
        f'L{w * 0.4:.1f},{mid + 5:.1f} L{w * 0.55:.1f},{mid + 5:.1f}" class="wire-only"/>',
        f'<line x1="{w * 0.75:.1f}" y1="{mid:.1f}" x2="{w:.1f}" y2="{mid:.1f}" class="wire-only"/>',
    ]


def _logicstate_body(w: float, h: float) -> List[str]:
    mid = h / 2.0
    return [
        f'<rect x="0" y="0" width="{w * 0.7:.1f}" height="{h:.1f}" class="body"/>',
        f'<text data-role="state" x="{w * 0.35:.1f}" y="{mid + 4:.1f}" class="state-text">0</text>',
        f'<line x1="{w * 0.7:.1f}" y1="{mid:.1f}" x2="{w:.1f}" y2="{mid:.1f}" class="wire-only"/>',
    ]


def _probe_body(w: float, h: float) -> List[str]:
    mid = h / 2.0
    return [
        f'<line x1="0" y1="{mid:.1f}" x2="{w * 0.3:.1f}" y2="{mid:.1f}" class="wire-only"/>',
        f'<circle cx="{w * 0.55:.1f}" cy="{mid:.1f}" r="{h * 0.42:.1f}" class="body"/>',
        f'<text data-role="probe" x="{w * 0.55:.1f}" y="{mid + 3.5:.1f}" class="probe-text">?</text>',
    ]


def _terminal_body(w: float, h: float) -> List[str]:
    mid = h / 2.0
    return [
        f'<line x1="0" y1="{mid:.1f}" x2="{w * 0.35:.1f}" y2="{mid:.1f}" class="wire-only"/>',
        f'<circle cx="{w * 0.4:.1f}" cy="{mid:.1f}" r="3" class="contact"/>',
    ]


def _motor_body(w: float, h: float) -> List[str]:
    return [f'<circle cx="{w / 2:.1f}" cy="{h / 2:.1f}" r="{min(w, h) / 2 - 2:.1f}" class="body"/>',
            f'<text x="{w / 2:.1f}" y="{h / 2 + 4:.1f}" class="symbol-letter">M</text>']


def _buzzer_body(w: float, h: float) -> List[str]:
    return [f'<path d="M0,0 L{w * 0.5:.1f},{-h * 0.15:.1f} L{w * 0.5:.1f},{h * 1.15:.1f} L0,{h:.1f} Z" class="body"/>',
            f'<line x1="{w * 0.5:.1f}" y1="{h / 2:.1f}" x2="{w:.1f}" y2="{h / 2:.1f}" class="wire-only"/>']


def _flipflop_body(w: float, h: float) -> List[str]:
    out = [f'<rect x="0" y="0" width="{w:.1f}" height="{h:.1f}" class="body"/>']
    # La muesca de reloj en el lado izquierdo.
    out.append(
        f'<path data-role="clknotch" d="M0,{h * 0.55:.1f} l5,-4 l-5,-4" class="wire-only"/>'
    )
    return out


_DRAWERS = {
    "gate2": lambda w, h, spec: _gate_body(w, h, _gate_kind(spec), _n_inputs(spec)),
    "inverter": _inverter_body,
    "buffer": lambda w, h, spec: _gate_body(w, h, "buffer", 1),
    "tribuf": _tribuf_body,
    "ic": _ic_body,
    "flipflop": _flipflop_body,
    "register": _ic_body,
    "counter": _ic_body,
    "memory": _ic_body,
    "mcu": _ic_body,
    "resistor": _resistor_body,
    "led": _led_body,
    "7seg": _seven_seg_body,
    "lcd": _lcd_body,
    "switch": _switch_body,
    "pushbutton": _switch_body,
    "clock": _clock_body,
    "logicstate": _logicstate_body,
    "vcc": _vcc_body,
    "gnd": _gnd_body,
    "probe": _probe_body,
    "terminal": _terminal_body,
    "motor": _motor_body,
    "buzzer": _buzzer_body,
    "battery": _vcc_body,
}


def _gate_kind(spec) -> str:
    m = (spec.model or "").lower()
    for k in ("nand", "nor", "xnor", "and", "or", "xor"):
        if m.startswith(k):
            return k
    return "and"


def _n_inputs(spec) -> int:
    return len([p for p in spec.pins if p.name.startswith("A") and p.direction == "in"])


def draw_symbol(symbol: str, w: float, h: float, spec=None) -> List[str]:
    """Devuelve los trozos SVG del cuerpo de un símbolo."""
    fn = _DRAWERS.get(symbol, _ic_body)
    if fn in (_ic_body, _resistor_body, _led_body, _seven_seg_body, _lcd_body,
              _switch_body, _gnd_body, _vcc_body, _clock_body, _logicstate_body,
              _probe_body, _terminal_body, _motor_body, _buzzer_body,
              _inverter_body, _tribuf_body, _flipflop_body):
        return fn(w, h)
    return fn(w, h, spec)
