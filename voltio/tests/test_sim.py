"""Pruebas del motor de simulación.

Estas son las pruebas importantes: verifican que el motor se comporta como un
circuito real, no solo que no revienta. Cubren la tabla de verdad de las
compuertas, el disparo por flanco (que es donde se rompen los simuladores
caseros), los buses tri-estado, la detección de colisiones y de lazos
combinatorios, y componentes MSI completos.
"""

import pytest

from voltio.builder import ONE, ZERO, Circuit
from voltio.core.logic import L
from voltio.sim import Engine


def _engine(builder: Circuit, until: float = 0.0) -> Engine:
    """Construye y, si procede, simula."""
    eng = Engine(builder.build())
    if until:
        eng.run(until=until)
    return eng


def _out(eng: Engine, ref: str, pin: str, bit: int = 0) -> L:
    """Lee una SALIDA del modelo.

    Se lee del búfer interno y no del cable a propósito: en estos tests las
    salidas suelen quedar sin conectar (solo interesa qué calcula el modelo),
    y un pin sin cablear lee X por definición. Para leer lo que hay en un
    cable, usa ``eng.net_value`` o ``eng.pin_value``.
    """
    return eng._pin_slot(ref, pin)[bit]


def _out_bus(eng: Engine, ref: str, pin: str):
    """Valor entero de un bus de salida, o None si algún bit es X/Z."""
    bits = eng._pin_slot(ref, pin)
    if any(not b.determinate for b in bits):
        return None
    v = 0
    for i, b in enumerate(bits):
        v |= b.as_bit << i
    return v


# ---------------------------------------------------------------------------
# Compuertas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b,esperado", [
    (0, 0, 1), (0, 1, 1), (1, 0, 1), (1, 1, 0),
])
def test_tabla_de_verdad_nand(a, b, esperado):
    c = Circuit("nand")
    s1 = c.logic("S1", state=a)
    s2 = c.logic("S2", state=b)
    u = c.part("74LS00", "U1")
    c.net("A").join(s1.Q, u.A0)
    c.net("B").join(s2.Q, u.A1)
    eng = _engine(c)
    assert _out(eng, "U1", "Y") is (L.HIGH if esperado else L.LOW)


def test_inversor():
    c = Circuit("inv")
    s = c.logic("S1", state=1)
    u = c.part("74LS04", "U1")
    c.net("IN").join(s.Q, u.A0)
    eng = _engine(c)
    assert _out(eng, "U1", "Y") is L.LOW
    eng.model("S1").set_interactive(0)
    eng.settle()
    assert _out(eng, "U1", "Y") is L.HIGH


def test_xor():
    casos = {(0, 0): 0, (0, 1): 1, (1, 0): 1, (1, 1): 0}
    for (a, b), esperado in casos.items():
        c = Circuit("xor")
        s1, s2 = c.logic("S1", state=a), c.logic("S2", state=b)
        u = c.part("74LS86", "U1")
        c.net("A").join(s1.Q, u.A0)
        c.net("B").join(s2.Q, u.A1)
        eng = _engine(c)
        assert _out(eng, "U1", "Y") is (L.HIGH if esperado else L.LOW)


def test_entrada_flotante_lee_indeterminado():
    """Una entrada sin conectar NO lee 0. Lee X. Es la diferencia entre un
    simulador de juguete y uno que te avisa de un fallo real."""
    c = Circuit("flotante")
    u = c.part("74LS08", "U1")
    c.net("A").join(u.A0)
    c.tie(ONE, u.A1)
    eng = _engine(c)
    assert eng.pin_value("U1", "A0") is L.X
    # Y un AND con X en una entrada y 1 en la otra da X, no 0.
    assert _out(eng, "U1", "Y") is L.X


def test_and_con_un_cero_domina_aunque_la_otra_este_flotando():
    """Regla eléctrica real: 0 AND cualquier cosa = 0, incluso X."""
    c = Circuit("dominio")
    u = c.part("74LS08", "U1")
    c.tie(ZERO, u.A0)
    c.net("LIBRE").join(u.A1)
    eng = _engine(c)
    assert eng.pin_value("U1", "A1") is L.X
    assert _out(eng, "U1", "Y") is L.LOW


# ---------------------------------------------------------------------------
# Secuenciales: aquí es donde fallan los simuladores mal hechos
# ---------------------------------------------------------------------------


def test_flipflop_d_arranca_indeterminado():
    c = Circuit("dff")
    ff = c.part("DFF", "U1")
    c.tie(ZERO, ff.D)
    c.net("CLK").join(ff.CLK)
    eng = _engine(c)
    assert eng.pin_value("U1", "Q") is L.X


def test_flipflop_d_captura_en_el_flanco():
    c = Circuit("dff")
    clk = c.clock("CLK1", freq=1000.0)
    ff = c.part("DFF", "U1")
    c.net("CLK").join(clk.Q, ff.CLK)
    c.tie(ONE, ff.D)
    eng = _engine(c)
    # Antes del primer flanco sigue en X.
    assert _out(eng, "U1", "Q") is L.X
    eng.run(until=0.0006)  # medio periodo de un reloj de 1 kHz
    assert _out(eng, "U1", "Q") is L.HIGH


def test_flipflop_no_cuenta_de_mas():
    """El fallo clásico de los simuladores caseros.

    Si el modelo secuencial se evalúa dentro del bucle de asentamiento, ve el
    mismo flanco en cada pasada y conmuta cientos de veces por un solo flanco
    real. Se comprueba contando transiciones en la forma de onda de Q: un
    divisor por 2 alimentado con 10 flancos de reloj debe conmutar unas 10
    veces, no 2000.

    Hay que presetear el flip-flop primero. Sin eso Q arranca en X y, como
    ``NOT(X)`` es ``X``, conmutaría de X a X indefinidamente sin que se viera
    ningún error. Eso es fiel a la realidad —un biestable sin inicializar está
    indeterminado— pero no sirve para medir nada.
    """
    c = Circuit("toggle")
    clk = c.clock("CLK1", freq=1000.0)
    ff = c.part("74LS74", "U1")
    pre = c.logic("PRE1", state=0)      # /PRE activo bajo: arranca presteado

    c.net("CLK").join(clk.Q, ff.CLK)
    # Q̄ directo a D: en cada flanco Q toma el valor contrario → divide entre 2.
    # (Poner un inversor en medio sería realimentación NO inversora, por la
    # doble negación, y Q se quedaría clavado. Error facilísimo de cometer.)
    c.net("FB").join(ff["/Q"], ff.D)
    c.net("SAL").join(ff.Q)             # Q a su propio cable para leer la onda
    c.net("PRE").join(pre.Q, ff["/PRE"])
    c.tie(ONE, ff["/CLR"])
    eng = _engine(c)

    assert _out(eng, "U1", "Q") is L.HIGH        # presteado a 1

    eng.model("PRE1").set_interactive(1)         # liberamos /PRE
    eng.settle()
    eng.run(until=0.01)                          # 10 flancos de subida

    wf = eng.waveform("SAL")
    assert wf is not None
    transiciones = len(wf)
    assert transiciones <= 12, (
        f"Q conmutó {transiciones} veces con solo 10 flancos de reloj: "
        f"el flip-flop se está re-disparando con el mismo flanco"
    )
    # Y el periodo de Q debe ser el doble que el del reloj.
    fq = wf.frequency()
    assert fq == pytest.approx(500.0, rel=0.25), fq


def test_flipflop_reset_asincrono():
    """/CLR a nivel bajo pone Q en 0 sin que importen el reloj ni D."""
    c = Circuit("reset")
    ff = c.part("74LS74", "U1")
    c.tie(ONE, ff.D, ff.CLK, ff["/PRE"])
    c.tie(ZERO, ff["/CLR"])
    eng = _engine(c)
    assert _out(eng, "U1", "Q") is L.LOW
    assert _out(eng, "U1", "/Q") is L.HIGH


def test_flipflop_preset_gana_al_reloj():
    """/PRE a nivel bajo fuerza Q a 1 aunque D y el reloj digan otra cosa."""
    c = Circuit("preset")
    ff = c.part("74LS74", "U1")
    c.tie(ZERO, ff.D)
    c.tie(ONE, ff.CLK, ff["/CLR"])
    c.tie(ZERO, ff["/PRE"])
    eng = _engine(c)
    assert _out(eng, "U1", "Q") is L.HIGH


def test_flipflop_jk_toggle():
    """Con J=K=1 el JK conmuta en cada flanco de bajada.

    Se prestea primero: un flip-flop en X conmuta a X, que es correcto pero
    no se puede medir.
    """
    c = Circuit("jk")
    ff = c.part("74LS76", "U1")
    clk = c.clock("CLK1", freq=1000.0)
    pre = c.logic("PRE1", state=0)
    c.net("CLK").join(clk.Q, ff.CLK)
    c.net("PRE").join(pre.Q, ff["/PRE"])
    c.vcc_net(ff.J, ff.K, ff["/CLR"])
    c.net("Q").join(ff.Q)
    eng = _engine(c)
    assert _out(eng, "U1", "Q") is L.HIGH

    eng.model("PRE1").set_interactive(1)
    eng.settle()
    eng.run(until=0.01)     # 10 flancos de bajada
    n = len(eng.waveform("Q"))
    assert 9 <= n <= 12, f"Q conmutó {n} veces, se esperaban ~10"


def test_contador_193_secuencia_binaria():
    """El contador debe seguir 0,1,2,...,15,0 — no otra cosa."""
    c = Circuit("cnt")
    clk = c.clock("CLK1", freq=1000.0)
    u = c.part("74LS193", "U1")
    c.net("CLK").join(clk.Q, u.CPU)
    c.tie(ONE, u["/MR"], u.CPD, u["/PL"])
    c.tie(ZERO, u.P.bits(0, 3))
    for i in range(4):                 # cada bit de Q a su propio cable
        c.net(f"QB{i}").join(u.Q.bit(i))
    eng = _engine(c)
    eng.run(until=0.02)  # 20 flancos
    wf_clk = eng.waveform("CLK")
    # Cableamos Q a cuatro sondas para poder leer la onda de cada bit.
    lecturas = []
    for t in wf_clk.edges(True):
        bits = [eng.waveform(f"QB{i}").at(t + 1e-6) for i in range(4)]
        v = 0
        for i, b in enumerate(bits):
            v |= (1 if b is L.HIGH else 0) << i
        lecturas.append(v)
    assert lecturas == [i % 16 for i in range(1, len(lecturas) + 1)]


def test_contador_reset_por_mr():
    c = Circuit("cnt_rst")
    clk = c.clock("CLK1", freq=1000.0)
    u = c.part("74LS193", "U1")
    c.net("CLK").join(clk.Q, u.CPU)
    c.tie(ONE, u.CPD, u["/PL"])
    c.tie(ZERO, u.P.bits(0, 3), u["/MR"])   # /MR a 0 → reset permanente
    eng = _engine(c)
    eng.run(until=0.01)
    assert _out_bus(eng, "U1", "Q") == 0


def test_registro_desplazamiento_595():
    """Mete 8 bits en serie por DS con pulsos reales de SHCP.

    Con flancos de verdad, no tocando el estado interno a mano: es justo lo que
    hay que verificar. Y con un ``settle()`` entre cada cambio de nivel, porque
    el motor detecta flancos comparando contra el último estado asentado; si se
    cambia de 0 a 1 y de 1 a 0 sin asentar en medio, el flanco no existe.
    """
    c = Circuit("sr")
    u = c.part("74LS595", "U1")
    s_data = c.logic("DS1", state=0)
    s_shcp = c.logic("SH1", state=0)
    s_stcp = c.logic("ST1", state=0)
    c.net("DS").join(s_data.Q, u.DS)
    c.net("SHCP").join(s_shcp.Q, u.SHCP)
    c.net("STCP").join(s_stcp.Q, u.STCP)
    c.vcc_net(u["/MR"])
    c.gnd(u["/OE"])                      # salidas habilitadas
    eng = _engine(c)

    def pulso(valor: int) -> None:
        eng.model("DS1").set_interactive(valor)
        eng.settle()
        eng.model("SH1").set_interactive(1)   # flanco de subida: entra el bit
        eng.settle()
        eng.model("SH1").set_interactive(0)
        eng.settle()

    serie = [1, 0, 1, 1, 0, 0, 1, 0]     # el primero que entra acaba en Q7
    for bit in serie:
        pulso(bit)

    # El primer bit que entra acaba en Q7, así que el valor es la serie leída
    # en orden directo como binario: [1,0,1,1,0,0,1,0] → 0b10110010.
    esperado = int("".join(str(b) for b in serie), 2)
    assert eng.model("U1").state["shift"] == esperado, eng.model("U1").state

    # Q todavía refleja el latch anterior (todo ceros).
    assert _out_bus(eng, "U1", "Q") == 0

    # Pulso de STCP: el registro de desplazamiento se publica en las salidas.
    eng.model("ST1").set_interactive(1)
    eng.settle()
    assert _out_bus(eng, "U1", "Q") == esperado

    # Q7S es la salida serie para encadenar otro registro.
    assert _out(eng, "U1", "Q7S") is (L.HIGH if esperado & 0x80 else L.LOW)


def test_595_salidas_en_z_cuando_oe_alto():
    """Con /OE en alta las salidas se sueltan: es lo que permite encadenarlos."""
    c = Circuit("sr_z")
    u = c.part("74LS595", "U1")
    c.vcc_net(u["/MR"], u["/OE"])
    c.tie(ZERO, u.DS, u.SHCP, u.STCP)
    eng = _engine(c)
    eng.model("U1").state["out"] = 0xFF
    eng.settle()
    assert all(_out(eng, "U1", "Q", i) is L.Z for i in range(8))


# ---------------------------------------------------------------------------
# Buses tri-estado
# ---------------------------------------------------------------------------


def test_bus_tristate_un_solo_driver():
    c = Circuit("bus")
    b1 = c.part("74LS125", "U1")
    b2 = c.part("74LS125", "U2")
    c.tie(ZERO, b1["/OE"])     # U1 habilitado
    c.tie(ONE, b2["/OE"])      # U2 deshabilitado
    c.tie(ONE, b1.A)
    c.tie(ZERO, b2.A)
    c.net("BUS").join(b1.Y, b2.Y)
    eng = _engine(c)
    assert eng.net_value("BUS") is L.HIGH


def test_bus_tristate_nadie_drivea_lee_x():
    c = Circuit("bus_flotante")
    b1 = c.part("74LS125", "U1")
    b2 = c.part("74LS125", "U2")
    c.tie(ONE, b1["/OE"], b2["/OE"])
    c.tie(ONE, b1.A, b2.A)
    c.net("BUS").join(b1.Y, b2.Y)
    eng = _engine(c)
    assert eng.net_value("BUS") is L.X


def test_colision_de_bus_se_detecta():
    """Dos buffers habilitados a la vez con niveles distintos: el motor debe
    reportarlo, porque en la placa eso quema algo."""
    c = Circuit("colision")
    b1 = c.part("74LS126", "U1")
    b2 = c.part("74LS126", "U2")
    c.tie(ONE, b1.OE, b2.OE)   # ambos habilitados (OE activo alto)
    c.tie(ONE, b1.A)
    c.tie(ZERO, b2.A)
    c.net("BUS").join(b1.Y, b2.Y)
    eng = _engine(c)
    eng.settle()
    assert eng.net_value("BUS") is L.X
    assert any(p.kind == "collision" for p in eng.problems), eng.problems


def test_transceptor_245_direccional():
    c = Circuit("245")
    u = c.part("74LS245", "U1")
    c.tie(ZERO, u["/E"])
    c.tie(ONE, u.DIR)          # A → B
    c.tie(ZERO, u.A.bits(0, 7))
    c.net("B0").join(u.B.bit(0))
    eng = _engine(c)
    # B es INOUT y no está cableado: se lee del búfer del modelo.
    assert _out(eng, "U1", "B", 0) is L.LOW


# ---------------------------------------------------------------------------
# MSI
# ---------------------------------------------------------------------------


def test_sumador_283():
    for a, b, cin, total in [(3, 4, 0, 7), (15, 1, 0, 16), (9, 9, 0, 18), (2, 2, 1, 5)]:
        c = Circuit("add")
        u = c.part("74LS283", "U1")
        c.tie(ZERO if not cin else ONE, u.C0)
        for i in range(4):
            c.tie(ONE if (a >> i) & 1 else ZERO, u.A.bit(i))
            c.tie(ONE if (b >> i) & 1 else ZERO, u.B.bit(i))
        eng = _engine(c)
        got = _out_bus(eng, "U1", "S")
        carry = _out(eng, "U1", "C4")
        assert got == (total & 0xF), f"{a}+{b}+{cin}: esperaba {total & 0xF}, dio {got}"
        assert (carry is L.HIGH) == (total > 15)


def test_decodificador_138():
    """Solo la salida seleccionada debe estar activa (baja); el resto altas."""
    for sel in range(8):
        c = Circuit("dec")
        u = c.part("74LS138", "U1")
        c.tie(ZERO, u["/E1"], u["/E2"])
        c.tie(ONE, u.E3)
        for i in range(3):
            c.tie(ONE if (sel >> i) & 1 else ZERO, u.A.bit(i))
        eng = _engine(c)
        bajos = [i for i in range(8) if _out(eng, "U1", "/Y", i) is L.LOW]
        assert bajos == [sel], f"sel={sel}: activas={bajos}"


def test_decodificador_138_deshabilitado():
    c = Circuit("dec_off")
    u = c.part("74LS138", "U1")
    c.tie(ONE, u["/E1"])      # enable inactivo
    c.tie(ZERO, u["/E2"], u.A.bits(0, 2))
    eng = _engine(c)
    assert all(_out(eng, "U1", "/Y", i) is L.HIGH for i in range(8))


def test_bcd_a_7_segmentos():
    """Para cada dígito BCD, los segmentos encendidos deben ser los correctos."""
    esperados = {
        0: "abcdef", 1: "bc", 2: "abdeg", 3: "abcdg", 4: "bcfg",
        5: "acdfg", 6: "acdefg", 7: "abc", 8: "abcdefg", 9: "abcdfg",
    }
    orden = "abcdefg"
    for dig, segs in esperados.items():
        c = Circuit("disp")
        u = c.part("74LS48", "U1")       # salidas activas en alta
        d = c.part("7SEG-CC", "DISP1")   # cátodo común
        for i in range(4):
            c.tie(ONE if (dig >> i) & 1 else ZERO, u.A.bit(i))
        c.tie(ONE, u["/LT"], u["/RBI"], u["/BI"])
        c.net("SEG", 7).join(u.a.bits(0, 6), d.a.bits(0, 6))
        c.gnd(d.CC, d.dp)
        eng = _engine(c)
        lit = eng.model("DISP1").segments_lit()
        obtenido = "".join(orden[i] for i in range(7) if lit[i])
        assert obtenido == segs, f"dígito {dig}: esperaba {segs}, dio {obtenido}"
        assert eng.model("DISP1").state["digit"] == format(dig, "X")


def test_polaridad_del_display_se_autodetecta():
    """El mismo decodificador con ánodo común: el display debe seguir mostrando
    el dígito correcto porque detecta la polaridad por el pin común."""
    c = Circuit("disp_ca")
    u = c.part("74LS47", "U1")       # salidas activas en BAJA
    d = c.part("7SEG-CA", "DISP1")   # ánodo común
    for i in range(4):
        c.tie(ONE if (5 >> i) & 1 else ZERO, u.A.bit(i))
    c.tie(ONE, u["/LT"], u["/RBI"], u["/BI"])
    c.net("SEG", 7).join(u["/a"].bits(0, 6), d.a.bits(0, 6))
    c.vcc_net(d.CA)                  # común a VCC → ánodo común
    eng = _engine(c)
    assert eng.model("DISP1").state["digit"] == "5"
    assert "ánodo común" in eng.model("DISP1").state["polarity"]


def test_multiplexor_151():
    for sel in range(8):
        c = Circuit("mux")
        u = c.part("74LS151", "U1")
        c.tie(ZERO, u["/E"])
        for i in range(3):
            c.tie(ONE if (sel >> i) & 1 else ZERO, u.S.bit(i))
        for i in range(8):
            c.tie(ONE if i % 2 else ZERO, u.D.bit(i))
        eng = _engine(c)
        esperado = L.HIGH if sel % 2 else L.LOW
        assert _out(eng, "U1", "Y") is esperado
        assert _out(eng, "U1", "/W") is (L.LOW if esperado is L.HIGH else L.HIGH)


# ---------------------------------------------------------------------------
# Oscilaciones y lazos
# ---------------------------------------------------------------------------


def test_anillo_par_converge():
    """Dos inversores en anillo NO oscilan: son un biestable.

    Es un error de concepto muy común (y este test existía mal escrito en la
    primera versión). Un anillo PAR de inversores tiene punto fijo: o se queda
    en un estado estable, o en X si arranca indeterminado. Lo que el motor debe
    hacer es converger, no quedarse colgado.
    """
    c = Circuit("biestable")
    u1 = c.part("74LS04", "U1")
    u2 = c.part("74LS04", "U2")
    c.net("A").join(u1.Y, u2.A0)
    c.net("B").join(u2.Y, u1.A0)
    eng = _engine(c)
    eng.settle()
    assert not any(p.kind == "oscillation" for p in eng.problems), eng.problems
    # Sin estado inicial definido, la única solución coherente es X.
    assert eng.net_value("A") is L.X


def test_anillo_impar_se_detecta_como_oscilacion():
    """Tres inversores en anillo no tienen punto fijo: oscilan de verdad.

    El motor debe cortarlo tras ``max_settle`` pasadas y reportarlo, en vez de
    entrar en un bucle infinito.
    """
    c = Circuit("oscilador_en_anillo")
    u1, u2, u3 = c.part("74LS04", "U1"), c.part("74LS04", "U2"), c.part("74LS04", "U3")
    c.net("A").join(u1.Y, u2.A0)
    c.net("B").join(u2.Y, u3.A0)
    c.net("C").join(u3.Y, u1.A0)
    eng = _engine(c)
    # Forzamos un estado definido; si arranca en X converge trivialmente a X y
    # no habría nada que detectar.
    eng.poke("A", L.LOW)
    assert any(p.kind == "oscillation" for p in eng.problems), eng.problems


def test_lazo_con_memoria_no_oscila():
    """Un lazo que pasa por un flip-flop converge: la memoria lo rompe.

    Es el contraste con ``test_anillo_impar_se_detecta_como_oscilacion``.
    """
    c = Circuit("anillo_ok")
    u1 = c.part("74LS04", "U1")
    ff = c.part("DFF", "U2")
    c.net("A").join(u1.Y, ff.D)
    c.net("B").join(ff.Q, u1.A0)
    c.tie(ZERO, ff.CLK)
    eng = _engine(c)
    eng.settle()
    assert not any(p.kind == "oscillation" for p in eng.problems), eng.problems


# ---------------------------------------------------------------------------
# Reloj y formas de onda
# ---------------------------------------------------------------------------


def test_reloj_genera_la_frecuencia_pedido():
    c = Circuit("clk")
    clk = c.clock("CLK1", freq=1000.0)
    c.net("CLK").join(clk.Q)
    eng = _engine(c)
    eng.run(until=0.01)
    wf = eng.waveform("CLK")
    f = wf.frequency()
    assert f == pytest.approx(1000.0, rel=0.05), f


def test_forma_de_onda_registra_transiciones():
    c = Circuit("clk2")
    clk = c.clock("CLK1", freq=100.0)
    c.net("CLK").join(clk.Q)
    eng = _engine(c)
    eng.run(until=0.05)
    wf = eng.waveform("CLK")
    assert len(wf) >= 9              # ~5 periodos → ~10 flancos
    assert wf.at(0.0) is L.LOW
    assert wf.at(0.005 + 1e-9) is L.HIGH


def test_componente_interactivo_cambia_en_caliente():
    c = Circuit("inter")
    s = c.logic("S1", state=0)
    u = c.part("74LS04", "U1")
    c.net("IN").join(s.Q, u.A0)
    eng = _engine(c)
    assert _out(eng, "U1", "Y") is L.HIGH
    eng.model("S1").set_interactive(1)
    eng.settle()
    assert _out(eng, "U1", "Y") is L.LOW
    assert "S1" in eng.interactive_parts()


# ---------------------------------------------------------------------------
# Integración: los ejemplos del repo
# ---------------------------------------------------------------------------


def test_ejemplo_nand_simula_sin_problemas():
    import os

    ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "examples", "compuerta_nand.yaml")
    from voltio.dsl import load_file

    c, issues = load_file(ruta)
    assert not [i for i in issues if i.kind == "error"]
    eng = Engine(c)
    eng.run(until=1e-6)
    assert not eng.problems, eng.problems
    # NAND con ambas entradas a 0 → salida 1 → LED encendido.
    assert eng.model("D1").lit()


def test_ejemplo_contador_simula_sin_problemas():
    import os

    ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "examples", "contador_7seg.yaml")
    from voltio.dsl import load_file

    c, issues = load_file(ruta)
    assert not [i for i in issues if i.kind == "error"]
    eng = Engine(c)
    eng.run(until=0.02)
    assert not eng.problems, eng.problems
    assert eng.model("U1").state["n"] == 4     # 20 flancos → 20 % 16
    assert eng.model("DISP1").state["digit"] == "4"


# ---------------------------------------------------------------------------
# Relojes en cascada: la salida de un dispositivo relojea al siguiente
# ---------------------------------------------------------------------------


def test_reloj_en_cascada_contador_de_rizado():
    """Dos flip-flops en cascada: el Q del primero relojea al segundo.

    Este es el caso que rompe los motores de eventos mal construidos. Si cada
    secuencial solo puede dispararse una vez por evento, el segundo flip-flop
    se evalúa antes de que la propagación publique el flanco del primero y no
    lo ve nunca: la segunda etapa se queda clavada. El motor tiene que permitir
    que un modelo vuelva a dispararse cuando aparece un flanco NUEVO a mitad
    del asentamiento, sin re-dispararse con el mismo.

    Se usan JK con J=K=1, que conmutan en cada flanco por construcción. Con un
    DFF haría falta realimentar Q̄ a D; conectar Q a D directamente retiene el
    valor en vez de dividir, que es un error fácil de cometer.

    Resultado esperado: 1 kHz → 500 Hz → 250 Hz.
    """
    c = Circuit("rizo")
    clk = c.clock("CLK1", freq=1000.0)
    ff1 = c.part("74LS76", "U1")
    ff2 = c.part("74LS76", "U2")
    pre = c.logic("PRE1", state=0)      # /PRE activo bajo: arrancan en 1

    c.net("CLK").join(clk.Q, ff1.CLK)
    c.net("SAL1").join(ff1.Q, ff2.CLK)  # ← cascada: Q1 es el reloj de U2
    c.net("SAL2").join(ff2.Q)
    c.net("PRE").join(pre.Q, ff1["/PRE"], ff2["/PRE"])
    # J=K=1 → conmuta en cada flanco. /CLR inactivo.
    c.vcc_net(ff1.J, ff1.K, ff2.J, ff2.K, ff1["/CLR"], ff2["/CLR"])
    eng = _engine(c)

    eng.model("PRE1").set_interactive(1)   # liberamos el preset
    eng.settle()
    eng.run(until=0.02)                    # 20 flancos de entrada

    f1 = eng.waveform("SAL1").frequency()
    f2 = eng.waveform("SAL2").frequency()
    assert f1 == pytest.approx(500.0, rel=0.2), f"primera etapa: {f1}"
    # Si el motor no propaga la cascada, la segunda etapa no conmuta jamás y
    # frequency() devuelve None en vez de un número.
    assert f2 is not None, "el segundo flip-flop nunca vio el flanco del primero"
    assert f2 == pytest.approx(250.0, rel=0.25), f"segunda etapa: {f2}"


def test_strobe_retardado_captura_el_dato():
    """Un latch disparado por una señal que genera otro dispositivo secuencial.

    Es el patrón CPU → puerto de salida. Falla de dos maneras distintas según
    cómo se temporicen las señales, y las dos se ven en la traza:

    * si el strobe dura menos de un ciclo, no tiene flanco de subida y el latch
      no captura nunca;
    * si el strobe sube a la vez que el dato, con retardo cero el latch ve el
      bus todavía en X y captura indeterminación.

    El ejemplo ``cpu8_bus.yaml`` depende de que esto funcione.
    """
    c = Circuit("handshake")
    clk = c.clock("CLK1", freq=1000.0)
    cpu = c.part("CPU8", "U1", PROGRAM="01 5A 08 00 09")   # LDA #0x5A; OUT; HLT
    port = c.part("74LS273", "U2")
    c.net("CLK").join(clk.Q, cpu.CLK)
    c.net("STROBE").join(cpu.WSTROBE, port.CP)
    c.net("DBUS", 8).join(cpu.DATA.bits(0, 7), port.D.bits(0, 7))
    c.tie(ONE, cpu["/RESET"], port["/MR"])
    eng = _engine(c)
    eng.run(until=0.03)

    st = eng.model("U1").state
    assert st["a"] == 0x5A and st["halted"]
    # El dato tiene que haber llegado al bus y haber sido capturado, no X.
    assert eng.model("U2").state["q"] == 0x5A, eng.model("U2").state
    assert not eng.problems, eng.problems


# ---------------------------------------------------------------------------
# CPU educativa
# ---------------------------------------------------------------------------


def test_cpu_ejecuta_un_programa():
    """LDA #0x42 / OUT 0 / HLT: el acumulador debe acabar en 0x42."""
    c = Circuit("cpu")
    clk = c.clock("CLK1", freq=1000.0)
    cpu = c.part("CPU8", "U1", PROGRAM="01 42 08 00 09")
    c.net("CLK").join(clk.Q, cpu.CLK)
    c.tie(ONE, cpu["/RESET"])
    eng = _engine(c)
    eng.run(until=0.05)
    st = eng.model("U1").state
    assert st["a"] == 0x42, st
    assert st["halted"], "el programa debía haber terminado con HLT"


def test_cpu_reset_la_devuelve_al_inicio():
    c = Circuit("cpu_rst")
    clk = c.clock("CLK1", freq=1000.0)
    cpu = c.part("CPU8", "U1", PROGRAM="01 07 09")
    c.net("CLK").join(clk.Q, cpu.CLK)
    c.tie(ONE, cpu["/RESET"])
    eng = _engine(c)
    eng.run(until=0.02)
    assert eng.model("U1").state["a"] == 0x07
    eng.model("U1").props["PROGRAM"] = "01 07 09"
    eng.reset()
    assert eng.model("U1").state["pc"] == 0
