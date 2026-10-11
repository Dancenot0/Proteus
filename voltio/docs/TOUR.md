# Voltio — mapa del espacio de trabajo

Una visita guiada para orientarse en cinco minutos. Lo técnico fino vive en
`PROTEUS_DSN.md` (formato binario) y `PLAN.md` (qué sigue); esto es el plano.

## La idea en un párrafo

Voltio es un **motor de circuitos digitales** cuyo punto de entrada es el
lenguaje natural: una descripción ("un contador de 4 bits con display de 7
segmentos") se convierte en un DSL YAML, el YAML se valida contra reglas
eléctricas reales, se simula con un motor de eventos de cuatro valores
(0/1/X/Z), se dibuja como esquemático SVG con los cables coloreados por nivel,
y se exporta a SPICE / EDIF / BOM / y —en progreso— al binario `.pdsprj` de
Proteus. Funciona también al revés: circuito → YAML → descripción.

```
 descripción natural
        │  (voltio/ai: 5 proveedores + bucle de autocorrección)
        ▼
   DSL YAML  ◄────── edición humana / voltio/builder.py (API Python)
        │  (voltio/dsl: carga + validador con mensajes que una IA puede leer)
        ▼
   IR (grafo: partes, nets, buses)
        │
   ┌────┼─────────────┬──────────────┐
   ▼    ▼             ▼              ▼
 sim   render       export        reverse
 (X/Z, (SVG con     (SPICE, EDIF,  (circuito →
 buses,  cables      BOM, netlist,  YAML → texto)
 colisiones) coloreados)  .pdsprj)
```

## Zonas del repo y su estado

| zona | qué hace | estado |
|---|---|---|
| `voltio/core/` | lógica de 4 valores, IR del circuito, **parser YAML propio** (el núcleo corre sin dependencias) | sólido, testeado |
| `voltio/dsl/` | carga del YAML, validador (pins inexistentes con sugerencia, cortocircuitos, buses, entradas flotantes), serialización inversa | sólido, testeado |
| `voltio/parts/` | 59 componentes (33 de la familia 74xx, memorias, displays, micros, una CPU8 educativa) con pines, direcciones y modelos | sólido |
| `voltio/sim/` | motor de eventos: flancos, relojes en cascada, colisiones de bus, lazos combinatorios, formas de onda | sólido, es el corazón |
| `voltio/render/` | símbolos vectoriales → SVG autónomo; rutas ortogonales, buses como carriles, juntas solo donde hay unión real | sólido *después* de mirarlo con `tools/svg2png.py` |
| `voltio/export/` | SPICE, EDIF 2 0 0, BOM CSV, netlist plano; contenedor `.pdsprj` | sólido salvo Proteus |
| `voltio/ai/` | prompt generado desde la librería, 5 proveedores (incluido `clipboard`, sin claves), extracción de YAML y bucle que devuelve los errores del validador a la IA | sólido |
| `voltio/reverse/` | circuito → análisis estructural → descripción local sin IA | sólido |
| `voltio/gui/` | canvas PySide6 interactivo | **compila, nunca se ha ejecutado aquí** (este entorno no tiene PySide6) |
| `voltio/proteus/` | lector/inspector del binario DSN + escritor experimental | investigación activa |
| `tools/` | runner de tests sin pytest, rasterizador SVG→PNG, diff de DSN, emisores de experimentos, extractor de PDF | utilería de guerra |
| `examples/` | 6 YAML: NAND, contador 7seg, CPU8 con bus, réplica del PDF del usuario, cerrojo SR, layout de morphing | todos simulan |
| `salida/` | SVG + PNG vistos por un ojo humano + exportaciones + 50 archivos de experimentos Proteus | el museo |
| `tests/` | 133 tests, incluidos los de regresión de los 4 bugs caros del motor y de la réplica del PDF | verdes |

## Recetario rápido

```bash
python3 -m voltio examples/contador_7seg.yaml                 # valida + simula + SVG
python3 -m voltio examples/sr_latch.yaml --trace              # formas de onda ASCII
python3 -m voltio examples/cpu8_bus.yaml --sim 30ms --stats   # la CPU ejecutando
python3 -m voltio --generate "un semáforo de 3 LEDs"          # IA (o --provider clipboard)
python3 -m voltio examples/X.yaml --proteus out.pdsprj --plantilla TU.pdsprj
python3 tools/run_tests.py                                    # suite sin dependencias
python3 tools/svg2png.py salida/X.svg /tmp/v.png --max 1600   # MIRAR el dibujo
```

## El frente Proteus, en una tabla

| ronda | qué se mandó | veredicto del oráculo (tu Proteus 8.6) |
|---|---|---|
| E0/E1/E2 | re-empaque idéntico + parches de coordenada | abren; E1 mueve la pieza ⇒ coordenadas X/Y descifradas |
| R3 | morph de coordenadas sobre tu NAND | abre y simula (dibujo "exótico": los cables no siguen) |
| R5–R8 | primeros sintéticos desde cero | crashes variados ⇒ faltaba la sección 2 y sobraban bytes |
| R10a | copia exacta | impecable (línea base) |
| R10b | puntos de cable a cero | abre y simula con abanico al origen ⇒ los puntos SON el dibujo |
| R10c | sin los 28 marcadores | crash ⇒ los marcadores (refs) son obligatorios |
| R11 | 4 bytes: tie de U3:A → pin 12 de U1:D | captura mostró el dibujo ORIGINAL ⇒ la ventana no cargó el parche |
| R12 | R11 + testigo de carga (codo hundido) | **pendiente de tu doble clic** |

Lo descodificado hasta hoy (detalle en `PROTEUS_DSN.md`): contenedor ZIP,
cabeceras, tabla de estilos, blobs de propiedades en texto plano, registros de
componente con dos espacios de coordenadas (u32 en 0.01" para el dibujo de
sección 1; i32 en 0.1 mm para sección 2), sub-registros de patilla con offsets,
marcadores de texto obligatorios, registros de cable
`[u16 0][u16 n][n×(i32,i32)][00 1d 00 00 00][style 20 B]["WIRE\0"]`, y la
estructura de la sección 2: **bloque A** (objetos + cables), **bloque B**
(segundo `ISIS CIRCUIT FILE` + `OBJECT DATA`, *siempre vacío*: probablemente el
lugar de las sub-hojas) y la tabla de hoja + opciones SPICE con **offsets
absolutos** que hay que recalcular al escribir.

La pregunta abierta que decide R12: ¿la conectividad eléctrica vive en la
geometría de los puntos (contención visible al pisar dos salidas) o en los u32
de las colas de objeto (simulación intacta pese al cable volado)? De eso
depende si el escritor sintetiza cables con solo puntos o con handles.

## Qué es de fiar y qué no

* **De fiar:** motor, validador, simulador, SVG, exportaciones no-Proteus,
  capa de IA, réplicas y tests. Todo eso tiene ojos encima (humanos o de
  `svg2png`).
* **Experimental:** todo lo que toque bytes de DSN (`voltio/proteus/writer.py`,
  `tools/morph_*`, `tools/emit_r*`). Ningún sintético ha abierto todavía en
  Proteus; los morphs sí.
* **Sin probar en este entorno:** la GUI PySide6.
