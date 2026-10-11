"""Parser YAML minimalista, sin dependencias.

Existe por una sola razón: el núcleo de Voltio debe poder ejecutarse en un
Python pelado, sin ``pip install``. Si **PyYAML** está instalado se usa ese
(es más correcto); si no, entra este.

Cubre exactamente el subconjunto que produce el DSL de Voltio y los LLMs:

* mapas y secuencias por indentación
* secuencias en línea ``[a, b, c]`` y mapas en línea ``{a: 1}``
* escalares: enteros, flotantes, ``true/false/null``, cadenas con o sin comillas
* comentarios ``#`` y documentos ``---``
* claves compuestas tipo ``U1.A[3]``

**No** cubre (y no hace falta): anclas, aliases, tags personalizados,
escalares multi-línea con ``|``/``>`` multi-bloque complejos, sets.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["load", "loads", "dump", "dumps", "YamlError"]


class YamlError(ValueError):
    """Error de sintaxis, con número de línea para que se pueda corregir."""

    def __init__(self, msg: str, line: int = 0, text: str = ""):
        self.line = line
        self.text = text
        super().__init__(f"línea {line}: {msg}" + (f"\n  | {text}" if text else ""))


# ---------------------------------------------------------------------------
# Escalares
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_HEX_RE = re.compile(r"^0[xX][0-9a-fA-F_]+$")
_BIN_RE = re.compile(r"^0[bB][01_]+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_QUOTED_RE = re.compile(r'^(["\'])(.*)\1$', re.S)


def _parse_scalar(tok: str) -> Any:
    """Convierte un trozo de texto en el valor Python que representa."""
    s = tok.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None

    m = _QUOTED_RE.match(s)
    if m:
        body = m.group(2)
        if m.group(1) == '"':
            return (
                body.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        return body  # comillas simples: literal, sin escapes

    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False

    if _HEX_RE.match(s):
        return int(s.replace("_", ""), 16)
    if _BIN_RE.match(s):
        return int(s[2:].replace("_", ""), 2)
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s) and not _INT_RE.match(s):
        return float(s)
    # Unidades de ingeniería: 10k, 4.7u, 100n, 1M, 10ms
    m = re.match(r"^([+-]?\d*\.?\d+)\s*([a-zA-Zµ]+)$", s)
    if m:
        return _with_unit(m.group(1), m.group(2))
    return s


_UNITS = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "meg": 1e6, "M": 1e6, "g": 1e9, "G": 1e9,
    "hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9,
    "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9, "ps": 1e-12,
    "v": 1.0, "mv": 1e-3, "uv": 1e-6, "kv": 1e3,
    "a": 1.0, "ma": 1e-3, "ua": 1e-6,
    "r": 1.0, "ohm": 1.0, "kohm": 1e3, "megohm": 1e6,
    "farad": 1.0, "fara": 1.0,
}


def _with_unit(num: str, unit: str) -> Any:
    """``10k → 10000``, ``100ns → 1e-7``. Si la unidad es desconocida, texto."""
    key = unit.strip()
    low = key.lower()
    if key in _UNITS:
        mult = _UNITS[key]
    elif low in _UNITS:
        mult = _UNITS[low]
    else:
        return num + key
    val = float(num) * mult
    return int(val) if val.is_integer() and abs(val) < 1e15 and "." not in num else val


# ---------------------------------------------------------------------------
# Análisis de líneas
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Quita el comentario ``#`` respetando comillas."""
    out, quote = [], None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in "\"'":
                quote = ch
                out.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _split_key(line: str) -> Tuple[Optional[str], str]:
    """Divide ``clave: resto`` respetando comillas, corchetes y llaves.

    Devuelve ``(None, line)`` si la línea no es un mapeo.
    """
    depth_sq = depth_cu = 0
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "[":
            depth_sq += 1
        elif ch == "]":
            depth_sq -= 1
        elif ch == "{":
            depth_cu += 1
        elif ch == "}":
            depth_cu -= 1
        elif ch == ":" and depth_sq == 0 and depth_cu == 0:
            if i + 1 == len(line) or line[i + 1] in " \t":
                return line[:i].strip(), line[i + 1:].strip()
    return None, line


def _split_flow(body: str) -> List[str]:
    """Divide ``a, b, [c, d]`` respetando anidamiento y comillas."""
    parts, cur = [], []
    depth = 0
    quote = None
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "[{":
            depth += 1
            cur.append(ch)
        elif ch in "]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip() != ""]


def _parse_flow(text: str, line_no: int = 0) -> Any:
    """Parsea ``[...]`` o ``{...}`` en línea."""
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_flow(p, line_no) if p.strip()[:1] in "[{" else _parse_scalar(p)
                for p in _split_flow(inner)]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        out: Dict[str, Any] = {}
        if not inner:
            return out
        for item in _split_flow(inner):
            k, v = _split_key(item)
            if k is None:
                raise YamlError(f"elemento de mapa inválido: {item!r}", line_no, item)
            out[_unquote_key(k)] = (
                _parse_flow(v, line_no) if v.strip()[:1] in "[{" else _parse_scalar(v)
            )
        return out
    return _parse_scalar(text)


def _unquote_key(k: str) -> str:
    k = k.strip()
    m = _QUOTED_RE.match(k)
    return m.group(2) if m else k


# ---------------------------------------------------------------------------
# Parser por indentación
# ---------------------------------------------------------------------------


class _Line:
    __slots__ = ("indent", "text", "no")

    def __init__(self, indent: int, text: str, no: int):
        self.indent, self.text, self.no = indent, text, no


def _tokenize(src: str) -> List[_Line]:
    src = _extract_block_scalars(src)
    out: List[_Line] = []
    for i, raw in enumerate(src.splitlines(), 1):
        if raw.lstrip().startswith("---") or raw.lstrip().startswith("..."):
            continue
        line = _strip_comment(raw.replace("\t", "    "))
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        out.append(_Line(indent, line.strip(), i))
    return out


# -- escalares de bloque (| y >) -------------------------------------------
#
# Los LLMs los generan sin parar en los campos de descripción, así que hay que
# soportarlos. Se resuelven en una pasada previa sobre el texto crudo: se
# localiza la cabecera, se recogen las líneas más indentadas que la sigan, y se
# sustituye todo por una cadena entrecomillada equivalente.

_BLOCK_HDR = re.compile(r"([|>])([+-]?)(\d*)\s*$")


def _extract_block_scalars(src: str) -> str:
    lines = src.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        no_tab = raw.replace("\t", "    ")
        # Solo tratamos la línea como cabecera si, tras quitar un posible
        # comentario, sigue terminando en el indicador de bloque. Así un '#'
        # dentro de un valor normal no confunde al detector.
        candidates = [no_tab]
        sc = _strip_comment(no_tab)
        if sc != no_tab:
            candidates.append(sc)
        stripped = None
        m = None
        for cand in candidates:
            body = cand.strip()
            if not body:
                continue
            mm = _BLOCK_HDR.search(body)
            if mm is not None:
                stripped, m = cand, mm
                break
        if m is None:
            out.append(raw)
            i += 1
            continue

        style = m.group(1)
        chomp = m.group(2)
        explicit = m.group(3)
        header_indent = len(stripped) - len(stripped.lstrip(" "))

        # Recolecta el cuerpo: líneas en blanco o más indentadas que la cabecera.
        j = i + 1
        collected: List[str] = []
        content_indent: Optional[int] = None
        while j < n:
            ln = lines[j].replace("\t", "    ")
            if not ln.strip():
                collected.append("")
                j += 1
                continue
            ind = len(ln) - len(ln.lstrip(" "))
            if ind <= header_indent:
                break
            if content_indent is None:
                content_indent = ind
            collected.append(ln[min(content_indent, ind):])
            j += 1

        # Quita las líneas en blanco del final según el modificador de chomping.
        if chomp == "-":
            while collected and not collected[-1]:
                collected.pop()
        else:
            while len(collected) > 1 and not collected[-1]:
                collected.pop()

        if explicit:
            # Indentación explícita relativa a la cabecera.
            content_indent = header_indent + int(explicit)

        if style == "|":
            text = "\n".join(collected)
        else:
            text = _fold(collected)

        if chomp == "-":
            text = text.rstrip("\n")
        else:
            text = text.rstrip("\n") + "\n" if text.strip() else ""

        # ``m.start(1)`` apunta al indicador de bloque, es decir, YA está
        # después de los dos puntos de la clave. Recortamos los modificadores
        # de chomping/indentación y devolvemos los dos puntos explícitamente.
        cut = m.start(1)
        prefix = stripped[:cut].rstrip(" \t+-0123456789")
        if not prefix.endswith(":"):
            prefix += ":"
        out.append(f"{prefix} {_yaml_quote(text)}")
        i = j
    return "\n".join(out)


def _fold(collected: List[str]) -> str:
    """Plegado estilo ``>``: los saltos de línea simples se vuelven espacios.

    Una línea en blanco produce un salto de línea real; una línea más indentada
    que la primera se conserva literal (regla estándar de YAML).
    """
    out: List[str] = []
    for k, ln in enumerate(collected):
        if not ln.strip():
            out.append("\n")
            continue
        if out and out[-1] not in ("\n", "") and not out[-1].endswith("\n"):
            out.append(" ")
        out.append(ln.strip())
    text = "".join(out)
    return re.sub(r"\n+", "\n", text).strip("\n")


def _yaml_quote(s: str) -> str:
    """Convierte texto arbitrario en un escalar YAML entrecomillado y seguro."""
    esc = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{esc}"'


def _is_seq_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _parse_block(lines: List[_Line], pos: int, indent: int) -> Tuple[Any, int]:
    """Parsea un bloque (mapa o secuencia) que empieza en ``lines[pos]``."""
    if pos >= len(lines) or lines[pos].indent < indent:
        return None, pos
    if _is_seq_item(lines[pos].text):
        return _parse_seq(lines, pos, lines[pos].indent)
    return _parse_map(lines, pos, lines[pos].indent)


def _value_after(text: str, lines: List[_Line], pos: int, owner_indent: int,
                 line_no: int) -> Tuple[Any, int]:
    """Resuelve el valor que sigue a una clave o a un guion de secuencia.

    ``owner_indent`` es la indentación del token dueño (la clave o el ``-``).
    Devuelve ``(valor, nueva_posición)``.
    """
    t = text.strip()
    if t:
        if t[:1] in "[{":
            return _parse_flow(t, line_no), pos
        return _parse_scalar(t), pos

    if pos >= len(lines):
        return None, pos
    nxt = lines[pos]

    # Caso A: bloque hijo más indentado.
    if nxt.indent > owner_indent:
        return _parse_block(lines, pos, nxt.indent)

    # Caso B: YAML permite que una secuencia cuelgue de una clave AL MISMO nivel
    # de indentación que la clave.  key:\n  - a\n  - b   (ambos a columna igual)
    if nxt.indent == owner_indent and _is_seq_item(nxt.text):
        return _parse_seq(lines, pos, nxt.indent)

    return None, pos


def _parse_seq(lines: List[_Line], pos: int, indent: int) -> Tuple[List[Any], int]:
    out: List[Any] = []
    while pos < len(lines):
        ln = lines[pos]
        if ln.indent != indent or not _is_seq_item(ln.text):
            if ln.indent > indent:
                raise YamlError("indentación inesperada en una secuencia", ln.no, ln.text)
            break

        rest = ln.text[1:]
        # Columna donde empieza el contenido del ítem (tras "- ").
        content_col = ln.indent + 1 + (len(rest) - len(rest.lstrip(" ")))
        rest = rest.strip()
        pos += 1

        if rest == "":
            val, pos = _value_after("", lines, pos, indent, ln.no)
            out.append(val)
            continue

        k, v = _split_key(rest)
        if k is not None and rest[:1] not in "[{":
            # El ítem es un mapa cuya primera clave va en esta misma línea.
            # Las claves hermanas se reconocen por su columna == content_col.
            sub: Dict[str, Any] = {}
            val, pos = _value_after(v, lines, pos, content_col - 1, ln.no)
            sub[_unquote_key(k)] = val
            while pos < len(lines):
                cur = lines[pos]
                if cur.indent != content_col or _is_seq_item(cur.text):
                    break
                sk, sv = _split_key(cur.text)
                if sk is None:
                    raise YamlError(
                        f"se esperaba 'clave:' y se encontró {cur.text!r}",
                        cur.no, cur.text,
                    )
                pos += 1
                val, pos = _value_after(sv, lines, pos, content_col - 1, cur.no)
                sub[_unquote_key(sk)] = val
            out.append(sub)
            continue

        if rest[:1] in "[{":
            out.append(_parse_flow(rest, ln.no))
        else:
            out.append(_parse_scalar(rest))
    return out, pos


def _parse_map(lines: List[_Line], pos: int, indent: int) -> Tuple[Dict[str, Any], int]:
    out: Dict[str, Any] = {}
    while pos < len(lines):
        ln = lines[pos]
        if ln.indent != indent:
            if ln.indent > indent:
                raise YamlError(
                    "indentación inesperada (¿falta la clave padre?)", ln.no, ln.text
                )
            break
        if _is_seq_item(ln.text):
            break
        k, v = _split_key(ln.text)
        if k is None:
            raise YamlError(
                f"se esperaba 'clave:' y se encontró {ln.text!r}", ln.no, ln.text
            )
        pos += 1
        val, pos = _value_after(v, lines, pos, indent, ln.no)
        key = _unquote_key(k)
        if key in out:
            raise YamlError(f"clave duplicada: {key!r}", ln.no, ln.text)
        out[key] = val
    return out, pos


try:  # PyYAML es preferible si está; si no, usamos el parser propio.
    import yaml as _pyyaml  # type: ignore
except Exception:  # pragma: no cover - depende del entorno
    _pyyaml = None


def loads(src: str) -> Any:
    """Parsea una cadena YAML (subconjunto) y devuelve estructuras Python.

    Usa PyYAML si está disponible; en caso contrario el parser incorporado.
    Para forzar el parser propio: ``miniyaml.loads(src, native=True)``.
    """
    return _loads(src, native=False)


def _loads(src: str, native: bool = False) -> Any:
    if _pyyaml is not None and not native:
        return _pyyaml.safe_load(src)
    lines = _tokenize(src)
    if not lines:
        return {}
    val, pos = _parse_block(lines, 0, lines[0].indent)
    if pos < len(lines):
        ln = lines[pos]
        raise YamlError("contenido sobrante sin parsear", ln.no, ln.text)
    return val


def load(path, native: bool = False) -> Any:
    """Parsea un archivo YAML."""
    with open(path, "r", encoding="utf-8") as f:
        return _loads(f.read(), native=native)


# ---------------------------------------------------------------------------
# Serializador (para que el motor pueda ESCRIBIR DSL, p. ej. en el sentido inverso)
# ---------------------------------------------------------------------------


def _needs_quotes(s: str) -> bool:
    if s == "":
        return True
    if any(ch in s for ch in "\n\r\t"):
        return True
    if s.strip() != s:
        return True
    if s[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True
    if ": " in s or s.endswith(":"):
        return True
    if s.lower() in ("true", "false", "yes", "no", "null", "on", "off", "~"):
        return True
    if _INT_RE.match(s) or _FLOAT_RE.match(s):
        return True
    return False


def _fmt_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    s = str(v)
    # Un salto de línea dentro de un valor OBLIGA a entrecomillar y escapar:
    # si se vuelca tal cual, el texto se parte en varias líneas físicas y el
    # archivo resultante ya no se puede volver a leer. Eso rompe el round-trip
    # (circuito → DSL → circuito) en cuanto una descripción tiene más de un
    # párrafo, que es lo normal.
    if "\n" in s or "\r" in s or "\t" in s or _needs_quotes(s):
        return _yaml_quote(s)
    return s


def dumps(obj: Any, indent: int = 0, sort_keys: bool = False) -> str:
    """Serializa a YAML legible (subconjunto)."""
    pad = "  " * indent
    out: List[str] = []
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n" if indent == 0 else "{}\n"
        keys = sorted(obj) if sort_keys else list(obj)
        for k in keys:
            v = obj[k]
            ks = _fmt_scalar(k) if not isinstance(k, str) or _needs_quotes(k) else k
            if isinstance(v, dict) and v:
                out.append(f"{pad}{ks}:")
                out.append(dumps(v, indent + 1, sort_keys).rstrip("\n"))
            elif isinstance(v, (list, tuple)) and v:
                out.append(f"{pad}{ks}:")
                out.append(_dump_seq(list(v), indent, sort_keys).rstrip("\n"))
            elif isinstance(v, dict):
                out.append(f"{pad}{ks}: {{}}")
            elif isinstance(v, (list, tuple)):
                out.append(f"{pad}{ks}: []")
            else:
                out.append(f"{pad}{ks}: {_fmt_scalar(v)}")
    elif isinstance(obj, (list, tuple)):
        out.append(_dump_seq(list(obj), indent, sort_keys).rstrip("\n"))
    else:
        out.append(pad + _fmt_scalar(obj))
    return "\n".join(out) + "\n"


def _dump_seq(items: List[Any], indent: int, sort_keys: bool) -> str:
    pad = "  " * indent
    out: List[str] = []
    for it in items:
        if isinstance(it, dict) and it:
            body = dumps(it, indent + 1, sort_keys).rstrip("\n").split("\n")
            first = body[0].strip()
            out.append(f"{pad}- {first}")
            out.extend(body[1:])
        elif isinstance(it, (list, tuple)) and it:
            sub = _dump_seq(list(it), indent + 1, sort_keys).rstrip("\n").split("\n")
            out.append(f"{pad}- {sub[0].strip()}")
            out.extend(sub[1:])
        else:
            out.append(f"{pad}- {_fmt_scalar(it) if not isinstance(it, (list, dict)) else dumps(it, 0, sort_keys).strip()}")
    return "\n".join(out) + "\n"


def dump(obj: Any, path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(obj))
