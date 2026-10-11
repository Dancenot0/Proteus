"""Canvas esquemático: ítems gráficos y dibujado de símbolos.

Los símbolos se pintan con ``QPainter`` directamente, en vez de reutilizar el
SVG de :mod:`voltio.render`. La razón es práctica: un ítem de QGraphicsScene
necesita pintarse a sí mismo en coordenadas locales y responder al ratón, y
meter un parser de SVG por medio solo añadiría capas sin aportar nada. La
geometría de los pines sí es compartida (``voltio.render.symbols.pin_anchors``),
de modo que un símbolo colocado en la GUI y el mismo circuito exportado a SVG
tienen las patillas en el mismo sitio.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
)

from ..core.ir import Circuit, Net, Part
from ..core.logic import L
from ..render.symbols import PIN_STUB, pin_anchors

__all__ = ["SchematicView", "PartItem", "WireItem", "PinHandle",
           "COLORS", "UNIT", "GRID"]

UNIT = 10.0        # px por unidad de esquema
GRID = 10.0        # paso de la rejilla de enganche

COLORS = {
    L.LOW: QColor("#1a1a1a"),
    L.HIGH: QColor("#0a8f2c"),
    L.X: QColor("#9aa0a6"),
    L.Z: QColor("#2f6fb3"),
}
COLOR_COLISION = QColor("#d21b1b")
COLOR_CABLE = QColor("#4a4f57")
COLOR_SELECCION = QColor("#2f6fb3")


def color_de(nivel: L, colision: bool = False) -> QColor:
    if colision:
        return COLOR_COLISION
    return COLORS.get(nivel, COLOR_CABLE)


# ---------------------------------------------------------------------------
# Ítem de componente
# ---------------------------------------------------------------------------


class PartItem(QGraphicsItem):
    """Un componente en el canvas. Se puede arrastrar; sus pines se pueden clicar."""

    Type = QGraphicsItem.UserType + 1

    def __init__(self, part: Part, engine=None):
        super().__init__()
        self.part = part
        self.engine = engine
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        # Las posiciones del DSL ya están en píxeles de rejilla.
        self.setPos(part.x, part.y)
        self.w = max((part.spec.width if part.spec else 4.0) * UNIT, 28.0)
        self.h = max((part.spec.height if part.spec else 3.0) * UNIT, 22.0)
        self.anchors: Dict[str, List[Tuple[float, float]]] = (
            pin_anchors(self.w, self.h, part.spec) if part.spec else {}
        )
        self.handles: List["PinHandle"] = []
        self.setZValue(10)

    # -- geometría --------------------------------------------------------
    def boundingRect(self) -> QRectF:
        m = PIN_STUB + 16
        return QRectF(-m, -m, self.w + 2 * m, self.h + 2 * m)

    def shape(self) -> QPainterPath:
        p = QPainterPath()
        p.addRect(QRectF(0, 0, self.w, self.h))
        return p

    def pin_scene_pos(self, pin: str, bit: int = 0) -> QPointF:
        """Posición en coordenadas de escena de un punto de conexión."""
        pts = self.anchors.get(pin, [])
        if not pts:
            return self.scenePos()
        x, y = pts[min(bit, len(pts) - 1)]
        side = self._side_of(pin)
        if side == "left":
            x -= PIN_STUB
        elif side == "right":
            x += PIN_STUB
        elif side == "top":
            y -= PIN_STUB
        else:
            y += PIN_STUB
        return self.mapToScene(QPointF(x, y))

    def _side_of(self, pin: str) -> str:
        if self.part.spec is None:
            return "left"
        p = self.part.spec.pin(pin)
        return p.side if p else "left"

    def pin_at(self, local: QPointF) -> Optional[Tuple[str, int]]:
        """Qué pin hay en un punto local, si hay alguno (tolerancia 6 px)."""
        best, best_d = None, 8.0
        for name, pts in self.anchors.items():
            for i, (x, y) in enumerate(pts):
                d = ((local.x() - x) ** 2 + (local.y() - y) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = (name, i), d
        return best

    # -- pintado ----------------------------------------------------------
    def paint(self, painter: QPainter, option, widget=None) -> None:
        spec = self.part.spec
        symbol = spec.symbol if spec else "ic"
        sel = self.isSelected()

        painter.setRenderHint(QPainter.Antialiasing, True)
        body = QPen(QColor("#23272e"), 1.6)
        if sel:
            body = QPen(COLOR_SELECCION, 2.2)
        painter.setPen(body)
        painter.setBrush(QBrush(QColor("#ffffff")))

        _draw_symbol(painter, symbol, self.w, self.h, spec)

        # Rabitos de las patillas, igual que en el exportador SVG.
        if spec:
            painter.setPen(QPen(QColor("#23272e"), 1.6))
            for p in spec.pins:
                for ax, ay in self.anchors.get(p.name, []):
                    ex, ey = ax, ay
                    if p.side == "left":
                        ex -= PIN_STUB
                    elif p.side == "right":
                        ex += PIN_STUB
                    elif p.side == "top":
                        ey -= PIN_STUB
                    else:
                        ey += PIN_STUB
                    painter.drawLine(QPointF(ax, ay), QPointF(ex, ey))

        # Referencia y nombre
        painter.setPen(QPen(QColor("#23272e")))
        f = QFont("Helvetica", 9)
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(QRectF(0, -16, self.w, 14), Qt.AlignHCenter, self.part.ref)
        painter.setFont(QFont("Helvetica", 7))
        painter.setPen(QPen(QColor("#565c66")))
        if symbol in ("ic", "register", "counter", "memory", "mcu", "flipflop"):
            # Debajo de la caja: dentro choca con los nombres de patilla.
            painter.drawText(QRectF(-20, self.h + 2, self.w + 40, 13),
                             Qt.AlignHCenter, spec.name if spec else self.part.part)
        else:
            val = _valor(self.part)
            if val:
                painter.drawText(QRectF(-10, self.h + 2, self.w + 20, 14),
                                 Qt.AlignHCenter, val)

        # Nombres de patilla en los rectángulos
        if spec and symbol in ("ic", "register", "counter", "memory", "mcu", "flipflop"):
            painter.setFont(QFont("Courier", 6))
            painter.setPen(QPen(QColor("#6b7280")))
            for p in spec.pins:
                for i, (x, y) in enumerate(self.anchors.get(p.name, [])):
                    label = p.display_name() if p.width == 1 else f"{p.name}[{i}]"
                    if p.side == "left":
                        painter.drawText(QPointF(x + 4, y + 3), label)
                    elif p.side == "right":
                        painter.drawText(QPointF(x - 4 - 5 * len(label), y + 3), label)

        # Estado en vivo
        if self.engine is not None:
            self._paint_live(painter)

    def _paint_live(self, painter: QPainter) -> None:
        model = self.engine.model(self.part.ref)
        if model is None:
            return
        spec = self.part.spec
        st = model.state
        sym = spec.symbol if spec else "ic"

        if sym == "7seg" and hasattr(model, "segments_lit"):
            painter.setPen(QPen(QColor("#ff2d2d"), 4.5, cap=Qt.RoundCap))
            for name, on in zip("abcdefg", model.segments_lit()):
                if on:
                    x1, y1, x2, y2 = _SEG[name]
                    painter.drawLine(QPointF(self.w * x1, self.h * y1),
                                     QPointF(self.w * x2, self.h * y2))
        elif sym == "led" and st.get("on"):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(255, 59, 48, 90)))
            painter.drawEllipse(QPointF(self.w * 0.6, self.h / 2), self.h * 0.55, self.h * 0.55)
        elif sym == "logicstate":
            painter.setPen(QPen(QColor("#23272e")))
            painter.setFont(QFont("Courier", 10, QFont.Bold))
            painter.drawText(QRectF(0, 0, self.w * 0.7, self.h),
                             Qt.AlignCenter, str(st.get("v", L.X)))
        elif sym == "lcd" and hasattr(model, "text"):
            painter.setPen(QPen(QColor("#16210f")))
            painter.setFont(QFont("Courier", 7, QFont.Bold))
            for r, line in enumerate(model.text()[:2]):
                painter.drawText(QPointF(self.w * 0.1, self.h * (0.42 + 0.28 * r)),
                                 line.rstrip()[:20])
        else:
            probe = model.on_probe(None) if hasattr(model, "on_probe") else ""
            if probe:
                painter.setPen(QPen(QColor("#7a4bd1")))
                painter.setFont(QFont("Courier", 7))
                painter.drawText(QRectF(-20, self.h + 15, self.w + 40, 14),
                                 Qt.AlignHCenter, str(probe))

    # -- interacción ------------------------------------------------------
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            # Enganche a rejilla: sin esto es imposible cablear a mano.
            p = value
            self.part.x = round(p.x() / GRID) * GRID
            self.part.y = round(p.y() / GRID) * GRID
            for w in self.handles:
                w.actualizar()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event):
        """Doble clic en un componente interactivo: lo conmuta."""
        if self.engine is not None:
            model = self.engine.model(self.part.ref)
            if model is not None and model.interactive():
                actual = model.state.get("v", L.LOW) if "v" in model.state \
                    else not model.state.get("closed", False)
                if "v" in model.state:
                    model.set_interactive(0 if actual is L.HIGH else 1)
                else:
                    model.set_interactive(0 if model.state.get("closed") else 1)
                self.engine.settle()
                scene = self.scene()
                if scene is not None:
                    scene.actualizar_todo()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


_SEG = {
    "a": (0.22, 0.10, 0.78, 0.10),
    "b": (0.80, 0.13, 0.80, 0.46),
    "c": (0.80, 0.54, 0.80, 0.87),
    "d": (0.22, 0.90, 0.78, 0.90),
    "e": (0.20, 0.54, 0.20, 0.87),
    "f": (0.20, 0.13, 0.20, 0.46),
    "g": (0.22, 0.50, 0.78, 0.50),
}


def _valor(part: Part) -> str:
    for k in ("R", "C", "L", "VALUE", "FREQ", "COLOR"):
        if k in part.props:
            return str(part.props[k]) if k == "VALUE" else f"{k}={part.props[k]}"
    return ""


def _draw_symbol(painter: QPainter, symbol: str, w: float, h: float, spec) -> None:
    """Dibuja el cuerpo del símbolo. Espejo en QPainter de ``render.symbols``."""
    pen_c = QPen(QColor("#23272e"), 1.6)
    if symbol in ("gate2", "inverter", "buffer", "tribuf"):
        kind = (spec.model or "and") if spec else "and"
        for k in ("nand", "nor", "xnor", "and", "or", "xor"):
            if kind.startswith(k):
                kind = k
                break
        r = h / 2.0
        path = QPainterPath()
        if kind in ("and", "nand"):
            path.moveTo(0, 0)
            path.lineTo(w - r, 0)
            path.arcTo(QRectF(w - 2 * r, 0, 2 * r, h), 90, -180)
            path.lineTo(0, h)
            path.closeSubpath()
        elif kind in ("or", "nor"):
            path.moveTo(0, 0)
            path.quadTo(QPointF(w * 0.35, h / 2), QPointF(0, h))
            path.quadTo(QPointF(w * 0.6, h), QPointF(w, h / 2))
            path.quadTo(QPointF(w * 0.6, 0), QPointF(0, 0))
        elif kind in ("xor", "xnor"):
            path.moveTo(w * 0.12, 0)
            path.quadTo(QPointF(w * 0.47, h / 2), QPointF(w * 0.12, h))
            path.quadTo(QPointF(w * 0.72, h), QPointF(w, h / 2))
            path.quadTo(QPointF(w * 0.72, 0), QPointF(w * 0.12, 0))
        else:  # buffer
            path.moveTo(0, 0)
            path.lineTo(w, h / 2)
            path.lineTo(0, h)
            path.closeSubpath()
        painter.setPen(pen_c)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawPath(path)
        if kind in ("nand", "nor", "xnor"):
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawEllipse(QPointF(w + 3.5, h / 2), 3.2, 3.2)
        if symbol == "tribuf":
            painter.drawLine(QPointF(w * 0.3, h), QPointF(w * 0.3, h * 0.55))
    elif symbol == "resistor":
        painter.setPen(pen_c)
        painter.setBrush(Qt.NoBrush)
        mid = h / 2
        seg = (w - 12) / 6
        pts = [QPointF(0, mid), QPointF(6, mid)]
        for i in range(6):
            pts.append(QPointF(6 + seg * (i + 0.5), mid + (-5 if i % 2 == 0 else 5)))
        pts += [QPointF(w - 6, mid), QPointF(w, mid)]
        for a, b in zip(pts, pts[1:]):
            painter.drawLine(a, b)
    elif symbol == "led":
        mid = h / 2
        tri = w * 0.5
        painter.setPen(pen_c)
        painter.drawLine(QPointF(0, mid), QPointF(tri, mid))
        p = QPainterPath(QPointF(tri, mid - 6))
        p.lineTo(tri + 10, mid)
        p.lineTo(tri, mid + 6)
        p.closeSubpath()
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawPath(p)
        painter.drawLine(QPointF(tri + 10, mid - 6), QPointF(tri + 10, mid + 6))
        painter.drawLine(QPointF(tri + 10, mid), QPointF(w, mid))
        painter.setPen(QPen(QColor("#d21b1b"), 1.2))
        painter.drawLine(QPointF(tri + 6, mid - 8), QPointF(tri + 11, mid - 13))
        painter.drawLine(QPointF(tri + 10, mid - 5), QPointF(tri + 15, mid - 10))
    elif symbol == "7seg":
        painter.setPen(pen_c)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRect(QRectF(0, 0, w, h))
        painter.setPen(QPen(QColor("#c9ced6"), 4.5, cap=Qt.RoundCap))
        for x1, y1, x2, y2 in _SEG.values():
            painter.drawLine(QPointF(w * x1, h * y1), QPointF(w * x2, h * y2))
    elif symbol == "lcd":
        painter.setPen(pen_c)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRect(QRectF(0, 0, w, h))
        painter.setBrush(QBrush(QColor("#b9d7a8")))
        painter.drawRect(QRectF(w * 0.07, h * 0.15, w * 0.86, h * 0.7))
    elif symbol in ("switch", "pushbutton"):
        mid = h / 2
        painter.setPen(pen_c)
        painter.drawLine(QPointF(0, mid), QPointF(w * 0.35, mid))
        painter.drawLine(QPointF(w * 0.72, mid), QPointF(w, mid))
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(QPointF(w * 0.35, mid), 2.5, 2.5)
        painter.drawEllipse(QPointF(w * 0.72, mid), 2.5, 2.5)
        painter.drawLine(QPointF(w * 0.35, mid), QPointF(w * 0.72, mid - 7))
    elif symbol == "gnd":
        painter.setPen(pen_c)
        mid = w / 2
        painter.drawLine(QPointF(mid, 0), QPointF(mid, h * 0.45))
        for frac, yy in ((1.0, 0.45), (0.6, 0.68), (0.25, 0.9)):
            painter.drawLine(QPointF(mid - 10 * frac, h * yy), QPointF(mid + 10 * frac, h * yy))
    elif symbol == "vcc":
        painter.setPen(pen_c)
        mid = w / 2
        painter.drawLine(QPointF(mid, h), QPointF(mid, h * 0.35))
        p = QPainterPath(QPointF(mid - 8, h * 0.35))
        p.lineTo(mid, h * 0.05)
        p.lineTo(mid + 8, h * 0.35)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(p)
    elif symbol == "clock":
        painter.setPen(pen_c)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRect(QRectF(0, 0, w * 0.75, h))
        mid = h / 2
        p = QPainterPath(QPointF(w * 0.1, mid + 5))
        for x, y in ((0.22, 5), (0.22, -5), (0.4, -5), (0.4, 5), (0.55, 5)):
            p.lineTo(w * x, mid + y)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(p)
        painter.drawLine(QPointF(w * 0.75, mid), QPointF(w, mid))
    elif symbol == "probe":
        mid = h / 2
        painter.setPen(pen_c)
        painter.drawLine(QPointF(0, mid), QPointF(w * 0.3, mid))
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(QPointF(w * 0.55, mid), h * 0.42, h * 0.42)
    elif symbol == "logicstate":
        painter.setPen(pen_c)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRect(QRectF(0, 0, w * 0.7, h))
        painter.drawLine(QPointF(w * 0.7, h / 2), QPointF(w, h / 2))
    elif symbol in ("motor", "buzzer"):
        painter.setPen(pen_c)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(QRectF(2, 2, w - 4, h - 4))
        painter.drawText(QRectF(0, 0, w, h), Qt.AlignCenter,
                         "M" if symbol == "motor" else "B")
    else:  # ic, register, counter, memory, mcu, flipflop, terminal, ...
        painter.setPen(pen_c)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRect(QRectF(0, 0, w, h))
        if symbol == "flipflop":
            painter.setBrush(Qt.NoBrush)
            p = QPainterPath(QPointF(0, h * 0.62))
            p.lineTo(5, h * 0.55)
            p.lineTo(0, h * 0.48)
            painter.drawPath(p)


# ---------------------------------------------------------------------------
# Cables
# ---------------------------------------------------------------------------


class WireItem(QGraphicsPathItem):
    """Un cable. Se redrawa cuando se mueve alguno de sus extremos."""

    def __init__(self, net: Net, view: "SchematicView"):
        super().__init__()
        self.net = net
        self.view = view
        self.setPen(QPen(COLOR_CABLE, 2.6 if net.kind == "bus" else 1.6,
                         cap=Qt.RoundCap, join=Qt.RoundJoin))
        self.setZValue(1)
        self.actualizar()

    def extremos(self) -> List[QPointF]:
        out = []
        for ep in self.net.endpoints:
            if ep.part == "$TIE":
                continue
            item = self.view.item_de(ep.part)
            if item is not None:
                out.append(item.pin_scene_pos(ep.pin, ep.bit))
        return out

    def actualizar(self) -> None:
        pts = self.extremos()
        path = QPainterPath()
        if len(pts) == 2:
            a, b = pts
            path.moveTo(a)
            if abs(a.x() - b.x()) < 1 or abs(a.y() - b.y()) < 1:
                path.lineTo(b)
            else:
                path.lineTo(QPointF(b.x(), a.y()))
                path.lineTo(b)
        elif len(pts) > 2:
            cx = sum(p.x() for p in pts) / len(pts)
            ys = sorted(p.y() for p in pts)
            path.moveTo(QPointF(cx, ys[0]))
            path.lineTo(QPointF(cx, ys[-1]))
            for p in pts:
                path.moveTo(p)
                path.lineTo(QPointF(cx, p.y()))
        self.setPath(path)

    def pintar_nivel(self, nivel: Optional[L], colision: bool = False) -> None:
        c = color_de(nivel, colision) if nivel is not None else COLOR_CABLE
        pen = QPen(c, 3.2 if self.net.kind == "bus" else 2.0,
                   cap=Qt.RoundCap, join=Qt.RoundJoin)
        self.setPen(pen)


class PinHandle(QGraphicsRectItem):
    """Cuadradito clicable en cada patilla, para empezar a cablear."""

    def __init__(self, item: PartItem, pin: str, bit: int):
        super().__init__(-3, -3, 6, 6, item)
        self.item, self.pin, self.bit = item, pin, bit
        self.setBrush(QBrush(QColor("#2f6fb3")))
        self.setPen(QPen(Qt.NoPen))
        self.setVisible(False)
        self.setZValue(20)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def mousePressEvent(self, event):
        self.item.scene().empezar_cable(self.item, self.pin, self.bit)
        event.accept()


# ---------------------------------------------------------------------------
# Vista
# ---------------------------------------------------------------------------


class SchematicView(QGraphicsView):
    """La vista principal. Dibuja la rejilla y gestiona el cableado."""

    cable_creado = Signal(str, str, int)   # net, ref, bit

    def __init__(self, circuit: Circuit, engine=None, parent=None):
        super().__init__(parent)
        self.circuit = circuit
        self.engine = engine
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.items_part: Dict[str, PartItem] = {}
        self.items_wire: Dict[str, WireItem] = {}
        self._cable_pendiente: Optional[Tuple[PartItem, str, int]] = None
        self._rubber: Optional[QGraphicsLineItem] = None
        self.setBackgroundBrush(QBrush(QColor("#fbfbfd")))
        self.construir()

    # -- construcción -----------------------------------------------------
    def construir(self) -> None:
        self.scene_.clear()
        self.items_part.clear()
        self.items_wire.clear()
        for ref, part in self.circuit.parts.items():
            item = PartItem(part, self.engine)
            self.scene_.addItem(item)
            self.items_part[ref] = item
            if part.spec is not None:
                for p in part.spec.pins:
                    puntos = item.anchors.get(p.name) or [(0.0, 0.0)]
                    for bit in range(max(p.width, 1)):
                        h = PinHandle(item, p.name, bit)
                        h.setPos(*puntos[min(bit, len(puntos) - 1)])
                        item.handles.append(h)
        for net in self.circuit.nets.values():
            w = WireItem(net, self)
            self.scene_.addItem(w)
            self.items_wire[net.name] = w
        self.ajustar_vista()

    def ajustar_vista(self) -> None:
        r = self.scene_.itemsBoundingRect()
        if r.isValid():
            self.scene_.setSceneRect(r.adjusted(-60, -60, 60, 60))
            self.fitInView(r.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)

    def item_de(self, ref: str) -> Optional[PartItem]:
        return self.items_part.get(ref)

    # -- refresco ---------------------------------------------------------
    def actualizar_todo(self) -> None:
        for w in self.items_wire.values():
            w.actualizar()
        self.pintar_niveles()
        self.scene_.update()

    def pintar_niveles(self) -> None:
        """Colorea cada cable según su nivel lógico simulado."""
        if self.engine is None:
            return
        for name, w in self.items_wire.items():
            nivel = self.engine.net_value(name)
            colision = any(p.kind == "collision" and p.where == (w.net.label)
                           for p in self.engine.problems)
            w.pintar_nivel(nivel, colision)

    # -- cableado interactivo ---------------------------------------------
    def empezar_cable(self, item: PartItem, pin: str, bit: int) -> None:
        if self._cable_pendiente is None:
            self._cable_pendiente = (item, pin, bit)
            self._rubber = self.scene_.addLine(0, 0, 0, 0,
                                               QPen(COLOR_SELECCION, 1.4, Qt.DashLine))
            self._rubber.setZValue(50)
        else:
            a_item, a_pin, a_bit = self._cable_pendiente
            if a_item is not item or a_pin != pin or a_bit != bit:
                self.cable_creado.emit(
                    f"{a_item.part.ref}.{a_pin}[{a_bit}]", item.part.ref, bit
                )
            self._cancelar_cable()

    def _cancelar_cable(self) -> None:
        self._cable_pendiente = None
        if self._rubber is not None:
            self.scene_.removeItem(self._rubber)
            self._rubber = None

    def mouseMoveEvent(self, event):
        if self._rubber is not None and self._cable_pendiente is not None:
            item, pin, bit = self._cable_pendiente
            a = item.pin_scene_pos(pin, bit)
            b = self.mapToScene(event.pos())
            self._rubber.setLine(a.x(), a.y(), b.x(), b.y())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if self._cable_pendiente is not None and event.button() == Qt.RightButton:
            self._cancelar_cable()
            event.accept()
            return
        super().mousePressEvent(event)

    # -- zoom -------------------------------------------------------------
    def wheelEvent(self, event):
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)

    # -- rejilla de fondo -------------------------------------------------
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        step = GRID * 2
        left = int(rect.left() // step) * step
        top = int(rect.top() // step) * step
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#d8dce2")))
        x = left
        while x < rect.right():
            y = top
            while y < rect.bottom():
                painter.drawEllipse(QPointF(x, y), 0.9, 0.9)
                y += step
            x += step
