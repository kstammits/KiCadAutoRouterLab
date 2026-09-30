"""Pure-Python S-expression reader for KiCad `.kicad_pcb` / `.kicad_sch` files.

KiCad board and schematic files are nested S-expressions, not JSON. This module
tokenizes the text into typed tokens (symbols, strings, numbers) and parses them
into an immutable :class:`SExpr` tree that downstream pipeline stages can walk to
extract pads, nets, vias, zones and the board outline.

No ``pcbnew`` import here: this is the pure-Python half of the ``parse`` stage and
runs in a plain virtualenv for testing.
"""

from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple, Union

Scalar = Union[str, int, float]


class Symbol(str):
    """A bare (unquoted) word token such as ``signal`` or ``no``.

    Subclasses :class:`str` so existing equality checks against plain strings
    keep working; the serializer uses it to re-emit words without quotes.
    Quoted string tokens stay plain :class:`str`.
    """


class SExprError(ValueError):
    """Raised when the input is not well-formed S-expression text."""


Token = namedtuple("Token", "kind value line col")


@dataclass(frozen=True)
class SExpr:
    """A parsed S-expression node.

    ``head`` is the leading bare symbol (e.g. ``footprint``, ``pad``), or ``None``
    for an anonymous list whose first element is not a symbol. ``args`` holds the
    remaining tokens as scalars or nested :class:`SExpr` nodes, in source order.
    Bare words are preserved as :class:`Symbol` (a str subclass) and quoted
    strings as plain :class:`str`, so serialization can restore the original
    quoting.
    """

    head: Optional[str] = None
    args: Tuple[Union[Scalar, "SExpr"], ...] = ()

    def children(self, name: str) -> Iterator["SExpr"]:
        """Yield child nodes whose ``head`` equals ``name``, in source order."""
        for arg in self.args:
            if isinstance(arg, SExpr) and arg.head == name:
                yield arg

    def find(self, name: str) -> Optional["SExpr"]:
        """Return the first child node with ``head`` equal to ``name``, else None."""
        for child in self.children(name):
            return child
        return None


_DELIMS = set(' \t\r\n()"')


def _unescape(ch: str) -> str:
    return {"n": "\n", "t": "\t", "r": "\r"}.get(ch, ch)


def _coerce(word: str) -> Tuple[str, Scalar]:
    """Classify a bare token as a number or a symbol and convert it."""
    body = word[1:] if word[:1] in "+-" else word
    if not body:
        return "symbol", word
    first = body[0]
    if first == "0" and len(body) > 1 and body[1] in "xX":
        hex_digits = body[2:].replace("_", "")
        value = int(hex_digits, 16)
        return "number", -value if word[0] == "-" else value
    if first.isdigit():
        if "." in body or "e" in body.lower():
            return "number", float(word)
        return "number", int(word)
    return "symbol", word


def tokenize(text: str) -> list[Token]:
    """Split S-expression text into a flat list of typed tokens."""
    tokens: list[Token] = []
    i, n = 0, len(text)
    line, col = 1, 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            i += 1
            line += 1
            col = 1
            continue
        if ch in " \t\r":
            i += 1
            col += 1
            continue
        start_line, start_col = line, col
        if ch in "()":
            kind = "open" if ch == "(" else "close"
            tokens.append(Token(kind, ch, start_line, start_col))
            i += 1
            col += 1
            continue
        if ch == '"':
            j = i + 1
            buf: list[str] = []
            closed = False
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    buf.append(_unescape(text[j + 1]))
                    j += 2
                    col += 2
                elif c == '"':
                    closed = True
                    break
                else:
                    if c == "\n":
                        line += 1
                        col = 1
                    else:
                        col += 1
                    buf.append(c)
                    j += 1
            if not closed:
                raise SExprError(f"unterminated string at line {line}, column {col}")
            tokens.append(Token("string", "".join(buf), start_line, start_col))
            i = j + 1
            col += 1
            continue
        j = i
        while j < n and text[j] not in _DELIMS:
            j += 1
        kind, value = _coerce(text[i:j])
        tokens.append(Token(kind, value, start_line, start_col))
        i = j
        col += j - i
    return tokens


def _read_value(tokens: list[Token], i: int) -> Tuple[Union[Scalar, SExpr], int]:
    tok = tokens[i]
    if tok.kind == "symbol":
        return Symbol(tok.value), i + 1
    if tok.kind in ("string", "number"):
        return tok.value, i + 1
    if tok.kind == "open":
        return _parse_list(tokens, i)
    raise SExprError(
        f"unexpected token {tok.value!r} at line {tok.line}, column {tok.col}"
    )


def _parse_list(tokens: list[Token], i: int) -> Tuple[SExpr, int]:
    # tokens[i] is the opening paren.
    i += 1
    if i < len(tokens) and tokens[i].kind == "close":
        return SExpr(None, ()), i + 1

    head: Optional[str] = None
    args: list[Union[Scalar, SExpr]] = []
    first = tokens[i]
    if first.kind == "symbol":
        head = first.value
        i += 1
    else:
        value, i = _read_value(tokens, i)
        args.append(value)

    while True:
        tok = tokens[i]
        if tok.kind == "close":
            return SExpr(head, tuple(args)), i + 1
        value, i = _read_value(tokens, i)
        args.append(value)


def parse(text: str) -> SExpr:
    """Parse a single top-level S-expression list from ``text``."""
    tokens = tokenize(text)
    if not tokens or tokens[0].kind != "open":
        raise SExprError("expected a top-level list starting with '('")
    node, i = _parse_list(tokens, 0)
    if i != len(tokens):
        tok = tokens[i]
        raise SExprError(
            f"trailing content after root list at line {tok.line}, column {tok.col}"
        )
    return node


def parse_file(path: Union[str, Path]) -> SExpr:
    """Read and parse a KiCad file from disk."""
    return parse(Path(path).read_text(encoding="utf-8"))


def _format_scalar(value: Scalar) -> str:
    """Render a scalar token back to S-expression text (strings quoted/escaped)."""
    if isinstance(value, Symbol):
        return value  # bare word; the tokenizer re-reads it identically
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    if isinstance(value, float):
        return repr(value)
    return str(value)


def to_sexpr(node: SExpr, indent: str = "\t") -> str:
    """Serialize an :class:`SExpr` tree back into KiCad-style S-expression text.

    Lists whose arguments are all scalars stay on one line; lists containing
    nested lists break after the leading scalar arguments and indent each
    remaining argument by one level, mirroring KiCad's own layout. Whitespace
    is not significant, so a parse→serialize→parse round trip preserves the
    tree exactly (hex masks like ``0x…`` re-emit as decimal integers).
    """

    def fmt(n: SExpr, depth: int) -> str:
        pad = indent * depth
        inner = indent * (depth + 1)
        head = n.head if n.head is not None else ""
        first_nested = next(
            (i for i, a in enumerate(n.args) if isinstance(a, SExpr)), None
        )
        if first_nested is None:
            parts = ([head] if head else []) + [
                _format_scalar(a) for a in n.args if not isinstance(a, SExpr)
            ]
            return "(" + " ".join(parts) + ")"
        lead = [head] if head else []
        for a in n.args[:first_nested]:
            assert not isinstance(a, SExpr)
            lead.append(_format_scalar(a))
        lines = ["(" + " ".join(lead)]
        for arg in n.args[first_nested:]:
            if isinstance(arg, SExpr):
                lines.append(inner + fmt(arg, depth + 1))
            else:
                lines.append(inner + _format_scalar(arg))
        lines.append(pad + ")")
        return "\n".join(lines)

    return fmt(node, 0)
