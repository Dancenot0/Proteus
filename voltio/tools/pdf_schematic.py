#!/usr/bin/env python3
"""Extractor de esquemáticos desde PDFs exportados por Proteus.

Un PDF de esquemático no es un dibujo tonto: lleva los cables como segmentos,
las uniones como puntos, los cuerpos de las compuertas como curvas de Bézier
cerradas (y sus burbujas de inversión como círculos) y los textos con
coordenadas y su propio CMap. Todo eso se puede leer, y con ello reconstruir
la conectividad REAL del circuito, que no siempre coincide con los rótulos que
el autor escribió encima (ver examples/compuertas_nand_pdf.yaml).

Este script vuelca los ingredientes de la reconstrucción:

    python3 tools/pdf_schematic.py "ruta/al.pdf"

Imprime textos decodificados con posición, cuerpos clasificados (NAND/AND,
NOR/OR, burbuja), segmentos de cable y puntos de unión. Con eso, el paso de
unir extremos a pines se hace por geometría (los extremos de los segmentos
coinciden con las puntas de las patillas).
"""
from __future__ import annotations

import re
import sys
import zlib


def leer_streams(data: bytes):
    out = []
    for m in re.finditer(rb'stream\r?\n(.*?)endstream', data, re.S):
        b = m.group(1)
        try:
            out.append(zlib.decompress(b))
        except Exception:
            out.append(b)
    return out


def leer_cmap(streams) -> dict:
    cid = {}
    for s in streams:
        if not s.startswith(b'/CIDInit'):
            continue
        cm = s.decode('latin-1')
        for bloque in re.findall(r'beginbfrange(.*?)endbfrange', cm, re.S):
            for lo, hi, arr in re.findall(
                    r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]', bloque, re.S):
                dsts = re.findall(r'<([0-9A-Fa-f]+)>', arr)
                for k, d in enumerate(dsts):
                    # El destino es un code point UTF-16BE completo: <0031> es
                    # U+0031, no dos bytes sueltos. Interpretarlo por pares de
                    # hex llenaba cada cadena de NULs iniciales.
                    cid[int(lo, 16) + k] = chr(int(d, 16))
            for lo, hi, dst in re.findall(
                    r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?=\n|$|<)',
                    bloque):
                for k in range(int(lo, 16), int(hi, 16) + 1):
                    cid.setdefault(k, chr(int(dst, 16) + k))
    return cid


def extraer(ruta: str) -> dict:
    data = open(ruta, 'rb').read()
    streams = leer_streams(data)
    cid = leer_cmap(streams)
    contenido = streams[0].decode('latin-1')

    segs, dots, curves, chars = [], [], [], []
    cur = None
    pend = None
    font = None
    abs_td = True   # el primer Td tras un BT/Tm es absoluto; el resto, relativo
    for line in contenido.splitlines():
        line = line.strip()
        if line == 'BT':
            pend = None
            abs_td = True
            continue
        if line == 'ET':
            pend = None
            continue
        m = re.fullmatch(r'(-?[\d.]+)\s+(-?[\d.]+)\s+(m|l)', line)
        if m:
            p = (float(m.group(1)), float(m.group(2)))
            cur = [p] if m.group(3) == 'm' else (cur or []) + [p]
            continue
        m = re.fullmatch(r'(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+'
                         r'(-?[\d.]+)\s+(-?[\d.]+)\s+c', line)
        if m:
            if cur is not None:
                cur.append(['C'] + [float(x) for x in m.groups()])
            continue
        if line == 'h':
            if cur and len(cur) == 1:
                dots.append(cur[0])
            continue
        if line == 'S':
            if cur:
                pts = [p for p in cur if isinstance(p, tuple)]
                if any(isinstance(p, list) for p in cur):
                    curves.append(cur)
                elif len(pts) >= 2:
                    segs.append(pts)
                elif len(pts) == 1:
                    dots.append(pts[0])
                cur = None
            continue
        m = re.fullmatch(r'/(F\d+)\s+([\d.]+)\s+Tf\s+.*Tm', line)
        if m:
            font = (m.group(1), int(float(m.group(2))))
            pend = None
            abs_td = True
            continue
        m = re.fullmatch(r'(-?[\d.]+)\s+(-?[\d.]+)\s+Td\s*(?:<([0-9A-Fa-f]+)>\s*Tj)?', line)
        if m:
            # En PDF, Td es RELATIVO a la posición de texto anterior dentro del
            # mismo bloque BT/ET; solo el primero tras el Tm es absoluto.
            # Tratarlos todos como absolutos rompe las cadenas de varios
            # caracteres en pedazos sueltos.
            dx, dy = float(m.group(1)), float(m.group(2))
            if pend is None or abs_td:
                pend = (dx, -dy)
                abs_td = False
            else:
                pend = (pend[0] + dx, pend[1] - dy)
            if m.group(3):
                h = m.group(3)
                for i in range(0, len(h), 4):
                    ch = cid.get(int(h[i:i + 4], 16), '')
                    if ch and ch != '\x00':
                        chars.append((pend[0], pend[1], font, ch))
                # NO se resetea pend: la posición de texto persiste entre Tj y
                # el siguiente Td es relativo a ella.
            continue
        m = re.fullmatch(r'<([0-9A-Fa-f]+)>\s+Tj', line)
        if m and pend:
            h = m.group(1)
            for i in range(0, len(h), 4):
                ch = cid.get(int(h[i:i + 4], 16), '')
                if ch and ch != '\x00':
                    chars.append((pend[0], pend[1], font, ch))

    # agrupa caracteres en cadenas por proximidad
    chars.sort(key=lambda c: (c[1], c[0]))
    cadenas = []
    for x, y, f, ch in chars:
        if cadenas and cadenas[-1]['f'] == f and abs(cadenas[-1]['y'] - y) < 25 \
                and 0 <= x - cadenas[-1]['xfin'] < 55:
            cadenas[-1]['s'] += ch
            cadenas[-1]['xfin'] = x
        else:
            cadenas.append({'x': x, 'y': y, 'xfin': x, 'f': f, 's': ch})

    cuerpos = []
    for c in curves:
        pts = [p for p in c if isinstance(p, tuple)]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ncur = sum(1 for p in c if isinstance(p, list))
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w < 40 and h < 40 and ncur == 4:
            tipo = 'burbuja'
        elif ncur == 4:
            tipo = 'OR/NOR'
        else:
            tipo = 'AND/NAND'
        cuerpos.append({'tipo': tipo, 'bbox': (min(xs), min(ys), max(xs), max(ys))})

    return {'textos': cadenas, 'segmentos': segs, 'puntos': dots, 'cuerpos': cuerpos}


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    r = extraer(argv[1])
    print(f"=== {len(r['textos'])} cadenas de texto ===")
    for t in sorted(r['textos'], key=lambda t: (round(t['y'] / 60), t['x'])):
        print(f"  ({t['x']:7.1f},{t['y']:7.1f}) {t['f'][0]} {t['f'][1]:3d}  {t['s']!r}")
    print(f"\n=== {len(r['cuerpos'])} cuerpos ===")
    for c in r['cuerpos']:
        x0, y0, x1, y1 = c['bbox']
        print(f"  {c['tipo']:<9} ({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f})")
    print(f"\n=== {len(r['segmentos'])} segmentos ===")
    for pts in r['segmentos']:
        print("   " + " -> ".join(f"({x:.0f},{y:.0f})" for x, y in pts))
    print(f"\n=== {len(r['puntos'])} puntos de unión ===")
    for p in r['puntos']:
        print(f"   ({p[0]:.0f},{p[1]:.0f})")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
