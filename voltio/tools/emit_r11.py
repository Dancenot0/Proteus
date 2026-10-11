#!/usr/bin/env python3
"""Ronda 11 — ¿conectividad geométrica o por handles?  (1 solo archivo)

Parte del proyecto bueno (Compuertas NAND.pdsprj == R10a, abre y simula) y
hace UN solo cambio: el punto terminal del tramo que entra al pin 2 de U3:A
se mueve al tip del pin 12 de U1:D, donde ya termina otro cable (W5).

Parche in-place: 8 bytes por 8 bytes, mismo largo, cero reencuadre, cero
tocar records/headers/handles.  Lo único que cambia es geometría de cable.

Qué mirar en Proteus 8.6 (doble clic al .pdsprj):
  1. Dibujo: el tramo que antes entraba a U3:A pin 2 ahora vuela hasta el
     pin 12 de U1:D (y el pin 2 de U3:A queda sin cable visible).
  2. Simulación con S=1, V=1, R=0, D=0:
       * si la conectividad es GEOMÉTRICA: el pin 12 de U1:D queda con dos
         salidas encima (U1:B y U2:A) => contención (rojo / X / parpadeo)
         en ese nodo y en el tramo movido.
       * si la conectividad es por HANDLES: la simulación se comporta
         exactamente como siempre (sin contención), aunque el dibujo
         muestre el cable volado.

Uso:  python3 tools/emit_r11.py
"""
import os
import struct
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.normpath(os.path.join(RAIZ, "..", "repo_dancenot", "Electronica",
                                    "Compuertas NAND.pdsprj"))
OUT = os.path.join(RAIZ, "salida", "proteus", "experimentos")

U = 254  # i32 crudo = valor_comp*10 * U?  NO: i32 crudo = (comp*10)*254/10...
# Ojo con las unidades: los valores que manejamos abajo ya están en la
# unidad "/254" de los dumps (comp*10).  i32 crudo = valor * 254.


def p(x, y):
    return struct.pack("<ii", x * 254, y * 254)


VIEJO = p(3000, 15000)    # tip pin 2 de U3:A (extremo del tramo W6 y de W7)
NUEVO = p(-9000, 15000)   # tip pin 12 de U1:D (allí ya muere W5)


def main():
    zin = zipfile.ZipFile(SRC)
    dsn = bytearray(zin.read("ROOT.DSN"))

    # El par (3000,15000) aparece 5 veces en el DSN: puntos de cable (W6 y W7),
    # el tip de pin guardado en el objeto U3:A y copias en sección 1.  Solo el
    # offset 77500 es el punto terminal del tramo W6: ése y ningún otro.
    OFF = 77500
    assert bytes(dsn[OFF:OFF + 8]) == VIEJO, "el offset ya no contiene el punto esperado"
    assert dsn.count(NUEVO) >= 1, "el punto destino no existe"
    dsn[OFF:OFF + 8] = NUEVO
    print(f"parche en offset {OFF} (0x{OFF:x}): {VIEJO.hex()} -> {NUEVO.hex()}")

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "R11_geometria.dsn"), "wb") as f:
        f.write(bytes(dsn))
    with zipfile.ZipFile(os.path.join(OUT, "R11_geometria.pdsprj"), "w",
                         zipfile.ZIP_DEFLATED) as z:
        for item in zin.infolist():
            data = bytes(dsn) if item.filename == "ROOT.DSN" else zin.read(item.filename)
            z.writestr(item, data)
    print("escrito:", os.path.join(OUT, "R11_geometria.pdsprj"))


if __name__ == "__main__":
    main()
