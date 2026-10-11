#!/usr/bin/env python3
"""Ronda 12 — R11 con TESTIGO DE CARGA.

R11 no pudo concluir: la captura del usuario mostraba el dibujo ORIGINAL, o
sea que esa ventana de Proteus no cargó el archivo parcheado (sesión ya
abierta, archivo equivocado o similar). R12 repite la sonda de contención de
R11 y le añade un testigo imposible de no ver: el codo del cable que alimenta
el pin 12 de U1:D se hunde 12000 unidades hacia la zona vacía de la hoja.

Dos parches in-place (8 bytes cada uno, mismo largo):
  P1 testigo : codo (-9000,18000) del cable L de U2:A→pin12  → (-9000, 6000)
  P2 sonda   : extremo (3000,15000) del tie U3:A pin1-pin2   → (-9000,15000)
               (el tip del pin 12 de U1:D: si la conectividad es geométrica,
               dos salidas quedan pisadas => contención con S=V=1, R=D=0)

Procedimiento (LEEME_R12.txt): cerrar TODAS las ventanas de Proteus antes del
doble clic, y comprobar que la barra de título diga R12.
"""
import os
import struct
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.normpath(os.path.join(RAIZ, "..", "repo_dancenot", "Electronica",
                                    "Compuertas NAND.pdsprj"))
OUT = os.path.join(RAIZ, "salida", "proteus", "experimentos")


def p(x, y):
    return struct.pack("<ii", x * 254, y * 254)


def parchear_triple(dsn, trio, nuevo_idx, nuevo):
    """Busca tres puntos consecutivos de cable y reemplaza uno."""
    blob = trio[0] + trio[1] + trio[2]
    i = dsn.find(blob)
    assert i >= 0, "triple de puntos no encontrado"
    assert dsn.find(blob, i + 1) < 0, "triple de puntos no único"
    off = i + 8 * nuevo_idx
    dsn[off:off + 8] = nuevo
    return off


def main():
    zin = zipfile.ZipFile(SRC)
    dsn = bytearray(zin.read("ROOT.DSN"))

    off1 = parchear_triple(dsn, (p(-10000, 18000), p(-9000, 18000), p(-9000, 15000)),
                           1, p(-9000, 6000))
    off2 = parchear_triple(dsn, (p(1000, 14000), p(1000, 15000), p(3000, 15000)),
                           2, p(-9000, 15000))
    print(f"P1 testigo  en offset {off1}: codo hundido a y=6000")
    print(f"P2 sonda    en offset {off2}: tie de U3:A redirigido al pin 12 de U1:D")

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "R12_sonda.dsn"), "wb") as f:
        f.write(bytes(dsn))
    with zipfile.ZipFile(os.path.join(OUT, "R12_sonda.pdsprj"), "w",
                         zipfile.ZIP_DEFLATED) as z:
        for item in zin.infolist():
            data = bytes(dsn) if item.filename == "ROOT.DSN" else zin.read(item.filename)
            z.writestr(item, data)
    print("escrito:", os.path.join(OUT, "R12_sonda.pdsprj"))


if __name__ == "__main__":
    main()
