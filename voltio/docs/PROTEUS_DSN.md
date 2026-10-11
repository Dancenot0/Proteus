# El formato `.pdsprj` de Proteus, por dentro

Todo lo que hay aquí se comprobó sobre archivos reales de Proteus 8.x
descargados de repositorios públicos. No hay nada adivinado: cada afirmación
viene de abrir el archivo y mirar los bytes.

**Resumen ejecutivo:** Proteus no está cifrado. Está *no documentado*, que es
una cosa muy distinta y tiene solución.

---

## 1. El contenedor

Un `.pdsprj` es un **ZIP estándar**. Se abre con cualquier descompresor:

```
$ unzip -l motor_control.pdsprj
FIRMWARE.XML                       189 bytes
FIRMWARE/ATMEGA16.XML            1,006 bytes
FIRMWARE/ATMEGA16/main.asm       1,110 bytes
PROJECT.XML                        237 bytes
ROOT.CDB                         1,152 bytes
ROOT.DSN                        95,334 bytes
SCRIPTS/PWRRAILS.DAT                17 bytes
```

Cuatro de esas siete entradas son **texto plano**:

### `PROJECT.XML`

```xml
<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<PROJECT>
 <TIMESTAMP RELEASE="800" MODIFIED="1412056335" FILEVER="800"
            CUSTID="1756753440" CREATED="1412056158"/>
 <SCRIPTS>
  <STRING>PWRRAILS.DAT</STRING>
 </SCRIPTS>
</PROJECT>
```

`RELEASE`/`FILEVER` es la versión de formato (800 = Proteus 8.0). Los
timestamps son epoch de Unix.

### `FIRMWARE.XML` y `FIRMWARE/<DEV>.XML`

Declaran qué microcontrolador usa el proyecto y dónde está su código.

### `FIRMWARE/ATMEGA16/main.asm`

**El código fuente del firmware, en texto plano, dentro del proyecto.** Esto es
notable: significa que se puede leer —y escribir— el programa de un proyecto de
Proteus sin tocar el binario.

```asm
; (33 líneas de ensamblador AVR de un proyecto real)
```

### Las dos entradas que no son texto

* `ROOT.CDB` — base de componentes del proyecto (1 KB en un proyecto pequeño).
* `ROOT.DSN` — **el esquemático**. Aquí está todo el trabajo.

---

## 2. `ROOT.DSN`: el esquemático

Empieza con una firma ASCII:

```
00000000  49 53 49 53 20 53 43 48  45 4d 41 54 49 43 20 46  |ISIS SCHEMATIC F|
00000010  49 4c 45 1a 00 00 79 20  44 65 73 69 67 6e 00 70  |ILE...y Design.p|
00000020  6c 61 74 65 00 44 53 20  20 6b 6a 68 5c 20 6b 6a  |late.DS  kjh\ kj|
00000030  64 67 5c 00 00 00 00 00  00 00 00 00 00 00 00 00  |dg\.............|
```

Es un formato binario **de registros con etiquetas**, no cifrado. Lo que se ha
podido establecer hasta ahora:

### 2.1 Tabla de estilos de registro

Alrededor del offset `0x3DC` hay una tabla con los tipos de objeto que el
esquemático puede contener. Las cadenas llevan **prefijo de longitud de 1 byte**:

```
... 01 00 00 00 12 00 | 09 "COMPONENT" | 01 19 00 00 00 ...
                      └─ longitud 9 ─┘
      ... | 03 "PIN" | 01 19 ...
      ... | 04 "PORT" | 01 19 ...
      ... | 06 "MARKER" | 01 19 ...
      ... | 08 "ACTUATOR" | 01 19 ...
      ... | 09 "INDICATOR" | 01 19 ...
```

Los 33 tipos observados:

```
COMPONENT   PORT        MARKER      ACTUATOR    INDICATOR
VPROBE      IPROBE      TAPE        GENERATOR   TERMINAL
SUBCIRCUIT  2D GRAPHIC  WIRE DOT    WIRE        BUS WIRE
BORDER      TEMPLATE    COMPONENT ID            COMPONENT VALUE
PROPERTIES  TERMINAL LABEL          WIRE LABEL  SUBCKT ID
SUBCKT NAME SCRIPT      PIN NAME    PIN NUMBER  VPROBE LABEL
IPROBE LABEL GENERATOR LABEL        TAPE LABEL  ACTIVE READOUT
ACTIVE REGION
```

Tras cada nombre viene lo que parece ser un registro de estilo fijo de unos
25 bytes (`01 19 00 00 00 00 c0 9e 00 00 80 00 00 00 00 c8 c8 aa 00 c8 c8 aa
00 02 7f 00`), con colores y grosores.

### 2.2 Las propiedades de los componentes son TEXTO PLANO

Este es el hallazgo más útil. Cada dispositivo lleva un bloque de texto sin
cifrar, delimitado por NUL, con líneas `{CLAVE=...}`:

```
{*DEVICE}
{ACTIVE=CTIMER,12,DLL}
{HELP=INSTRUMENTS>POPUP,505}
{*PROPDEFS}
{PRIMITIVE="Primitive Type",HIDDEN STRING}
{MODDLL="VSM Model DLL",HIDDEN STRING}
{MODE="Operating Mode",KWDLIST,4,0="Time (secs)",1="Time (hms)",2="Frequency",3="Count"}
{GATEPOL="Count Enable Polarity",KWDLIST,2,0="High",1="Low"}
{RSTEDGE="Reset Edge Polarity",KWDLIST,2,0="Low-High",1="High-Low"}
{PACKAGE=PCB Package,HIDDEN PACKAGE}
{*INDEX}
{*COMPONENT}
{PRIMITIVE=DIGITAL}
{MODDLL=CTIMER.DLL}
{RSTEDGE=0}
{GATEPOL=0}
{MODE=0}
{PACKAGE=NULL}
```

Cuatro secciones:

| Sección        | Contenido |
|----------------|-----------|
| `{*DEVICE}`    | identidad del dispositivo y qué modelo de simulación usa |
| `{*PROPDEFS}`  | el **esquema** de propiedades: tipo, etiqueta legible, valores permitidos |
| `{*INDEX}`     | índice (vacío en las muestras) |
| `{*COMPONENT}` | los **valores reales** de esta instancia |

`{*PROPDEFS}` es especialmente valioso: el archivo se auto-documenta. No hay
que adivinar qué significa `MODE=0`, porque el propio bloque dice
`MODE="Operating Mode",KWDLIST,4,0="Time (secs)",1="Time (hms)",...`.

Tipos de propiedad observados: `STRING`, `INT`, `FLOAT`, `YESNO`, `KWDLIST`
(lista de palabras clave), `KWDEDIT`, `PACKAGE`, `HIDDEN *`, y modificadores
como `PNZ` (positivo no nulo) y `ADVANCED`.

En el proyecto de muestra hay **15 bloques de dispositivo**, de los que se
extraen sin esfuerzo:

```
[0] READOUT      ACTIVE=IMETER,5,DLL      STATE=2  TC=100ms
[2] CTIMER.DLL   PRIMITIVE=DIGITAL        MODE=0  RSTEDGE=0
[5] i2cdebug.dll PRIMITIVE=DIGITAL        CLOCKFREQ=100000  BAUDRATE=9600
[7] DSO.DLL      PRIMITIVE=ANALOG         CHAVAL=5.  CHBPOS=40  HORIZVAL=1.e-003
```

Y `{CAT=...}` da las categorías de la librería: `Analog ICs`,
`Electromechanical`, `Microprocessor ICs`, `Miscellaneous`, `Virtual
Instruments`.

### 2.3 Lo que todavía no se ha descifrado

El **framing geométrico**: cómo se codifican las coordenadas de cada símbolo,
los identificadores que enlazan un cable con sus dos extremos, y la tabla de
instancias. Se ven candidatos plausibles —por ejemplo, tras la cadena
`ATMEGA16` a `0xd5f0` aparecen `2e 1c 00 00` (1598) y `3e 06 00 00` (1598), y en
otro sitio el par `30 e0 03 00 c0 19 03 00` (255024, 203712)— pero confirmar
cuál es cuál exige el método de la sección 3.

También falta `ROOT.CDB`, que probablemente sea la cache de componentes del
proyecto.

---

## 2bis. Progreso con múltiples muestras (tres proyectos reales)

Comparando `motor_control`, `Fire-Alarm/project` y `SAP-8/Accumulator`
(95 KB, 232 KB y 104 KB de DSN) aparece una estructura de registro
coherente:

```
[u16 tipo][cstring etiqueta][campos según tipo][u32 N][blob de N bytes terminado en NUL]
```

* El `u32` inmediatamente anterior al blob es su longitud: coincide con el
  tamaño del bloque `{*DEVICE}...{*COMPONENT}` en los tres archivos.
* Los campos intermedios dependen del tipo: `COMPONENT` lleva 16 bytes (dos
  pares de `u32` que parecen coordenadas firmadas, idénticos en los tres
  archivos para los objetos por defecto), `MARKER` lleva 10, `READOUT` ninguno.
* **Todo proyecto empieza con los mismos objetos por defecto** (instrumentos y
  marcadores con las mismas coordenadas): la secuencia de preámbulos de los
  primeros ~4 registros es byte-idéntica entre proyectos distintos. Eso da un
  punto de anclaje fijo para cualquier escritor.

Dónde se atasca el walker automático: tras el primer registro, el siguiente
`u16` no es un tipo conocido (52). O bien hay campos *después* del blob que el
walker no salta, o bien la etiqueta no va en todos los tipos. Esa es la próxima
piedra: hay que alinear a mano dos registros consecutivos de un mismo archivo
y medir qué sobra entre el fin del blob y el inicio del siguiente preámbulo.

## 2bis-bis. Ronda 1 de experimentos: RESULTADOS (oráculo = Proteus 8.6 SP2 real)

| Experimento | Resultado | Conclusión |
|---|---|---|
| `E0_control.dsn` (re-empaquetado idéntico) | **Abre** con doble clic directamente | El contenedor y el DSN re-emitidos son válidos; la vía de entrega es el `.dsn` legado por asociación de archivo, más simple que el import |
| `E0_control.pdsprj` | Abre con Open Project | Mi re-empaquetado ZIP no rompe nada |
| `E1_movX.dsn` (+300 en el u32 tras el timestamp) | Abre, y el componente afectado aparece **desplazado a la derecha** con el cable estirado | Ese u32 es la **coordenada X**. Confirmado por el oráculo |
| `E2_movY.dsn` | (pendiente de contraste fino, pero el par quedó fijado por descarte) | El u32 siguiente es la Y |
| Simulación de E0/E1 | **Falla en DSIM**: ``Cannot find pin [U?:D]`` | No es el re-empaquetado: el DSN de E0 es byte-idéntico al original, y el original tiene puertas sin anotar (``U?``) dentro del subcircuito. Netlist, linking, partición y PROSPICE pasan OK antes del fallo: el contenedor está íntegro. Regla para el escritor: toda componente lleva referencia asignada, y la plantilla de experimentos debe ser un proyecto que sí simule |

Consecuencia: las coordenadas de cualquier objeto se pueden parchear en sitio.
Lo que sigue faltando es el **registro de cable**, y la forma más barata de
cerrarlo es que Proteus lo escriba por nosotros: protocolo de la ronda 2 en la
cabecera de `tools/dsn_diff.py` (salvar base, dibujar un cable, salvar, y
diferenciar).

## 2cinco. Ronda 3: MORPH FUNCIONAL (primer archivo editado por Voltio que abre Y simula)

`R3_nand_relayout` re-colocó las seis piezas del proyecto real del usuario
parcheando solo coordenadas de igual longitud. Resultado en Proteus 8.6:
**abre y simula**, con dibujo "exótico" (piezas en su sitio nuevo, parte de los
cables quedados atrás). Lectura:

* El parcheo de coordenadas es estructuralmente válido: Proteus no rechaza el
  archivo ni repara nada visible.
* La conectividad ES geométrica: los cables que no se movieron dejaron patillas
  flotando y DSIM simuló con X sin fatalidad. No hay capa lógica aparte.
* Conviven TRES espacios de coordenadas: registro de componente (u32, 0.01"),
  puntas de cable y conexión (i32, 0.1 mm), y marcadores de texto que viven
  lejos de su pieza. Los deltas entre espacios están calibrados (×2540, Y
  negado); falta el origen absoluto de cada espacio, que se cierra con el
  experimento de movimiento controlado de 10 cuadros (r2_base / r2_mov10).

## 2cuater. EL REGISTRO DE CABLE (encontrado en el repo del usuario)

Diferenciando `Backup Of multiplicador.pdsbak` contra `multiplicador.pdsprj`
(el usuario añadió un LED y tres tramos de cable entre saves), la inserción de
183 bytes resultó ser tres registros de cable consecutivos:

```
[u16 0][u16 npuntos][npoints × 8 bytes de coordenadas][00 1d 00 00 00]
[chunk de estilo de 25 bytes]["WIRE" 00]
```

Tres consecuencias enormes:

1. **El cable no referencia a nadie.** No hay ids de componente ni de patilla:
   solo geometría. En ISIS un cable conecta *tocando* la punta de la patilla.
   Para el escritor esto es excelente: no hay que fabricar handles, basta que
   el extremo del cable coincida con la punta del pin, que se calcula como
   ``componente.(x, y) + offset del sub-registro $PINDEFAULT``.
2. Los tramos de varios puntos (codos) van en el mismo registro: ``npuntos=3``
   en el tercer cable del diff.
3. Añadir un componente inserta bytes en **varios sitios a la vez** (registro
   del esquemático, texto de propiedades, sección de circuito): el diff de un
   save contra otro nunca es una sola inserción limpia. ``tools/dsn_diff.py``
   ya maneja eso con resincronización por anclas.

Lo único que falta: la **escala y el origen** de las coordenadas de cable (los
valores son i32 grandes, ±millones, mientras que las de componente son u32
pequeños). Se cierra con el experimento de la ronda 2: un cable de exactamente
tres cuadros entre dos patillas conocidas.

## 2ter. Anatomía completa de un DSN (lo que se sabe hoy)

Un ``ROOT.DSN`` no es un archivo: son **dos archivos pegados**.

```
┌─ ISIS SCHEMATIC FILE ─────────────────────────────┐
│ cabecera (título, autor, fechas, tamaño de hoja)  │
│ tabla de 33 estilos + tabla de fuentes            │
│ objetos del esquemático, en serie:                │
│    instrumentos por defecto (idénticos en todo    │
│    proyecto: misma secuencia de bytes siempre)    │
│    componentes, marcadores, readouts...           │
│ registros PINOUT por pieza:  [len]["[74126]"]     │
│    [u32 timestamp][u32 len][texto "*PINOUT ..."]  │
├─ ISIS CIRCUIT FILE ───────────────────────────────┤
│ "OBJECT DATA" + objetos de simulación + opciones  │
│ SPICE (TRTOL, VNTOL, TRANGMIN...)                 │
└───────────────────────────────────────────────────┘
```

Gramática de un objeto del esquemático, confirmada en tres proyectos:

```
[u8 len][nombre de pieza]      p.ej. 05 "74126"
[u32 timestamp]                epoch de creación del objeto; NO es un id,
                               no sirve para referencias (es igual en todos
                               los objetos por defecto de un mismo archivo)
[u32 x][u32 y]                 coordenadas en unidades de hoja (hipótesis
                               fuerte: los experimentos E1/E2 lo confirman)
[u16 tipo][u16 ?][u16 1][u16 1]
[u32 ?]
[chunk de estilo "COMPONENT"]  (u32 len + 24 bytes de estilo + nombre + NUL)
[subregistros de patillas]     "$MKRORIGIN", "$PINDEFAULT" + nombre + número
                               + coordenadas relativas
[cstring etiqueta]             la referencia visible ("O", "U1", "TRIG"...)
[u32 L][blob de propiedades]   el texto plano {*DEVICE}...{*COMPONENT}
```

Lo que falta: **los cables**. No existen como registros de tipo 14 con cuatro
coordenadas ``u32`` (búsqueda exhaustiva: cero candidatos). Las hipótesis
abiertas, en orden de probabilidad: coordenadas ``u16``, un enum de tipos
distinto al de la tabla de estilos, o cables que viven solo en la sección
``ISIS CIRCUIT FILE`` con el esquemático guardando solo la gráfica por otro
lado. Resolverlo es el objetivo de la próxima ronda de experimentos.

## 3. El método para terminar de descifrarlo

Con formatos binarios no documentados, leer un archivo no basta: hace falta
**comparar muchos**. El procedimiento es mecánico y cada paso es media hora:

1. Crear en Proteus un proyecto con **un solo componente**. Guardar como
   `base.pdsprj`.
2. Mover ese componente 10 unidades a la derecha. Guardar como `x+10.pdsprj`.
3. Ejecutar `voltio.proteus.compare(base, x10)`. Los bytes que difieren son,
   con alta probabilidad, la coordenada X. Se repite con Y.
4. Repetir con: rotación, espejo, nombre del componente, cada propiedad.
5. Pasar a **dos** componentes, para ver cómo se asignan los identificadores y
   en qué orden se escriben los registros.
6. Añadir **un cable** entre dos pines y repetir. Aquí aparece la estructura que
   enlaza cable ↔ extremos, que es lo último que falta.
7. Con eso ya se puede escribir un generador.

`voltio.proteus.dsn.compare()` ya implementa el paso de comparación y lista los
offsets y los bytes concretos que cambian.

**Regla de oro:** cada experimento debe cambiar *una sola cosa*. Si se mueven
dos componentes a la vez, el diff no se puede interpretar.

---

## 4. Las tres rutas de salida, comparadas

| Ruta | Esfuerzo | Riesgo | Resultado |
|---|---|---|---|
| **SVG / PNG** | hecho | ninguno | Se ve el circuito; se copia a mano |
| **EDIF 2 0 0** | hecho | bajo | Proteus lo importa oficialmente. Lleva la conectividad, **no** las posiciones |
| **Netlist + pasos** | hecho | bajo | Lista de materiales y de conexiones, para reconstruir guiado |
| **`.pdsprj` binario** | semanas | medio | Se abre en Proteus con todo colocado |

La ruta EDIF es la que mejor relación esfuerzo/resultado tiene mientras no se
termine la binaria: es un estándar abierto, Proteus lo soporta de forma nativa,
y no depende de que el formato interno no cambie en la próxima versión.

La ruta binaria es la única que conserva la **colocación** de los símbolos, que
es lo que hace que un esquemático sea legible y no un plato de espaguetis. Por
eso merece la pena terminarla, pero no es lo primero.

---

## 5. Consideraciones legales

Esto es ingeniería inversa de un formato de archivo con el fin de
**interoperar**, que es el mismo principio por el que existen KiCad, EDIF y
todos los importadores de formatos ajenos. No se ha eludido ninguna medida
technológica de protección: el archivo no está cifrado, está sin documentar.

Aun así, conviene tener presente:

* El formato es propiedad de Labcenter Electronics y puede cambiar sin aviso en
  cualquier versión. Un escritor binario puede romperse con cada actualización.
* Los nombres `Proteus`, `ISIS` y `ARES` son marcas de Labcenter. Este código
  los usa solo para referirse al producto.
* La ruta EDIF evita todo este problema, porque es un estándar público.

---

## 6. Reproducir el análisis

```bash
# Descargar un proyecto de ejemplo
python3 - <<'EOF'
import urllib.request
urllib.request.urlretrieve(
  "https://github.com/divyanshusrivastava/proteus-projects/raw/master/motor_control.pdsprj",
  "motor_control.pdsprj")
EOF

# Inspeccionarlo
python3 -c "
from voltio.proteus import open_project
print(open_project('motor_control.pdsprj').report())
"

# Comparar dos variantes (el paso clave de la ingeniería inversa)
python3 -c "
from voltio.proteus import open_project, compare
print(compare(open_project('base.pdsprj'), open_project('x10.pdsprj')))
"
```

## Addendum R8/R10 — modelo invertido (2026-10-11)

Resultados R8: R8a (esqueleto+sección2 vacía+CDB vacío) cierre silencioso;
R8b (mis registros de componente + sección2 vacía + CDB plantilla) =>
`MSVCR90.DLL [0003A27A]` (firma de strlen/memcpy sobre string corrupto: mis
registros sintéticos de sección 1 tienen cadenas o longitudes mal formadas);
R8c y demás => LXLCORE [0003313D] (sección 2 inconsistente con sección 1).

Descubrimiento estructural (Compuertas NAND.pdsprj, 78,781 B):
- Sección 1 (SCHEMATIC, 0..72504) = DIBUJO: exactamente 5 registros de
  componente (uno por familia: 74LS00, 74LS02, LED-RED, LOGICSTATE, RES; ts
  compartidos por sesión de colocación), ~8 pares de formas gráficas
  (style 02 67/02 7d "COMPONENT") que son cuerpos/cables restantes, y 28
  marcadores de texto (refs U1:A..U3:A, familias, etiquetas booleanas).
  Los marcadores son arrastrables: NO están rígidamente ligados al origen.
- Sección 2 (CIRCUIT, 72504..fin, ~6.2 KB) = ELECTRICA: 12 objetos (4 estados,
  5 puertas 74LS00, 74LS02, LED, RES) con familia+ts+coords en espacio de
  cable, y 8 registros de cable (19 puntos) con head [u16 0][u16 np],
  puntos i32, sentinela 00 1d 00 00 00, style 21 B, 02 7f, "WIRE\0".
  NO hay registros WIRE en la sección 1.
- Consecuencias: (a) la simulación vive entera en sección 2 (por eso R3 abrió
  y simuló moviendo sólo dibujo); (b) todo archivo válido exige consistencia
  1:1 dibujo↔eléctrica (ley de consistencia); (c) el objetivo real de síntesis
  es la gramática de sección 2; (d) el repo de GitHub está en un estado
  anterior al PDF del usuario (LED/RES sin cablear, cuerpos como gráficos).
- R10 (bisección inversa sobre archivo bueno): R10a control; R10b puntos de
  cable eléctrico a (0,0); R10c sin los 28 marcadores de sección 1.

## Addendum R10/R11 — veredictos y gramática de sección 2 (2026-10-11)

Resultados R10 (Proteus 8.6 del usuario):
- **R10a** (copia exacta) impecable ⇒ el re-empaquetado ZIP del escritor es
  inocente; línea base sana.
- **R10b** (19 puntos de cable a cero) abre y simula, con los cables dibujados
  en abanico pin→origen ⇒ los puntos de cable de sección 2 SON el dibujo
  (i32 absolutos, espacio = comp×254, signos y media rejilla permitidos).
  Que simule pese al colapso geomético sugiere conectividad por handles
  (R11 lo pone a prueba).
- **R10c** (sin los 28 marcadores de sección 1) ⇒ `SEH KERNELBASE [00163A44]`:
  los marcadores son obligatorios (llevan identidad/refdes).

Estructura fina de sección 2 (alineando backup vacío / Con_cable / NAND):
- Bloque A = head 35 B (`ISIS CIRCUIT FILE\x1a` + 7 B + u32 ts + 6 B) +
  `OBJECT DATA\0` + registros + `\xff`. Contiene, en orden de creación,
  objetos de componente, registros de cable Y marcadores de texto.
- Bloque B = misma cabecera, SIEMPRE vacío (`\xff`) en todo archivo visto.
- Bloque C = `[u16 2][u32 3]` + entradas `[u8 len][nombre\0][u32 offset abs]`
  (`CCT000`→bloque A, `__DEFAULT__`→bloque B, tercera vacía) + blob de
  opciones SPICE `[u32 len][nombre\0][valor\0]…` (TDUPPER…VNTOL), constante.
  Los offsets del bloque C son ABSOLUTOS en ROOT.DSN: el escritor debe
  recalcularlos.
- Tags de registro: `00 ff` = componente/texto, `08 ff` = estados/LED/RES.
  Objeto = tag, len, ref, i32 x,y (comp×254), constantes (u32 0, u32 50,
  u32 0x5D048, u32 203200), 4 sub-textos `[f1][20 ff][f2][font\0][nombre\0]
  [u32 0][ff][len][valor][i32 x,y][u16 0][u32 2][u32 L][u32 254000]
  [u32 203200][u32 0]`, ancla de familia `[u16 n][len][familia][i32 x,y]`,
  y cola `[u8 0][u8 k_gate][u32 0×3][u32 h1][u32 h2][u32 h3]` donde h* son
  handles de pin.
- Cable = `[u32 handle…][00 1d 00 00 00][style 21 B][02 7f][WIRE\0]` + puntos
  (cabecera de handles aún sin cerrar; los u32 iniciales de cada cable
  coinciden con handles de cola de los objetos ⇒ ahí vive la conexión).
- Tips de pin: delta constante comp (−192,−108) para entradas 1/2 de NAND
  respecto al origen del objeto; los puntos de cable aterrizan exactos ahí.
