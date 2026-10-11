#!/usr/bin/env python3
"""R5: primer DSN 100% sintético emitido por el motor Voltio.

Circuito objetivo: el cerrojo SR de examples/sr_latch.yaml.

Piezas del ensamblaje (todo copiado o sintetizado a partir de plantillas
reales del usuario, dialecto 8.6):

  · esqueleto (cabecera + estilos + opciones SPICE) del DSN vacío mínimo;
  · un registro de componente por instancia, copiado byte a byte de la
    plantilla de su familia y parcheando SOLO x, y y el STATE del blob;
  · un par de marcadores de texto por instancia (referencia + familia),
    copiados y reparcheados con las coordenadas nuevas;
  · registros de cable sintetizados desde cero con el formato descodizado;
  · PINOUTs y ROOT.CDB copiados de la plantilla.

Decisiones conservadoras, documentadas a propósito:

  * Dos paquetes 74LS00 separados (U1 usa la puerta A, U2 la puerta A) en vez
    de un cuadruple con dos puertas: los offsets de patilla del paquete
    completo no están todos descodificados, y los de la puerta A sí.
  * La tierra es un LOGICSTATE en 0: eléctricamente es una fuente ideal de
    nivel bajo, idéntica al riel, y evita necesitar la familia del símbolo
    de tierra, que aún no está localizada.
  * Offsets de patilla en unidades de cable: 74LS00 puerta A:
    1=(-762000,-254000) 2=(-762000,+254000) 3=(+762000,0), leídos de los
    $PINDEFAULT de la plantilla; LOGICSTATE Q=(0,0) (su origen ES la patilla);
    LED-RED y RES: (∓254000,0) estimados por simetría de símbolo.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from voltio.dsl.loader import load_file  # noqa: E402

SCALE = 2540
OX, OY = 4750, 1730          # origen del espacio de cable calibrado en Con_cable

PIN_OFF = {
    ("74LS00", "A0"): (-762000, -254000),
    ("74LS00", "A1"): (-762000, +254000),
    ("74LS00", "Y"):  (+762000, 0),
    ("LOGICSTATE", "Q"): (0, 0),
    ("LED-RED", "A"): (-254000, 0),
    ("LED-RED", "K"): (+254000, 0),
    ("RES", "1"): (-254000, 0),
    ("RES", "2"): (+254000, 0),
}

# ref del YAML -> (familia plantilla, etiqueta que verá Proteus)
INST = {
    "S":  ("LOGICSTATE", "S"),
    "R":  ("LOGICSTATE", "R"),
    "G1": ("LOGICSTATE", "GND0"),
    "U1A": ("74LS00", "U1"),
    "U1B": ("74LS00", "U2"),
    "D1": ("LED-RED", "D1"),
    "D2": ("LED-RED", "D2"),
    "R1": ("RES", "R1"),
    "R2": ("RES", "R2"),
}
# pin del YAML -> pin de la puerta A del paquete
PINMAP = {"A0": "A0", "A1": "A1", "Y": "Y", "Q": "Q", "A": "A", "K": "K",
          "1": "1", "2": "2", "P": "Q"}


def T(x, y):
    return (int(round((x - OX) * SCALE)), int(round((OY - y) * SCALE)))


def main(argv):
    yaml_path, tpl_path, dst = argv[1], argv[2], argv[3]
    circuit, issues = load_file(yaml_path)
    errs = [i for i in issues if i.kind == "error"]
    if errs:
        raise SystemExit(f"el YAML no valida: {errs[:3]}")

    with zipfile.ZipFile(tpl_path) as z:
        names = z.namelist()
        d = z.read([n for n in names if n.upper().endswith(".DSN")][0])
        cdb = z.read("ROOT.CDB") if "ROOT.CDB" in names else b""
        pxml = z.read("PROJECT.XML")
        scripts = {n: z.read(n) for n in names if n.upper().startswith("SCRIPTS/")}

    # esqueleto vacío (cabecera+estilos+opciones) del backup sin objetos
    empty = zipfile.ZipFile(os.path.join(os.path.dirname(tpl_path),
                                         "Backup Of Con_cable.pdsbak"))
    e = empty.read("ROOT.DSN")
    i_opt = e.find(b"TDUPPER")
    i_opt = e.rfind(b"\x0c\x00\x00\x00", 0, i_opt)
    head, tail = e[:i_opt], e[i_opt:]

    # registros de componente y marcadores, por familia, de la plantilla llena
    fam_rec, fam_marks = {}, {}
    for fam in {f for f, _ in INST.values()}:
        pat = re.compile(re.escape(fam.encode()))
        m = pat.search(d)
        while m:
            i = m.start()
            if i >= 1 and d[i - 1] == len(fam):
                ts, x, y = struct.unpack_from("<III", d, i + len(fam))
                if 0x40000000 < ts < 0x7FFFFFFF and x < 100000:
                    bm = re.search(rb"\{\*DEVICE\}", d[i:i + 6000])
                    q = i + bm.start() - 4
                    (L,) = struct.unpack_from("<I", d, q)
                    fam_rec[fam] = (i - 1, q + 4 + L)
                    break
            m = pat.search(d, m.end())
        # marcadores de texto de una instancia de esa familia
        mm = re.search(rb"\x00\xff([\x01-\x20])([ -~]{1,12}?)\x00?\xff?", d)
        marks = []
        for mk in re.finditer(rb"\x00\xff([\x01-\x20])", d):
            ln = mk.group(1)[0]
            s_ = d[mk.end():mk.end() + ln]
            nxt = struct.unpack_from("<ii", d, mk.end() + ln)
            if abs(nxt[0]) > 40_000_000:
                continue
            if s_.decode("latin-1") == fam or (b":" in s_ and s_.split(b":")[0].decode("latin-1") in {l for f, l in INST.values() if f == fam}):
                marks.append((mk.start(), mk.end() + ln + 8))
        if marks:
            a, b = marks[0]
            for a2, b2 in marks[1:3]:
                b = max(b, b2)
            fam_marks[fam] = d[a:b]

    body = bytearray()
    lay = {p.ref: (p.x, p.y) for p in circuit.parts.values()}

    # ---- componentes + marcadores -----------------------------------------
    for ref, (fam, label) in INST.items():
        if ref not in lay:
            continue
        x, y = lay[ref]
        a, b = fam_rec[fam]
        rec = bytearray(d[a:b])
        # parchea x,y (offset: 1 + len(fam) + 4 tras el inicio del registro)
        struct.pack_into("<II", rec, 1 + len(fam) + 4, int(x), int(y))
        # STATE del blob para los estados lógicos
        want = {"S": b"1", "R": b"1", "G1": b"0"}.get(ref)
        if want:
            rec = bytearray(bytes(rec).replace(b"{STATE=0}", b"{STATE=" + want + b"}")
                            .replace(b"{STATE=1}", b"{STATE=" + want + b"}")
                            if want == b"0" else
                            bytes(rec).replace(b"{STATE=0}", b"{STATE=1}"))
        body += rec
        # marcadores de texto: referencia y familia, con coordenadas nuevas
        mk = bytearray(fam_marks.get(fam, b""))
        if mk:
            wx, wy = T(x, y)
            # primer par de i32 tras la primera referencia
            k = re.search(rb"\x00\xff.", bytes(mk))
            if k:
                ln = mk[k.end() - 1]
                p0 = k.end() + ln
                struct.pack_into("<ii", mk, p0, wx - 254000, wy - 254000)
                # segunda referencia (familia): su par de i32
                k2 = re.search(rb"\x00\xff.", bytes(mk), k.end())
                if k2:
                    ln2 = mk[k2.end() - 1]
                    struct.pack_into("<ii", mk, k2.end() + ln2, wx, wy + 254000)
            body += mk

    # ---- cables sintetizados ----------------------------------------------
    nw = 0
    for net in circuit.nets.values():
        pts = []
        for ep in net.endpoints:
            if ep.part not in INST:
                continue
            fam, _ = INST[ep.part]
            pin = PINMAP.get(ep.pin, ep.pin)
            off = PIN_OFF.get((fam, pin))
            if off is None:
                continue
            cx, cy = lay[ep.part]
            wx, wy = T(cx, cy)
            pts.append((wx + off[0], wy + off[1]))
        if len(pts) < 2:
            continue
        def emite(ruta):
            nonlocal nw
            body2 = b"\x00\x00" + struct.pack("<H", len(ruta))
            for px, py in ruta:
                body2 += struct.pack("<ii", px, py)
            body2 += b"\x00\x1d\x00\x00\x00"
            body2 += (b"\x00\xc0\x9e\x00\x00\x00\x40\x00\x00\x01\xff\xff\xff\x00"
                      b"\xff\xff\xff\x00\x02\x7f")
            body2 += b"WIRE\x00"
            nw += 1
            return body2

        if len(pts) == 2 and (pts[0][0] == pts[1][0] or pts[0][1] == pts[1][1]):
            body += emite(pts)
        elif len(pts) == 2:
            body += emite([pts[0], (pts[1][0], pts[0][1]), pts[1]])
        else:
            # Tres o más extremos: peine ortogonal. Un cable en L por extremo
            # más una espina vertical que los une. En ISIS los cables que se
            # tocan en un punto se conectan solos, así que el peine es
            # eléctricamente un solo nodo, y visualmente es el abanico de
            # siempre: un renglón por cable, sin diagonales.
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            jx = min(xs) - 254000
            for (px, py) in pts:
                body += emite([(px, py), (jx, py)])
            body += emite([(jx, min(ys)), (jx, max(ys))])

    # ---- PINOUTs de la plantilla ------------------------------------------
    for fam in ("74LS00",):
        pat = bytes([len(fam) + 2]) + b"[" + fam.encode() + b"]"
        i = d.find(pat)
        if i != -1:
            (L,) = struct.unpack_from("<I", d, i + len(pat) + 4)
            body += d[i - 1:i + len(pat) + 8 + L]

    dsn = head + bytes(body) + tail
    out_dsn = dst.replace(".pdsprj", ".dsn")
    with open(out_dsn, "wb") as f:
        f.write(dsn)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("PROJECT.XML", pxml)
        z.writestr("ROOT.DSN", dsn)
        if cdb:
            z.writestr("ROOT.CDB", cdb)
        for n, b in scripts.items():
            z.writestr(n, b)
    print(f"componentes emitidos: {sum(1 for r in INST if r in lay)}")
    print(f"cables sintetizados : {nw}")
    print(f"DSN : {out_dsn} ({len(dsn):,} bytes)")
    print(f"ZIP : {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
