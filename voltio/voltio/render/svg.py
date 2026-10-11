"""Renderizador a SVG.

Genera un esquemático con aspecto de CAD: rejilla, símbolos vectoriales,
cables ortogonales con puntos de unión, etiquetas de red, buses con su marca
de ancho, y un cajetín. Si se le pasa el resultado de una simulación, pinta
cada cable según su nivel lógico y muestra los indicadores en vivo.

Unidad de coordenadas: **las posiciones ``at`` van ya en píxeles**, sobre una
rejilla de 10 px. Los *tamaños* de los símbolos se declaran en unidades de
rejilla en la plantilla del componente (``spec.width``) y se multiplican por
``UNIT`` al dibujarlos. Mezclar ambas convenciones —multiplicar también las
posiciones— hace que los cables midan miles de píxeles junto a símbolos de
cuarenta, y el texto quede microscópico: exactamente el desastre que esta
línea evitó la segunda vez.

El SVG resultante es autónomo (estilos incrustados, sin recursos externos), de
modo que se ve igual en el navegador, en un PDF impreso y en el visor.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..core.ir import Circuit, Endpoint, Net, TIE_PART
from ..core.logic import L
from .symbols import PIN_STUB, draw_symbol, pin_anchors

__all__ = ["to_svg", "save_svg", "Layout", "UNIT"]

#: píxeles por unidad de esquema
UNIT = 10.0

# Colores por nivel lógico.
_COLORS = {
    L.LOW: "#1a1a1a",
    L.HIGH: "#0a8f2c",
    L.X: "#9aa0a6",
    L.Z: "#2f6fb3",
}
_CONTENTION = "#d21b1b"
_DEFAULT_WIRE = "#4a4f57"


class Layout:
    """Geometría ya resuelta de un circuito: dónde va cada cosa."""

    def __init__(self, circuit: Circuit):
        self.circuit = circuit
        self.unit = UNIT
        self.parts: Dict[str, Dict[str, Any]] = {}
        self.pin_points: Dict[str, Dict[Tuple[str, int], Tuple[float, float]]] = {}
        self.pin_sides: Dict[str, Dict[Tuple[str, int], str]] = {}
        self._compute()

    def _compute(self) -> None:
        for ref, part in self.circuit.parts.items():
            spec = part.spec
            if spec is None:
                w = h = 4.0
            else:
                w, h = spec.width, spec.height
            # Deja hueco para las patillas que salen por arriba/abajo.
            n_top = len([p for p in (spec.pins if spec else []) if p.side == "top"])
            n_bot = len([p for p in (spec.pins if spec else []) if p.side == "bottom"])
            W = max(w * self.unit, 24.0)
            H = max(h * self.unit, 20.0)
            # Las posiciones ya vienen en píxeles de rejilla. Multiplicarlas
            # por UNIT otra vez fue el bug que produjo esquemáticos con cables
            # de 1600 px entre símbolos de 40.
            x = part.x
            y = part.y
            self.parts[ref] = {"x": x, "y": y, "w": W, "h": H, "part": part, "spec": spec}

            anchors = pin_anchors(W, H, spec) if spec else {}
            pts: Dict[Tuple[str, int], Tuple[float, float]] = {}
            sides: Dict[Tuple[str, int], str] = {}
            for pname, plist in anchors.items():
                for i, (ax, ay) in enumerate(plist):
                    side = "left"
                    if spec:
                        p = spec.pin(pname)
                        if p is not None:
                            side = p.side
                    ex, ey = _stub(ax, ay, side)
                    pts[(pname, i)] = (x + ex, y + ey)
                    sides[(pname, i)] = side
            self.pin_points[ref] = pts
            self.pin_sides[ref] = sides

    # -- consultas --------------------------------------------------------
    def endpoint_point(self, ep: Endpoint) -> Tuple[float, float]:
        """Punto de conexión de un extremo de cable, en px."""
        if ep.part == TIE_PART:
            return None  # los atados se dibujan con un símbolo propio
        pts = self.pin_points.get(ep.part, {})
        return pts.get((ep.pin, ep.bit))

    def endpoint_side(self, ep: Endpoint) -> str:
        return self.pin_sides.get(ep.part, {}).get((ep.pin, ep.bit), "left")

    def bounds(self, pad: float = 60.0) -> Tuple[float, float, float, float]:
        xs: List[float] = []
        ys: List[float] = []
        for d in self.parts.values():
            xs += [d["x"] - PIN_STUB, d["x"] + d["w"] + PIN_STUB]
            ys += [d["y"] - PIN_STUB, d["y"] + d["h"] + PIN_STUB]
        if not xs:
            return 0, 0, 400, 300
        return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad

    def auto_layout(self, cols: int = 6, gap_x: float = 220.0,
                    gap_y: float = 160.0) -> None:
        """Coloca las partes en rejilla si el DSL no traía posiciones.

        Es lo que permite que un circuito generado por una IA sin coordenadas
        se vea decente de inmediato, en vez de apilarse todo en el origen.
        """
        placed = [r for r, d in self.parts.items() if d["x"] or d["y"]]
        if placed:
            return
        for i, (ref, d) in enumerate(self.parts.items()):
            d["x"] = (i % cols) * gap_x + 80.0
            d["y"] = (i // cols) * gap_y + 80.0
        self._recompute_points()

    def _recompute_points(self) -> None:
        for ref, d in self.parts.items():
            part, spec = d["part"], d["spec"]
            if spec is None:
                continue
            anchors = pin_anchors(d["w"], d["h"], spec)
            pts, sides = {}, {}
            for pname, plist in anchors.items():
                p = spec.pin(pname)
                side = p.side if p else "left"
                for i, (ax, ay) in enumerate(plist):
                    ex, ey = _stub(ax, ay, side)
                    pts[(pname, i)] = (d["x"] + ex, d["y"] + ey)
                    sides[(pname, i)] = side
            self.pin_points[ref] = pts
            self.pin_sides[ref] = sides


def _stub(x: float, y: float, side: str) -> Tuple[float, float]:
    if side == "left":
        return x - PIN_STUB, y
    if side == "right":
        return x + PIN_STUB, y
    if side == "top":
        return x, y - PIN_STUB
    return x, y + PIN_STUB


# ---------------------------------------------------------------------------
# Encaminado de cables
# ---------------------------------------------------------------------------


def _route(points: Sequence[Tuple[float, float]],
           sides: Sequence[str]) -> Tuple[List[str], List[Tuple[float, float]]]:
    """Encamina un net y devuelve ``(trozos_svg, puntos_de_junta)``.

    Las juntas (el punto gordo) solo existen donde un cable se ramifica, o sea
    donde tres o más tramos coinciden. Dibujarlas en cada extremo, como hacía
    la primera versión, pone un punto negro en cada patilla y el esquemático
    parece lleno de cortocircuitos.

    Estrategia: separar los extremos horizontales (left/right) de los
    verticales (top/bottom), y conectar cada grupo a un "tronco" común. Es un
    encaminado sencillo pero produce esquemáticos legibles y sin diagonales,
    que es lo que importa. Con dos puntos se hace una L directa.
    """
    if not points or len(points) == 1:
        return [], []

    juntas: List[Tuple[float, float]] = []

    if len(points) == 2:
        (x1, y1), (x2, y2) = points
        if abs(x1 - x2) < 0.5 or abs(y1 - y2) < 0.5:
            return [_poly([(x1, y1), (x2, y2)])], []
        # L: sale horizontal del primero, dobla hacia el segundo. La esquina
        # de una L no es una junta: solo se unen dos tramos.
        if sides[0] in ("left", "right"):
            return [_poly([(x1, y1), (x2, y1), (x2, y2)])], []
        return [_poly([(x1, y1), (x1, y2), (x2, y2)])], []

    horiz = [(p, s) for p, s in zip(points, sides) if s in ("left", "right")]
    vert = [(p, s) for p, s in zip(points, sides) if s in ("top", "bottom")]
    out: List[str] = []

    if len(horiz) >= 2:
        trunk_x = sum(p[0] for p, _ in horiz) / len(horiz)
        ys = sorted(p[1] for p, _ in horiz)
        out.append(_poly([(trunk_x, ys[0]), (trunk_x, ys[-1])]))
        for (px, py), _ in horiz:
            out.append(_poly([(px, py), (trunk_x, py)]))
            juntas.append((trunk_x, py))
    elif horiz:
        out.append(_poly([horiz[0][0]]))

    if len(vert) >= 2:
        trunk_y = sum(p[1] for p, _ in vert) / len(vert)
        xs = sorted(p[0] for p, _ in vert)
        out.append(_poly([(xs[0], trunk_y), (xs[-1], trunk_y)]))
        for (px, py), _ in vert:
            out.append(_poly([(px, py), (px, trunk_y)]))
            juntas.append((px, trunk_y))

    # Une los dos troncos si existen ambos.
    if len(horiz) >= 2 and len(vert) >= 2:
        tx = sum(p[0] for p, _ in horiz) / len(horiz)
        ty = sum(p[1] for p, _ in vert) / len(vert)
        out.append(_poly([(tx, sum(p[1] for p, _ in horiz) / len(horiz)), (tx, ty)]))
        juntas.append((tx, ty))
    elif len(horiz) >= 2 and len(vert) == 1:
        # Un solo punto en el grupo vertical (típico: un símbolo de tierra
        # debajo de un peine horizontal). Sin este caso, el punto se quedaba
        # SIN DIBUJAR: el net seguía conectado eléctricamente pero el símbolo
        # quedaba flotando en el dibujo, que es peor porque no se ve.
        tx = sum(p[0] for p, _ in horiz) / len(horiz)
        (vx, vy), _ = vert[0]
        ys = [p[1] for p, _ in horiz]
        # El tronco vertical se extiende hasta alcanzar al punto suelto, y de
        # ahí sale un tramo horizontal hasta él. Forma una L con el peine.
        out.append(_poly([(tx, min(min(ys), vy)), (tx, max(max(ys), vy))]))
        out.append(_poly([(tx, vy), (vx, vy)]))
        juntas.append((tx, vy))
    elif len(vert) >= 2 and len(horiz) == 1:
        ty = sum(p[1] for p, _ in vert) / len(vert)
        (hx, hy), _ = horiz[0]
        xs = [p[0] for p, _ in vert]
        out.append(_poly([(min(min(xs), hx), ty), (max(max(xs), hx), ty)]))
        out.append(_poly([(hx, ty), (hx, hy)]))
        juntas.append((hx, ty))
    return [o for o in out if o], juntas


def _route_bus(groups: List[Tuple[str, List[Tuple[float, float]]]],
               offsets: Optional[List[float]] = None,
               ) -> Tuple[List[Tuple], List[Tuple[float, float]], Optional[Tuple[float, float]]]:
    """``offsets`` separa bandas que se enganchan al mismo lado de la misma
    parte: sin eso, el bus de direcciones y el de datos de una CPU se dibujan
    uno encima del otro."""
    """Encamina un bus como banda gruesa con entradas finas, como en un CAD.

    Dibujar cada bit del bus como un cable independiente produce una mancha
    negra cuando el bus tiene 8 o 16 bits: los cables quedan a menos de diez
    píxeles entre sí y con grosor de bus se funden. La convención esquemática
    es otra: una **banda** gruesa que corre paralela a la columna de patillas,
    con una **entrada** fina por patilla, y un corte oblicuo con el ancho.

    ``groups`` es la lista de grupos de conexión del bus: cada grupo es
    ``(lado, [puntos de cada bit])``. Devuelve ``(trazos, entradas, pos_etiqueta)``
    donde cada trazo es una polilínea de la banda y cada entrada une una
    patilla con la banda.
    """
    trazos: List[Tuple] = []
    entradas: List[Tuple[float, float]] = []
    bandas: List[Tuple[float, float, float]] = []   # (x, y_ini, y_fin)
    offsets = offsets or [0.0] * len(groups)

    for (side, pts), off in zip(groups, offsets):
        if not pts:
            continue
        ys = sorted(p[1] for p in pts)
        xs = [p[0] for p in pts]
        bx = ((max(xs) + 28.0 + off) if side == "right"
              else (min(xs) - 28.0 - off))
        bandas.append((bx, ys[0], ys[-1]))
        for (px, py) in pts:
            entradas.append((px, py, bx, py))

    # Une bandas consecutivas saliendo por el extremo más cercano.
    for (x1, a1, b1), (x2, a2, b2) in zip(bandas, bandas[1:]):
        ye = b1 if abs(b1 - b2) < abs(a1 - b2) else a1
        ys2 = a2 if abs(b1 - b2) < abs(a1 - b2) else b2
        trazos.append([(x1, a1), (x1, b1)])
        trazos.append([(x1, ye), (x2, ye)])
        trazos.append([(x2, ys2), (x2, (b2 if ys2 == a2 else a2))])
    if bandas:
        x1, a1, b1 = bandas[-1]
        if len(bandas) == 1:
            trazos.append([(x1, a1), (x1, b1)])

    etiqueta = None
    if bandas:
        bx, a, b = bandas[0]
        # La etiqueta va al LADO del arranque de la banda, nunca encima:
        # sobre la banda el texto azul no se lee, y encima de las entradas
        # se confunde con el peine.
        side0 = groups[0][0] if groups else "left"
        if side0 == "right":
            etiqueta = (bx + 8.0, a - 6.0, "start")
        else:
            etiqueta = (bx - 8.0, a - 6.0, "end")
    return trazos, entradas, etiqueta


def _styled(chunk: str, color: str, width: float) -> str:
    if not chunk:
        return chunk
    return chunk.replace('class="wire"',
                         f'class="wire" stroke="{color}" stroke-width="{width}"')


def _poly(pts: Sequence[Tuple[float, float]], extra: str = "") -> str:
    if len(pts) < 2:
        return ""
    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return f'<polyline points="{d}" class="wire{extra}"/>'


# ---------------------------------------------------------------------------
# Render principal
# ---------------------------------------------------------------------------


def to_svg(
    circuit: Circuit,
    engine: Any = None,
    title: Optional[str] = None,
    show_grid: bool = True,
    show_labels: bool = True,
    show_values: bool = True,
    legend: bool = True,
    padding: float = 60.0,
    auto_layout: bool = True,
) -> str:
    """Convierte un circuito en un documento SVG completo.

    ``engine`` es opcional: si se pasa una :class:`~voltio.sim.engine.Engine`
    ya simulada, los cables se colorean según su nivel y los indicadores
    (display, LED, LCD, sonda) muestran su estado.
    """
    lay = Layout(circuit)
    if auto_layout:
        lay.auto_layout()

    values = _collect_values(circuit, engine) if (engine and show_values) else {}
    x0, y0, x1, y1 = lay.bounds(padding)
    # Reserva sitio abajo para la leyenda y el cajetín.
    footer = (56.0 if legend else 0.0) + 46.0
    W = max(x1 - x0, 200.0)
    H = max(y1 - y0, 160.0) + footer

    out: List[str] = []
    out.append(_header(W, H))
    out.append(f'<g transform="translate({-x0:.1f},{-y0:.1f})">')

    if show_grid:
        out.append(_grid(x0, y0, x1, y1))

    # 1. Cables primero, para que queden por debajo de los símbolos.
    out.append('<g class="wires">')
    junctions: Set[Tuple[float, float]] = set()
    bus_labels: List[Tuple[str, float, float, int, str]] = []
    buses_hechos: Set[str] = set()
    # Cuántas bandas hay ya enganchadas a cada (parte, lado): cada bus nuevo
    # se coloca un poco más afuera para no pisar al anterior.
    side_use: Dict[Tuple[str, str], int] = {}
    for bname, bus in circuit.buses.items():
        miembros = [n for n in circuit.nets.values() if n.bus == bname]
        if not miembros:
            continue
        buses_hechos.add(bname)
        miembros.sort(key=lambda n: (n.bit if n.bit is not None else 0))

        # Grupos de conexión: cada (parte, pin) aporta sus puntos en orden de bit.
        grupos: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
        orden: List[Tuple[str, str]] = []
        lados: Dict[Tuple[str, str], str] = {}
        for n in miembros:
            for ep in n.endpoints:
                if ep.part == TIE_PART:
                    continue
                p = lay.endpoint_point(ep)
                if p is None:
                    continue
                clave = (ep.part, ep.pin)
                if clave not in grupos:
                    grupos[clave] = []
                    orden.append(clave)
                    lados[clave] = lay.endpoint_side(ep)
                grupos[clave].append(p)
        glist = [(lados[k], grupos[k]) for k in orden]
        tie = miembros[0].tie_value()

        # ------------------------------------------------------------------
        # Caso 0: un solo grupo y sin atar. No hay ningún cable que dibujar:
        # son patillas que el diseño deja sin conectar (p. ej. los bits altos
        # de un puerto que no se usan). Dibujarles una banda sugeriría un bus
        # que no existe; las patillas ya tienen su rabito y basta.
        # ------------------------------------------------------------------
        if len(glist) == 1 and tie is None:
            side0, pts0 = glist[0]
            top = min(p[1] for p in pts0)
            bus_labels.append((bname, pts0[0][0], top - 12.0, len(miembros),
                               "end" if side0 == "left" else "start"))
            continue

        # ------------------------------------------------------------------
        # Caso A: dos grupos → UN CABLE POR BIT, cada uno en su renglón.
        #
        # Es como dibuja Proteus a mano (ver las capturas de referencia): los
        # cables paralelos no se funden en una banda, cada uno ocupa su propio
        # canal. Si las patillas quedan alineadas, el cable es recto; si no,
        # hace un quiebre en Z por un canal vertical ESCALONADO, de modo que
        # dos cables nunca comparten columna y el dibujo se puede traducir a
        # .pdsprj tal cual, sin ambigüedades.
        # ------------------------------------------------------------------
        if len(glist) == 2 and tie is None:
            (side_a, pts_a), (side_b, pts_b) = glist
            n = min(len(pts_a), len(pts_b))
            cxs = [p[0] for p in pts_a] + [p[0] for p in pts_b]
            cys = [p[1] for p in pts_a] + [p[1] for p in pts_b]
            dx = abs((pts_a[0][0] + pts_a[-1][0]) / 2 - (pts_b[0][0] + pts_b[-1][0]) / 2)
            dy = abs((pts_a[0][1] + pts_a[-1][1]) / 2 - (pts_b[0][1] + pts_b[-1][1]) / 2)

            if dy > dx:
                # ---- flujo vertical: una pieza encima de la otra ----------
                # Cada cable sale por su columna junto a la pieza de origen,
                # cruza por UN renglón propio del pasillo vacío que hay ENTRE
                # LAS DOS CAJAS, y entra por su columna junto a la pieza de
                # destino. El pasillo se calcula con las cajas de los
                # componentes, no con las patillas: calcularlo con las
                # patillas ponía los renglones dentro de la CPU, con los
                # cables cruzando el cuerpo por encima.
                ref_a, ref_b = orden[0][0], orden[1][0]
                da, db = lay.parts[ref_a], lay.parts[ref_b]
                if da["y"] <= db["y"]:
                    ref_up, ref_dn = ref_a, ref_b
                    pts_up, pts_dn = pts_a, pts_b
                    side_up, side_dn = side_a, side_b
                else:
                    ref_up, ref_dn = ref_b, ref_a
                    pts_up, pts_dn = pts_b, pts_a
                    side_up, side_dn = side_b, side_a
                du, dd = lay.parts[ref_up], lay.parts[ref_dn]
                # +44 y no +16: debajo de la caja viven el nombre del
                # componente y su valor en vivo, y el pasillo no debe
                # pisarlos.
                gap_top = du["y"] + du["h"] + 44.0
                gap_bot = dd["y"] - 16.0
                if gap_bot <= gap_top:
                    mid = (du["y"] + du["h"] + dd["y"]) / 2
                    gap_top = gap_bot = mid
                paso = 9.0
                if n > 1 and gap_top + (n - 1) * paso > gap_bot:
                    paso = max(5.0, (gap_bot - gap_top) / (n - 1))
                # Las columnas de salida/entrada reservan sitio: si un bus ya
                # ocupó ese lado de esa pieza (una banda, u otro bus de
                # carriles), este empieza más afuera. Sin esta contabilidad, la
                # banda del bus de datos y las columnas del bus de direcciones
                # se dibujaban encima, que es la mancha negra que se veía en
                # el flanco derecho de la CPU.
                off_up = 18.0 * side_use.get((ref_up, side_up), 0)
                off_dn = 18.0 * side_use.get((ref_dn, side_dn), 0)
                side_use[(ref_up, side_up)] = side_use.get((ref_up, side_up), 0) + 4
                side_use[(ref_dn, side_dn)] = side_use.get((ref_dn, side_dn), 0) + 4
                x_der_base = max(cxs) + 24.0
                x_izq_base = min(cxs) - 24.0
                for i in range(n):
                    p_up, p_dn = pts_up[i], pts_dn[i]
                    color = _wire_color(miembros[i], values) if i < len(miembros) else _DEFAULT_WIRE
                    fila = gap_top + i * paso
                    if side_up == "right":
                        x1 = x_der_base + off_up + (i % 6) * 7.0
                    else:
                        x1 = x_izq_base - off_up - (i % 6) * 7.0
                    if side_dn == "left":
                        x2 = x_izq_base - off_dn - (i % 6) * 7.0
                    else:
                        x2 = x_der_base + off_dn + (i % 6) * 7.0
                    ruta = [p_up, (x1, p_up[1]), (x1, fila), (x2, fila),
                            (x2, p_dn[1]), p_dn]
                    out.append(_styled(_poly(ruta), color, 1.6))
                bus_labels.append((bname, max(cxs) + off_up + 60.0,
                                   (gap_top + gap_bot) / 2, len(miembros), "start"))
                continue

            # ---- flujo horizontal: canal vertical escalonado por bit ------
            lo = min(cxs)
            hi = max(cxs)
            mismo_lado = side_a == side_b
            base = (hi + 26.0) if mismo_lado else (lo + 26.0)
            paso = 10.0
            if not mismo_lado and base + (n - 1) * paso > hi - 26.0 and n > 1:
                paso = max(6.0, (hi - lo - 52.0) / (n - 1))
            k = 0
            for i in range(n):
                pa, pb = pts_a[i], pts_b[i]
                color = _wire_color(miembros[i], values) if i < len(miembros) else _DEFAULT_WIRE
                if abs(pa[1] - pb[1]) < 0.6:
                    out.append(_styled(_poly([pa, pb]), color, 1.6))
                else:
                    xj = base + k * paso
                    k += 1
                    out.append(_styled(_poly([pa, (xj, pa[1]), (xj, pb[1]), pb]), color, 1.6))
            if pts_a:
                top = min(min(p[1] for p in pts_a), min(p[1] for p in pts_b))
                lx = (pts_a[0][0] + pts_b[0][0]) / 2
                bus_labels.append((bname, lx, top - 12.0, len(miembros), "middle"))
            continue

        # ------------------------------------------------------------------
        # Caso B: bus atado a un nivel fijo → abanico a UN solo marcador.
        #
        # Los ocho bits comparten el mismo riel ideal (tierra o VCC), que sí es
        # un único nodo eléctrico: por eso aquí el peine con puntos de junta
        # en cada T es correcto, y por eso se dibuja con cable fino y no con
        # banda: una banda sugeriría un bus de datos, que no es lo que es.
        # ------------------------------------------------------------------
        if tie is not None and len(glist) <= 1:
            if glist:
                side0, pts0 = glist[0]
                ys = sorted(p[1] for p in pts0)
                xs = [p[0] for p in pts0]
                sx = (min(xs) - 26.0) if side0 == "left" else (max(xs) + 26.0)
                out.append(_styled(_poly([(sx, ys[0]), (sx, ys[-1])]), _DEFAULT_WIRE, 1.6))
                for p in pts0:
                    out.append(_styled(_poly([p, (sx, p[1])]), _DEFAULT_WIRE, 1.6))
                    junctions.add((round(sx, 1), round(p[1], 1)))
                ty = (ys[0] + ys[-1]) / 2
                out.append(_tie_marker((sx, ty), side0, tie, miembros[0]))
                bus_labels.append((bname, sx, ys[0] - 12.0, len(miembros),
                                   "end" if side0 == "left" else "start"))
            continue

        # ------------------------------------------------------------------
        # Caso C: tres o más grupos compartiendo el bus → banda de bus real,
        # que es exactamente para lo que existe el símbolo en Proteus:
        # agrupar sin conectar. Las entradas son finas y la banda no lleva
        # puntos de junta porque no es un conductor.
        # ------------------------------------------------------------------
        offsets = []
        for k in orden:
            clave_lado = (k[0], lados[k])
            offsets.append(18.0 * side_use.get(clave_lado, 0))
            side_use[clave_lado] = side_use.get(clave_lado, 0) + 1
        trazos, entradas, eti = _route_bus(glist, offsets)
        for tr in trazos:
            out.append(
                f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in tr)}" '
                f'class="busband"/>'
            )
        for px, py, bx, by in entradas:
            out.append(
                f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
                f'class="wire" stroke="#8a9099" stroke-width="0.9"/>'
            )
        if eti is not None:
            bus_labels.append((bname, eti[0], eti[1], bus.width, eti[2]))
            bx, by = eti[0], eti[1]
            out.append(
                f'<line x1="{bx - 5:.1f}" y1="{by + 14:.1f}" x2="{bx + 5:.1f}" '
                f'y2="{by + 4:.1f}" class="bus-slash"/>'
            )

    for net in circuit.nets.values():
        if net.bus in buses_hechos:
            continue
        pts, sides = [], []
        for ep in net.endpoints:
            if ep.part == TIE_PART:
                continue
            p = lay.endpoint_point(ep)
            if p is None:
                continue
            pts.append(p)
            sides.append(lay.endpoint_side(ep))
        color = _wire_color(net, values)
        width = 2.6 if net.kind == "bus" else 1.6
        chunks, js = _route(pts, sides)
        for chunk in chunks:
            out.append(chunk.replace('class="wire"', f'class="wire" stroke="{color}" stroke-width="{width}"'))
        for j in js:
            # Un punto de junta solo es correcto donde se encuentran tres o
            # más tramos. En un codo (dos tramos) el punto gordo sobraba: es la
            # convención de siempre de los esquemáticos, y ponerlos en todos
            # los extremos hacía que cualquier recodo pareciese un cortocircuito.
            if _grado(pts, j) >= 3:
                junctions.add((round(j[0], 1), round(j[1], 1)))

        # Atados constantes: un marcador de riel en el primer extremo...
        # ...salvo que el net ya tenga su propio símbolo de tierra o de VCC
        # dibujado. Sin esta guarda salían DOS tierras en el mismo cable: la
        # del componente GND1 y la del atado automático del DSL.
        tie = net.tie_value()
        has_rail_part = any(
            (circuit.parts.get(ep.part).spec.model if ep.part in circuit.parts
             and circuit.parts[ep.part].spec else None) in ("ground", "rail")
            for ep in net.endpoints
        )
        if tie is not None and pts and not has_rail_part:
            out.append(_tie_marker(pts[0], sides[0] if sides else "left", tie, net))
    for (jx, jy) in sorted(junctions):
        out.append(f'<circle cx="{jx:.1f}" cy="{jy:.1f}" r="2.6" class="junction"/>')
    out.append("</g>")

    # 2. Etiquetas de red. Un bus se etiqueta UNA sola vez, en su bit más
    #    a la izquierda, con la notación NOMBRE[lo:hi]; repetir el nombre en
    #    cada bit ensuciaba el dibujo.
    if show_labels:
        out.append('<g class="net-labels">')
        placer = _LabelPlacer()
        # Las referencias (U1, U_PORT...) ya ocupan su sitio encima de cada
        # caja: se registran primero para que las etiquetas de red las
        # esquiven en vez de caer encima, que es lo que pasaba con PORT_MR.
        for ref, d in lay.parts.items():
            placer.placed.append((d["x"] + d["w"] / 2, d["y"] - 7.0,
                                  len(ref) * 7.5 + 10))
        for bname, bx, by, bwidth, anch in bus_labels:
            lx, ly = placer.place(bx, by, f"{bname}[0:{bwidth - 1}]")
            out.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" class="net-label" '
                f'text-anchor="{anch}">{_esc(bname)}[0:{bwidth - 1}]</text>'
            )
        bus_done: Set[str] = set(buses_hechos)
        for net in _nets_left_to_right(circuit):
            if net.kind == "bus" and net.bus:
                if net.bus in bus_done:
                    continue
                bus_done.add(net.bus)
                label, pts = _bus_label(circuit, lay, net.bus)
            else:
                label = _net_label(net)
                pts = [p for p in (lay.endpoint_point(ep) for ep in net.endpoints
                                   if ep.part != TIE_PART) if p is not None]
            if not label or not pts:
                continue
            if len(pts) == 2:
                # Con dos extremos la etiqueta va sobre el propio cable, en un
                # punto intermedio: es donde el ojo la busca.
                (x1, y1), (x2, y2) = pts
                lx = x1 + (x2 - x1) * 0.42
                ly = min(y1, y2) - 5 if abs(y1 - y2) < 2 else (y1 + y2) / 2 - 5
            else:
                lx = sum(p[0] for p in pts) / len(pts)
                ly = min(p[1] for p in pts) - 11
            lx, ly = placer.place(lx, ly, label)
            out.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" class="net-label">{_esc(label)}</text>'
            )
        out.append("</g>")

    # 3. Símbolos.
    out.append('<g class="parts">')
    for ref, d in lay.parts.items():
        out.append(_draw_part(ref, d, circuit, engine, values))
    out.append("</g>")

    out.append("</g>")  # fin del grupo trasladado

    if legend:
        out.append(_legend(W, H - footer + 10, values, engine))
    out.append(_titleblock(W, H, circuit, title))
    out.append("</svg>")
    return "\n".join(out)


def _grado(tronco: List[Tuple[float, float]], j: Tuple[float, float]) -> int:
    """Cuántos tramos coinciden en un punto del tronco.

    En un tronco vertical ordenado de arriba abajo, un punto intermedio toca
    tres tramos (el tronco hacia arriba, el hacia abajo y su rama horizontal)
    mientras que los dos extremos tocan solo dos: son codos, no juntas.
    """
    ys = sorted(p[1] for p in tronco)
    if not ys:
        return 0
    if abs(j[1] - ys[0]) < 0.51 or abs(j[1] - ys[-1]) < 0.51:
        # Extremo del tronco: codo si el tronco tiene más de un punto.
        return 2 if len(ys) > 1 else 3
    return 3


def _wire_color(net: Net, values: Dict[str, L]) -> str:
    if net.name not in values:
        return _DEFAULT_WIRE
    v = values[net.name]
    return _COLORS.get(v, _DEFAULT_WIRE)


def _collect_values(circuit: Circuit, engine) -> Dict[str, L]:
    return {name: engine.net_value(name) for name in circuit.nets}


class _LabelPlacer:
    """Coloca etiquetas evitando que se monten unas sobre otras.

    Sin esto, cuatro redes de un bit que corren paralelas (los PORT0..PORT3
    del ejemplo de la CPU) apilan sus cuatro nombres en el mismo sitio y el
    resultado es un borrón de texto azul.
    """

    def __init__(self):
        self.placed: List[Tuple[float, float, float]] = []

    def place(self, x: float, y: float, text: str) -> Tuple[float, float]:
        # Estimación de ancho con holgura: con 5.6 px por carácter las etiquetas
        # quedaban a dos píxeles del umbral de colisión y se montaban sobre las
        # referencias de todos modos.
        w = len(text) * 6.2 + 10
        # Prueba primero el sitio pedido, y si está ocupado alterna hacia
        # arriba y hacia abajo. Escapar solo hacia arriba apilaba las
        # etiquetas de cuatro cables paralelos en lo alto del lienzo, lejos de
        # sus cables, y empujaba otras encima de las referencias.
        for k in range(24):
            yy = y + (11.0 * ((k + 1) // 2) * (1 if k % 2 else -1))
            if not any(abs(yy - py) < 11 and abs(x - px) < (w + pw) / 2
                       for px, py, pw in self.placed):
                self.placed.append((x, yy, w))
                return x, yy
        self.placed.append((x, y, w))
        return x, y


def _nets_left_to_right(circuit: Circuit):
    """Ordena las redes por su extremo más a la izquierda (para etiquetar)."""
    return sorted(circuit.nets.values(), key=lambda n: n.name or "")


def _bus_label(circuit: Circuit, lay, bus_name: str) -> Tuple[str, List[Tuple[float, float]]]:
    """Etiqueta agregada de un bus: ``Q[0:3]``, y todos sus puntos de conexión."""
    members = [n for n in circuit.nets.values() if n.bus == bus_name]
    if not members:
        return "", []
    bits = [n.bit for n in members if n.bit is not None]
    pts: List[Tuple[float, float]] = []
    for n in members:
        for ep in n.endpoints:
            if ep.part == TIE_PART:
                continue
            p = lay.endpoint_point(ep)
            if p is not None:
                pts.append(p)
    if bits and len(members) > 1:
        return f"{bus_name}[{min(bits)}:{max(bits)}]", pts
    return bus_name, pts


def _net_label(net: Net) -> str:
    if net.kind == "bus":
        return net.bus or net.label
    if net.name and net.name.startswith("N$"):
        return ""
    return net.label


def _tie_marker(pt: Tuple[float, float], side: str, level: L, net: Net) -> str:
    """Dibuja el símbolo de riel (VCC/GND) o el cuadrito de nivel fijo."""
    x, y = pt
    dx = -22 if side == "left" else 22
    if level is L.LOW and (net.name or "").upper().rstrip("$") in ("GND", "GROUND", "0V"):
        return (
            f'<g class="rail">'
            f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + dx:.1f}" y2="{y:.1f}" class="wire"/>'
            f'<line x1="{x + dx - 7:.1f}" y1="{y:.1f}" x2="{x + dx + 7:.1f}" y2="{y:.1f}" class="wire"/>'
            f'<line x1="{x + dx - 4:.1f}" y1="{y + 4:.1f}" x2="{x + dx + 4:.1f}" y2="{y + 4:.1f}" class="wire"/>'
            f'<line x1="{x + dx - 1:.1f}" y1="{y + 8:.1f}" x2="{x + dx + 1:.1f}" y2="{y + 8:.1f}" class="wire"/>'
            f"</g>"
        )
    if level is L.HIGH and (net.name or "").upper().rstrip("$") in ("VCC", "VDD", "+5V", "5V"):
        return (
            f'<g class="rail">'
            f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + dx:.1f}" y2="{y:.1f}" class="wire"/>'
            f'<path d="M{x + dx - 6:.1f},{y:.1f} L{x + dx:.1f},{y - 9:.1f} L{x + dx + 6:.1f},{y:.1f}" class="wire"/>'
            f"</g>"
        )
    txt = str(level)
    return (
        f'<g class="rail">'
        f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + dx:.1f}" y2="{y:.1f}" class="wire"/>'
        f'<rect x="{x + dx - 7:.1f}" y="{y - 9:.1f}" width="14" height="18" rx="2" class="tie-box"/>'
        f'<text x="{x + dx:.1f}" y="{y + 4:.1f}" class="tie-text">{txt}</text>'
        f"</g>"
    )


def _draw_part(ref: str, d: Dict[str, Any], circuit: Circuit, engine,
               values: Dict[str, L]) -> str:
    part = d["part"]
    spec = d["spec"]
    x, y, W, H = d["x"], d["y"], d["w"], d["h"]
    rot = part.rotation or 0
    out: List[str] = []
    transform = f'transform="translate({x:.1f},{y:.1f})'
    if rot:
        transform += f" rotate({rot},{W / 2:.1f},{H / 2:.1f})"
    if part.mirror:
        transform += f" translate({W:.1f},0) scale(-1,1)"
    transform += '"'
    out.append(f'<g class="part" data-ref="{_esc(ref)}" {transform}>')

    symbol = spec.symbol if spec else "ic"
    for chunk in draw_symbol(symbol, W, H, spec):
        out.append(chunk)

    # Rabitos de las patillas: el tramo de cable que va del borde del símbolo
    # hasta el punto donde enganchan los cables del net. Sin esto, los cables
    # terminaban en el aire a doce píxeles del componente y el símbolo parecía
    # flotar. Es de esas cosas que solo se ven mirando el dibujo.
    if spec:
        anchors = pin_anchors(W, H, spec)
        for p in spec.pins:
            pts = anchors.get(p.name, [])
            for _bit, (ax, ay) in enumerate(pts):
                ex, ey = _stub(ax, ay, p.side)
                out.append(
                    f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
                    f'class="wire-only"/>'
                )

    # Nombre y referencia.
    name = spec.name if spec else part.part
    out.append(f'<text x="{W / 2:.1f}" y="-7" class="refdes">{_esc(ref)}</text>')
    # El nombre del componente va DEBAJO de la caja. Dentro chocaba con los
    # nombres de patilla de los rectángulos anchos, y el resultado era un
    # amasijo de texto ilegible justo en el centro del símbolo.
    # El nombre del componente va debajo del símbolo en las familias donde el
    # esquemático original lo lleva (compuertas y rectángulos: cada puerta de
    # Proteus muestra su "74LS00" debajo). En fuentes e instrumentos se muestra
    # el VALOR si existe (R=330, FREQ=1000...) y nada más: escribir
    # "LOGICSTATE" o "CLOCK" debajo es ruido que el original no tiene.
    if symbol in ("gate2", "inverter", "buffer", "tribuf", "flipflop",
                  "ic", "register", "counter", "memory", "mcu"):
        out.append(
            f'<text x="{W / 2:.1f}" y="{H + 13:.1f}" class="devname">{_esc(name)}</text>'
        )
    val = _part_value(part, spec)
    if val and symbol not in ("ic", "register", "counter", "memory", "mcu"):
        out.append(
            f'<text x="{W / 2:.1f}" y="{H + 13:.1f}" class="devname">{_esc(val)}</text>'
        )

    # Nombres de patilla en los rectángulos.
    if spec and symbol in ("ic", "register", "counter", "memory", "mcu", "flipflop"):
        anchors = pin_anchors(W, H, spec)
        for p in spec.pins:
            pts = anchors.get(p.name, [])
            if not pts:
                continue
            # Un pin de 16 bits con una etiqueta por bit produce dieciséis
            # textos solapados e ilegibles: es lo que se veía en el primer
            # render de la CPU. Para pines anchos se pone UNA etiqueta con el
            # rango, centrada en la columna de patillas.
            if p.width > 2:
                ay = sum(q[1] for q in pts) / len(pts)
                ax = pts[0][0]
                inside = 5 if p.side == "left" else -5
                anchor = "start" if p.side == "left" else "end"
                out.append(
                    f'<text x="{ax + inside:.1f}" y="{ay + 3:.1f}" '
                    f'class="pinname" text-anchor="{anchor}">'
                    f'{_esc(p.display_name())}[0:{p.width - 1}]</text>'
                )
                continue
            for i, (ax, ay) in enumerate(pts):
                inside = 5 if p.side == "left" else -5
                anchor = "start" if p.side == "left" else "end"
                label = p.display_name() if p.width == 1 else f"{p.name}[{i}]"
                out.append(
                    f'<text x="{ax + inside:.1f}" y="{ay + 3.5:.1f}" '
                    f'class="pinname" text-anchor="{anchor}">{_esc(label)}</text>'
                )

    # Estado en vivo.
    if engine is not None:
        out.extend(_live_overlay(ref, part, spec, W, H, engine, values))

    out.append("</g>")
    return "\n".join(out)


def _part_value(part, spec) -> str:
    if not spec:
        return ""
    for key in ("R", "C", "L", "VALUE", "FREQ", "COLOR"):
        if key in part.props:
            return f"{key}={part.props[key]}" if key not in ("VALUE",) else str(part.props[key])
    return ""


def _live_overlay(ref: str, part, spec, W: float, H: float, engine,
                  values: Dict[str, L]) -> List[str]:
    """Dibuja el estado de la simulación encima del símbolo."""
    model = engine.model(ref)
    if model is None:
        return []
    out: List[str] = []
    st = model.state

    if spec and spec.symbol == "7seg":
        lit = model.segments_lit() if hasattr(model, "segments_lit") else []
        order = "abcdefg"
        for i, name in enumerate(order):
            if i < len(lit) and lit[i]:
                out.append(f'<line data-seg="{name}" class="seg-on" '
                           f'{_seg_coords(name, W, H)}/>')
        if len(lit) > 7 or "dp" in st:
            pass
    elif spec and spec.symbol == "led":
        if st.get("on"):
            out.append(f'<circle cx="{W * 0.5 + 5:.1f}" cy="{H / 2:.1f}" r="{H * 0.55:.1f}" class="led-glow"/>')
    elif spec and spec.symbol == "logicstate":
        out.append(f'<text x="{W * 0.35:.1f}" y="{H / 2 + 4:.1f}" class="state-text">'
                   f'{st.get("v", L.X)}</text>')
    elif spec and spec.symbol == "switch":
        if st.get("closed"):
            out.append(f'<line x1="{W * 0.35:.1f}" y1="{H / 2:.1f}" x2="{W * 0.72:.1f}" '
                       f'y2="{H / 2:.1f}" class="wire" stroke="#0a8f2c"/>')
    elif spec and spec.symbol == "probe":
        out.append(f'<text x="{W * 0.55:.1f}" y="{H / 2 + 3.5:.1f}" class="probe-text">'
                   f'{st.get("v", L.X)}</text>')
    elif spec and spec.symbol == "lcd":
        text = model.text() if hasattr(model, "text") else []
        for r, line in enumerate(text[:2]):
            out.append(f'<text x="{W * 0.1:.1f}" y="{H * (0.42 + 0.28 * r):.1f}" '
                       f'class="lcd-text">{_esc(line.rstrip()[:20])}</text>')
    elif spec and spec.symbol in ("ic", "counter", "register", "mcu"):
        probe = model.on_probe(None) if hasattr(model, "on_probe") else ""
        if probe:
            out.append(f'<text x="{W / 2:.1f}" y="{H + 25:.1f}" class="live-value">'
                       f'{_esc(str(probe))}</text>')
    return out


def _seg_coords(name: str, W: float, H: float) -> str:
    from .symbols import _SEVEN_LAYOUT

    x1, y1, x2, y2 = _SEVEN_LAYOUT[name]
    return (f'x1="{W * x1:.1f}" y1="{H * y1:.1f}" '
            f'x2="{W * x2:.1f}" y2="{H * y2:.1f}"')


def _grid(x0: float, y0: float, x1: float, y1: float, step: float = 20.0) -> str:
    """Rejilla de puntos.

    Se hace con un ``<pattern>`` y un único rectángulo relleno: dibujar un
    círculo por punto generaba cientos de miles de nodos y archivos de 700 KB
    en circuitos grandes. Con el patrón el coste es constante.
    """
    uid = f"g{abs(hash((round(x0), round(y0), round(step)))) % 10**8:x}"
    return (
        f'<defs><pattern id="{uid}" width="{step:.0f}" height="{step:.0f}" '
        f'patternUnits="userSpaceOnUse">'
        f'<circle cx="{step / 2:.1f}" cy="{step / 2:.1f}" r="0.8" class="grid"/>'
        f'</pattern></defs>'
        f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{x1 - x0:.0f}" height="{y1 - y0:.0f}" '
        f'fill="url(#{uid})"/>'
    )


def _legend(W: float, y: float, values: Dict[str, L], engine) -> str:
    items = [
        (_COLORS[L.LOW], "0 — nivel bajo"),
        (_COLORS[L.HIGH], "1 — nivel alto"),
        (_COLORS[L.X], "X — indeterminado"),
        (_COLORS[L.Z], "Z — alta impedancia"),
        (_CONTENTION, "colisión de bus"),
    ]
    out = [f'<g class="legend" transform="translate(20,{y:.0f})">']
    x = 0.0
    row = 0.0
    # La leyenda se sale del lienzo en cuanto el circuito es estrecho: cinco
    # entradas de ~150 px no caben en 600. Se pasa de línea cuando no cabe.
    for color, text in items:
        ancho = 32 + len(text) * 5.6 + 26
        if x + ancho > W - 40 and x > 0:
            x = 0.0
            row += 15.0
        out.append(f'<line x1="{x:.0f}" y1="{6 + row:.0f}" x2="{x + 26:.0f}" '
                   f'y2="{6 + row:.0f}" stroke="{color}" stroke-width="2.6"/>')
        out.append(f'<text x="{x + 32:.0f}" y="{10 + row:.0f}" class="legend-text">'
                   f'{_esc(text)}</text>')
        x += ancho
    if engine is not None:
        stats = getattr(engine, "step_count", 0)
        t = getattr(engine, "time", 0.0)
        txt = f'simulado: t={t:g} s · {stats} eventos'
        if x + len(txt) * 5.6 > W - 40:
            x = 0.0
            row += 15.0
        out.append(f'<text x="{x + 2:.0f}" y="{10 + row:.0f}" class="legend-text">'
                   f'{_esc(txt)}</text>')
    out.append("</g>")
    return "\n".join(out)


def _titleblock(W: float, H: float, circuit: Circuit, title: Optional[str]) -> str:
    y = H - 34
    t = title or circuit.title or circuit.name
    meta = circuit.metadata.get("meta") or {}
    author = meta.get("author", "")
    return (
        f'<g class="titleblock">'
        f'<line x1="14" y1="{y - 12:.0f}" x2="{W - 14:.0f}" y2="{y - 12:.0f}" class="frame"/>'
        f'<text x="18" y="{y + 6:.0f}" class="tb-title">{_esc(str(t))}</text>'
        f'<text x="18" y="{y + 22:.0f}" class="tb-sub">'
        f'{len(circuit.parts)} componentes · {len(circuit.nets)} redes'
        f'{(" · " + _esc(str(author))) if author else ""}'
        f'</text>'
        f'<text x="{W - 18:.0f}" y="{y + 22:.0f}" class="tb-sub" text-anchor="end">'
        f'Voltio {_esc(circuit.name)}</text>'
        f'</g>'
    )


def _esc(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_CSS = """
  .grid       { fill: #d8dce2; }
  .body       { fill: #ffffff; stroke: #23272e; stroke-width: 1.6; }
  .wire-only  { fill: none; stroke: #23272e; stroke-width: 1.6; }
  .wire       { fill: none; stroke: #4a4f57; stroke-width: 1.6; stroke-linejoin: round; }
  .busband    { fill: none; stroke: #23272e; stroke-width: 4.5; stroke-linejoin: round; }
  .bus-slash  { stroke: #23272e; stroke-width: 1.6; }
  .junction   { fill: #23272e; }
  .contact    { fill: #ffffff; stroke: #23272e; stroke-width: 1.4; }
  .emit       { fill: none; stroke: #d21b1b; stroke-width: 1.2; }
  .seg        { stroke: #c9ced6; stroke-width: 5; stroke-linecap: round; fill: none; }
  .seg-on     { stroke: #ff2d2d; stroke-width: 5; stroke-linecap: round; fill: none; }
  .lcd-screen { fill: #b9d7a8; stroke: #23272e; stroke-width: 1.2; }
  .lcd-text   { font: 600 9px ui-monospace, Menlo, Consolas, monospace; fill: #16210f; }
  .led-glow   { fill: #ff3b30; opacity: 0.35; }
  .refdes     { font: 700 11px ui-sans-serif, system-ui, sans-serif; fill: #23272e;
                text-anchor: middle; }
  .devname    { font: 10px ui-sans-serif, system-ui, sans-serif; fill: #565c66;
                text-anchor: middle; }
  .pinname    { font: 8.5px ui-monospace, Menlo, Consolas, monospace; fill: #6b7280; }
  .net-label  { font: 9.5px ui-monospace, Menlo, Consolas, monospace; fill: #2f6fb3;
                text-anchor: middle; stroke: #fbfbfd; stroke-width: 3px;
                paint-order: stroke; }
  .state-text { font: 700 12px ui-monospace, Menlo, Consolas, monospace; fill: #23272e;
                text-anchor: middle; }
  .probe-text { font: 700 11px ui-monospace, Menlo, Consolas, monospace; fill: #0a8f2c;
                text-anchor: middle; }
  .live-value { font: 9px ui-monospace, Menlo, Consolas, monospace; fill: #7a4bd1;
                text-anchor: middle; }
  .symbol-letter { font: 700 12px ui-sans-serif, system-ui, sans-serif; fill: #23272e;
                   text-anchor: middle; }
  .tie-box    { fill: #fff6d6; stroke: #b58900; stroke-width: 1.2; }
  .tie-text   { font: 700 11px ui-monospace, Menlo, Consolas, monospace; fill: #7a5c00;
                text-anchor: middle; }
  .rail .wire { stroke: #23272e; }
  .legend-text{ font: 10px ui-sans-serif, system-ui, sans-serif; fill: #43484f; }
  .frame      { stroke: #b6bcc6; stroke-width: 1; }
  .tb-title   { font: 700 14px ui-sans-serif, system-ui, sans-serif; fill: #1b1f26; }
  .tb-sub     { font: 10px ui-sans-serif, system-ui, sans-serif; fill: #6b7280; }
  svg         { background: #fbfbfd; }
"""


def _header(W: float, H: float) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
        f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="ui-sans-serif, system-ui, sans-serif">\n'
        f"<style>{_CSS}</style>\n"
        f'<rect width="{W:.0f}" height="{H:.0f}" fill="#fbfbfd"/>'
    )


def save_svg(circuit: Circuit, path, engine=None, **kw) -> str:
    svg = to_svg(circuit, engine=engine, **kw)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path
