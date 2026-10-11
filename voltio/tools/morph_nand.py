#!/usr/bin/env python3
"""R3: primer DSN emitido por Voltio — morphing rígido en espacio de cable.

Toma el DSN real del usuario (Compuertas NAND.pdsprj: abre Y simula en su
Proteus 8.6) y re-coloca sus instancias según el layout que calcula el DSL de
Voltio para la réplica del PDF.

Qué se parchea (todo en espacio de cable, i32 en unidades de 0.1 mm):
  · marcadores de instancia  [00 ff|01 00][len][familia][i32 x][i32 y]
  · registros de conexión    [00 ff][len]["U1:C"][i32 x][i32 y]
  · puntos de cada cable     dentro del registro WIRE
Qué NO se toca: blobs de propiedades, PINOUTs, CDB, sección de circuito,
referencias de instancia sin coordenadas. Cada instancia se mueve como un
cuerpo rígido, así que la conectividad sobrevive al traslado.

El delta de cada instancia sale de conservar el layout RELATIVO del YAML
anclado a la posición vieja de la primera entrada: así el origen absoluto del
espacio de cable no necesita conocerse.

    python3 tools/morph_nand.py <Compuertas NAND.pdsprj> <yaml> <salida.pdsprj>
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
MARK = re.compile(rb"\x00\xff([\x01-\x20])([ -~]{1,12})")


def _fam_de(fam):
    return {"74LS00": ("U1", "U3"), "74LS02": ("U2",),
            "LOGICSTATE": ("S", "V", "R", "D")}.get(fam, ())


def main(argv):
    src, yaml_path, dst = argv[1], argv[2], argv[3]
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        d = bytearray(z.read([n for n in names if n.upper().endswith(".DSN")][0]))
        others = {n: z.read(n) for n in names if not n.upper().endswith(".DSN")}

    circuit, issues = load_file(yaml_path)
    if [i for i in issues if i.kind == "error"]:
        raise SystemExit("el YAML no valida")

    # ---- posiciones viejas en espacio de componente, por marcadores -------
    marc = []   # (off_coords, familia, x_wire, y_wire)
    for m in re.finditer(rb"\x00\xff([\x01-\x20])", bytes(d)):
        ln = m.group(1)[0]
        fam = d[m.end():m.end() + ln].decode("latin-1", "replace")
        x, y = struct.unpack_from("<ii", d, m.end() + ln)
        if abs(x) > 40_000_000 or abs(y) > 40_000_000:
            continue
        marc.append([m.end() + ln, fam, x, y])

    # calibra el origen con el único componente con registro doble conocido:
    # el LOGICSTATE instancia 1 (registro comp 1350,1030 ↔ primer marcador)
    estados = [m for m in marc if m[1] == "LOGICSTATE"]
    estados.sort(key=lambda m: -m[3])          # y wire descendente = arriba primero
    if not estados:
        raise SystemExit("sin marcadores de estado para calibrar")
    Ox = 1350 - estados[0][2] // SCALE
    Oy = 1030 + estados[0][3] // SCALE

    def comp(m):
        return (m[2] / SCALE + Ox, Oy - m[3] / SCALE)

    # instancia de cada marcador: por familia y agrupación espacial
    inst_old = {}   # label -> (cx, cy) en espacio comp
    gates = [m for m in marc if m[1] in ("74LS00", "74LS02")]
    # U1 = grupo 74LS00 más a la izquierda, U3 = el otro, U2 = el 74LS02
    g00 = sorted([m for m in gates if m[1] == "74LS00"], key=lambda m: m[2])
    g02 = [m for m in gates if m[1] == "74LS02"]
    if len(g00) < 2 or not g02:
        raise SystemExit("no encuentro los marcadores de las puertas")
    inst_old["U1"] = comp(g00[0])
    inst_old["U3"] = comp(g00[-1])
    inst_old["U2"] = comp(g02[0])
    for m in marc:
        if m[1].startswith("U1:") or m[1].startswith("U3:") or m[1].startswith("U2:"):
            inst_old[m[1].split(":")[0]] = inst_old.get(m[1].split(":")[0]) or comp(m)
    for k, m in enumerate(estados):
        inst_old[["S", "V", "R", "D"][k]] = comp(m)

    # conexiones: su ref dice la instancia; sus coords son puntas de pin.
    # La ref mide exactamente lo que dice su byte de longitud: leerla con
    # {1,12} glotón se comía bytes de coordenadas y las convertía en basura.
    conns = []
    for m in re.finditer(rb"\x00\xff([\x01-\x20])", bytes(d)):
        ln = m.group(1)[0]
        ref = d[m.end():m.end() + ln].decode("latin-1", "replace")
        if ":" not in ref or not ref[:1].isalnum():
            continue
        x, y = struct.unpack_from("<ii", d, m.end() + ln)
        if abs(x) > 40_000_000 or abs(y) > 40_000_000:
            continue
        conns.append([m.end() + ln, ref.split(":")[0], x, y])

    # ---- objetivos: layout relativo del YAML anclado en S -----------------
    yaml_at = {p.ref: (p.x, p.y) for p in circuit.parts.values()}
    paquete = {"U1A": "U1", "U1B": "U1", "U1C": "U1", "U1D": "U1",
               "U2A": "U2", "U3A": "U3"}
    anchor_yaml = yaml_at["S"]
    anchor_old = inst_old["S"]
    deltas = {}
    for inst in ("S", "V", "R", "D", "U1", "U2", "U3"):
        yref = inst if inst in yaml_at else [k for k, v in paquete.items() if v == inst][0]
        ax, ay = yaml_at[yref]
        tx = anchor_old[0] + (ax - anchor_yaml[0])
        ty = anchor_old[1] + (ay - anchor_yaml[1])
        ox, oy = inst_old[inst]
        deltas[inst] = (tx - ox, ty - oy)

    # ---- parcheo: marcadores y conexiones ---------------------------------
    n_mark = n_conn = 0
    for off, fam, x, y in marc:
        inst = None
        if fam == "LOGICSTATE":
            cx, cy = x / SCALE + Ox, Oy - y / SCALE
            inst = min(deltas, key=lambda k: abs(cx - inst_old[k][0]) + abs(cy - inst_old[k][1]))
            if abs(cx - inst_old[inst][0]) + abs(cy - inst_old[inst][1]) > 60:
                inst = None
        elif fam in ("74LS00", "74LS02"):
            cx, cy = x / SCALE + Ox, Oy - y / SCALE
            cand = [k for k in deltas if inst_old[k] and
                    abs(cx - inst_old[k][0]) < 90 and abs(cy - inst_old[k][1]) < 200]
            inst = cand[0] if cand else None
        if inst:
            dx, dy = deltas[inst]
            struct.pack_into("<ii", d, off, int(round(x + dx * SCALE)), int(round(y - dy * SCALE)))
            n_mark += 1
    for off, ref, x, y in conns:
        if ref in deltas:
            dx, dy = deltas[ref]
            struct.pack_into("<ii", d, off, int(round(x + dx * SCALE)), int(round(y - dy * SCALE)))
            n_conn += 1

    # ---- parcheo: puntos de cable ----------------------------------------
    # Los extremos se emparejan EXACTAMENTE con las puntas de conexión viejas
    # (son las mismas coordenadas), y los codos se recalculan desde los
    # extremos ya parcheados: un codo es siempre (x de un extremo, y del otro).
    viejo_delta = {}
    for off, ref, x, y in [(c[0] - 0, c[1], None, None) for c in []]:
        pass
    conn_old = {}
    for m in re.finditer(rb"\x00\xff([\x01-\x20])", bytes(d)):
        ln = m.group(1)[0]
        ref = d[m.end():m.end() + ln].decode("latin-1", "replace")
        if ":" not in ref:
            continue
        x, y = struct.unpack_from("<ii", d, m.end() + ln)
        inst = ref.split(":")[0]
        if inst in deltas:
            conn_old[(x, y)] = deltas[inst]
    n_pts = 0
    for m in re.finditer(rb"\x00\x00([\x02-\x06]\x00)", bytes(d)):
        n = struct.unpack("<H", m.group(1))[0]
        base = m.end()
        if base + 8 * n > len(d):
            continue
        if d[base + 8 * n:base + 8 * n + 5] != b"\x00\x1d\x00\x00\x00":
            continue
        pts = [struct.unpack_from("<ii", d, base + k * 8) for k in range(n)]
        nuevos = []
        for k, (wx, wy) in enumerate(pts):
            dlt = conn_old.get((wx, wy))
            if dlt is not None:
                nuevos.append((int(round(wx + dlt[0] * SCALE)),
                               int(round(wy - dlt[1] * SCALE))))
            else:
                nuevos.append(None)
        # codos: se reconstruyen desde los extremos parcheados
        if n == 3 and nuevos[0] and nuevos[2] and nuevos[1] is None:
            if pts[1][0] == pts[0][0]:
                nuevos[1] = (nuevos[0][0], nuevos[2][1])
            else:
                nuevos[1] = (nuevos[2][0], nuevos[0][1])
        for k, nv in enumerate(nuevos):
            if nv is not None:
                struct.pack_into("<ii", d, base + k * 8, nv[0], nv[1])
                n_pts += 1

    # ---- parcheo: registros de componente en SU espacio (u32, 0.01") -----
    # Los marcadores en espacio de cable mandan sobre el dibujo (ronda 3,
    # caso A), pero se parchean los dos espacios para que el archivo quede
    # interno-consistente lo mires por donde lo mires.
    n_comp = 0
    for fam in ("74LS00", "74LS02", "LOGICSTATE", "LED-RED", "RES"):
        for m in re.finditer(re.escape(fam.encode()), bytes(d)):
            i = m.start()
            if i < 1 or d[i - 1] != len(fam):
                continue
            ts, x, y = struct.unpack_from("<III", d, i + len(fam))
            if not (0x40000000 < ts < 0x7FFFFFFF) or x > 100000 or y > 100000:
                continue
            # instancia por cercanía en espacio de componente
            cx, cy = x, y
            cand = [k for k in deltas
                    if abs(cx - inst_old[k][0]) < 5 and abs(cy - inst_old[k][1]) < 5]
            if not cand:
                # los registros de componente viven en su propio marco: se
                # identifican por familia y orden
                cand = [k for k in deltas if k in _fam_de(fam)]
                if not cand:
                    continue
            dx, dy = deltas[cand[0]]
            struct.pack_into("<ii", d, i + len(fam) + 4, int(x + round(dx)), int(y + round(dy)))
            n_comp += 1

    # ---- empaquetado -------------------------------------------------------
    dsn_path = dst.replace(".pdsprj", ".dsn")
    with open(dsn_path, "wb") as f:
        f.write(bytes(d))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, bytes(d) if n.upper().endswith(".DSN") else others[n])
    print(f"marcadores movidos: {n_mark}   conexiones: {n_conn}   puntos de cable: {n_pts}")
    for k, (dx, dy) in sorted(deltas.items()):
        print(f"   {k:<3} delta comp=({dx:+6.0f},{dy:+6.0f})")
    print(f"emitidos: {dst}\n          {dsn_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
