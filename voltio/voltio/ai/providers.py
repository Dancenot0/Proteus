"""Proveedores de IA intercambiables.

Todos implementan la misma interfaz mínima —un método ``complete``— y se eligen
por nombre. Cinco rutas, para cinco situaciones distintas:

===============  =========================================================
``openai``       API de OpenAI. Necesita ``OPENAI_API_KEY``.
``anthropic``    API de Anthropic. Necesita ``ANTHROPIC_API_KEY``.
``gemini``       API de Google. Necesita ``GEMINI_API_KEY``.
``ollama``       Modelo local. Sin claves ni coste, pero menos fiable con
                 DSLs estructurados.
``clipboard``    **Sin API ninguna.** Te imprime el prompt completo, lo pegas
                 en ChatGPT/Claude/Gemini en el navegador, y pegas la
                 respuesta de vuelta. Funciona con la cuenta gratuita y es
                 la ruta que menos falla.
===============  =========================================================

Las llamadas HTTP se hacen con :mod:`urllib` de la biblioteca estándar, así
que **no hay que instalar ningún SDK**. Si tienes ``openai`` o ``anthropic``
instalados y prefieres usarlos, el conector lo detecta y los aprovecha.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "Provider",
    "ProviderError",
    "get",
    "available",
    "names",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    "ClipboardProvider",
]


class ProviderError(RuntimeError):
    """Fallo del proveedor (red, clave ausente, cuota, respuesta inválida)."""


@dataclass
class Message:
    role: str  # system | user | assistant
    content: str


class Provider:
    """Interfaz común."""

    name = "base"
    needs_key = True
    env_key = ""

    def complete(
        self,
        messages: Sequence[Message],
        temperature: float = 0.2,
        max_tokens: int = 8000,
    ) -> str:
        raise NotImplementedError

    # -- utilidades compartidas -------------------------------------------
    def _key(self) -> str:
        key = os.environ.get(self.env_key, "").strip()
        if not key:
            raise ProviderError(
                f"falta la variable de entorno {self.env_key}. "
                f"Expórtala (export {self.env_key}=...) o usa --provider clipboard, "
                f"que no necesita ninguna clave."
            )
        return key

    @staticmethod
    def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str],
              timeout: int = 180) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise ProviderError(f"HTTP {e.code} de {url}: {detail}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"no se pudo conectar con {url}: {e.reason}") from e

    def available_reason(self) -> Optional[str]:
        """``None`` si está listo para usarse; si no, el motivo."""
        if self.needs_key and not os.environ.get(self.env_key, "").strip():
            return f"falta {self.env_key}"
        return None


# ---------------------------------------------------------------------------


class OpenAIProvider(Provider):
    name = "openai"
    env_key = "OPENAI_API_KEY"

    def __init__(self, model: str = "gpt-4o-mini", base_url: Optional[str] = None):
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")

    def complete(self, messages, temperature=0.2, max_tokens=8000) -> str:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        data = self._post(
            f"{self.base_url}/chat/completions",
            payload,
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key()}",
            },
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"respuesta inesperada de OpenAI: {str(data)[:400]}") from e


class AnthropicProvider(Provider):
    name = "anthropic"
    env_key = "ANTHROPIC_API_KEY"

    def __init__(self, model: str = "claude-sonnet-4-5"):
        self.model = model

    def complete(self, messages, temperature=0.2, max_tokens=8000) -> str:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        rest = [{"role": m.role, "content": m.content}
                for m in messages if m.role != "system"]
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": rest,
        }
        data = self._post(
            "https://api.anthropic.com/v1/messages",
            payload,
            {
                "Content-Type": "application/json",
                "x-api-key": self._key(),
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            return "".join(b.get("text", "") for b in data["content"])
        except (KeyError, TypeError) as e:
            raise ProviderError(f"respuesta inesperada de Anthropic: {str(data)[:400]}") from e


class GeminiProvider(Provider):
    name = "gemini"
    env_key = "GEMINI_API_KEY"

    def __init__(self, model: str = "gemini-2.0-flash"):
        self.model = model

    def complete(self, messages, temperature=0.2, max_tokens=8000) -> str:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        contents = []
        for m in messages:
            if m.role == "system":
                continue
            contents.append({
                "role": "user" if m.role != "assistant" else "model",
                "parts": [{"text": m.content}],
            })
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        data = self._post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            payload,
            {"Content-Type": "application/json", "x-goog-api-key": self._key()},
        )
        try:
            return "".join(
                p.get("text", "")
                for c in data["candidates"]
                for p in c["content"]["parts"]
            )
        except (KeyError, TypeError) as e:
            raise ProviderError(f"respuesta inesperada de Gemini: {str(data)[:400]}") from e


class OllamaProvider(Provider):
    """Modelo local vía Ollama. Sin claves ni coste."""

    name = "ollama"
    needs_key = False

    def __init__(self, model: str = "llama3.1", host: Optional[str] = None):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")

    def complete(self, messages, temperature=0.2, max_tokens=8000) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        data = self._post(f"{self.host}/api/chat", payload,
                          {"Content-Type": "application/json"}, timeout=600)
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as e:
            raise ProviderError(
                f"respuesta inesperada de Ollama: {str(data)[:300]}. "
                f"¿Está el modelo '{self.model}' descargado? (ollama pull {self.model})"
            ) from e

    def available_reason(self) -> Optional[str]:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=3) as r:
                json.loads(r.read())
            return None
        except Exception as e:
            return f"Ollama no responde en {self.host} ({type(e).__name__})"


class ClipboardProvider(Provider):
    """La ruta sin API: imprimir el prompt y leer la respuesta.

    Por qué existe: es la que **siempre** funciona. No necesita claves, no
    depende de cuotas ni de red, y deja usar el modelo que tengas abierto en el
    navegador. El coste es un copiar/pegar manual.
    """

    name = "clipboard"
    needs_key = False

    def __init__(self, prompt_path: str = "prompt.txt", answer_path: str = "respuesta.txt",
                 quiet: bool = False):
        self.prompt_path = prompt_path
        self.answer_path = answer_path
        self.quiet = quiet

    def complete(self, messages, temperature=0.2, max_tokens=8000) -> str:
        text = "\n\n".join(
            f"===== {m.role.upper()} =====\n{m.content}" for m in messages
        )
        with open(self.prompt_path, "w", encoding="utf-8") as f:
            f.write(text)

        if not self.quiet:
            self._print_instructions(text)

        # Si ya hay una respuesta escrita, úsala (modo no interactivo).
        if os.path.exists(self.answer_path):
            with open(self.answer_path, "r", encoding="utf-8") as f:
                ans = f.read().strip()
            if ans:
                return ans

        if not sys.stdin.isatty():
            raise ProviderError(
                f"el proveedor 'clipboard' necesita interacción. Escribe la "
                f"respuesta del modelo en {self.answer_path} y vuelve a ejecutar, "
                f"o usa --provider openai|anthropic|gemini|ollama."
            )

        print("\n" + "=" * 72)
        print("Pega aquí la respuesta del modelo y termina con una línea que")
        print("contenga solo: FIN")
        print("=" * 72)
        lines: List[str] = []
        for line in sys.stdin:
            if line.strip() in ("FIN", "fin", "EOF", ":wq"):
                break
            lines.append(line)
        answer = "".join(lines).strip()
        if not answer:
            raise ProviderError("respuesta vacía")
        return answer

    def _print_instructions(self, text: str) -> None:
        bar = "=" * 72
        print(bar)
        print("PROVEEDOR CLIPBOARD — no se llama a ninguna API")
        print(bar)
        print(f"1. El prompt completo se guardó en: {self.prompt_path}")
        print(f"   ({len(text):,} caracteres)")
        print("2. Ábrelo, cópialo entero y pégalo en el chat del modelo que")
        print("   prefieras (ChatGPT, Claude, Gemini...).")
        print(f"3. Copia la respuesta y guárdala en: {self.answer_path}")
        print("   — o pégala por consola cuando se te pida.")
        print(bar)
        print("Primeros 800 caracteres del prompt:\n")
        print(text[:800])
        print("\n[... recortado; el archivo tiene el resto]\n")


# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, type] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
    "clipboard": ClipboardProvider,
    "copiar": ClipboardProvider,
    "local": OllamaProvider,
}


def names() -> List[str]:
    return ["openai", "anthropic", "gemini", "ollama", "clipboard"]


def get(name: str = "clipboard", model: Optional[str] = None, **kw) -> Provider:
    """Instancia un proveedor por nombre."""
    key = (name or "clipboard").lower().strip()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise ProviderError(
            f"proveedor desconocido '{name}'. Disponibles: {', '.join(names())}"
        )
    if model and cls not in (ClipboardProvider,):
        return cls(model=model, **kw)
    return cls(**kw)


def available() -> Dict[str, Optional[str]]:
    """Estado de cada proveedor: ``None`` si está listo, o el motivo por el que no."""
    out: Dict[str, Optional[str]] = {}
    for n in names():
        try:
            out[n] = get(n).available_reason()
        except Exception as e:  # pragma: no cover
            out[n] = f"{type(e).__name__}: {e}"
    return out


def pick_auto(prefer: Sequence[str] = ("anthropic", "openai", "gemini", "ollama",
                                       "clipboard")) -> Provider:
    """Elige el primer proveedor que esté realmente disponible."""
    for n in prefer:
        try:
            p = get(n)
        except ProviderError:
            continue
        if p.available_reason() is None:
            return p
    return ClipboardProvider()
