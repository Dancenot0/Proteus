"""Ventana principal de la GUI.

Diseño deliberadamente conservador: el canvas vive en
:mod:`voltio.gui.canvas` y toda la lógica de circuito sigue en el núcleo. La
GUI no sabe simular ni validar; llama al motor y muestra lo que este devuelve.
Así se puede rehacer la interfaz entera (o sustituirla por una web) sin tocar
nada de la parte que importa.

Requiere ``pip install PySide6``.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.ir import Circuit, Part
from ..core.logic import L
from ..dsl.loader import DslError, dump_file, load_file
from ..parts.library import LIBRARY
from ..reverse import describe_local, summary_lines
from ..sim import Engine
from .canvas import SchematicView

__all__ = ["run", "MainWindow"]


class GenerarDialog(QDialog):
    """Diálogo para pedir un circuito a la IA."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generar circuito con IA")
        self.resize(620, 460)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("Describe el circuito que quieres:"))
        self.descripcion = QPlainTextEdit()
        self.descripcion.setPlaceholderText(
            "Ej: un contador de 4 bits con un 74LS193, un decodificador 74LS48 "
            "y un display de 7 segmentos, con reloj de 1 kHz."
        )
        self.descripcion.setFixedHeight(90)
        lay.addWidget(self.descripcion)

        lay.addWidget(QLabel("Datos o restricciones adicionales (opcional):"))
        self.extra = QPlainTextEdit()
        self.extra.setFixedHeight(60)
        lay.addWidget(self.extra)

        fila = QHBoxLayout()
        fila.addWidget(QLabel("Proveedor:"))
        self.proveedor = QComboBox()
        self.proveedor.addItems(["clipboard", "anthropic", "openai", "gemini", "ollama"])
        fila.addWidget(self.proveedor)
        fila.addWidget(QLabel("Modelo:"))
        self.modelo = QLineEdit()
        self.modelo.setPlaceholderText("(el del proveedor)")
        fila.addWidget(self.modelo)
        lay.addLayout(fila)

        fila2 = QHBoxLayout()
        fila2.addWidget(QLabel("Intentos de autocorrección:"))
        self.intentos = QComboBox()
        self.intentos.addItems(["1", "2", "3", "4", "5"])
        self.intentos.setCurrentIndex(2)
        fila2.addWidget(self.intentos)
        fila2.addStretch()
        lay.addLayout(fila2)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Aquí aparecerá el progreso de la generación.")
        lay.addWidget(self.log, 1)

        botones = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        botones.button(QDialogButtonBox.Ok).setText("Generar")
        botones.button(QDialogButtonBox.Cancel).setText("Cancelar")
        botones.accepted.connect(self.accept)
        botones.rejected.connect(self.reject)
        lay.addWidget(botones)

    def valores(self) -> Dict[str, Any]:
        return {
            "descripcion": self.descripcion.toPlainText().strip(),
            "extra": self.extra.toPlainText().strip(),
            "proveedor": self.proveedor.currentText(),
            "modelo": self.modelo.text().strip() or None,
            "intentos": int(self.intentos.currentText()),
        }


class MainWindow(QMainWindow):
    def __init__(self, circuit: Optional[Circuit] = None, dsl_path: str = "",
                 duration: float = 1e-3):
        super().__init__()
        self.setWindowTitle("Voltio" + (f" — {os.path.basename(dsl_path)}" if dsl_path else ""))
        self.resize(1440, 900)

        self.circuit: Optional[Circuit] = circuit
        self.dsl_path = dsl_path
        self.duration = duration
        self.engine: Optional[Engine] = None
        self.simulando = False

        self.view = SchematicView(circuit if circuit else Circuit("nuevo"))
        self.view.cable_creado.connect(self._cable_manual)
        self.setCentralWidget(self.view)

        self._construir_menu()
        self._construir_docks()
        self._construir_estado()

        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._paso_simulacion)

        if circuit is not None:
            self.recargar(circuit)

    # -- interfaz ---------------------------------------------------------
    def _construir_menu(self) -> None:
        bar = self.menuBar()

        m = bar.addMenu("&Archivo")
        self._accion(m, "&Abrir DSL…", self.abrir, QKeySequence.Open)
        self._accion(m, "&Guardar DSL", self.guardar, QKeySequence.Save)
        self._accion(m, "Guardar DSL &como…", self.guardar_como)
        m.addSeparator()
        self._accion(m, "Exportar &SVG…", self.exportar_svg)
        self._accion(m, "Exportar &SPICE…", lambda: self._exportar("spice"))
        self._accion(m, "Exportar &EDIF…", lambda: self._exportar("edif"))
        self._accion(m, "Exportar &BOM (CSV)…", lambda: self._exportar("bom"))
        m.addSeparator()
        self._accion(m, "&Salir", self.close, QKeySequence.Quit)

        m = bar.addMenu("&IA")
        self._accion(m, "&Generar circuito desde descripción…", self.generar_ia,
                     QKeySequence("Ctrl+G"))
        self._accion(m, "&Explicar circuito…", self.explicar_ia)
        m.addSeparator()
        self._accion(m, "Ver &estado de proveedores", self.ver_proveedores)

        m = bar.addMenu("&Simular")
        self.act_run = self._accion(m, "&Ejecutar", self.ejecutar, QKeySequence("F5"))
        self.act_step = self._accion(m, "&Paso", self.paso, QKeySequence("F6"))
        self.act_stop = self._accion(m, "&Detener y reiniciar", self.parar, QKeySequence("F7"))
        m.addSeparator()
        self._accion(m, "&Validar circuito", self.validar, QKeySequence("Ctrl+R"))
        self._accion(m, "Duración de la simulación…", self.cambiar_duracion)

        m = bar.addMenu("&Ver")
        self._accion(m, "&Ajustar a la ventana", self.view.ajustar_vista, QKeySequence("Ctrl+0"))
        self._accion(m, "Analizar circuito (sin IA)", self.analizar)

        m = bar.addMenu("A&yuda")
        self._accion(m, "Componentes disponibles", self.ver_partes)
        self._accion(m, "Acerca de Voltio", self.acerca_de)

    def _accion(self, menu, texto, slot, atajo=None) -> QAction:
        a = QAction(texto, self)
        if atajo is not None:
            a.setShortcut(atajo)
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _construir_docks(self) -> None:
        # -- Paleta de componentes ----------------------------------------
        dock = QDockWidget("Componentes", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        w = QWidget()
        lay = QVBoxLayout(w)
        self.busca = QLineEdit()
        self.busca.setPlaceholderText("Buscar (74, led, mux, memoria…)")
        self.busca.textChanged.connect(self._refrescar_paleta)
        lay.addWidget(self.busca)
        self.arbol = QTreeWidget()
        self.arbol.setHeaderHidden(True)
        self.arbol.itemDoubleClicked.connect(self._anadir_componente)
        lay.addWidget(self.arbol)
        lay.addWidget(QLabel("Doble clic para colocar"))
        dock.setWidget(w)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self._refrescar_paleta()

        # -- Panel derecho: propiedades + consola --------------------------
        dock2 = QDockWidget("Inspector", self)
        dock2.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        tabs = QTabWidget()

        self.props_form = QFormLayout()
        self.props_widget = QWidget()
        self.props_widget.setLayout(self.props_form)
        tabs.addTab(self.props_widget, "Propiedades")

        self.consola = QPlainTextEdit()
        self.consola.setReadOnly(True)
        self.consola.setStyleSheet("font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px")
        tabs.addTab(self.consola, "Consola")

        self.panel_ondas = QPlainTextEdit()
        self.panel_ondas.setReadOnly(True)
        self.panel_ondas.setStyleSheet("font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 10px")
        tabs.addTab(self.panel_ondas, "Formas de onda")

        self.panel_estado = QListWidget()
        tabs.addTab(self.panel_estado, "Indicadores")

        dock2.setWidget(tabs)
        self.tabs = tabs
        self.addDockWidget(Qt.RightDockWidgetArea, dock2)
        self.tabs_right = dock2

        self.view.scene_.selectionChanged.connect(self._mostrar_propiedades)

    def _construir_estado(self) -> None:
        sb = self.statusBar()
        self.lbl_estado = QLabel("listo")
        self.lbl_tiempo = QLabel("t = 0")
        sb.addWidget(self.lbl_estado, 1)
        sb.addPermanentWidget(self.lbl_tiempo)

    # -- paleta -----------------------------------------------------------
    def _refrescar_paleta(self, texto: str = "") -> None:
        self.arbol.clear()
        specs = LIBRARY.search(texto) if texto else list(LIBRARY.specs())
        grupos: Dict[str, List] = {}
        for s in specs:
            grupos.setdefault(s.category, []).append(s)
        for cat in sorted(grupos):
            nodo = QTreeWidgetItem(self.arbol, [f"{cat}  ({len(grupos[cat])})"])
            nodo.setFlags(nodo.flags() & ~Qt.ItemIsSelectable)
            for s in sorted(grupos[cat], key=lambda x: x.name):
                hijo = QTreeWidgetItem(nodo, [f"{s.name} — {s.description}"])
                hijo.setData(0, Qt.UserRole, s.name)
                hijo.setToolTip(0, _tooltip(s))
            nodo.setExpanded(bool(texto))
        self.arbol.resizeColumnToContents(0)

    def _anadir_componente(self, item: QTreeWidgetItem, col: int) -> None:
        nombre = item.data(0, Qt.UserRole)
        if not nombre:
            return
        if self.circuit is None:
            return
        spec = LIBRARY.get(nombre)
        ref = self._ref_libre(spec)
        # Se coloca en el centro de la vista actual.
        centro = self.view.mapToScene(self.view.viewport().rect().center())
        p = Part(ref=ref, part=spec.name, x=centro.x() / 10.0, y=centro.y() / 10.0,
                 props=spec.defaults())
        self.circuit.add_part(p)
        self.recargar(self.circuit, mantener_vista=True)
        self.lbl_estado.setText(f"añadido {ref} ({spec.name})")

    def _ref_libre(self, spec) -> str:
        prefijos = {"74xx": "U", "microcontroladores": "U", "cpu": "U",
                    "memoria": "U", "displays": "DISP", "optoelectronica": "D",
                    "pasivos": "R", "electromecanicos": "SW", "fuentes": "V",
                    "instrumentos": "P", "primitivas": "U"}
        base = prefijos.get(spec.category, "U")
        if spec.name in ("GND", "VCC"):
            base = spec.name[:3]
        elif spec.name == "CLOCK":
            base = "CLK"
        n = 1
        while f"{base}{n}" in (self.circuit.parts if self.circuit else {}):
            n += 1
        return f"{base}{n}"

    # -- circuito ---------------------------------------------------------
    def recargar(self, circuit: Circuit, mantener_vista: bool = False) -> None:
        self.circuit = circuit
        self.engine = Engine(circuit)
        self.view.circuit = circuit
        self.view.engine = self.engine
        rect = self.view.scene_.sceneRect() if mantener_vista else None
        self.view.construir()
        if mantener_vista and rect.isValid():
            self.view.setSceneRect(rect)
        self.validar(silencioso=True)
        self._pintar_indicadores()
        self.lbl_tiempo.setText("t = 0")

    def abrir(self) -> None:
        ruta, _ = QFileDialog.getOpenFileName(
            self, "Abrir DSL de Voltio", "",
            "Voltio DSL (*.yaml *.yml);;Todos (*)"
        )
        if not ruta:
            return
        try:
            circuit, issues = load_file(ruta)
        except DslError as e:
            QMessageBox.critical(self, "No se pudo cargar", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Error inesperado", f"{type(e).__name__}: {e}")
            return
        self.dsl_path = ruta
        self.sim = (circuit.metadata.get("simulation") or {}) if circuit.metadata else {}
        d = self.sim.get("duration")
        if isinstance(d, (int, float)):
            self.duration = float(d)
        self.setWindowTitle(f"Voltio — {os.path.basename(ruta)}")
        self.recargar(circuit)
        self.escribir_consola(
            f"Cargado {ruta}\n"
            f"  {len(circuit.parts)} componentes, {len(circuit.nets)} redes, "
            f"{len(circuit.buses)} buses\n"
            + "".join(f"  {i}\n" for i in issues if i.kind != "info")
        )

    def guardar(self) -> None:
        if not self.dsl_path:
            self.guardar_como()
            return
        self._guardar_en(self.dsl_path)

    def guardar_como(self) -> None:
        ruta, _ = QFileDialog.getSaveFileName(
            self, "Guardar DSL", "circuito.yaml", "Voltio DSL (*.yaml)"
        )
        if ruta:
            self._guardar_en(ruta)
            self.dsl_path = ruta

    def _guardar_en(self, ruta: str) -> None:
        if self.circuit is None:
            return
        try:
            dump_file(self.circuit, ruta)
            self.lbl_estado.setText(f"guardado en {ruta}")
        except Exception as e:
            QMessageBox.critical(self, "No se pudo guardar", f"{type(e).__name__}: {e}")

    # -- validación -------------------------------------------------------
    def validar(self, silencioso: bool = False) -> None:
        if self.circuit is None:
            return
        issues = self.circuit.validate()
        errs = [i for i in issues if i.kind == "error"]
        warns = [i for i in issues if i.kind == "warning"]
        if silencioso:
            self.lbl_estado.setText(
                f"{len(self.circuit.parts)} componentes · "
                f"{len(errs)} errores · {len(warns)} avisos"
            )
            if errs or warns:
                self.escribir_consola(
                    "Validación:\n"
                    + "".join(f"  {i}\n" for i in issues if i.kind != "info")
                )
            return
        texto = "".join(f"{i}\n" for i in issues if i.kind != "info") or "Sin problemas."
        QMessageBox.information(
            self, "Validación",
            f"{len(errs)} errores, {len(warns)} avisos.\n\n{texto}"
        )

    # -- simulación -------------------------------------------------------
    def ejecutar(self) -> None:
        if self.engine is None:
            return
        self.simulando = True
        self.timer.start()
        self.lbl_estado.setText("simulando…")

    def paso(self) -> None:
        if self.engine is None:
            return
        nxt = self.engine.next_event_time()
        if nxt is None:
            self.lbl_estado.setText("no quedan eventos programados")
            return
        self.engine.run(until=nxt + 1e-15)
        self._refrescar_tras_simulacion()

    def parar(self) -> None:
        self.timer.stop()
        self.simulando = False
        if self.engine is not None:
            self.engine.reset()
        self.view.actualizar_todo()
        self._pintar_indicadores()
        self.lbl_estado.setText("reiniciado")
        self.lbl_tiempo.setText("t = 0")

    def _paso_simulacion(self) -> None:
        if self.engine is None:
            return
        # Avanza un tramo proporcional a la duración pedida: la animación se ve
        # fluida sin tener que calcular cuántos eventos hay.
        paso = max(self.duration / 240.0, 1e-9)
        self.engine.run(until=self.engine.time + paso)
        self._refrescar_tras_simulacion()
        if self.engine.time >= self.duration or self.engine.halted:
            self.timer.stop()
            self.simulando = False
            self.lbl_estado.setText(
                "simulación completa" if not self.engine.problems
                else f"simulación completa con {len(self.engine.problems)} problemas"
            )

    def _refrescar_tras_simulacion(self) -> None:
        self.view.actualizar_todo()
        self._pintar_indicadores()
        self.lbl_tiempo.setText(f"t = {_fmt_t(self.engine.time)}")

    def cambiar_duracion(self) -> None:
        texto, ok = QInputDialog.getText(
            self, "Duración", "Cuánto simular (p. ej. 10ms, 1us, 0.5):",
            text=_fmt_t(self.duration)
        )
        if not ok:
            return
        from ..core.miniyaml import _parse_scalar
        v = _parse_scalar(texto.strip())
        if isinstance(v, (int, float)) and v > 0:
            self.duration = float(v)
            self.lbl_estado.setText(f"duración = {_fmt_t(self.duration)}")
        else:
            QMessageBox.warning(self, "No entiendo eso", f"'{texto}' no es una duración válida")

    # -- paneles ----------------------------------------------------------
    def _pintar_indicadores(self) -> None:
        self.panel_estado.clear()
        if self.engine is None:
            return
        for ref in sorted(self.engine.models()):
            model = self.engine.model(ref)
            spec = model.spec
            if spec is None:
                continue
            sym = spec.symbol
            texto = None
            if sym == "7seg" and hasattr(model, "segments_lit"):
                barras = "".join("#" if x else "." for x in model.segments_lit())
                texto = f"{ref:<8} display = {model.state.get('digit','?')!r}  [{barras}]"
            elif sym == "led":
                texto = f"{ref:<8} LED {'ENCENDIDO' if model.state.get('on') else 'apagado'}"
            elif sym == "lcd" and hasattr(model, "text"):
                texto = f"{ref:<8} LCD | " + " | ".join(t.rstrip() for t in model.text())
            elif sym == "logicstate":
                texto = f"{ref:<8} estado = {model.state.get('v', L.X)}"
            elif sym == "switch":
                texto = f"{ref:<8} {'CERRADO' if model.state.get('closed') else 'abierto'}"
            elif sym == "probe":
                texto = f"{ref:<8} sonda = {model.state.get('v', L.X)}"
            elif hasattr(model, "on_probe"):
                p = model.on_probe(None)
                if p:
                    texto = f"{ref:<8} {p}"
            if texto:
                it = QListWidgetItem(texto)
                if sym == "led" and model.state.get("on"):
                    it.setForeground(Qt.red)
                self.panel_estado.addItem(it)
        for p in self.engine.problems:
            it = QListWidgetItem(f"⚠ {p}")
            it.setForeground(Qt.darkRed)
            self.panel_estado.addItem(it)

    def _mostrar_propiedades(self) -> None:
        while self.props_form.rowCount():
            self.props_form.removeRow(0)
        items = self.view.scene_.selectedItems()
        partes = [i for i in items if hasattr(i, "part")]
        if not partes:
            self.props_form.addRow(QLabel("Selecciona un componente"))
            return
        part = partes[0].part
        spec = part.spec
        self.props_form.addRow(QLabel(f"<b>{part.ref}</b> — {part.part}"))
        if spec:
            self.props_form.addRow(QLabel(spec.description))
        for p in (spec.props if spec else []):
            actual = part.props.get(p.name, p.default)
            if p.kind == "choice" and p.choices:
                combo = QComboBox()
                combo.addItems([str(c) for c in p.choices])
                if str(actual) in [str(c) for c in p.choices]:
                    combo.setCurrentText(str(actual))
                combo.currentTextChanged.connect(
                    lambda v, k=p.name: self._set_prop(part, k, v)
                )
                self.props_form.addRow(p.label, combo)
            else:
                ed = QLineEdit("" if actual is None else str(actual))
                ed.editingFinished.connect(
                    lambda e=ed, k=p.name: self._set_prop(part, k, e.text())
                )
                self.props_form.addRow(p.label, ed)
        self.tabs.setCurrentWidget(self.props_widget)

    def _set_prop(self, part: Part, key: str, value: str) -> None:
        part.props[key] = value
        if self.engine is not None:
            model = self.engine.model(part.ref)
            if model is not None:
                model.props[key] = value
                model.setup()
                self.engine.settle()
                self.view.actualizar_todo()
                self._pintar_indicadores()
        self.lbl_estado.setText(f"{part.ref}.{key} = {value}")

    def escribir_consola(self, texto: str) -> None:
        self.consola.setPlainText(texto)
        self.tabs.setCurrentWidget(self.consola)

    # -- IA ---------------------------------------------------------------
    def generar_ia(self) -> None:
        dlg = GenerarDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.valores()
        if not v["descripcion"]:
            QMessageBox.warning(self, "Falta la descripción", "Escribe qué circuito quieres.")
            return

        progreso = QProgressDialog("Generando circuito…", "Cancelar", 0, 0, self)
        progreso.setWindowTitle("IA")
        progreso.setWindowModality(Qt.WindowModal)
        progreso.setMinimumDuration(0)

        from ..ai.pipeline import generate

        def al_intento(att):
            progreso.setLabelText(
                f"Intento {att.index}: {len(att.errors)} errores, "
                f"{len(att.warnings)} avisos ({att.seconds:.1f}s)"
            )
            QApplication.processEvents()

        try:
            res = generate(
                v["descripcion"],
                provider_name=v["proveedor"],
                model=v["modelo"],
                extra_data=v["extra"],
                max_attempts=v["intentos"],
                on_attempt=al_intento,
            )
        except Exception as e:
            progreso.cancel()
            QMessageBox.critical(self, "Fallo de la IA", f"{type(e).__name__}: {e}")
            return
        progreso.close()

        if res.circuit is not None:
            self.recargar(res.circuit)
            self.escribir_consola(res.report() + "\n\n" + (res.yaml_text or ""))
            QMessageBox.information(
                self, "Generado",
                "Circuito generado.\n\n" + res.report().split("\n")[-1]
                if not res.ok else "Circuito generado y válido."
            )
        else:
            QMessageBox.warning(self, "No se generó", res.report())
            self.escribir_consola(res.report())

    def explicar_ia(self) -> None:
        if self.circuit is None:
            return
        pregunta, ok = QInputDialog.getText(self, "Explicar", "¿Pregunta concreta? (opcional)")
        if not ok:
            return
        from ..ai.pipeline import describe
        try:
            texto = describe(self.circuit, question=pregunta or "")
        except Exception as e:
            QMessageBox.critical(self, "Fallo", f"{type(e).__name__}: {e}")
            return
        self.escribir_consola(texto)

    def analizar(self) -> None:
        if self.circuit is None:
            return
        self.escribir_consola(describe_local(self.circuit))
        self.tabs.setCurrentWidget(self.consola)

    def ver_proveedores(self) -> None:
        from ..ai.providers import available
        lineas = ["Proveedor     Estado", "-" * 56]
        for n, r in available().items():
            lineas.append(f"{n:<13} {'LISTO' if r is None else r}")
        self.escribir_consola("\n".join(lineas))

    def ver_partes(self) -> None:
        lineas = [f"{len(LIBRARY)} componentes en la librería", ""]
        for cat, specs in sorted(LIBRARY.by_category().items()):
            lineas.append(f"[{cat}]")
            for s in specs:
                pins = ", ".join(
                    p.display_name() + (f"[0:{p.width-1}]" if p.width > 1 else "")
                    for p in s.pins
                )
                lineas.append(f"  {s.name:<12} {s.description}")
                lineas.append(f"  {'':<12} {pins}")
            lineas.append("")
        self.escribir_consola("\n".join(lineas))

    def acerca_de(self) -> None:
        QMessageBox.about(
            self, "Voltio",
            "<b>Voltio</b> — motor de circuitos digitales accionado por IA.<br><br>"
            "Descripción en lenguaje natural → DSL → esquemático → simulación.<br>"
            "Lógica de cuatro valores (0, 1, X, Z) con buses tri-estado.<br><br>"
            "El núcleo no depende de PySide6 ni de ninguna otra biblioteca."
        )

    # -- exportar ---------------------------------------------------------
    def exportar_svg(self) -> None:
        ruta, _ = QFileDialog.getSaveFileName(self, "Exportar SVG", "circuito.svg", "SVG (*.svg)")
        if not ruta:
            return
        from ..render import save_svg
        save_svg(self.circuit, ruta, engine=self.engine)
        self.lbl_estado.setText(f"SVG guardado en {ruta}")

    def _exportar(self, kind: str) -> None:
        ext, fn = {
            "spice": ("cir", None),
            "edif": ("edf", None),
            "bom": ("csv", None),
        }[kind]
        ruta, _ = QFileDialog.getSaveFileName(self, f"Exportar {kind.upper()}",
                                              f"circuito.{ext}", f"*.{ext}")
        if not ruta:
            return
        try:
            if kind == "spice":
                from ..export.spice import to_spice
                open(ruta, "w", encoding="utf-8").write(to_spice(self.circuit))
            elif kind == "edif":
                from ..export.edif import to_edif
                open(ruta, "w", encoding="utf-8").write(to_edif(self.circuit))
            else:
                from ..export.netlist import to_bom_csv
                open(ruta, "w", encoding="utf-8").write(to_bom_csv(self.circuit))
            self.lbl_estado.setText(f"{kind.upper()} guardado en {ruta}")
        except Exception as e:
            QMessageBox.critical(self, "Fallo al exportar", f"{type(e).__name__}: {e}")

    # -- cableado manual --------------------------------------------------
    def _cable_manual(self, tok_a: str, ref_b: str, bit_b: int) -> None:
        """El usuario ha unido dos patillas en el canvas."""
        if self.circuit is None:
            return
        from ..core.ir import Endpoint, Net
        a_ref, resto = tok_a.split(".", 1)
        a_pin = resto.split("[")[0]
        net = self.circuit.add_net(Net(name=None))
        net.add(Endpoint(a_ref, a_pin, int(resto.split("[")[1].rstrip("]")) if "[" in resto else 0))
        # El segundo extremo llega como ref + bit; el pin lo deducimos del handle.
        self.lbl_estado.setText(
            f"cable {net.name}: {tok_a} → {ref_b}[{bit_b}]  "
            f"(guarda el DSL para consolidarlo)"
        )
        self.recargar(self.circuit, mantener_vista=True)


def _fmt_t(t: float) -> str:
    if t == 0:
        return "0"
    for div, suf in ((1.0, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns")):
        if abs(t) >= div:
            return f"{t / div:.4g} {suf}"
    return f"{t:.3g} s"


def _tooltip(spec) -> str:
    lineas = [f"<b>{spec.name}</b> — {spec.description}", ""]
    for p in spec.pins:
        d = {"in": "entrada", "out": "salida", "inout": "bidireccional",
             "power": "alimentación"}.get(p.direction, p.direction)
        w = f"[0:{p.width-1}]" if p.width > 1 else ""
        lineas.append(f"&nbsp;&nbsp;{p.display_name()}{w} — {d}")
    if spec.props:
        lineas.append("")
        for p in spec.props:
            d = f" (por defecto {p.default})" if p.default is not None else ""
            lineas.append(f"&nbsp;&nbsp;<i>{p.label}</i>{d}")
    return "<br>".join(lineas)


def run(circuit: Optional[Circuit] = None, dsl_path: str = "",
        duration: float = 1e-3) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Voltio")
    win = MainWindow(circuit=circuit, dsl_path=dsl_path, duration=duration)
    win.show()
    return app.exec()
