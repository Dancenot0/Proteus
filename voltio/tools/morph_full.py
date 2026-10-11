#!/usr/bin/env python3
"""Morphing completo: re-coloca un proyecto real de Proteus según el layout de
un YAML de Voltio, con cables que siguen a sus piezas.

Uso:
    python3 tools/morph_full.py PLANTILLA.pdsprj LAYOUT.yaml SALIDA.pdsprj

Método: cada instancia se identifica por sus marcadores de texto (referencias
"U1:A" o familias como LOGICSTATE) y se mueve como cuerpo rígido en los DOS
espacios de coordenadas: registros de componente (u32, 0.01") y registros en
espacio de cable (i32, 0.1 mm, Y invertido, origen O calibrado con la primera
pareja componente↔marcador). Los puntos de cable se asignan a la instancia
más cercana y viajan con ella. La topología, los blobs, los PINOUTs, la
sección de circuito y el CDB no se tocan: por eso el resultado abre Y simula.
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
FAMS = ["74LS00", "74LS02", "LOGICSTATE", "LED-RED", "RES"]


def main(argv):
    tpl_path, yaml_path, dst = argv[1], argv[2], argv[3]
    with zipfile.ZipFile(tpl_path) as z:
        names = z.namelist()
        d = bytearray(z.read([n for n in names if n.upper().endswith(".DSN")][0]))
        others = {n: z.read(n) for n in names if not n.upper().endswith(".DSN")}

    from voltio.core import miniyaml
    with open(yaml_path, "r", encoding="utf-8") as f:
        doc = miniyaml.loads(f.read())
    target = {}
    for pr in (doc or {}).get("parts", []):
        at = pr.get("at") or [0, 0]
        target[str(pr["ref"])] = (float(at[0]), float(at[1]))

    # ---- registros de componente (espacio comp) ---------------------------
    comps = []
    for fam in FAMS:
        pat = re.compile(re.escape(fam.encode()))
        for m in pat.finditer(bytes(d)):
            i = m.start()
            if i < 1 or d[i - 1] != len(fam):
                continue
            ts, x, y = struct.unpack_from("<III", d, i + len(fam))
            if 0x40000000 < ts < 0x7FFFFFFF and x < 100000 and y < 100000:
                comps.append({"fam": fam, "off": i + len(fam) + 4, "x": x, "y": y})

    # ---- marcadores (espacio cable) ---------------------------------------
    marc = []
    for m in re.finditer(rb"\x00\xff([\x01-\x20])", bytes(d)):
        ln = m.group(1)[0]
        txt = d[m.end():m.end() + ln].decode("latin-1", "replace")
        x, y = struct.unpack_from("<ii", d, m.end() + ln)
        if abs(x) > 40_000_000 or abs(y) > 40_000_000:
            continue
        marc.append({"off": m.end() + ln, "txt": txt, "x": x, "y": y})

    # ---- origen O: primer LOGICSTATE con registro y marcador --------------
    c0 = next(c for c in comps if c["fam"] == "LOGICSTATE")
    m0 = sorted((m for m in marc if m["txt"] == "LOGICSTATE"), key=lambda m: -m["y"])[0]
    Ox = c0["x"] - m0["x"] // SCALE
    Oy = c0["y"] + m0["y"] // SCALE

    def T(cx, cy):
        return ((cx - Ox) * SCALE, (Oy - cy) * SCALE)

    # ---- clusters de instancia --------------------------------------------
    inst = {}   # label -> dict(markers=[], center=(wx,wy), comp_off, comp_xy)
    refs = {}
    for m in marc:
        t = m["txt"]
        if re.fullmatch(r"[A-Z0-9]+:\w", t):
            refs.setdefault(t.split(":")[0], []).append(m)
        elif t in FAMS:
            refs.setdefault(t + "#%d" % len([k for k in refs if k.startswith(t + "#")]),
                            []).append(m)
    # estados: los 4 LOGICSTATE en orden de y con los nombres S,V,R,D del YAML
    estados = sorted([m for m in marc if m["txt"] == "LOGICSTATE"], key=lambda m: -m["y"])
    nombres_est = [r for r in ("S", "V", "R", "D") if r in target]
    for nm, m in zip(nombres_est, estados):
        inst[nm] = {"markers": [m], "comp": None}
    for lab, ms in refs.items():
        base = lab.split("#")[0]
        if base in FAMS and base == "LOGICSTATE":
            continue
        key = base if not lab.endswith(("#0", "#1", "#2")) else None
        if key is None:
            # familia 74LS00 #0/#1: emparejar por x con U1/U3 del YAML si existen
            key = None
        inst.setdefault(lab, {"markers": ms, "comp": None})
    # paquetes: U1 = 74LS00 con puertas A-D (4 refs), U3 = el otro 74LS00
    g00 = [k for k in inst if k.startswith("74LS00#")]
    g00.sort(key=lambda k: min(m["x"] for m in inst[k]["markers"]))
    if len(g00) == 2:
        inst["U1"] = inst.pop(g00[0])
        inst["U3"] = inst.pop(g00[1])
    if "74LS02#0" in inst:
        inst["U2"] = inst.pop("74LS02#0")
    # LED y RES: por registro de componente, sin marcadores
    for fam, lab in (("LED-RED", "D1"), ("RES", "R1")):
        cc = [c for c in comps if c["fam"] == fam]
        if cc and lab in target:
            inst[lab] = {"markers": [], "comp": cc[0]}

    # centros en espacio de cable y deltas
    deltas = {}
    for lab, info in inst.items():
        if lab not in target:
            continue
        if info["markers"]:
            cx = sum(m["x"] for m in info["markers"]) / len(info["markers"])
            cy = sum(m["y"] for m in info["markers"]) / len(info["markers"])
        elif info["comp"]:
            cx, cy = T(info["comp"]["x"], info["comp"]["y"])
        else:
            continue
        ocx, ocy = cx / SCALE + Ox, Oy - cy / SCALE
        tx, ty = target[lab]
        # ancla relativa: el layout del YAML vive en su propio marco; se usa el
        # delta entre el target y el marco del YAML anclado en S
        deltas[lab] = None  # se resuelve abajo
    ay = target.get("S", (0, 0))
    for lab in deltas:
        info = inst[lab]
        if info["markers"]:
            cx = sum(m["x"] for m in info["markers"]) / len(info["markers"])
            cy = sum(m["y"] for m in info["markers"]) / len(info["markers"])
        else:
            cx, cy = T(info["comp"]["x"], info["comp"]["y"])
        ocx, ocy = cx / SCALE + Ox, Oy - cy / SCALE
        tx = ocx + (target[lab][0] - ay[0])
        ty = ocy + (target[lab][1] - ay[1])
        deltas[lab] = (tx - ocx, ty - ocy) and (target[lab][0] - ay[0], target[lab][1] - ay[1])

    # ---- parcheo -----------------------------------------------------------
    n = {"comp": 0, "mark": 0, "conn": 0, "wire": 0}
    for lab, (dx, dy) in deltas.items():
        info = inst[lab]
        if info["comp"]:
            c = info["comp"]
            struct.pack_into("<II", d, c["off"], c["x"] + int(dx), c["y"] + int(dy))
            n["comp"] += 1
        for c in comps:
            if info["comp"] is None and lab in ("U1", "U2", "U3") and c["fam"] in FAMS:
                pass
        for m in info["markers"]:
            struct.pack_into("<ii", d, m["off"],
                             m["x"] + int(dx * SCALE), m["y"] - int(dy * SCALE))
            n["mark"] += 1
    # paquetes 74LS00: su registro de componente (uno por paquete)
    for lab in ("U1", "U3"):
        if lab not in deltas:
            continue
        cc = [c for c in comps if c["fam"] == "74LS00"]
        cc.sort(key=lambda c: c["x"])
        pick = cc[0] if lab == "U1" else (cc[1] if len(cc) > 1 else cc[0])
        dx, dy = deltas[lab]
        struct.pack_into("<II", d, pick["off"], pick["x"] + int(dx), pick["y"] + int(dy))
        n["comp"] += 1
    for lab in ("U2",):
        if lab in deltas:
            cc = [c for c in comps if c["fam"] == "74LS02"]
            if cc:
                dx, dy = deltas[lab]
                struct.pack_into("<II", d, cc[0]["off"], cc[0]["x"] + int(dx), cc[0]["y"] + int(dy))
                n["comp"] += 1
    # conexiones con referencia
    for m in re.finditer(rb"\x00\xff([\x01-\x20])", bytes(d)):
        ln = m.group(1)[0]
        txt = d[m.end():m.end() + ln].decode("latin-1", "replace")
        if ":" not in txt:
            continue
        base = txt.split(":")[0]
        if base in deltas:
            dx, dy = deltas[base]
            o = m.end() + ln
            x, y = struct.unpack_from("<ii", d, o)
            struct.pack_into("<ii", d, o, x + int(dx * SCALE), y - int(dy * SCALE))
            n["conn"] += 1
    # dueño de cada punto de cable = la PUNTA DE PATILLA exacta que toca:
    # tip = T(registro comp) + PIN_OFF[familia, patilla] (ya en espacio cable).
    lab_de_comp = []
    est = sorted([c for c in comps if c["fam"] == "LOGICSTATE"], key=lambda c: c["y"])
    for nm, c in zip(("S", "V", "R", "D"), est):
        lab_de_comp.append((id(c), nm))
    c00 = sorted([c for c in comps if c["fam"] == "74LS00"], key=lambda c: c["x"])
    if len(c00) >= 2:
        lab_de_comp += [(id(c00[0]), "U1"), (id(c00[1]), "U3")]
    c02 = [c for c in comps if c["fam"] == "74LS02"]
    if c02:
        lab_de_comp.append((id(c02[0]), "U2"))
    for fam, lab in (("LED-RED", "D1"), ("RES", "R1")):
        cc = [c for c in comps if c["fam"] == fam]
        if cc:
            lab_de_comp.append((id(cc[0]), lab))
    from voltio.proteus.writer import PIN_OFF
    tips = []
    for cid, lab in lab_de_comp:
        c = next(c for c in comps if id(c) == cid)
        wx, wy = T(c["x"], c["y"])
        for (fam, pin), (ox, oy) in PIN_OFF.items():
            if fam == c["fam"]:
                tips.append((wx + ox, wy + oy, lab))
    for m in re.finditer(rb"\x00\x00([\x02-\x06]\x00)", bytes(d)):
        np_ = struct.unpack("<H", m.group(1))[0]
        base = m.end()
        if base + 8 * np_ > len(d) or d[base + 8 * np_:base + 8 * np_ + 5] != b"\x00\x1d\x00\x00\x00":
            continue
        viejos = [struct.unpack_from("<ii", d, base + k * 8) for k in range(np_)]
        nuevos = [None] * np_
        for k, (wx, wy) in enumerate(viejos):
            best, bd = None, 60_000
            for tx, ty, lab in tips:
                dd = abs(wx - tx) + abs(wy - ty)
                if dd < bd:
                    best, bd = lab, dd
            if best in deltas:
                dx, dy = deltas[best]
                nuevos[k] = (wx + int(dx * SCALE), wy - int(dy * SCALE))
        if np_ == 3 and nuevos[0] and nuevos[2] and nuevos[1] is None:
            if viejos[1][0] == viejos[0][0]:
                nuevos[1] = (nuevos[0][0], nuevos[2][1])
            else:
                nuevos[1] = (nuevos[2][0], nuevos[0][1])
        for k, nv in enumerate(nuevos):
            if nv:
                struct.pack_into("<ii", d, base + k * 8, nv[0], nv[1])
                n["wire"] += 1

    with open(dst.replace(".pdsprj", ".dsn"), "wb") as f:
        f.write(bytes(d))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for nm, bl in others.items():
            z.writestr(nm, bl)
        z.writestr("ROOT.DSN", bytes(d))
    print(f"instancias movidas: {len(deltas)}  {n}")
    for lab, dd in sorted(deltas.items()):
        print(f"   {lab:<4} delta=({dd[0]:+6.0f},{dd[1]:+6.0f}) comp-units")
    print(f"emitido: {dst} (+ .dsn)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
