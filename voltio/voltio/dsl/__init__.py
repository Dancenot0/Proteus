"""DSL de Voltio: el lenguaje que genera la IA y consume el motor."""
from .loader import (
    DslError,
    TIE,
    dump_file,
    load_dict,
    load_file,
    load_text,
    parse_endpoint,
    to_dict,
)

__all__ = ["load_file", "load_text", "load_dict", "to_dict", "dump_file",
           "parse_endpoint", "DslError", "TIE"]
