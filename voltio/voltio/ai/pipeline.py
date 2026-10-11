"""Pipeline de generación: descripción → YAML → validación → autocorrección.

Aquí está el truco que hace que esto funcione de verdad. Un LLM no genera YAML
de circuitos perfecto a la primera: inventa nombres de pines, olvida atar las
entradas que no usa, conecta dos salidas push-pull al mismo cable. Lo que sí
hace muy bien es **corregir errores concretos que se le muestran**.

Así que el bucle es:

1. Generar con el prompt completo (:mod:`voltio.ai.prompt`).
2. Extraer el bloque YAML de la respuesta (los modelos lo envuelven en prosa).
3. Cargarlo con el validador del DSL.
4. Si hay errores, devolvérselos **textualmente** al modelo y pedirle el YAML
   completo corregido. Repetir hasta N intentos.

Los mensajes del validador están escritos para ser leídos por un modelo, no
por una persona: dicen qué pin falta, sugieren el correcto por similitud, y
explican la regla que se violó. Eso convierte cada ronda de corrección en algo
casi siempre resolutivo.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..core.ir import Circuit, Issue
from ..dsl.loader import DslError, load_text, to_dict
from ..core import miniyaml
from .prompt import repair_prompt, reverse_prompt, system_prompt, user_prompt
from .providers import Message, Provider, ProviderError, get

__all__ = ["Result", "Attempt", "extract_yaml", "generate", "describe"]


@dataclass
class Attempt:
    """Una ronda del bucle de generación."""

    index: int
    raw: str = ""
    yaml: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ok: bool = False
    seconds: float = 0.0
    note: str = ""


@dataclass
class Result:
    """Resultado de :func:`generate`."""

    circuit: Optional[Circuit] = None
    yaml_text: str = ""
    issues: List[Issue] = field(default_factory=list)
    attempts: List[Attempt] = field(default_factory=list)
    ok: bool = False
    provider: str = ""
    model: str = ""
    seconds: float = 0.0
    prose: str = ""

    @property
    def errors(self) -> List[str]:
        return [str(i) for i in self.issues if i.kind == "error"]

    @property
    def warnings(self) -> List[str]:
        return [str(i) for i in self.issues if i.kind in ("warning", "info")]

    def report(self) -> str:
        """Resumen legible de cómo fue la generación."""
        out = [
            f"proveedor : {self.provider}" + (f" ({self.model})" if self.model else ""),
            f"intentos  : {len(self.attempts)}",
            f"tiempo    : {self.seconds:.1f} s",
            f"resultado : {'CIRCUITO VÁLIDO' if self.ok else 'FALLÓ'}",
        ]
        if self.circuit is not None:
            s = self.circuit.stats()
            out.append(f"circuito  : {s['parts']} componentes, {s['nets']} redes, "
                       f"{s['buses']} buses")
        for a in self.attempts:
            mark = "✓" if a.ok else "✗"
            extra = f" — {a.note}" if a.note else ""
            out.append(f"  {mark} intento {a.index}: {len(a.errors)} errores, "
                       f"{len(a.warnings)} avisos, {a.seconds:.1f}s{extra}")
            for e in a.errors[:6]:
                out.append(f"      · {e}")
        if self.issues:
            out.append("diagnóstico final:")
            for i in self.issues:
                out.append(f"  {i}")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Extracción del YAML de una respuesta de LLM
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:ya?ml)?[ \t]*\r?\n(.*?)```", re.S | re.I)


def extract_yaml(text: str) -> str:
    """Saca el documento YAML de una respuesta de modelo.

    Los LLMs rodean el código de prosa, así que hay que buscarlo. Se prueban,
    en este orden:

    1. Bloques ```` ```yaml ```` o ```` ``` ```` — se elige el que **valide**.
    2. Si el modelo devolvió JSON (``{"yaml": "..."}``), se desenvuelve.
    3. Si no hay vallas, se busca la primera línea que empiece por una clave
       conocida del DSL y se toma desde ahí hasta el final.

    Devuelve la cadena tal cual; quien valida es :func:`generate`.
    """
    if not text or not text.strip():
        return ""

    # ¿JSON envolvente?
    t = text.strip()
    if t.startswith("{") and ("yaml" in t or "dsl" in t or "circuit" in t):
        try:
            import json

            obj = json.loads(t)
            for k in ("yaml", "dsl", "circuit", "code", "output"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        except Exception:
            pass

    fences = [m.group(1) for m in _FENCE.finditer(text)]
    if fences:
        # Preferimos el que cargue sin excepción; si ninguno, el más largo.
        best = None
        for cand in fences:
            if not cand.strip():
                continue
            try:
                data = miniyaml.loads(cand)
                if isinstance(data, dict) and ("parts" in data or "nets" in data):
                    return cand
            except Exception:
                pass
            if best is None or len(cand) > len(best):
                best = cand
        if best:
            return best
        return fences[0]

    # Sin vallas: recorta desde la primera clave reconocible.
    keys = ("name:", "title:", "parts:", "nets:", "simulation:", "meta:")
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith(keys) and not ln.strip().startswith("#"):
            start = i
            break
    if start is None:
        return text.strip()
    return "\n".join(lines[start:]).strip()


# ---------------------------------------------------------------------------
# Bucle principal
# ---------------------------------------------------------------------------


def generate(
    description: str,
    provider: Optional[Provider] = None,
    provider_name: str = "clipboard",
    model: Optional[str] = None,
    extra_data: str = "",
    max_attempts: int = 3,
    library=None,
    strict: bool = False,
    on_attempt: Optional[Callable[[Attempt], None]] = None,
    style: str = "digital",
) -> Result:
    """Convierte una descripción en lenguaje natural en un circuito validado.

    ``strict=True`` trata los avisos como errores y fuerza otra ronda de
    corrección; por defecto solo los errores la disparan, porque un "entrada
    sin conectar" en un componente que el diseño no usa no siempre es un fallo.
    """
    prov = provider or get(provider_name, model=model)
    res = Result(provider=getattr(prov, "name", provider_name),
                 model=getattr(prov, "model", "") or "")
    t0 = time.time()

    messages: List[Message] = [
        Message("system", system_prompt(library=library, style=style)),
        Message("user", user_prompt(description, extra_data or None)),
    ]

    last_yaml = ""
    for i in range(1, max_attempts + 1):
        att = Attempt(index=i)
        ta = time.time()
        try:
            raw = prov.complete(messages, temperature=0.2 if i == 1 else 0.0)
        except ProviderError as e:
            att.note = f"fallo del proveedor: {e}"
            att.seconds = time.time() - ta
            res.attempts.append(att)
            if on_attempt:
                on_attempt(att)
            res.seconds = time.time() - t0
            return res

        att.raw = raw
        att.seconds = time.time() - ta
        yaml_text = extract_yaml(raw)
        att.yaml = yaml_text
        last_yaml = yaml_text
        res.prose = re.sub(r"```.*?```", "", raw, flags=re.S).strip()[:2000]

        if not yaml_text.strip():
            att.errors = ["la respuesta no contiene ningún bloque YAML"]
            att.note = "respuesta vacía o sin YAML"
            res.attempts.append(att)
            messages.append(Message("assistant", raw))
            messages.append(Message("user", "No veo ningún YAML en tu respuesta. "
                                             "Devuelve solo el bloque ```yaml completo."))
            if on_attempt:
                on_attempt(att)
            continue

        # Validación.
        try:
            circuit, issues = load_text(yaml_text, library=library)
        except DslError as e:
            att.errors = [str(e)]
            att.note = "el YAML no carga"
            res.attempts.append(att)
            messages.append(Message("assistant", raw))
            messages.append(Message("user", repair_prompt(yaml_text, [str(e)])))
            if on_attempt:
                on_attempt(att)
            continue
        except Exception as e:
            att.errors = [f"{type(e).__name__}: {e}"]
            att.note = "excepción inesperada al cargar"
            res.attempts.append(att)
            messages.append(Message("assistant", raw))
            messages.append(Message("user", repair_prompt(yaml_text, att.errors)))
            if on_attempt:
                on_attempt(att)
            continue

        errs = [str(x) for x in issues if x.kind == "error"]
        warns = [str(x) for x in issues if x.kind in ("warning", "info")]
        att.errors, att.warnings = errs, warns
        res.circuit, res.issues = circuit, issues

        blocking = errs + (warns if strict else [])
        if not blocking:
            att.ok = True
            att.note = "circuito válido" if not warns else f"válido con {len(warns)} avisos"
            res.ok = True
            res.yaml_text = yaml_text
            res.attempts.append(att)
            if on_attempt:
                on_attempt(att)
            break

        att.note = f"{len(blocking)} problemas bloqueantes"
        res.attempts.append(att)
        if on_attempt:
            on_attempt(att)
        messages.append(Message("assistant", raw))
        messages.append(Message("user", repair_prompt(yaml_text, blocking)))

    if not res.ok:
        res.yaml_text = last_yaml
    res.seconds = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Sentido inverso
# ---------------------------------------------------------------------------


def describe(
    circuit: Circuit,
    question: str = "",
    provider: Optional[Provider] = None,
    provider_name: str = "clipboard",
    model: Optional[str] = None,
    library=None,
) -> str:
    """Circuito → explicación en lenguaje natural.

    El sentido inverso del pipeline. Se serializa el circuito al DSL y se le
    pide al modelo que lo explique, que es útil para revisar un esquemático
    heredado o para documentar un diseño.
    """
    prov = provider or get(provider_name, model=model)
    dsl = miniyaml.dumps(to_dict(circuit))
    messages = [
        Message(
            "system",
            "Eres un ingeniero electrónico que explica circuitos con claridad. "
            "Recibes el DSL de Voltio (un YAML de partes y redes) y describes "
            "qué hace el circuito, cómo se organiza en bloques funcionales, "
            "qué papel cumple cada componente y cómo fluyen las señales. "
            "Señala también posibles problemas o mejoras. Responde en prosa, "
            "con encabezados si ayudan, y en el mismo idioma del usuario.\n\n"
            "Referencia del DSL:\n" + DSL_BRIEF,
        ),
        Message("user", reverse_prompt(dsl, question)),
    ]
    return prov.complete(messages, temperature=0.3)


DSL_BRIEF = """
Un circuito Voltio es YAML con `parts` (componentes con `ref`, `part`, `at`,
`props`) y `nets` (cables con `name`, `connect` y opcionalmente `bus: N` o
`tie: 0|1`). Los extremos se escriben `REF.PIN`, `REF.PIN[i]`, `REF.PIN[a:b]`;
una barra inicial (`/MR`) indica activo en baja. `$0`/`$1` atan a nivel fijo.
""".strip()


