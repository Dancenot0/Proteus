# Plan de continuación

Estado a día de hoy: motor, simulador, renderizador, exportadores, capa de IA,
CLI y 119 tests funcionando. La GUI PySide6 compila y está completa, pero no se
ha podido ejecutar en el entorno donde se escribió (no tiene PySide6 instalado):
**lo primero es arrancarla en tu máquina y ver qué se rompe.**

---

## 0. Verificación inmediata (una tarde)

Y antes de nada: **mira los dibujos.** `python3 tools/svg2png.py` existe porque
el renderizador se construyó a ciegas y tenía cinco defectos que solo se ven con
los ojos (escalas, patillas sin rabito, juntas mal, buses como mancha, textos
solapados). Cualquier cambio en `voltio/render/` debe terminar mirando un PNG,
no solo pasando tests.

```bash
pip install PySide6 pytest
python -m voltio --gui examples/contador_7seg.yaml
pytest tests/ -q          # con pytest de verdad, no solo el runner incluido
```

La GUI es el componente con más riesgo de tener fallos tontos (señales de Qt,
coords de los handles de pin) porque no se ha podido probar. Todo lo demás está
cubierto por tests.

---

## 1. Terminar el escritor `.pdsprj` (el objetivo original)

**Estado (ronda 11, 2026-10-11):** el método "plantilla + parche" de abajo ya
no es una apuesta: es lo que funcionan las rondas R3–R11. Lo que queda es una
sola pregunta binaria que decide R11 (`salida/proteus/experimentos/LEEME_R11.txt`):
¿la conectividad de un cable vive en sus puntos (geometría) o en los u32 de
handles de su cabecera? Con esa respuesta el escritor emite sección 2 completa
(gramática de bloques A/B/C y colas de objeto ya documentadas en
`docs/PROTEUS_DSN.md`, addendum R10/R11) y se cierra el ciclo sr_latch →
Proteus. Mientras tanto, las fases A–D siguen siendo el plan de contingencia:

Es lo único que falta para poder abrir en Proteus un circuito generado por IA.
El método está en `docs/PROTEUS_DSN.md` sección 3. Concretamente:

**Fase A — geometría de un componente.**
Generar en Proteus 8 proyectos con UN componente, cada uno movido 10 unidades en
una dirección distinta. Ejecutar `voltio.proteus.compare()` entre pares
consecutivos. Los bytes que cambian son las coordenadas. Con eso ya se sabe
escribir la posición.

**Fase B — identidad y propiedades.**
Repetir cambiando: el nombre del componente, una propiedad, la rotación, el
espejo. Los bloques `{*COMPONENT}` ya se saben leer; falta saber escribirlos y
saber dónde van anclados en el binario.

**Fase C — cables.**
El paso difícil y el que da valor. Añadir un cable entre dos pines y comparar.
Hay que identificar: el registro de tipo `WIRE`, cómo referencia los dos
extremos, y cómo se codifica la polilínea (los cables de ISIS son ortogonales
con vértices).

**Fase D — escribir.**
Con A+B+C se puede montar `voltio/export/proteus.py` de verdad. La estrategia
recomendada es **plantilla + parche**: partir de un `.pdsprj` real mínimo y
reescribir solo las secciones que se entienden, en vez de sintetizar el archivo
completo desde cero. Reduce muchísimo el riesgo.

Cada fase es independiente y deja el proyecto en un estado utilizable.

---

## 2. Ampliar la librería

Ahora hay 59 componentes. Lo que más falta para electrónica digital y
arquitectura de hardware:

**Prioridad alta** (desbloquean diseños reales):
- `74LS373` / `74LS374` — latch y registro tri-estado, el par estándar para
  buses de CPU. El ejemplo `cpu8_bus` los necesita.
- `74LS139`, `74LS42` — más decodificadores.
- `74LS06`, `74LS05` — inversores de colector abierto, para buses con pull-up.
- Puertas de 3 estados con Schmitt trigger (`74LS14`).
- `74LS670` — registro de 16 palabras × 4 bits, útil para bancos de registros.

**Prioridad media** (arquitectura de hardware):
- Un banco de registros genérico (4×4, 8×8, configurable).
- `74LS189` — RAM de 16×4, la típica para bancos de registros educativos.
- Multiplexores de más entradas (`74LS150` 16→1).
- Un decodificador de direcciones configurable, que es lo que le falta al
  ejemplo de la CPU para tener un mapa de memoria real.
- Contadores de más bits y con carga síncrona (`74LS160/162/163`).

**Prioridad baja**:
- Famila CMOS 4000 (4017, 4040, 4060).
- Displays de más dígitos con multiplexado.
- Memoria serie (24C02 I²C, 25C08 SPI) — requiere modelar el protocolo.

Añadir un componente es: una entrada en `voltio/parts/library.py` + un modelo en
`voltio/sim/parts.py` + un test. El prompt de la IA se entera solo.

---

## 2b. Deuda visual conocida

* Las etiquetas de redes de un bit paralelas (PORT0..PORT3 del ejemplo de la
  CPU) acaban apiladas arriba por el anticolisión, en vez de sobre su cable.
  El colocador (`_LabelPlacer`) empuja siempre hacia arriba; debería probar
  abajo y al lado antes de rendirse.
* Cuando dos buses se enganchan al mismo lado de una misma pieza (ABUS y DBUS
  en la CPU), las entradas de uno cruzan la banda del otro. Se mitiga con
  separación de bandas, pero un encaminado de entradas en diagonal, como el de
  los CAD de verdad, lo resolvería bien.
* El halo del LED se dibuja encima del símbolo en vez de detrás.

## 3. Mejorar el motor

- **Retardos de propagación reales.** Los modelos ya tienen campo `tpd` pero el
  motor no lo usa. Con `tpd` en cada compuerta, las carreras y los glitches se
  verían de verdad, y el `setup`/`hold` de los flip-flops se podría comprobar.
  Es la mejora con más valor pedagógico.
- **Consumo y niveles analógicos.** Ahora todo es digital puro. Un modo mixto
  que delegue lo analógico a ngspice (los exportadores ya están) permitiría
  simular LEDs con su corriente real, pull-ups, etc.
- **Modelos de microcontrolador.** `ATMEGA328P` y `PIC16F877A` están en la
  librería con modelo `avr`/`pic`, pero esos modelos NO están implementados:
  caen en el `_Stub`. Implementar un subconjunto del AVR (SREG, pila, los modos
  de direccionamiento básicos) es un proyecto grande pero muy visible, porque
  es lo que la gente espera de Proteus.
- **Detección de carreras.** Con retardos reales se puede avisar de
  "esta señal cambia mientras el latch está transparente".
- **Punto de interrupción y paso a paso por instrucción** en la CPU8.

---

## 4. Mejorar la capa de IA

- **Validación previa al envío.** Antes de gastar una llamada, comprobar que la
  descripción no pide componentes inexistentes y sugerir los más cercanos.
- **Few-shot dinámico.** Meter en el prompt el ejemplo más parecido al pedido,
  no los tres siempre. Reduce tokens y mejora la precisión.
- **Modo "reparar mi circuito".** Dado un DSL que no valida, pedirle a la IA que
  lo arregle. El bucle de autocorrección ya hace esto, pero falta la entrada
  desde un archivo existente.
- **Streaming.** Con los proveedores de API, mostrar el YAML conforme llega en
  vez de esperar.
- **Coste.** Contar tokens por llamada y avisar si una generación va a ser cara.

---

## 5. Cosas que conviene saber antes de tocar nada

Los cuatro bugs que más costaron encontrar, y que volverán si se refactoriza el
motor sin los tests delante:

1. **La referencia de flanco no puede ser global ni re-snapshotearse.** Ver el
   docstring de `Engine._settle`. Si se congela mal, los flip-flops o no
   capturan nunca, o cuentan 200 veces por pulso.
2. **Los modelos secuenciales deben poder re-dispararse** cuando aparece un
   flanco nuevo a mitad del asentamiento (relojes en cascada), pero solo ante
   flancos *distintos*. `_inputs_changed` es lo que separa ambas cosas.
3. **Las colisiones se juzgan sobre el estado asentado**, no sobre los
   intermedios. Con retardo cero hay glitches internos legítimos.
4. **Una resistencia no es un buffer direccional**, y si suelta el bus cuando
   ambos lados están definidos, el circuito oscila entre 1 y X para siempre.

Los cuatro tienen test de regresión en `tests/test_sim.py`.
