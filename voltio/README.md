# Voltio — motor de circuitos digitales accionado por IA

Un clon de **Proteus** (esquemático + simulación VSM) construido alrededor de una
idea distinta: **tú describes el problema en lenguaje natural, una IA lo traduce
a un DSL, y este motor lo valida, lo dibuja y lo simula al instante.** Sin
tutorial paso a paso, sin copiar nada a mano.

Y también funciona al revés: circuito → DSL → descripción.

```
  «un contador de 4 bits con display de 7 segmentos y reloj de 1 kHz»
                              │
                              ▼  (IA: OpenAI · Anthropic · Gemini · Ollama · copiar/pegar)
                        contador_7seg.yaml          ← el DSL, fuente de verdad
                              │
              ┌───────────────┼───────────────┬──────────────┐
              ▼               ▼               ▼              ▼
         validación      simulación        SVG/PNG      SPICE · EDIF · .pdsprj
        con mensajes    digital de eventos  con los       para Proteus, KiCad,
        que la IA       (0/1/X/Z, buses     cables        ngspice
        entiende y      tri-estado,         coloreados
        autocorrige     colisiones)         por nivel
```

---

## Por qué existe

Proteus parece "encriptado". **No lo está.** Está *no documentado*, que es muy
distinto y tiene solución. Un `.pdsprj` es un ZIP común, y dentro el esquemático
(`ROOT.DSN`) es un formato binario de registros con las propiedades de los
componentes guardadas como **texto plano** `{CLAVE=VALOR}`:

```
{*DEVICE}
{ACTIVE=CTIMER,12,DLL}
{*PROPDEFS}
{MODE="Operating Mode",KWDLIST,4,0="Time (secs)",1="Time (hms)",...}
{*COMPONENT}
{PRIMITIVE=DIGITAL}
{MODDLL=CTIMER.DLL}
```

Eso abre tres rutas de salida, y las tres están implementadas en distinto grado:

| Ruta | Estado | Qué conserva |
|---|---|---|
| **SVG / PNG** | ✅ funciona | el dibujo completo, colocado |
| **EDIF 2 0 0** | ✅ funciona | la conectividad (Proteus lo importa oficialmente) |
| **Netlist SPICE + BOM** | ✅ funciona | para ngspice, KiCad, compras |
| **`.pdsprj` binario** | 🔬 en investigación | todo, incluida la colocación |

El análisis completo del formato, lo que ya se ha descifrado y el método exacto
para terminar, están en **[`docs/PROTEUS_DSN.md`](docs/PROTEUS_DSN.md)**.

---

## Instalación

```bash
git clone <este repo> && cd voltio

# El núcleo no necesita NADA. Python 3.9+ y listo.
python3 -m voltio examples/contador_7seg.yaml

# Opcional, por partes:
pip install PySide6                    # la GUI interactiva
pip install pytest                     # correr los tests con pytest de verdad
export ANTHROPIC_API_KEY=...           # llamadas directas a la IA (o OPENAI_API_KEY, GEMINI_API_KEY)
```

El parser YAML es propio (`voltio/core/miniyaml.py`), las llamadas HTTP usan
`urllib` de la biblioteca estándar, y el SVG se genera a mano. **No hay ni un
solo requisito obligatorio.** Si PyYAML está instalado se usa en su lugar.

---

## Uso rápido

```bash
# Ver el circuito: valida, simula y guarda el esquemático en SVG
python3 -m voltio examples/contador_7seg.yaml

# Formas de onda en la terminal
python3 -m voltio examples/cpu8_bus.yaml --trace --sim 30ms

# Forzar entradas antes de simular
python3 -m voltio examples/compuerta_nand.yaml --set S1=1 --set S2=0

# Solo validar (sale código distinto de 0 si hay errores)
python3 -m voltio mi_circuito.yaml --check --strict

# Exportar
python3 -m voltio mi_circuito.yaml --spice out.cir --edif out.edf --bom out.csv

# El catálogo de componentes
python3 -m voltio --parts          # los 59
python3 -m voltio --parts mux      # búsqueda

# Pedirle a una IA que construya un circuito
python3 -m voltio --generate "un semáforo de 3 LEDs con contador"
python3 -m voltio --providers      # qué proveedores tienes disponibles

# Sentido inverso: que la IA explique un circuito
python3 -m voltio mi_circuito.yaml --explain --question "¿por qué hay un 74LS47?"

# GUI
python3 -m voltio --gui examples/cpu8_bus.yaml
```

```python
from voltio.builder import Circuit, ONE, ZERO
from voltio.sim import Engine
from voltio.render import save_svg

c = Circuit("divisor")
clk = c.clock("CLK1", freq=1000)          # 1 kHz
ff  = c.part("74LS74", "U1")
led = c.led("D1")

c.net("CLK").join(clk.Q, ff.CLK)
c.net("FB").join(ff["/Q"], ff.D)          # Q̄ → D: divide entre 2
c.net("OUT").join(ff.Q, led.A)
c.gnd(led.K)
c.vcc_net(ff["/CLR"], ff["/PRE"])

circ = c.build()
eng = Engine(circ)
eng.run(until=0.01)

print(eng.waveform("OUT").frequency())    # ≈ 500 Hz
save_svg(circ, "divisor.svg", engine=eng)
```

---

## Convenciones de dibujo (y por qué importan para Proteus)

El esquemático se dibuja con la regla de que **la imagen exportada tiene que
poder traducirse tal cual a un archivo de Proteus**. De ahí salen tres
convenciones que cumplen los renderizados:

1. **Un renglón por cable.** Los buses de dos extremos se dibujan como N cables
   paralelos, cada uno en su propio canal, con quiebres escalonados cuando las
   patillas no coinciden. Nada de fundir cables en una mancha: si dos cables
   comparten columna, la traducción a Proteus sería ambigua.
2. **Agrupar no es conectar.** Un bus de tres o más grupos (el bus de datos de
   la CPU, por ejemplo) se dibuja con el símbolo de banda de Proteus: entradas
   finas por patilla y banda gruesa que **agrupa sin conducir**. Los puntos de
   junta solo aparecen donde se cruzan tres o más tramos eléctricos de verdad.
   En el IR esto corresponde a un net independiente por bit: unir tres cables
   en un conductor sería un cortocircuito, y el validador lo rechaza.
3. **Cables atados a un nivel** (los `tie:` del DSL) se dibujan como un peine
   fino con un solo marcador de riel, porque todos esos puntos sí son el mismo
   nodo eléctrico (tierra o VCC).

Las capturas de Proteus que sirven de referencia de estilo están descritas en
`docs/PLAN.md` (§2b).

---

## El simulador

No es SPICE analógico. Para electrónica digital y arquitectura de hardware eso es
la herramienta equivocada: lento, y no entiende buses ni relojes. Voltio usa un
**motor de eventos** con lógica de cuatro valores:

| Valor | Significado | Por qué importa |
|---|---|---|
| `0` | nivel bajo | |
| `1` | nivel alto | |
| `X` | indeterminado | un flip-flop sin resetear **no** está en 0; simularlo como 0 oculta fallos reales |
| `Z` | alta impedancia | es lo que permite que varios dispositivos compartan un bus |

Con `X` y `Z` se puede simular un bus compartido —el corazón de cualquier
arquitectura de hardware— y **detectar colisiones**: dos drivers empujando el
mismo cable en direcciones opuestas. En una placa eso quema algo; aquí sale un
mensaje.

El motor resuelve correctamente los casos que rompen a los simuladores caseros:

- **Relojes en cascada.** La salida de un dispositivo relojea al siguiente
  (contadores de rizado, el strobe de una CPU que captura un latch de puerto).
- **Lazos combinatorios.** Un anillo impar de inversores no tiene punto fijo: se
  detecta y se reporta en vez de colgar.
- **Colisiones de bus**, distinguidas de la mera indeterminación.
- **Glitches transitorios** del asentamiento, que no se confunden con fallos.

Cuatro bugs de este tipo costaron más de encontrar; están documentados al final
de `docs/PLAN.md` con su test de regresión cada uno.

---

## El DSL

YAML legible, versionable en git, y lo bastante estricto para que una IA no
pueda colar un circuito roto.

```yaml
name: contador_7seg
simulation: {timescale: 1us, duration: 20ms}

parts:
  - {ref: CLK1, part: CLOCK,   at: [30, 120], props: {FREQ: 1000}}
  - {ref: U1,   part: 74LS193, at: [200, 80]}
  - {ref: U2,   part: 74LS48,  at: [420, 80]}
  - {ref: DISP1, part: 7SEG-CC, at: [620, 60]}

nets:
  - {name: CLK, connect: [CLK1.Q, U1.CPU]}
  - {name: Q,   bus: 4, connect: [U1.Q[0:3], U2.A[0:3]]}     # bus de 4 bits
  - {name: SEG, bus: 7, connect: [U2.a[0:6], DISP1.a[0:6]]}
  - {name: CTRL_CNT, tie: 1, connect: [U1./MR, U1.CPD, U1./PL]}   # atado a nivel fijo
  - {name: GND, connect: [DISP1.CC, DISP1.dp]}                     # GND se ata solo a 0
```

Lo que el validador comprueba, y que son justo los errores que comete una IA (y
un humano):

- componente o pin inexistente → **sugiere el correcto** por similitud
- un pin en dos redes distintas
- dos salidas push-pull en el mismo cable → cortocircuito
- VCC y GND enfrentados → cortocircuito
- ancho de bus incompatible con el rango de un extremo
- entradas sin conectar (leen `X` y contaminan todo)
- un cable sin nada que lo defina

Los mensajes están escritos **para que los lea un modelo**, no una persona.

---

## Cómo la IA entra en el bucle

`voltio/ai/prompt.py` genera el manual **dinámicamente desde la librería**: si
añades un componente, el prompt se entera solo. Son ~3.800 tokens con los 59
componentes, sus pines exactos (barras de activo-en-baja incluidas), la gramática
del DSL, las reglas del validador y tres ejemplos comentados.

El bucle de generación es lo que hace que esto sea fiable y no una ruleta:

1. Generar.
2. Extraer el bloque YAML de la respuesta (los modelos lo rodean de prosa; el
   extractor prueba los bloques y se queda con el que **valida**).
3. Cargar y validar.
4. Si hay errores, devolvérselos **textualmente** al modelo y pedir el YAML
   completo corregido. Repetir hasta N intentos.

Cinco proveedores intercambiables, incluida la ruta `clipboard`, que no necesita
ninguna clave ni conexión: te guarda el prompt en un archivo, lo pegas en el chat
que tengas abierto, y pegas la respuesta de vuelta.

---

## Contenido

```
voltio/
  core/       lógica de 4 valores · grafo del circuito · parser YAML propio
  parts/      59 componentes: 74xx, flip-flops, contadores, memorias, displays, CPU
  sim/        motor de eventos · 54 modelos eléctricos · colisiones · formas de onda
  render/     símbolos vectoriales → SVG autónomo (estilos incrustados)
  export/     SPICE · EDIF 2 0 0 · netlist · BOM · .pdsprj
  proteus/    lector e inspector del formato de Labcenter (ingeniería inversa)
  ai/         5 proveedores · prompt generado desde la librería · bucle de autocorrección
  reverse/    circuito → DSL → análisis estructural → descripción
  builder.py  API Python para generar circuitos con bucles y patrones
  gui/        canvas PySide6 interactivo
examples/     compuerta_nand · contador_7seg · cpu8_bus
tests/        119 tests, sin dependencias
docs/         PROTEUS_DSN.md (el formato) · PLAN.md (qué sigue)
tools/        run_tests.py — corre la suite en un Python pelado
              svg2png.py — rasteriza el SVG para poder MIRARLO (ver abajo)
              pdf_schematic.py — extrae cables, compuertas y textos con
                                 posición de un PDF exportado por Proteus
```

### Los tres ejemplos

| Archivo | Qué enseña |
|---|---|
| `compuerta_nand.yaml` | lo mínimo: dos entradas manuales, una NAND, un LED con su resistencia |
| `contador_7seg.yaml` | buses, decodificador BCD, y la trampa de las polaridades 74LS47/48 |
| `cpu8_bus.yaml` | **arquitectura de hardware**: CPU de 8 bits ejecutando un programa, bus tri-estado compartido, RAM, latch de puerto con strobe retardado, display |
| `compuertas_nand_pdf.yaml` | **réplica por ingeniería inversa**: un proyecto de Proteus recibido como PDF, reconstruido cable por cable (y con un error de De Morgan del autor original detectado y documentado) |

---

## Estado honesto

**Funciona:** motor, validador, simulador de eventos, renderizador SVG, los tres
exportadores, la capa de IA con autocorrección, el lector de proyectos de
Proteus, el módulo inverso, la CLI y 119 tests.

**Compila pero no se ha ejecutado:** la GUI PySide6. Se escribió en un entorno
sin PySide6 disponible, así que es el componente con más riesgo de tener fallos
tontos de Qt. `docs/PLAN.md` empieza por ahí.

### Por qué hay PNGs en `salida/` y un rasterizador en `tools/`

El renderizador SVG se escribió sin poder ver su salida: una IA no renderiza
vectoriales. La primera validación fue estructural ("hay 23 polilíneas"), y no
detectó nada de esto:

* posiciones multiplicadas por dos veces la escala → cables de 1600 px entre
  símbolos de 40 px y texto de 3 px;
* patillas sin su rabito: los cables terminaban en el aire y los símbolos
  flotaban;
* puntos de junta en cada extremo, como si todo fuera un cortocircuito;
* buses de 16 bits dibujados como 16 cables gordos → una mancha negra sólida;
* nombres de componente dentro de la caja, solapados con los nombres de pin.

`tools/svg2png.py` convierte el SVG en PNG con PIL (sin dependencias de sistema)
para poder mirar el dibujo de verdad. Cada uno de esos defectos se encontró
mirando, no testeando. Si tocas el renderizador, **mira la salida**:

```bash
python3 tools/svg2png.py salida/cpu8_bus.svg /tmp/vista.png --max 1800
```

Los `salida/vista_*.png` son las capturas actuales de los tres ejemplos.

**No funciona todavía:** escribir un `.pdsprj` que Proteus abra con tu circuito
dentro. Se genera el contenedor ZIP con los XML correctos, pero el binario
`ROOT.DSN` es un marcador de posición y el módulo lo dice explícitamente en vez
de fingir. El camino está trazado y el lector ya funciona.

**No existe:** el modelo de `ATMEGA328P` y `PIC16F877A`. Están en la librería
para que se puedan *dibujar*, pero caen en el modelo comodín y no simulan. La
`CPU8` educativa sí simula de verdad (16 instrucciones, fetch/decode/execute
observable, bus tri-estado).

---

## Pruebas

```bash
python3 tools/run_tests.py           # sin instalar nada
python3 tools/run_tests.py test_sim  # solo un archivo
pytest tests/ -q                     # si tienes pytest
```

```
119 pasaron, 0 fallaron, 0 omitidos
```
