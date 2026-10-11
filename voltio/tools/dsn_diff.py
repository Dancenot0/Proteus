#!/usr/bin/env python3
"""Diff alineado de dos proyectos Proteus (.pdsprj / .pdsbak / .dsn).

Hecho para el protocolo de experimentos: si salvas `base`, luego dibujas UN
cable y salvas `con_cable`, la región insertada entre ambos ES el registro del
cable, escrito por Proteus. Este script la aísla y la vuelca en hex+ASCII, con
los campos candidatos (timestamps, coordenadas, referencias) ya marcados.

    python3 tools/dsn_diff.py base.pdsprj con_cable.pdsprj

Salida: prefijo/sufijo comunes, regiones de cambio (igual longitud = campos
editados; distinta longitud = objetos insertados o borrados) y volcado de cada
región con hipótesis de campos.
"""
from __future__ import annotations

import struct
import sys
import zipfile
from typing import List, Tuple


def leer_dsn(ruta: str) -> bytes:
    with open(ruta, "rb") as f:
        cab = f.read(4)
    if cab == b"PK\x03\x04":
        z = zipfile.ZipFile(ruta)
        n = [x for x in z.namelist() if x.upper().endswith(".DSN")]
        if not n:
            raise SystemExit(f"{ruta}: el ZIP no lleva ROOT.DSN")
        return z.read(n[0])
    if cab == b"ISIS":
        return open(ruta, "rb").read()
    raise SystemExit(f"{ruta}: no es ni ZIP de Proteus ni DSN legado")


def resync(a: bytes, b: bytes, i: int, ancla: int = 48) -> int:
    """Busca en b el punto donde el hilo de a vuelve a aparecer."""
    return b.find(a[i:i + ancla], max(0, i - 4096))


def campos(txt: bytes, base: int) -> List[str]:
    """Hipótesis de campos sobre un volcado: timestamps, coords, u16/u32."""
    out = []
    for off in range(0, len(txt) - 3, 2):
        (u32,) = struct.unpack_from("<I", txt, off) if off + 4 <= len(txt) else (0,)
        if 0x50000000 <= u32 <= 0x6C000000:
            out.append(f"    +{off:4d} u32 0x{u32:08x}  ← TIMESTAMP candidato")
        elif 0 < u32 < 20000:
            out.append(f"    +{off:4d} u32 {u32:6d}      ← coordenada/cantidad candidata")
    return out


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 1
    a, b = leer_dsn(argv[1]), leer_dsn(argv[2])
    print(f"A = {argv[1]}  ({len(a):,} bytes)")
    print(f"B = {argv[2]}  ({len(b):,} bytes)")
    print(f"delta = {len(b) - len(a):+,} bytes\n")

    p = 0
    while p < min(len(a), len(a)) and a[p] == b[p]:
        p += 1
    s = 0
    while s < min(len(a), len(b)) - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    ma, mb = a[p:len(a) - s], b[p:len(b) - s]
    print(f"prefijo común: {p:,}   sufijo común: {s:,}")
    print(f"región A: {len(ma):,} bytes   región B: {len(mb):,} bytes\n")

    if len(ma) == len(mb):
        print("=== CAMBIOS DE IGUAL LONGITUD (campos editados) ===")
        i = 0
        n = len(ma)
        while i < n:
            if ma[i] != mb[i]:
                j = i
                while j < n and ma[j] != mb[j]:
                    j += 1
                print(f"  offset {p + i:,}  ({j - i} bytes)")
                print(f"    A: {ma[i:j].hex(' ')}")
                print(f"    B: {mb[i:j].hex(' ')}")
                for c in campos(mb[max(0, i - 4):j + 4], p + i):
                    print(c)
                i = j
            else:
                i += 1
    else:
        print("=== REGIONES INSERTADAS/BORRADAS (objetos completos) ===")
        # Diff goloso con resincronización por anclas: al divergir, busca dónde
        # vuelve a aparecer el hilo de A dentro de B (inserción en B) o el de B
        # dentro de A (borrado). Añadir UN componente en Proteus inserta bytes
        # en varios sitios distintos del archivo (registro del esquemático,
        # sección de circuito, pinouts...), así que hay que esperar varias
        # inserciones pequeñas, no una grande.
        i = j = 0
        eventos = []
        while i < len(ma) and j < len(mb):
            if ma[i] == mb[j]:
                i += 1
                j += 1
                continue
            # divergencia: intenta insert-en-B primero
            jb = mb.find(ma[i:i + 24], j + 1, j + 60000)
            ia = ma.find(mb[j:j + 24], i + 1, i + 60000)
            if jb != -1 and (ia == -1 or (jb - j) <= (ia - i)):
                eventos.append(("INSERT_B", p + i, mb[j:jb]))
                j = jb
            elif ia != -1:
                eventos.append(("DELETE_A", p + i, ma[i:ia]))
                i = ia
            else:
                # sin ancla cercana: cambio de campo; avanza hasta resincronizar
                k = 1
                while k < 64 and (i + k >= len(ma) or mb.find(ma[i + k:i + k + 16], j, j + 64) == -1):
                    k += 1
                eventos.append(("EDIT", p + i, None))
                i += k
                j += k
        for kind, off, blk in eventos:
            if kind == "EDIT":
                print(f"\n  edit pequeño ~{off:,}")
                continue
            print(f"\n--- {kind} en offset ~{off:,}: {len(blk):,} bytes ---")
            lim = len(blk) if "--full" in argv else min(len(blk), 640)
            for off2 in range(0, lim, 16):
                ch = blk[off2:off2 + 16]
                asc = "".join(chr(x) if 32 <= x < 127 else "." for x in ch)
                print(f"  {off + off2:7d}  {' '.join(f'{x:02x}' for x in ch):<47}  {asc}")
            if lim < len(blk):
                print(f"  ... ({len(blk) - lim:,} bytes más; --full para todo)")
            print("  campos candidatos:")
            for c in campos(blk, off)[:20]:
                print(c)
        nins = sum(len(blk) for k, _, blk in eventos if k == "INSERT_B")
        ndel = sum(len(blk) for k, _, blk in eventos if k == "DELETE_A")
        print(f"\nresumen: {nins:,} bytes insertados en B, {ndel:,} borrados de A "
              f"(neto {nins - ndel:+,}, delta del archivo {len(b) - len(a):+,})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
