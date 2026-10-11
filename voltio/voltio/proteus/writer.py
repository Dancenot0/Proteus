"""Escritor de DSN legado v0 (experimental, dialécto 800).

Emite un ``.dsn`` que Proteus puede abrir con doble clic, para circuitos de
Voltio cuyas familias de dispositivo existan en una plantilla real.

Ensamblaje, no síntesis ciega
-----------------------------
* **Cabecera + tabla de estilos + opciones SPICE**: del DSN vacío mínimo.
* **Registros de componente**: se copian de una plantilla que ya tenga esa
  familia (``74NAND2.MDF``, ``LOGICSTATE``...), parcheando coordenadas,
  etiqueta de instancia y propiedades.
* **Registros de cable**: se sintetizan con el formato descodificado::

      [u16 0][u16 n][n × (i32 x, i32 y)][00 1d 00 00 00][estilo]["WIRE" 00]

* **PINOUT por familia y ROOT.CDB**: copiados de la plantilla.
* **Sección de circuito**: se omite a propósito. Hipótesis: es caché de
  compilación ("Compiling design... / Netlist compilation completed OK" la
  regeneran al abrir). Si un test de usuario la demuestra necesaria, se añade.

Espacios de coordenadas
-----------------------
Componentes: u32 en unidades de 0.01". Cables: i32 en unidades de 0.1 mm
(2540 por unidad de componente), con Y invertido respecto al origen de la
hoja::

    T(x, y) = ((x - Ox) * 2540, (Oy - y) * 2540)

El origen ``(Ox, Oy)`` se resuelve por cada plantilla calzando las puntas de
pin calculadas contra los cables que esa plantilla ya tiene
(:func:`solve_origin`). Si el calce no cierra, :func:`build_dsn` **se niega a
emitir** en vez de generar un archivo sospechoso.
"""

from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import os

from ..core.ir import Circuit, TIE_PART

__all__ = ["SCALE", "DsnTemplate", "load_template", "build_dsn", "selfcheck"]

SCALE = 2540


# ---------------------------------------------------------------------------
# Parseo de la plantilla
# ---------------------------------------------------------------------------


@dataclass
class FamilyRec:
    family: str
    raw: bytes                 # registro completo, para copiar
    x: int                     # coordenadas de componente en la plantilla
    y: int
    x_off: int                 # offset de x dentro de raw
    label_off: int             # offset de la etiqueta de instancia
    label: str
    blob_off: int              # offset del u32 de longitud del blob
    blob: bytes
    pins: Dict[str, Tuple[int, int]] = field(default_factory=dict)


@dataclass
class WireSample:
    points: List[Tuple[int, int]]
    style: bytes               # bytes entre 00 1d 00 00 00 y "WIRE\0"
    raw: bytes


@dataclass
class DsnTemplate:
    dsn: bytes
    head: bytes                # cabecera + estilos (hasta el primer objeto)
    tail: bytes                # opciones SPICE finales
    families: Dict[str, FamilyRec] = field(default_factory=dict)
    wire_style: bytes = b""
    pinouts: Dict[str, bytes] = field(default_factory=dict)
    cdb: bytes = b""
    project_xml: bytes = b""
    scripts: Dict[str, bytes] = field(default_factory=dict)
    sheet_h: int = 800
    origin: Optional[Tuple[int, int]] = None
    wires: List[WireSample] = field(default_factory=list)


_WIRE_RE = re.compile(
    rb"\x00\x00(\x02\x00|\x03\x00|\x04\x00|\x05\x00|\x06\x00)(.{8,96}?)\x00\x1d\x00\x00\x00",
    re.S)


def _parse_wires(d: bytes) -> List[WireSample]:
    out = []
    for m in _WIRE_RE.finditer(d):
        n = struct.unpack("<H", m.group(1))[0]
        body = m.group(2)
        if len(body) != 8 * n:
            continue
        pts = [struct.unpack_from("<ii", body, i * 8) for i in range(n)]
        end = d.find(b"WIRE\x00", m.end())
        if end == -1 or end - m.end() > 96:
            continue
        end += 5
        out.append(WireSample(points=pts, style=d[m.end():end - 5], raw=d[m.start():end]))
    return out


def _parse_family(d: bytes, family: str) -> Optional[FamilyRec]:
    """Localiza el primer registro de componente de una familia."""
    for m in re.finditer(re.escape(family.encode()), d):
        i = m.start()
        if i < 1 or d[i - 1] != len(family):
            continue
        ts, x, y = struct.unpack_from("<III", d, i + len(family))
        if not (0x40000000 < ts < 0x7FFFFFFF):
            continue
        bm = re.search(rb"\{\*DEVICE\}", d[i:i + 6000])
        if not bm:
            continue
        q = i + bm.start() - 4
        (L,) = struct.unpack_from("<I", d, q)
        blob = d[q + 4:q + 4 + L]
        # etiqueta: cstring terminado en NUL justo antes del u32 de longitud
        e = q
        while e > i and d[e - 1] != 0x00:
            e -= 1
        label = d[e:q].decode("latin-1", "replace")
        # Sub-registros de patilla: "$PINDEFAULT\0 nombre\0 numero\0
        # [u8][u8][dirección 2-3 chars] i32 i32". La dirección tiene longitud
        # variable (IN, OUT, IO, PP), así que se valida que los offsets que
        # salgan sean múltiplos de 2540 (rejilla de 0.01" en unidades de
        # cable): si no lo son, el parseo se deslizó y se descarta el match.
        pins: Dict[str, Tuple[int, int]] = {}
        for pm in re.finditer(rb"\$PINDEFAULT\x00([^\x00]{1,10})\x00(\d{1,2})\x00", d[i:q]):
            name = pm.group(1).decode("latin-1")
            o = i + pm.end() + 2          # dos bytes sueltos tras el número
            for dlen in (2, 3):           # IN/IO/PP = 2, OUT = 3
                px, py = struct.unpack_from("<ii", d, o + dlen)
                if px % 2540 == 0 and py % 2540 == 0:
                    pins[name] = (px, py)
                    break
        # inicio del registro: retrocede hasta el u8 de longitud de la familia
        rec_start = i - 1
        # fin: incluyendo el blob con su NUL
        rec_end = q + 4 + L
        raw = d[rec_start:rec_end]
        return FamilyRec(family=family, raw=raw, x=x, y=y,
                         x_off=i + len(family) + 4 - rec_start,
                         label_off=e - rec_start,
                         label=label,
                         blob_off=q - rec_start,
                         blob=blob, pins=pins)
    return None


def _parse_pinouts(d: bytes, families: List[str]) -> Dict[str, bytes]:
    out = {}
    for fam in families:
        pat = bytes([len(fam) + 2]) + b"[" + fam.encode() + b"]"
        i = d.find(pat)
        if i == -1:
            continue
        # el registro PINOUT: [u8 len]["[fam]"][u32 ts][u32 L][texto]
        ts_off = i + len(pat)
        (L,) = struct.unpack_from("<I", d, ts_off + 4)
        out[fam] = d[i - 1:ts_off + 8 + L]
    return out


def load_template(path: str, families: List[str]) -> DsnTemplate:
    z = zipfile.ZipFile(path)
    d = z.read([n for n in z.namelist() if n.upper().endswith(".DSN")][0])
    tpl = DsnTemplate(dsn=d, head=b"", tail=b"")
    tpl.wires = _parse_wires(d)
    tpl.wire_style = tpl.wires[0].style if tpl.wires else b""
    for fam in families:
        rec = _parse_family(d, fam)
        if rec is not None:
            tpl.families[fam] = rec
    tpl.pinouts = _parse_pinouts(d, families)
    if "ROOT.CDB" in z.namelist():
        tpl.cdb = z.read("ROOT.CDB")
    if "PROJECT.XML" in z.namelist():
        tpl.project_xml = z.read("PROJECT.XML")
    for n in z.namelist():
        if n.upper().startswith("SCRIPTS/"):
            tpl.scripts[n] = z.read(n)
    # cabeza: hasta el primer objeto; cola: desde las opciones SPICE
    i_opt = d.find(b"TDUPPER")
    i_opt = d.rfind(b"\x0c\x00\x00\x00", 0, i_opt) if i_opt != -1 else -1
    first_obj = min((f.raw and d.find(f.raw)) for f in tpl.families.values() if f.raw)
    tpl.head = d[:first_obj]
    tpl.tail = d[i_opt:] if i_opt != -1 else b""
    m = re.search(rb"\x00{4}([\x00-\xff]{4})([\x00-\xff]{4})\x00{6}", d[:300])
    tpl.sheet_h = struct.unpack("<I", m.group(2))[0] if m else 800
    tpl.origin = solve_origin(d, tpl)
    return tpl


# ---------------------------------------------------------------------------
# Calibración
# ---------------------------------------------------------------------------


def solve_origin(d: bytes, tpl: DsnTemplate) -> Optional[Tuple[int, int]]:
    """Resuelve (Ox, Oy) calzando puntas de pin contra cables existentes."""
    if not tpl.wires or not tpl.families:
        return None
    # candidatos: componente + patilla cuyo tip, para algún extremo de cable,
    # produzca un origen múltiplo de 10 unidades (rejilla).
    wire_pts = [p for w in tpl.wires for p in w.points]
    for fam, rec in tpl.families.items():
        for pname, (ox, oy) in rec.pins.items():
            for (wx, wy) in wire_pts:
                numx = rec.x * SCALE + ox - wx
                numy = rec.y * SCALE + oy + wy
                if numx % SCALE or numy % SCALE:
                    continue
                Ox, Oy = numx // SCALE, numy // SCALE
                if Ox % 10 == 0 and Oy % 10 == 0:
                    return (Ox, Oy)
    return None


def selfcheck(tpl: DsnTemplate, tol: int = 2600) -> Dict[str, int]:
    """¿Cuántos extremos de cable de la plantilla coinciden con una punta
    de pin calculada? Si esto no cierra, el escritor no emite."""
    if tpl.origin is None:
        return {"tips": 0, "hits": 0, "misses": 0}
    Ox, Oy = tpl.origin
    tips = []
    for rec in tpl.families.values():
        for ox, oy in rec.pins.values():
            tips.append(((rec.x - Ox) * SCALE + ox, (Oy - rec.y) * SCALE + oy))
    hits = misses = 0
    for w in tpl.wires:
        for p in w.points:
            if tips and min(abs(p[0] - t[0]) + abs(p[1] - t[1]) for t in tips) <= tol:
                hits += 1
            else:
                misses += 1
    return {"tips": len(tips), "hits": hits, "misses": misses}


# ---------------------------------------------------------------------------
# Emisión
# ---------------------------------------------------------------------------


def _patch_record(rec: FamilyRec, x: int, y: int, label: str,
                  blob_patch: Optional[bytes]) -> bytes:
    raw = bytearray(rec.raw)
    struct.pack_into("<II", raw, rec.x_off, x, y)
    blob = blob_patch if blob_patch is not None else rec.blob
    # reconstruye: cabeza hasta la etiqueta + etiqueta + cola desde el u32 L
    head = bytes(raw[:rec.label_off])
    tail = bytes(raw[rec.blob_off:])
    return head + label.encode("latin-1") + b"\x00" + struct.pack("<I", len(blob) + 1) \
        + blob + b"\x00" + tail[4 + len(rec.blob) + 1:]


def build_dsn(circuit: Circuit, tpl: DsnTemplate,
              family_of: Dict[str, str],
              layout: Dict[str, Tuple[float, float]],
              blob_of: Optional[Dict[str, bytes]] = None) -> bytes:
    """Ensamba el DSN. Lanza ValueError si la calibración no cierra."""
    sc = selfcheck(tpl)
    if tpl.origin is None or sc["hits"] == 0 or sc["misses"] > sc["hits"]:
        raise ValueError(
            f"calibración insuficiente para emitir un DSN fiable: {sc}. "
            f"El escritor se niega a generar un archivo sospechoso."
        )
    Ox, Oy = tpl.origin
    blob_of = blob_of or {}

    body = bytearray()
    labels: Dict[str, str] = {}
    for ref, part in circuit.parts.items():
        fam = family_of.get(part.part)
        rec = tpl.families.get(fam or "")
        if rec is None:
            raise ValueError(f"la plantilla no tiene la familia {fam!r} "
                             f"(necesaria para {ref})")
        x, y = layout[ref]
        xi, yi = int(round(x)), int(round(y))
        lab = labels.get(fam, "") or rec.label or ref
        labels[fam] = lab
        body += _patch_record(rec, xi, yi, ref, blob_of.get(ref))

    # cables: una polilínea por net, por las puntas de pin de sus extremos
    tips: Dict[Tuple[str, str, int], Tuple[int, int]] = {}
    for ref, part in circuit.parts.items():
        rec = tpl.families.get(family_of.get(part.part, ""), None)
        if rec is None or part.spec is None:
            continue
        x, y = layout[ref]
        for p in part.spec.pins:
            for bit in range(max(p.width, 1)):
                key = (ref, p.name, bit)
                off = rec.pins.get(_pin_key(rec, p.name), None)
                if off is None:
                    off = rec.pins.get(p.name.lstrip("/"), None)
                if off is None:
                    continue
                tips[key] = ((int(x) - Ox) * SCALE + off[0],
                             (Oy - int(y)) * SCALE + off[1])

    for net in circuit.nets.values():
        pts = []
        for ep in net.endpoints:
            if ep.part == TIE_PART:
                continue
            t = tips.get((ep.part, ep.pin, ep.bit))
            if t is not None:
                pts.append(t)
        if len(pts) < 2:
            continue
        body += b"\x00\x00" + struct.pack("<H", len(pts))
        for (px, py) in pts:
            body += struct.pack("<ii", px, py)
        body += b"\x00\x1d\x00\x00\x00" + tpl.wire_style + b"WIRE\x00"

    for fam in {family_of.get(p.part) for p in circuit.parts.values()}:
        if fam in tpl.pinouts:
            body += tpl.pinouts[fam]

    return tpl.head + bytes(body) + tpl.tail


def _pin_key(rec: FamilyRec, name: str) -> str:
    """Cómo se llama la patilla dentro del registro de la plantilla."""
    if name in rec.pins:
        return name
    bare = name.lstrip("/")
    if bare in rec.pins:
        return bare
    for k in rec.pins:
        if k.lstrip("/") == bare:
            return k
    return name


# ---------------------------------------------------------------------------
# Síntesis de DSN a partir de un circuito de Voltio
# ---------------------------------------------------------------------------

#: familia de plantilla por nombre de parte de Voltio
FAMILY_OF = {
    "74LS00": "74LS00", "74LS02": "74LS02", "74LS04": "74LS00",
    "LOGICSTATE": "LOGICSTATE", "LED": "LED-RED", "RES": "RES",
    "GND": "LOGICSTATE",     # un estado fijo en 0 ES una fuente ideal de 0
    "VCC": "LOGICSTATE",     # (y uno en 1, de 1: mismo truco)
}

#: offsets de patilla en unidades de cable, leídos de $PINDEFAULT reales o
#: estimados por simetría de símbolo. Lo que falta se calibra con reportes.
PIN_OFF = {
    ("74LS00", "A0"): (-762000, -254000),
    ("74LS00", "A1"): (-762000, +254000),
    ("74LS00", "Y"):  (+762000, 0),
    ("74LS02", "A0"): (-762000, -254000),
    ("74LS02", "A1"): (-762000, +254000),
    ("74LS02", "Y"):  (+762000, 0),
    ("LOGICSTATE", "Q"): (0, 0),
    ("LED-RED", "A"): (-254000, 0),
    ("LED-RED", "K"): (+254000, 0),
    ("RES", "1"): (-254000, 0),
    ("RES", "2"): (+254000, 0),
}

#: nombre de pin Voltio -> nombre de pin de la plantilla
PINMAP = {"A0": "A0", "A1": "A1", "Y": "Y", "Q": "Q", "A": "A", "K": "K",
          "1": "1", "2": "2", "P": "Q"}

WIRE_STYLE = (b"\x00\xc0\x9e\x00\x00\x00\x40\x00\x00\x01\xff\xff\xff\x00"
              b"\xff\xff\xff\x00\x02\x7f")


def synthesize(circuit: Circuit, template_path: str, out_path: str,
               scale: float = 6.0, offset: Tuple[float, float] = (700.0, 150.0),
               origin: Tuple[int, int] = (4750, 1730),
               solo: Optional[Tuple[str, ...]] = None,
               section: bytes = b"auto",
               section_src: Optional[str] = None,
               cdb: Optional[bytes] = None) -> Dict[str, object]:
    """``cdb`` permite elegir la base de componentes: la de la plantilla (con
    familias) o la vacía del proyecto sin objetos. Si el CDB no concuerda con
    lo que hay en el esquemático, Proteus descarta el archivo en silencio."""
    """``solo`` limita qué se emite ("comp", "wires", "pinouts"): sirve para
    mandar archivos de bisección al oráculo humano y localizar cuál familia de
    registros estrella al parser de Proteus."""
    """Emite un ``.dsn`` (y su ``.pdsprj``) con el circuito de Voltio dentro.

    Ensambla, no inventa: el esqueleto y las familias salen de una plantilla
    real del usuario (dialecto 8.6), los cables se sintetizan con el formato
    descodizado. Devuelve un resumen de lo emitido.

    ``scale``/``offset`` mapean las coordenadas ``at`` del DSL (pensadas para
    el render, ~0-600) al rango de coordenadas de componente que usan los
    proyectos reales (~700-4300). ``origin`` es el origen del espacio de
    cables calibrado contra Con_cable.
    """
    import zipfile as _zf

    with _zf.ZipFile(template_path) as z:
        names = z.namelist()
        d = z.read([n for n in names if n.upper().endswith(".DSN")][0])
        cdb_tpl = z.read("ROOT.CDB") if "ROOT.CDB" in names else b""
        pxml = z.read("PROJECT.XML") if "PROJECT.XML" in names else b""
        scripts = {n: z.read(n) for n in names if n.upper().startswith("SCRIPTS/")}

    Ox, Oy = origin

    def T(x, y):
        return (int(round((x - Ox) * SCALE)), int(round((Oy - y) * SCALE)))

    # -- cabeza y cola: sin objetos propios de la plantilla ----------------
    fams_needed = sorted({FAMILY_OF[p.part] for p in circuit.parts.values()
                          if p.part in FAMILY_OF})
    starts = []
    for fam in fams_needed:
        pat = re.compile(re.escape(fam.encode()))
        for m in pat.finditer(d):
            i = m.start()
            if i >= 1 and d[i - 1] == len(fam):
                ts, x, y = struct.unpack_from("<III", d, i + len(fam))
                if 0x40000000 < ts < 0x7FFFFFFF and x < 100000:
                    # el registro empieza en el u16 de tipo, DOS bytes antes
                    # del byte de longitud. Cortar en el byte de longitud
                    # dejaba el registro sin cabeza y el parser de Proteus
                    # se estrellaba dentro de LXLCORE.DLL.
                    starts.append(i - 3)
                    break
    if not starts:
        raise ValueError("la plantilla no contiene ninguna familia usable")
    head = d[:min(starts)]
    i_opt = d.rfind(b"\x0c\x00\x00\x00", 0, d.find(b"TDUPPER"))
    tail = d[i_opt:] if i_opt != -1 else b""

    # -- registros de componente y marcadores, por familia ------------------
    fam_rec: Dict[str, Tuple[int, int]] = {}
    fam_marks: Dict[str, bytes] = {}
    for fam in fams_needed:
        pat = re.compile(re.escape(fam.encode()))
        for m in pat.finditer(d):
            i = m.start()
            if i < 1 or d[i - 1] != len(fam):
                continue
            ts, x, y = struct.unpack_from("<III", d, i + len(fam))
            if not (0x40000000 < ts < 0x7FFFFFFF) or x > 100000:
                continue
            bm = re.search(rb"\{\*DEVICE\}", d[i:i + 6000])
            if not bm:
                continue
            q = i + bm.start() - 4
            (L,) = struct.unpack_from("<I", d, q)
            fam_rec[fam] = (i - 3, q + 4 + L)
            break
        # (los marcadores de texto se omiten en v0: copiarlos por rebanadas
        # cortaba registros por la mitad, que es una de las dos cosas que
        # estrellaban a LXLCORE.DLL)
        fam_marks[fam] = b""

    body = bytearray()
    lay = {p.ref: (offset[0] + p.x * scale, offset[1] + p.y * scale)
           for p in circuit.parts.values()}
    n_comp = 0
    for part in circuit.parts.values():
        fam = FAMILY_OF.get(part.part)
        if fam is None or fam not in fam_rec:
            raise ValueError(f"sin plantilla para {part.part} ({part.ref})")
        a, b = fam_rec[fam]
        rec = bytearray(d[a:b])
        x, y = lay[part.ref]
        struct.pack_into("<II", rec, 3 + len(fam) + 4, int(x), int(y))
        want = None
        if part.part == "GND":
            want = b"0"
        elif part.part == "VCC":
            want = b"1"
        elif fam == "LOGICSTATE":
            want = str(part.props.get("STATE", "0")).strip().encode()[:1]
        if want:
            r2 = bytes(rec).replace(b"{STATE=0}", b"{STATE=" + want + b"}")
            r2 = r2.replace(b"{STATE=1}", b"{STATE=" + want + b"}") if want == b"0" \
                else r2.replace(b"{STATE=0}", b"{STATE=" + want + b"}") if want == b"1" else r2
            rec = bytearray(r2)
        if solo is None or "comp" in solo:
            body += rec
        n_comp += 1

    # -- cables -------------------------------------------------------------
    n_wire = 0
    for net in circuit.nets.values():
        pts = []
        for ep in net.endpoints:
            if ep.part == TIE_PART:
                continue
            part = circuit.parts.get(ep.part)
            if part is None:
                continue
            fam = FAMILY_OF.get(part.part)
            off = PIN_OFF.get((fam, PINMAP.get(ep.pin, ep.pin)))
            if off is None:
                continue
            cx, cy = lay[ep.part]
            wx, wy = T(cx, cy)
            pts.append((wx + off[0], wy + off[1]))
        if len(pts) < 2:
            continue
        if solo is not None and "wires" not in solo:
            continue

        def emite(ruta):
            nonlocal n_wire
            outb = b"\x00\x00" + struct.pack("<H", len(ruta))
            for px, py in ruta:
                outb += struct.pack("<ii", int(px), int(py))
            return outb + b"\x00\x1d\x00\x00\x00" + WIRE_STYLE + b"WIRE\x00"

        if len(pts) == 2 and (pts[0][0] == pts[1][0] or pts[0][1] == pts[1][1]):
            body += emite(pts)
            n_wire += 1
        elif len(pts) == 2:
            body += emite([pts[0], (pts[1][0], pts[0][1]), pts[1]])
            n_wire += 1
        else:
            xs = [q[0] for q in pts]
            ys = [q[1] for q in pts]
            jx = min(xs) - 254000
            for q in pts:
                body += emite([q, (jx, q[1])])
                n_wire += 1
            body += emite([(jx, min(ys)), (jx, max(ys))])
            n_wire += 1

    # -- PINOUTs de las familias usadas -------------------------------------
    for fam in fams_needed:
        if solo is not None and "pinouts" not in solo:
            break
        patt = bytes([len(fam) + 2]) + b"[" + fam.encode() + b"]"
        i = d.find(patt)
        if i != -1:
            (L,) = struct.unpack_from("<I", d, i + len(patt) + 4)
            body += d[i - 2:i + len(patt) + 8 + L]

    # La segunda sección (ISIS CIRCUIT FILE) es obligatoria: un DSN sin ella
    # ni siquiera abre (SEH trap en KERNELBASE). Si no se indica otra cosa se
    # usa la de la plantilla; para bisección se puede pasar una vacía.
    if section == b"auto":
        src = section_src or template_path
        with _zf.ZipFile(src) as zs:
            ds = zs.read([n for n in zs.namelist() if n.upper().endswith(".DSN")][0])
        i2 = ds.find(DSN_MAGIC, ds.find(DSN_MAGIC) + 1)
        io2 = ds.rfind(b"\x0c\x00\x00\x00", 0, ds.find(b"TDUPPER"))
        section = ds[i2:io2] if i2 != -1 and io2 > i2 else b""
    dsn = head + bytes(body) + section + tail
    dsn_path = out_path if out_path.endswith(".dsn") else \
        os.path.splitext(out_path)[0] + ".dsn"
    with open(dsn_path, "wb") as f:
        f.write(dsn)
    if cdb is None:
        cdb = cdb_tpl
    if out_path.endswith(".pdsprj") or not out_path.endswith(".dsn"):
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            if pxml:
                z.writestr("PROJECT.XML", pxml)
            z.writestr("ROOT.DSN", dsn)
            if cdb:
                z.writestr("ROOT.CDB", cdb)
            for n, bl in scripts.items():
                z.writestr(n, bl)
    return {"componentes": n_comp, "cables": n_wire,
            "dsn": dsn_path, "bytes": len(dsn)}
