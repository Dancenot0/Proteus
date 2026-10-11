"""Lector e inspector de proyectos de Proteus (``.pdsprj`` / ``.DSN``).

Esto es ingeniería inversa documentada, no adivinanza. Todo lo que hay aquí se
comprobó sobre archivos reales de Proteus 8.x. Los detalles y el método están
en ``docs/PROTEUS_DSN.md``.

Lo que sí funciona ya:

* Abrir un ``.pdsprj`` (que es un ZIP) y listar su contenido.
* Leer ``PROJECT.XML`` y ``FIRMWARE.XML``, que son texto plano.
* Extraer el firmware (código fuente) del proyecto.
* Sacar del ``ROOT.DSN`` binario la lista de dispositivos, sus definiciones de
  propiedades (``{*PROPDEFS}``) y los valores de cada instancia
  (``{*COMPONENT}``), los nombres de pines y las fuentes usadas.

Lo que todavía no: reconstruir la geometría (coordenadas de cada símbolo y
trazado de cada cable) con fiabilidad suficiente para *escribir* un DSN. Eso es
lo que falta para generar un proyecto que Proteus abra.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

__all__ = [
    "ProteusProject",
    "DsnInfo",
    "DeviceBlock",
    "open_project",
    "inspect_dsn",
    "DSN_MAGIC",
]

#: Cabecera con la que empieza todo esquemático de ISIS.
DSN_MAGIC = b"ISIS SCHEMATIC FILE"

#: Cabecera del archivo de PCB (ARES), por si acaso.
LYT_MAGIC = b"ARES LAYOUT FILE"


@dataclass
class DeviceBlock:
    """Un dispositivo tal como aparece dentro del ``ROOT.DSN``.

    El formato guarda, por cada dispositivo, su definición de propiedades
    (el *esquema*, con tipos y valores permitidos) y luego los valores reales
    de la instancia. Ambos son texto plano ``{CLAVE=...}`` separado por ``\\n``.
    """

    index: int
    device: Dict[str, str] = field(default_factory=dict)     # {*DEVICE}
    active: str = ""                                          # {ACTIVE=...}
    propdefs: List[str] = field(default_factory=list)          # {*PROPDEFS}
    component: Dict[str, str] = field(default_factory=dict)   # {*COMPONENT}
    raw: bytes = b""

    @property
    def model_dll(self) -> str:
        """La DLL de simulación (VSM) que Proteus carga para este dispositivo."""
        return self.component.get("MODDLL", "") or self.component.get("MODFILE", "")

    @property
    def primitive(self) -> str:
        return self.component.get("PRIMITIVE", "")

    @property
    def package(self) -> str:
        return self.component.get("PACKAGE", "")

    @property
    def name(self) -> str:
        return self.model_dll or self.active or f"device#{self.index}"

    def describe(self) -> str:
        lines = [f"[{self.index}] {self.name}"]
        if self.active:
            lines.append(f"    ACTIVE    = {self.active}")
        if self.primitive:
            lines.append(f"    PRIMITIVE = {self.primitive}")
        if self.package and self.package != "NULL":
            lines.append(f"    PACKAGE   = {self.package}")
        extra = {
            k: v for k, v in self.component.items()
            if k not in ("MODDLL", "MODFILE", "PRIMITIVE", "PACKAGE")
        }
        for k, v in sorted(extra.items()):
            lines.append(f"    {k:<9} = {v}")
        if self.propdefs:
            lines.append(f"    ({len(self.propdefs)} propiedades definidas)")
        return "\n".join(lines)


@dataclass
class DsnInfo:
    """Lo que se pudo averiguar de un ``ROOT.DSN`` sin romperlo."""

    size: int = 0
    magic: str = ""
    release: str = ""
    title: str = ""
    template: str = ""
    author: str = ""
    created: str = ""
    modified: str = ""
    sheet: Tuple[int, int] = (0, 0)
    fonts: List[str] = field(default_factory=list)
    style_names: List[str] = field(default_factory=list)
    pin_names: List[str] = field(default_factory=list)
    devices: List[DeviceBlock] = field(default_factory=list)
    unknown_bytes: float = 0.0

    def report(self) -> str:
        out = [
            f"ROOT.DSN — {self.size:,} bytes",
            f"  cabecera   : {self.magic}",
            f"  versión    : {self.release or '?'}",
            f"  título     : {self.title or '(vacío)'}",
            f"  plantilla  : {self.template or '(ninguna)'}",
            f"  autor      : {self.author or '(ninguno)'}",
            f"  creado     : {self.created or '?'}   modificado: {self.modified or '?'}",
        ]
        if self.sheet != (0, 0):
            out.append(f"  hoja       : {self.sheet[0]} × {self.sheet[1]} (unidades internas)")
        if self.fonts:
            out.append(f"  fuentes    : {', '.join(self.fonts)}")
        if self.style_names:
            out.append(f"  estilos    : {len(self.style_names)} "
                       f"({', '.join(self.style_names[:8])}"
                       f"{'...' if len(self.style_names) > 8 else ''})")
        if self.pin_names:
            out.append(f"  pines      : {len(self.pin_names)} nombres "
                       f"({', '.join(self.pin_names[:12])}"
                       f"{'...' if len(self.pin_names) > 12 else ''})")
        out.append(f"  dispositivos: {len(self.devices)}")
        for d in self.devices:
            out.append("    · " + d.describe().replace("\n", "\n      "))
        if self.unknown_bytes:
            out.append(f"  bytes sin interpretar: {self.unknown_bytes:.0%} "
                       f"(geometría y trazado de cables)")
        return "\n".join(out)


@dataclass
class ProteusProject:
    """Un proyecto ``.pdsprj`` abierto."""

    path: str = ""
    entries: Dict[str, int] = field(default_factory=dict)
    project_xml: Dict[str, Any] = field(default_factory=dict)
    firmware_xml: List[Dict[str, Any]] = field(default_factory=list)
    firmware_files: Dict[str, str] = field(default_factory=dict)
    dsn: Optional[DsnInfo] = None
    scripts: Dict[str, bytes] = field(default_factory=dict)
    raw: Dict[str, bytes] = field(default_factory=dict)

    @property
    def release(self) -> str:
        return str(self.project_xml.get("release", ""))

    @property
    def is_zip(self) -> bool:
        return True

    def report(self) -> str:
        out = [f"Proyecto de Proteus: {self.path or '(en memoria)'}"]
        out.append(f"  {len(self.entries)} entradas en el contenedor ZIP:")
        for name, size in sorted(self.entries.items()):
            out.append(f"    {name:<38} {size:>9,} bytes")
        if self.project_xml:
            px = self.project_xml
            out.append(f"  PROJECT.XML : release={px.get('release','?')} "
                       f"filever={px.get('filever','?')} "
                       f"creado={px.get('created','?')} modificado={px.get('modified','?')}")
            if px.get("scripts"):
                out.append(f"                scripts: {', '.join(px['scripts'])}")
        if self.firmware_xml:
            out.append(f"  FIRMWARE.XML: {len(self.firmware_xml)} controlador(es)")
            for fw in self.firmware_xml:
                out.append(f"                {fw}")
        if self.firmware_files:
            out.append("  código fuente incluido:")
            for name, text in self.firmware_files.items():
                n = len(text.splitlines())
                out.append(f"    {name:<38} {n} líneas")
        if self.dsn:
            out.append("")
            out.append(self.dsn.report())
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Apertura
# ---------------------------------------------------------------------------


def open_project(path_or_bytes) -> ProteusProject:
    """Abre un ``.pdsprj`` (o un ``.DSN`` suelto) y extrae todo lo legible."""
    proj = ProteusProject()
    data: bytes
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        proj.path = str(path_or_bytes)
        with open(proj.path, "rb") as f:
            data = f.read()

    if data.startswith(b"PK\x03\x04"):
        _read_container(proj, data)
    elif data.startswith(DSN_MAGIC):
        proj.raw["ROOT.DSN"] = data
        proj.entries["ROOT.DSN"] = len(data)
        proj.dsn = inspect_dsn(data)
    else:
        raise ValueError(
            "esto no parece un proyecto de Proteus: no empieza ni por la firma "
            "ZIP (PK\\x03\\x04) ni por 'ISIS SCHEMATIC FILE'"
        )
    return proj


def _read_container(proj: ProteusProject, data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            proj.entries[info.filename] = info.file_size
        for name in proj.entries:
            proj.raw[name] = z.read(name)

    # PROJECT.XML
    if "PROJECT.XML" in proj.raw:
        proj.project_xml = _parse_project_xml(proj.raw["PROJECT.XML"])

    # FIRMWARE.XML y los XML de cada controlador
    for name, blob in proj.raw.items():
        if not name.upper().endswith(".XML") or name == "PROJECT.XML":
            continue
        try:
            proj.firmware_xml.append({"archivo": name, **_xml_to_dict(blob)})
        except ET.ParseError as e:
            proj.firmware_xml.append({"archivo": name, "error": str(e)})

    # Código fuente embebido
    for name, blob in proj.raw.items():
        if name.upper().endswith((".ASM", ".C", ".H", ".BAS", ".HEX", ".TXT")):
            proj.firmware_files[name] = blob.decode("utf-8", "replace")

    # Scripts
    for name, blob in proj.raw.items():
        if name.upper().startswith("SCRIPTS/"):
            proj.scripts[name] = blob

    # El esquemático
    dsn_name = next((n for n in proj.raw if n.upper().endswith(".DSN")), None)
    if dsn_name:
        proj.dsn = inspect_dsn(proj.raw[dsn_name])


def _parse_project_xml(blob: bytes) -> Dict[str, Any]:
    root = ET.fromstring(blob.decode("utf-8", "replace"))
    out: Dict[str, Any] = {}
    ts = root.find("TIMESTAMP")
    if ts is not None:
        out.update({k.lower(): v for k, v in ts.attrib.items()})
    scripts = [s.text for s in root.findall(".//SCRIPTS/STRING") if s.text]
    if scripts:
        out["scripts"] = scripts
    return out


def _xml_to_dict(blob: bytes) -> Dict[str, Any]:
    root = ET.fromstring(blob.decode("utf-8", "replace"))

    def walk(el) -> Any:
        kids = list(el)
        if not kids:
            return (el.text or "").strip()
        out = {}
        for k in kids:
            v = walk(k)
            if k.tag in out:
                if not isinstance(out[k.tag], list):
                    out[k.tag] = [out[k.tag]]
                out[k.tag].append(v)
            else:
                out[k.tag] = v
        return out

    return {root.tag: walk(root)}


# ---------------------------------------------------------------------------
# Análisis del DSN
# ---------------------------------------------------------------------------

_STR = re.compile(rb"[ -~]{3,}")
_LENSTR = re.compile(rb"([\x01-\x28])([ -~]{1,40})")


def inspect_dsn(data: bytes) -> DsnInfo:
    """Extrae toda la información legible de un ``ROOT.DSN``."""
    info = DsnInfo(size=len(data))
    if not data.startswith(DSN_MAGIC):
        info.magic = "(cabecera no reconocida)"
        return info
    info.magic = DSN_MAGIC.decode()

    # -- cabecera ------------------------------------------------------------
    # Disposición observada empíricamente en archivos de Proteus 8.x. No está
    # documentada en ningún sitio; esto es lo que sale de comparar varios:
    #
    #   0x00  "ISIS SCHEMATIC FILE"     firma fija, 19 bytes
    #   0x13  \x1a\x00\x00              separador / banderas
    #   0x16  título del diseño         cadena terminada en NUL
    #   ...   plantilla                 cadena terminada en NUL (a menudo vacía)
    #   ...   ruta del autor            cadena terminada en NUL
    #   ...   dos fechas mm/dd/aa       creación y modificación
    #   ...   par de u32 con el tamaño de hoja en unidades internas
    #
    # El título es el primer campo; el resto se localiza por búsqueda, porque
    # la longitud de los campos anteriores varía.
    head = data[:0x200]
    after = head[len(DSN_MAGIC):]
    fields = after.split(b"\x00")
    texts: List[str] = []
    for f in fields:
        t = f.decode("latin-1", "replace").strip()
        # Se descartan los restos binarios: solo interesan cadenas imprimibles.
        if len(t) >= 2 and all(32 <= ord(ch) < 127 for ch in t):
            texts.append(t)
    if texts:
        info.title = texts[0]
    if len(texts) > 1:
        info.template = texts[1]
    if len(texts) > 2:
        info.author = texts[2]

    dates = re.findall(rb"\d{2}/\d{2}/\d{2}", head)
    if len(dates) >= 1:
        info.created = dates[0].decode()
    if len(dates) >= 2:
        info.modified = dates[1].decode()

    info.sheet = _find_sheet_size(head)

    # Nombres de estilo y de fuente: cadenas con prefijo de longitud de 1 byte.
    known_styles = (
        "COMPONENT", "PORT", "MARKER", "ACTUATOR", "INDICATOR", "VPROBE",
        "IPROBE", "TAPE", "GENERATOR", "TERMINAL", "SUBCIRCUIT", "2D GRAPHIC",
        "WIRE DOT", "WIRE", "BUS WIRE", "BORDER", "TEMPLATE", "COMPONENT ID",
        "COMPONENT VALUE", "PROPERTIES", "TERMINAL LABEL", "WIRE LABEL",
        "SUBCKT ID", "SUBCKT NAME", "SCRIPT", "PIN NAME", "PIN NUMBER",
        "VPROBE LABEL", "IPROBE LABEL", "GENERATOR LABEL", "TAPE LABEL",
        "ACTIVE READOUT", "ACTIVE REGION",
    )
    for m in re.finditer(_LENSTR, data[:0x2000]):
        length, text = m.group(1)[0], m.group(2)
        if len(text) != length:
            continue
        s = text.decode("latin-1")
        if s in known_styles and s not in info.style_names:
            info.style_names.append(s)

    for s in _STR.findall(data):
        t = s.decode("latin-1")
        if t in ("Arial", "Default Font", "Symbol", "Terminal") and t not in info.fonts:
            info.fonts.append(t)

    # Nombres de pines: suelen ir en mayúsculas alfanuméricas.
    pins = set()
    for m in re.finditer(rb"\x00([A-Z][A-Z0-9]{0,9})\x00", data):
        t = m.group(1).decode()
        if t not in known_styles:
            pins.add(t)
    info.pin_names = sorted(pins)

    # Bloques de dispositivo: texto plano delimitado por NUL.
    info.devices = _parse_device_blocks(data)

    # ¿Cuánto del archivo pudimos interpretar?
    explained = sum(len(d.raw) for d in info.devices)
    explained += sum(len(s) for s in info.style_names)
    info.unknown_bytes = max(0.0, 1.0 - explained / max(len(data), 1))
    return info


def _find_sheet_size(head: bytes) -> Tuple[int, int]:
    """Busca el par de u32 que da el tamaño de la hoja.

    En los archivos observados son dos enteros iguales y razonables (del orden
    de cientos a miles de unidades internas). Se buscan así, por patrón, en vez
    de por offset fijo, porque la longitud de los campos de texto anteriores
    varía de un proyecto a otro.
    """
    import struct as _st

    for off in range(len(DSN_MAGIC) + 3, min(len(head), 0x140) - 8):
        try:
            a, b = _st.unpack_from("<II", head, off)
        except Exception:
            continue
        if a == b and 64 <= a <= 100000:
            # Descartamos coincidencias dentro de las fechas o de texto.
            return (a, b)
    return (0, 0)


def _parse_device_blocks(data: bytes) -> List[DeviceBlock]:
    """Localiza los bloques ``{*DEVICE} ... {*COMPONENT} ...`` y los parsea.

    El formato es notablemente amable: cada bloque es texto plano con una
    línea ``{CLAVE=...}`` por propiedad, y las secciones van marcadas por
    ``{*DEVICE}``, ``{*PROPDEFS}``, ``{*INDEX}`` y ``{*COMPONENT}``.
    """
    out: List[DeviceBlock] = []
    blob_re = re.compile(
        rb"\{\*DEVICE\}(?P<body>.*?)(?=\x00|\{\*DEVICE\}|\Z)", re.S
    )
    for i, m in enumerate(blob_re.finditer(data)):
        body = m.group("body")
        blk = DeviceBlock(index=i, raw=m.group(0))
        section = "device"
        for raw_line in body.split(b"\n"):
            line = raw_line.decode("latin-1").strip()
            if not line:
                continue
            if line == "{*DEVICE}":
                section = "device"
                continue
            if line == "{*PROPDEFS}":
                section = "propdefs"
                continue
            if line == "{*INDEX}":
                section = "index"
                continue
            if line == "{*COMPONENT}":
                section = "component"
                continue
            if not (line.startswith("{") and line.endswith("}")):
                continue
            inner = line[1:-1]
            if section == "propdefs":
                blk.propdefs.append(inner)
                continue
            if "=" not in inner:
                continue
            k, v = inner.split("=", 1)
            target = blk.device if section == "device" else (
                blk.component if section == "component" else None
            )
            if target is None:
                continue
            target[k.strip()] = v.strip()
            if k.strip() == "ACTIVE":
                blk.active = v.strip()
        if blk.device or blk.component or blk.propdefs:
            out.append(blk)
    return out


def compare(a: ProteusProject, b: ProteusProject) -> str:
    """Diff de dos proyectos: la herramienta clave de la ingeniería inversa.

    La manera realista de descifrar el DSN es dibujar en Proteus el mismo
    circuito moviendo UNA sola cosa cada vez y comparar los bytes. Esta
    función automatiza la comparación.
    """
    out: List[str] = []
    ka, kb = set(a.raw), set(b.raw)
    for name in sorted(ka | kb):
        if name not in ka:
            out.append(f"+ {name}: solo en el segundo ({len(b.raw[name])} bytes)")
        elif name not in kb:
            out.append(f"- {name}: solo en el primero ({len(a.raw[name])} bytes)")
        else:
            ba, bb = a.raw[name], b.raw[name]
            if ba == bb:
                out.append(f"= {name}: idéntico ({len(ba)} bytes)")
            else:
                diffs = _byte_diff(ba, bb)
                out.append(f"~ {name}: {len(ba)} → {len(bb)} bytes, "
                           f"{diffs['changed']} posiciones distintas")
                for off, x, y in diffs["samples"][:20]:
                    out.append(f"      0x{off:06x}: {x:02x} → {y:02x}")
    return "\n".join(out)


def _byte_diff(a: bytes, b: bytes) -> Dict[str, Any]:
    n = min(len(a), len(b))
    changed = [i for i in range(n) if a[i] != b[i]]
    samples = [(i, a[i], b[i]) for i in changed[:200]]
    return {"changed": len(changed) + abs(len(a) - len(b)), "samples": samples}
