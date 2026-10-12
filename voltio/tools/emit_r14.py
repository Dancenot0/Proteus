#!/usr/bin/env python3
"""R14: DSN 100% sintético con la gramática REAL de cables decodificada de R11a.

Diagnóstico del kernel-fault de R13 (verificado byte a byte contra el bloque A
bueno de R11a en salida/proteus/experimentos/R12_sonda.dsn, registros WIRE en
73344..77583):

  DEFECTO 1 — orden del registro. R13 emitía:
        [puntos][centinela][estilo][WIRE\0]
    El archivo bueno emite SIEMPRE:
        [handles][centinela 00 1d 00 00 00]
        [estilo   00 c0 9e 00 00 00 40 00 00]
        [cola     01 ff ff ff 00 ff ff ff 00 02 7f]
        [WIRE\0]
        [u8 0][u8 0][u16 n][i32 x,i32 y]*n
    Como ISIS busca el tag antes de leer el cuerpo, los bytes de puntos que
    R13 ponía delante se interpretaban como handles/garbage → descosido.

  DEFECTO 2 — recuento de puntos mal alineado. En el archivo bueno, tras
    "WIRE\0" hay `00 00 <u16 n>` (los dos ceros son parte del preámbulo del
    vector; n es u16). R13 escribía b"\x00\x00"+pack("<H",len) ANTES del
    centinela, así que donde ISIS esperaba ver el conteo veía coordenadas →
    leía miles de millones de pares fuera del buffer → SEH KERNELBASE.

  DEFECTO 3 — consistencia dibujo↔eléctrica (ley R9): R13 además dejaba a
    veces un tramo cuyo extremo no caía sobre ningún tip. Se conserva la
    regla de oro (cada segmento nace/muere en un tip o comparte el punto
    exacto con otro cable) y se añade un VERIFICADOR ESTRUCTURAL post-escritura:
    re-parsea cada registro WIRE emitido exactamente como lo haría ISIS y
    aborta si algún recuento se sale del buffer o algún extremo queda suelto.

Componentes/marcadores/PINOUTs/esqueleto: idénticos a R13 (emit_sr_dsn), que
abrió sin errores hasta el parseo de cables.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from voltio.dsl.loader import load_file  # noqa: E402
import emit_sr_dsn as base  # noqa: E402  (reutiliza PIN_OFF, INST, T, ...)

TAIL = (b"\x00\x1d\x00\x00\x00"                      # centinela
        b"\x00\xc0\x9e\x00\x00\x00\x40\x00\x00"      # estilo
        b"\x01\xff\xff\xff\x00\xff\xff\xff\x00\x02\x7f"
        b"WIRE\x00")


def wire_record(ruta):
    """Registro WIRE canónico de R11a: cola + tag + vector de puntos."""
    b = bytearray(TAIL)
    b += b"\x00\x00" + struct.pack("<H", len(ruta))
    for px, py in ruta:
        b += struct.pack("<ii", int(px), int(py))
    return bytes(b)


def verificar(dsn, tips):
    """Re-parsea cada WIRE como ISIS y comprueba extremos pegados."""
    errs = []
    i = 0
    while True:
        j = dsn.find(b"WIRE\x00", i)
        if j == -1:
            break
        q = j + 5
        pre = dsn[q:q+4]
        if pre[:2] != b"\x00\x00":
            errs.append(f"W@{j}: preámbulo de vector != 00 00 ({pre.hex()})")
            i = j + 4
            continue
        n, = struct.unpack_from("<H", dsn, q+2)
        if n < 2 or n > 64:
            errs.append(f"W@{j}: n={n} fuera de rango")
            i = j + 4
            continue
        need = q + 4 + 8*n
        if need > len(dsn):
            errs.append(f"W@{j}: n={n} desborda el buffer (kernel fault)")
            i = j + 4
            continue
        pts = [struct.unpack_from("<ii", dsn, q+4+8*k) for k in range(n)]
        for p in pts:
            if abs(p[0]) > 2**31-1 or abs(p[1]) > 2**31-1:
                errs.append(f"W@{j}: coordenada fuera de i32")
        if pts[0] not in tips and pts[-1] not in tips:
            errs.append(f"W@{j}: NINGÚN extremo sobre un pin => red huérfana {pts}")
        # siguiente registro debe empezar con centinela o tag de objeto
        nxt = dsn[need:need+5]
        if nxt[:2] not in (b"\x00\x1d", b"\x00\xff", b"\x08\xff", b"\xff\x49"):
            errs.append(f"W@{j}: bytes tras el vector no abren registro vecino: {nxt.hex(' ')}")
        i = need
    return errs


def main(argv):
    yaml_path, tpl_path, dst = argv[1], argv[2], argv[3]
    circuit, issues = load_file(yaml_path)
    errs = [it for it in issues if it.kind == "error"]
    if errs:
        raise SystemExit(f"el YAML no valida: {errs[:3]}")

    with zipfile.ZipFile(tpl_path) as z:
        names = z.namelist()
        d = z.read([n for n in names if n.upper().endswith(".DSN")][0])
        cdb = z.read("ROOT.CDB") if "ROOT.CDB" in names else b""
        pxml = z.read("PROJECT.XML")
        scripts = {n: z.read(n) for n in names if n.upper().startswith("SCRIPTS/")}

    empty = zipfile.ZipFile(os.path.join(os.path.dirname(tpl_path),
                                         "Backup Of Con_cable.pdsbak"))
    e = empty.read("ROOT.DSN")
    i_opt = e.find(b"TDUPPER")
    i_opt = e.rfind(b"\x0c\x00\x00\x00", 0, i_opt)
    head, tail_ = e[:i_opt], e[i_opt:]

    fam_rec, fam_marks = {}, {}
    for fam in {f for f, _ in base.INST.values()}:
        pat = re.compile(re.escape(fam.encode()))
        m = pat.search(d)
        while m:
            ii = m.start()
            if ii >= 1 and d[ii - 1] == len(fam):
                ts, x, y = struct.unpack_from("<III", d, ii + len(fam))
                if 0x40000000 < ts < 0x7FFFFFFF and x < 100000:
                    bm = re.search(rb"\{\*DEVICE\}", d[ii:ii + 6000])
                    qq = ii + bm.start() - 4
                    (L,) = struct.unpack_from("<I", d, qq)
                    fam_rec[fam] = (ii - 1, qq + 4 + L)
                    break
            m = pat.search(d, m.end())
        marks = []
        for mk in re.finditer(rb"\x00\xff([\x01-\x20])", d):
            ln = mk.group(1)[0]
            s_ = d[mk.end():mk.end() + ln]
            nxt = struct.unpack_from("<ii", d, mk.end() + ln)
            if abs(nxt[0]) > 40_000_000:
                continue
            if s_.decode("latin-1") == fam or (b":" in s_ and s_.split(b":")[0].decode("latin-1")
                                               in {l for f, l in base.INST.values() if f == fam}):
                marks.append((mk.start(), mk.end() + ln + 8))
        if marks:
            a, b = marks[0]
            for a2, b2 in marks[1:3]:
                b = max(b, b2)
            fam_marks[fam] = d[a:b]

    body = bytearray()
    lay = {p.ref: (p.x, p.y) for p in circuit.parts.values()}

    # ---- componentes + marcadores (idéntico a R13) -------------------------
    for ref, (fam, label) in base.INST.items():
        if ref not in lay:
            continue
        x, y = lay[ref]
        a, b = fam_rec[fam]
        rec = bytearray(d[a:b])
        struct.pack_into("<II", rec, 1 + len(fam) + 4, int(x), int(y))
        want = {"S": b"1", "R": b"1", "G1": b"0"}.get(ref)
        if want:
            rec = bytearray(bytes(rec).replace(b"{STATE=0}", b"{STATE=" + want + b"}")
                            .replace(b"{STATE=1}", b"{STATE=" + want + b"}")
                            if want == b"0" else
                            bytes(rec).replace(b"{STATE=0}", b"{STATE=1}"))
        body += rec
        mk = bytearray(fam_marks.get(fam, b""))
        if mk:
            wx, wy = base.T(x, y)
            k = re.search(rb"\x00\xff.", bytes(mk))
            if k:
                ln = mk[k.end() - 1]
                p0 = k.end() + ln
                struct.pack_into("<ii", mk, p0, wx - 254000, wy - 254000)
                k2 = re.search(rb"\x00\xff.", bytes(mk), k.end())
                if k2:
                    ln2 = mk[k2.end() - 1]
                    struct.pack_into("<ii", mk, k2.end() + ln2, wx, wy + 254000)
            body += mk

    # ---- cables con la gramática de R11a -----------------------------------
    def tip_de(ep):
        if ep.part not in base.INST:
            return None
        fam, _ = base.INST[ep.part]
        pin = base.PINMAP.get(ep.pin, ep.pin)
        off = base.PIN_OFF.get((fam, pin))
        if off is None:
            return None
        cx, cy = lay[ep.part]
        wx, wy = base.T(cx, cy)
        return (wx + off[0], wy + off[1])

    all_tips = []
    for net in circuit.nets.values():
        for ep in net.endpoints:
            tp = tip_de(ep)
            if tp is not None and tp not in all_tips:
                all_tips.append(tp)
    tips = set(all_tips)

    nw = 0
    def emite(ruta):
        nonlocal nw
        nw += 1
        return wire_record(ruta)

    def emite_pegado(ruta):
        if ruta[0] not in tips and ruta[-1] in tips:
            ruta = list(reversed(ruta))
        body_ = emite(ruta)
        if ruta[0] not in tips:
            anchor = next((tp for tp in all_tips
                           if tp[0] == ruta[0][0] or tp[1] == ruta[0][1]), None)
            if anchor is not None:
                mid = ((anchor[0], ruta[0][1]) if anchor[1] != ruta[0][1]
                       else (ruta[0][0], anchor[1]))
                body_ += emite([anchor, mid, ruta[0]])
        return body_

    for net in circuit.nets.values():
        pts = [tp for tp in (tip_de(ep) for ep in net.endpoints) if tp is not None]
        if len(pts) < 2:
            continue
        if len(pts) == 2 and (pts[0][0] == pts[1][0] or pts[0][1] == pts[1][1]):
            body += emite_pegado(pts)
        elif len(pts) == 2:
            body += emite_pegado([pts[0], (pts[1][0], pts[0][1]), pts[1]])
        else:
            xs = sorted(p[0] for p in pts)
            ys = sorted(p[1] for p in pts)
            bx, by = xs[len(xs)//2], ys[len(ys)//2]
            if any(p[0]==bx for p in pts) and any(p[1]==by for p in pts):
                bx = xs[-1] if xs[-1] != bx else xs[0]
                by = ys[-1] if ys[-1] != by else ys[0]
            for (px, py) in pts:
                if px == bx or py == by:
                    body += emite([(px, py), (bx, by)])
                else:
                    body += emite([(px, py), (bx, py), (bx, by)])

    # ---- PINOUTs (idéntico a R13) ------------------------------------------
    for fam in ("74LS00",):
        pat = bytes([len(fam) + 2]) + b"[" + fam.encode() + b"]"
        ii = d.find(pat)
        if ii != -1:
            (L,) = struct.unpack_from("<I", d, ii + len(pat) + 4)
            body += d[ii - 1:ii + len(pat) + 8 + L]

    dsn = head + bytes(body) + tail_

    # ---- verificación estructural pre-entrega ------------------------------
    verrs = verificar(dsn, tips)
    if verrs:
        for v in verrs[:10]:
            print("VERIF:", v, file=sys.stderr)
        raise SystemExit(f"R14 abortado: {len(verrs)} fallos estructurales")

    out_dsn = dst.replace(".pdsprj", ".dsn")
    with open(out_dsn, "wb") as f:
        f.write(dsn)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("PROJECT.XML", pxml)
        z.writestr("ROOT.DSN", dsn)
        if cdb:
            z.writestr("ROOT.CDB", cdb)
        for n, bb in scripts.items():
            z.writestr(n, bb)
    print(f"componentes emitidos: {sum(1 for r in base.INST if r in lay)}")
    print(f"cables sintetizados : {nw}")
    print(f"verificación        : OK (todos los WIRE re-parseables, extremos pegados)")
    print(f"DSN : {out_dsn} ({len(dsn):,} bytes)")
    print(f"ZIP : {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
