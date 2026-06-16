"""openfde/language_packs/js_ts_treesitter.py — OPTIONAL tree-sitter adapter for JS/TS
architecture extraction (Plugin/Lang L1-D).

A precise-parser path BEHIND the regex JS/TS assimilation in ``openfde.architect``. When the
optional ``tree-sitter`` + ``tree-sitter-javascript`` + ``tree-sitter-typescript`` packages are
installed, :func:`extract` returns the same per-file facts the regex extractor produces — top-level
functions / arrows / function-expressions, classes + their methods and field-arrows, named
object-literal methods (``obj.method``), and import specifiers — parsed from a real AST, so JSX,
one-line class bodies, regex literals, and deep TS no longer fool a regex. If the packages are
missing, or a parse errors, it returns ``None`` and the caller uses the existing regex path. NOTHING
here is required to run OpenFDE.

LAZY + SAFE: tree-sitter is imported only inside :func:`_grammars` (cached), so importing THIS module
(or ``openfde.architect`` / ``openfde.language_packs``) imports no tree-sitter and stays cheap. Every
failure — missing package, ABI mismatch, parse error, unexpected AST — degrades to ``None`` (regex).

Boundary: this slice is PARSER QUALITY for the ArchGraph symbol + import extraction. Function-level
flows, the failure-flow resolver (``js_call_context``), and HTML→JS entrypoint mapping stay on their
existing regex paths; deep TS type graphs and big-repo scoped assimilation remain Next/Deferred.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

logger = logging.getLogger("openfde.architect")

_OBJ_METHOD_CAP = 16                       # mirror the regex obj-method cap
_METHOD_SKIP = frozenset({"constructor"})  # mirror the regex method skip-list (the relevant entry)

# Grammar key by file extension. Unknown → TypeScript (a superset that still parses plain JS).
_GRAMMAR_BY_EXT = {
    ".ts": "ts", ".mts": "ts", ".cts": "ts",
    ".tsx": "tsx",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
}

# AST node types (tree-sitter-javascript / -typescript).
_FUNC_DECL = {"function_declaration", "generator_function_declaration"}
_FUNC_VALUE = {"arrow_function", "function", "function_expression",
               "generator_function", "generator_function_declaration"}
_DECL_TYPES = _FUNC_DECL | {"class_declaration"} | {"lexical_declaration", "variable_declaration"}


@lru_cache(maxsize=1)
def _grammars():
    """Load + cache the JS/TS tree-sitter languages, or ``None`` if unavailable. Imported lazily so
    this module stays import-cheap and OpenFDE never requires tree-sitter."""
    try:
        import tree_sitter as ts
        import tree_sitter_javascript as tsj
        import tree_sitter_typescript as tst
        return {
            "js":  ts.Language(tsj.language()),
            "ts":  ts.Language(tst.language_typescript()),
            "tsx": ts.Language(tst.language_tsx()),
        }
    except Exception as exc:  # noqa: BLE001 — optional dep / ABI mismatch → regex fallback
        logger.debug("tree-sitter unavailable; JS/TS uses the regex path: %s", exc)
        return None


def available() -> bool:
    """True when the optional tree-sitter JS/TS grammars can be loaded (cheap, cached)."""
    return _grammars() is not None


def _parser_for(rel_path: str):
    grams = _grammars()
    if grams is None:
        return None
    lang = grams.get(_GRAMMAR_BY_EXT.get(os.path.splitext(rel_path)[1].lower(), "ts"))
    if lang is None:
        return None
    import tree_sitter as ts
    return ts.Parser(lang)


def extract(source: str, rel_path: str):
    """Tree-sitter facts for ONE JS/TS file, or ``None`` to signal the regex fallback.

    Returns ``{"functions": [(qualified_name, line_1based), ...], "imports": [specifier, ...],
    "parser": "tree-sitter"}`` — functions mirror the regex extractor (top-level declarations /
    arrows / function-expressions, ``Class.method``, ``Class.fieldArrow``, named-object
    ``obj.method``). ``None`` on any failure (no grammars / parse error / unexpected tree)."""
    try:
        parser = _parser_for(rel_path)
        if parser is None:
            return None
        data = source.encode("utf-8", "replace")
        root = parser.parse(data).root_node
        if root is None or root.type != "program" or root.has_error:
            return None                         # syntax error → the forgiving regex path handles it
        return {"functions": _functions(root, data),
                "imports": _imports(root, data),
                "parser": "tree-sitter"}
    except Exception as exc:  # noqa: BLE001 — any parse/walk surprise → regex fallback
        logger.debug("tree-sitter parse failed for %s; using regex: %s", rel_path, exc)
        return None


# ── tree walking ─────────────────────────────────────────────────────────────── #

def _text(node, data) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1          # 1-based, like the regex extractor


def _name(node, data):
    n = node.child_by_field_name("name")
    return _text(n, data) if n is not None else None


def _prop_name(node, data):
    """Property name for an object member / class member — field 'name'/'key', else the first
    property_identifier child (class field_definition exposes no 'name' field)."""
    for field in ("name", "key"):
        n = node.child_by_field_name(field)
        if n is not None:
            return _text(n, data)
    for c in node.named_children:
        if c.type in ("property_identifier", "identifier"):
            return _text(c, data)
    return None


def _functions(root, data) -> list:
    out, seen = [], set()

    def emit(qualified, line):
        if qualified and qualified not in seen:
            seen.add(qualified)
            out.append((qualified, line))

    for node in root.named_children:
        decl = node
        if node.type == "export_statement":     # unwrap `export <decl>` / `export default <decl>`
            decl = node.child_by_field_name("declaration") or next(
                (c for c in node.named_children if c.type in _DECL_TYPES), None)
            if decl is None:
                continue                          # `export { … }`, `export default <expr>` → nothing
        _emit_decl(decl, data, emit)
    return out


def _emit_decl(decl, data, emit) -> None:
    t = decl.type
    if t in _FUNC_DECL:
        emit(_name(decl, data), _line(decl))
    elif t == "class_declaration":
        cls = _name(decl, data)
        emit(cls, _line(decl))
        if cls:
            _emit_class_methods(decl, cls, data, emit)
    elif t in ("lexical_declaration", "variable_declaration"):
        for d in decl.named_children:
            if d.type != "variable_declarator":
                continue
            name = _name(d, data)
            value = d.child_by_field_name("value")
            if not name or value is None:
                continue
            if value.type in _FUNC_VALUE:
                emit(name, _line(d))
            elif value.type == "object":
                _emit_object_methods(value, name, data, emit)


def _emit_class_methods(class_decl, cls, data, emit) -> None:
    body = class_decl.child_by_field_name("body")
    if body is None:
        return
    seen = set()
    for member in body.named_children:
        name = None
        if member.type == "method_definition":
            name = _prop_name(member, data)
        elif member.type in ("public_field_definition", "field_definition"):
            value = member.child_by_field_name("value")
            if value is not None and value.type in _FUNC_VALUE:
                name = _prop_name(member, data)
        if name and name not in _METHOD_SKIP and name not in seen:
            seen.add(name)
            emit(f"{cls}.{name}", _line(member))


def _emit_object_methods(obj, owner, data, emit) -> None:
    seen, count = set(), 0
    for prop in obj.named_children:
        name = None
        if prop.type == "method_definition":
            name = _prop_name(prop, data)
        elif prop.type == "pair":
            value = prop.child_by_field_name("value")
            if value is not None and value.type in _FUNC_VALUE:
                name = _prop_name(prop, data)
        if not name or name in _METHOD_SKIP or name in seen:
            continue
        seen.add(name)
        emit(f"{owner}.{name}", _line(prop))
        count += 1
        if count >= _OBJ_METHOD_CAP:
            break


def _string_literal(node, data):
    if node is None or node.type not in ("string", "template_string"):
        return None
    s = _text(node, data)
    if len(s) >= 2 and s[0] in "\"'`":
        s = s[1:-1]
    return s or None


def _imports(root, data) -> list:
    """Import specifiers (relative paths or bare names) from ``import … from``, side-effect
    ``import "x"``, ``require("x")``, and dynamic ``import("x")`` — matching the regex extractor's
    specifier list, plus dynamic import which regex also captures."""
    specs, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.type == "import_statement":
            spec = _string_literal(node.child_by_field_name("source"), data)
            if spec:
                specs.append(spec)
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function") or (
                node.named_children[0] if node.named_children else None)
            is_dyn = fn is not None and fn.type == "import"
            is_req = fn is not None and fn.type == "identifier" and _text(fn, data) == "require"
            if is_dyn or is_req:
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for a in args.named_children:
                        spec = _string_literal(a, data)
                        if spec:
                            specs.append(spec)
                            break
        stack.extend(node.named_children)
    return specs
