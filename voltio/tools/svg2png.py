"""Rasterizador SVG → PNG para poder MIRAR lo que generamos.

Existe por una razón muy concreta: el autor de este código (una IA) no puede
ver SVG. ``read_file`` renderiza imágenes raster, no vectoriales. Así que sin
esta herramienta la única validación posible del dibujante sería estructural
("hay 23 polilíneas"), que no detecta ni un símbolo encima de otro, ni una
etiqueta ilegible, ni un cable que no conecta con nada.

No es un motor SVG general: cubre el subconjunto que emite
:mod:`voltio.render.svg` (que además es fijo y conocido), con transformaciones
``translate``/``rotate``/``scale``, los elementos ``line``, ``polyline``,
``rect``, ``circle``, ``ellipse`` y ``path`` con comandos M/L/A/Q/Z/H/V, y
``text`` con alineación.

Uso::

    python3 tools/svg2png.py entrada.svg salida.png [--scale 0.5] [--max 2000]
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# -- fuentes ----------------------------------------------------------------
_CANDIDATOS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/app/node_modules/pdfjs-dist/standard_fonts/LiberationSans-Regular.ttf",
    "/opt/arena-python/lib/site-packages/reportlab/fonts/Vera.ttf",
    "/opt/arena-python/lib/python3.11/site-packages/reportlab/fonts/Vera.ttf",
]
_FONT_PATH: Optional[str] = next((p for p in _CANDIDATOS if os.path.exists(p)), None)


def _font(size: float, bold: bool = False, mono: bool = False):
    size = max(int(round(size)), 1)
    if _FONT_PATH:
        try:
            if bold:
                alt = _FONT_PATH.replace("Regular", "Bold").replace("Vera.ttf", "VeraBd.ttf")
                if os.path.exists(alt):
                    return ImageFont.truetype(alt, size)
            return ImageFont.truetype(_FONT_PATH, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # PIL antigua
        return ImageFont.load_default()


# -- parseo mínimo de XML ---------------------------------------------------
_TAG = re.compile(r"<(/?)([A-Za-z_][\w:.-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>", re.S)
_ATTR = re.compile(r"([A-Za-z_:][\w:.-]*)\s*=\s*(\"([^\"]*)\"|'([^']*)')")


class _Node:
    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent=None):
        self.tag = tag
        self.attrs = attrs
        self.children: List[Any] = []
        self.text = ""
        self.parent = parent

    def get(self, k: str, d=None):
        return self.attrs.get(k, d)

    def num(self, k: str, d: float = 0.0) -> float:
        try:
            return float(self.attrs.get(k, d))
        except (TypeError, ValueError):
            return d

    def walk(self):
        for ch in self.children:
            if isinstance(ch, _Node):
                yield ch
                yield from ch.walk()


def parse_xml(src: str) -> _Node:
    """Parser XML mínimo suficiente para SVG generado por máquina."""
    root = _Node("#root", {})
    stack = [root]
    pos = 0
    for m in _TAG.finditer(src):
        closing, tag, attrtext, selfclose = m.groups()
        # Texto entre etiquetas (solo interesa el de <text>).
        entre = src[pos:m.start()]
        if entre.strip() and stack:
            stack[-1].text += _unesc(entre)
        pos = m.end()
        if tag in ("style", "defs", "pattern", "title", "desc", "metadata"):
            if not closing and not selfclose:
                # Saltarse el contenido hasta el cierre.
                fin = src.find(f"</{tag}", pos)
                if fin != -1:
                    cierre = src.find(">", fin)
                    pos = cierre + 1 if cierre != -1 else fin
            continue
        if closing:
            if len(stack) > 1:
                stack.pop()
            continue
        attrs = {}
        for am in _ATTR.finditer(attrtext):
            attrs[am.group(1)] = _unesc(am.group(3) if am.group(3) is not None else am.group(4))
        node = _Node(tag, attrs, stack[-1])
        stack[-1].children.append(node)
        if not selfclose:
            stack.append(node)
    return root


def _unesc(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&#39;", "'").replace("&amp;", "&"))


# -- geometría --------------------------------------------------------------


def parse_transform(s: str) -> List[Tuple]:
    out = []
    for m in re.finditer(r"(\w+)\s*\(([^)]*)\)", s or ""):
        fn = m.group(1)
        nums = [float(x) for x in re.split(r"[\s,]+", m.group(2).strip()) if x]
        out.append((fn, nums))
    return out


def apply_ops(pt: Tuple[float, float], ops: List[Tuple]) -> Tuple[float, float]:
    """Aplica la lista de transformaciones. Se recorren al revés: en SVG la
    última declarada es la que se aplica primero al punto."""
    x, y = pt
    for fn, n in reversed(ops):
        if fn == "translate":
            x += n[0]
            y += n[1] if len(n) > 1 else 0.0
        elif fn == "scale":
            sx = n[0]
            sy = n[1] if len(n) > 1 else sx
            x, y = x * sx, y * sy
        elif fn == "rotate":
            a = math.radians(n[0])
            cx = n[1] if len(n) > 2 else 0.0
            cy = n[2] if len(n) > 2 else 0.0
            x, y = x - cx, y - cy
            x, y = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
            x, y = x + cx, y + cy
        elif fn == "matrix" and len(n) == 6:
            a, b, c, d, e, f = n
            x, y = a * x + c * y + e, b * x + d * y + f
    return x, y


def path_points(d: str) -> List[List[Tuple[float, float]]]:
    """Convierte un atributo ``d`` en polilíneas. Las curvas se teselan."""
    subpaths: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    x = y = 0.0
    sx = sy = 0.0
    toks = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.?\d+(?:[eE][-+]?\d+)?)", d)
    i = 0
    cmd = None

    def num():
        nonlocal i
        while i < len(toks) and re.match(r"[A-Za-z]", toks[i]):
            i += 1
        v = float(toks[i]) if i < len(toks) else 0.0
        i += 1
        return v

    while i < len(toks):
        t = toks[i]
        if re.match(r"[A-Za-z]", t):
            cmd = t
            i += 1
        if cmd is None:
            break
        c = cmd
        if c in ("M", "m"):
            if cur:
                subpaths.append(cur)
            px, py = num(), num()
            x, y = (x + px, y + py) if c == "m" else (px, py)
            sx, sy = x, y
            cur = [(x, y)]
            cmd = "L" if c == "M" else "l"
        elif c in ("L", "l"):
            px, py = num(), num()
            x, y = (x + px, y + py) if c == "l" else (px, py)
            cur.append((x, y))
        elif c in ("H", "h"):
            px = num()
            x = x + px if c == "h" else px
            cur.append((x, y))
        elif c in ("V", "v"):
            py = num()
            y = y + py if c == "v" else py
            cur.append((x, y))
        elif c in ("Q", "q", "C", "c", "T", "t", "S", "s"):
            # Teselado simple: se muestrea la curva. Suficiente para ver el dibujo.
            npts = 16
            x0, y0 = x, y
            if c in ("Q", "q"):
                cx1, cy1 = num(), num()
                cx2, cy2 = num(), num()
                if c == "q":
                    cx1 += x0; cy1 += y0; cx2 += x0; cy2 += y0
                pts = [_bez3(x0, y0, (x0 + 2 * cx1) / 3, (y0 + 2 * cy1) / 3,
                             (cx2 + 2 * cx1) / 3, (cy2 + 2 * cy1) / 3, cx2, cy2, k / npts)
                       for k in range(npts + 1)]
                x, y = cx2, cy2
            elif c in ("C", "c"):
                a1, a2 = num(), num()
                b1, b2 = num(), num()
                e1, e2 = num(), num()
                if c == "c":
                    a1 += x0; a2 += y0; b1 += x0; b2 += y0; e1 += x0; e2 += y0
                pts = [_bez3(x0, y0, a1, a2, b1, b2, e1, e2, k / npts)
                       for k in range(npts + 1)]
                x, y = e1, e2
            else:
                pts = [(x, y)]
            cur.extend(pts)
        elif c in ("A", "a"):
            rx, ry = num(), num()
            num()  # rotación del eje
            large = num()
            sweep = num()
            ex, ey = num(), num()
            if c == "a":
                ex += x; ey += y
            cur.extend(_arc(x, y, rx, ry, large, sweep, ex, ey))
            x, y = ex, ey
        elif c in ("Z", "z"):
            cur.append((sx, sy))
            subpaths.append(cur)
            cur = []
            x, y = sx, sy
        else:
            i += 1
    if cur:
        subpaths.append(cur)
    return subpaths


def _bez3(x0, y0, x1, y1, x2, y2, x3, y3, t) -> Tuple[float, float]:
    u = 1 - t
    return (u**3 * x0 + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * x3,
            u**3 * y0 + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * y3)


def _arc(x0, y0, rx, ry, large, sweep, x1, y1, n: int = 24) -> List[Tuple[float, float]]:
    """Aproxima un arco elíptico SVG por una polilínea."""
    if rx == 0 or ry == 0:
        return [(x1, y1)]
    rx, ry = abs(rx), abs(ry)
    dx, dy = (x0 - x1) / 2, (y0 - y1) / 2
    lam = (dx * dx) / (rx * rx) + (dy * dy) / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = max(0.0, (rx*rx*ry*ry - rx*rx*dy*dy - ry*ry*dx*dx) /
              max(1e-12, rx*rx*dy*dy + ry*ry*dx*dx))
    co = math.sqrt(num)
    if large == sweep:
        co = -co
    cx = co * rx * dy / ry
    cy = -co * ry * dx / rx
    hx, hy = (x0 + x1) / 2, (y0 + y1) / 2
    cx += hx; cy += hy

    def ang(ux, uy, vx, vy):
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        if d == 0:
            return 0.0
        c = max(-1.0, min(1.0, (ux * vx + uy * vy) / d))
        a = math.acos(c)
        return -a if (ux * vy - uy * vx) < 0 else a

    a0 = ang(1, 0, (x0 - cx) / rx, (y0 - cy) / ry)
    delta = ang((x0 - cx) / rx, (y0 - cy) / ry, (x1 - cx) / rx, (y1 - cy) / ry)
    # `delta` es el ángulo RECORRIDO, no el ángulo final. Interpolar entre a0 y
    # delta como si delta fuera absoluto dibuja arcos de más: una semicircunfe-
    # rencia salía de 270° y las compuertas parecían un Pac-Man. Ese bug estaba
    # en el rasterizador, no en el SVG: en un navegador el arco siempre fue
    # correcto. Moraleja: cuando te construyes unos ojos, también hay que
    # calibrarlos.
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    pts = []
    for k in range(n + 1):
        a = a0 + delta * k / n
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


# -- color y estilo ---------------------------------------------------------


def parse_color(s: Optional[str], default=(0, 0,0)) -> Optional[Tuple[int, int, int]]:
    if not s:
        return None
    s = s.strip()
    if s in ("none", "transparent"):
        return None
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) >= 6:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.match(r"rgba?\(([^)]+)\)", s)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        return tuple(int(float(p)) for p in parts[:3])
    nombres = {
        "black": (0, 0, 0), "white": (255, 255, 255), "red": (210, 27, 27),
        "green": (10, 143, 44), "blue": (47, 111, 179), "gray": (154, 160, 166),
        "grey": (154, 160, 166), "orange": (255, 140, 0),
    }
    return nombres.get(s.lower(), default)


# Clases CSS del renderer de Voltio → estilo. Se extraen del <style> del propio
# SVG para no duplicar la información.
_CLASES: Dict[str, Dict[str, str]] = {}


def _cargar_clases(src: str) -> None:
    _CLASES.clear()
    m = re.search(r"<style[^>]*>(.*?)</style>", src, re.S)
    if not m:
        return
    for bloque in re.finditer(r"([^{}]+)\{([^{}]*)\}", m.group(1)):
        sels, cuerpo = bloque.groups()
        props = {}
        for decl in cuerpo.split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                props[k.strip()] = v.strip()
        for sel in sels.split(","):
            sel = sel.strip()
            if sel.startswith("."):
                _CLASES[sel[1:]] = props


def _estilo(node: _Node) -> Dict[str, Any]:
    """Resuelve el estilo efectivo de un nodo (clase + atributos de presentación)."""
    props: Dict[str, str] = {}
    for cls in (node.get("class") or "").split():
        props.update(_CLASES.get(cls, {}))
    for k in ("fill", "stroke", "stroke-width", "opacity", "font", "font-size",
              "font-family", "font-weight", "text-anchor", "stroke-linecap",
              "stroke-linejoin"):
        if k in node.attrs:
            props[k] = node.attrs[k]
    if "style" in node.attrs:
        for decl in node.attrs["style"].split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                props[k.strip()] = v.strip()

    out: Dict[str, Any] = {}
    out["fill"] = parse_color(props.get("fill"), (255, 255, 255))
    out["stroke"] = parse_color(props.get("stroke"))
    try:
        out["width"] = float(props.get("stroke-width", "1"))
    except ValueError:
        out["width"] = 1.0
    try:
        out["opacity"] = float(props.get("opacity", "1"))
    except ValueError:
        out["opacity"] = 1.0

    # Font: puede venir como shorthand ("700 11px sans") o suelto.
    fam = props.get("font", "")
    size = 10.0
    bold = False
    mono = False
    mm = re.search(r"(\d+(?:\.\d+)?)px", fam or props.get("font-size", ""))
    if mm:
        size = float(mm.group(1))
    peso = props.get("font-weight", "")
    if "700" in fam or "bold" in fam.lower() or peso in ("700", "bold"):
        bold = True
    ff = (props.get("font-family", "") + " " + fam).lower()
    if "mono" in ff or "courier" in ff or "menlo" in ff or "consolas" in ff:
        mono = True
    out["font"] = _font(size, bold, mono)
    out["size"] = size
    out["anchor"] = props.get("text-anchor", "start")
    # Halo de texto: stroke pintado DEBAJO del relleno (paint-order: stroke).
    # Sin esto, las etiquetas de red quedan tachadas por el cable que pasan por
    # encima: el navegador lo resuelve solo, pero un rasterizador casero tiene
    # que implementarlo a mano.
    out["paint_order"] = props.get("paint-order", "")
    out["text_stroke"] = parse_color(props.get("stroke"))
    sw = props.get("stroke-width", "0")
    try:
        out["text_stroke_w"] = float(sw.replace("px", ""))
    except ValueError:
        out["text_stroke_w"] = 0.0
    out["color"] = parse_color(props.get("fill"), (35, 39, 46)) or (35, 39, 46)
    return out


# -- dibujado ---------------------------------------------------------------


def render(src: str, scale: float = 1.0, max_dim: int = 2400,
           bg=(251, 251, 253)) -> Image.Image:
    _cargar_clases(src)
    root = parse_xml(src)
    svg = next((n for n in root.walk() if n.tag == "svg"), None)
    if svg is None:
        raise ValueError("no hay elemento <svg>")

    vb = (svg.get("viewBox") or "").split()
    if len(vb) == 4:
        ox, oy, W, H = (float(v) for v in vb)
    else:
        ox, oy = 0.0, 0.0
        W, H = svg.num("width", 800), svg.num("height", 600)

    s = scale
    if max(W, H) * s > max_dim:
        s = max_dim / max(W, H)
    iw, ih = max(int(W * s), 1), max(int(H * s), 1)

    img = Image.new("RGB", (iw, ih), bg)
    dr = ImageDraw.Draw(img, "RGBA")

    def T(ops, x, y):
        xx, yy = apply_ops((x, y), ops)
        return ((xx - ox) * s, (yy - oy) * s)

    def draw(node: _Node, ops: List[Tuple]) -> None:
        ops = ops + parse_transform(node.get("transform", ""))
        st = _estilo(node)
        w = max(st["width"] * s, 0.6) if st["stroke"] else 0
        stroke = st["stroke"]
        fill = st["fill"]
        alpha = int(255 * st["opacity"])

        tag = node.tag
        if tag == "g":
            pass
        elif tag == "rect":
            x, y = node.num("x"), node.num("y")
            ww, hh = node.num("width"), node.num("height")
            p1, p2 = T(ops, x, y), T(ops, x + ww, y + hh)
            box = [p1[0], p1[1], p2[0], p2[1]]
            if fill:
                dr.rectangle(box, fill=fill + (alpha,))
            if stroke:
                dr.rectangle(box, outline=stroke + (alpha,), width=max(int(round(w)), 1))
        elif tag == "circle":
            cx, cy, r = node.num("cx"), node.num("cy"), node.num("r")
            c = T(ops, cx, cy)
            rr = r * s
            box = [c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr]
            if fill:
                dr.ellipse(box, fill=fill + (alpha,))
            if stroke:
                dr.ellipse(box, outline=stroke + (alpha,), width=max(int(round(w)), 1))
        elif tag == "ellipse":
            cx, cy = node.num("cx"), node.num("cy")
            rx, ry = node.num("rx"), node.num("ry")
            c = T(ops, cx, cy)
            box = [c[0] - rx * s, c[1] - ry * s, c[0] + rx * s, c[1] + ry * s]
            if fill:
                dr.ellipse(box, fill=fill + (alpha,))
            if stroke:
                dr.ellipse(box, outline=stroke + (alpha,), width=max(int(round(w)), 1))
        elif tag == "line":
            a = T(ops, node.num("x1"), node.num("y1"))
            b = T(ops, node.num("x2"), node.num("y2"))
            col = stroke or fill or (0, 0, 0)
            dr.line([a, b], fill=col + (alpha,), width=max(int(round(w)), 1))
        elif tag == "polyline" or tag == "polygon":
            pts_s = (node.get("points") or "").replace(",", " ").split()
            nums = [float(v) for v in pts_s if re.match(r"^[-+]?\d*\.?\d+([eE][-+]?\d+)?$", v)]
            pts = [T(ops, nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
            if len(pts) >= 2:
                col = stroke or fill or (0, 0, 0)
                dr.line(pts, fill=col + (alpha,), width=max(int(round(w)), 1), joint="curve")
        elif tag == "path":
            d = node.get("d") or ""
            for sub in path_points(d):
                pts = [T(ops, x, y) for x, y in sub]
                if len(pts) < 2:
                    continue
                if fill and _cerrado(d):
                    try:
                        dr.polygon(pts, fill=fill + (alpha,),
                                   outline=(stroke or fill) + (alpha,))
                        continue
                    except Exception:
                        pass
                col = stroke or fill or (0, 0, 0)
                dr.line(pts, fill=col + (alpha,), width=max(int(round(w)), 1), joint="curve")
        elif tag == "text":
            x, y = node.num("x"), node.num("y")
            txt = (node.text or "").strip()
            if not txt:
                txt = "".join(
                    ch.text for ch in node.children if isinstance(ch, _Node)
                ).strip()
            if txt:
                p = T(ops, x, y)
                f = _font(st["size"] * s, bold=st["font"].path if hasattr(st["font"], "path") else False)
                f = st["font"]
                try:
                    f = ImageFont.truetype(f.path, max(int(round(st["size"] * s)), 1))
                except Exception:
                    pass
                bbox = dr.textbbox((0, 0), txt, font=f)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                if st["anchor"] == "middle":
                    p = (p[0] - tw / 2, p[1])
                elif st["anchor"] == "end":
                    p = (p[0] - tw, p[1])
                # SVG alinea por la línea base; PIL por arriba del glifo.
                kw = {}
                if st["paint_order"].startswith("stroke") and st["text_stroke"]:
                    kw["stroke_width"] = max(int(round(st["text_stroke_w"] * s)), 1)
                    kw["stroke_fill"] = st["text_stroke"] + (alpha,)
                dr.text((p[0] - bbox[0], p[1] - th - bbox[1] + th * 0.22), txt,
                        font=f, fill=st["color"] + (alpha,), **kw)

        for ch in node.children:
            if isinstance(ch, _Node):
                draw(ch, ops)

    for ch in svg.children:
        if isinstance(ch, _Node):
            draw(ch, [])
    return img


def _cerrado(d: str) -> bool:
    return bool(re.search(r"[Zz]\s*$", d.strip()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entrada")
    ap.add_argument("salida")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--max", type=int, default=2400, dest="max_dim")
    a = ap.parse_args(argv)
    with open(a.entrada, "r", encoding="utf-8") as f:
        src = f.read()
    img = render(src, scale=a.scale, max_dim=a.max_dim)
    img.save(a.salida)
    print(f"{a.salida}: {img.size[0]}×{img.size[1]} px  "
          f"({os.path.getsize(a.salida) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
