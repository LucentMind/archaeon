from pathlib import Path

import tree_sitter_c
import tree_sitter_cpp
from tree_sitter import Language, Parser

C_SUFFIXES = {".c", ".h"}
CPP_SUFFIXES = {".cc", ".cpp", ".cxx", ".hpp"}


def _language(path: Path) -> Language:
    if path.suffix in C_SUFFIXES:
        return Language(tree_sitter_c.language())
    if path.suffix in CPP_SUFFIXES:
        return Language(tree_sitter_cpp.language())
    raise ValueError(f"unsupported suffix: {path.suffix}")


def _identifier(node, src: bytes) -> str | None:
    if node.type in ("identifier", "field_identifier", "type_identifier",
                     "qualified_identifier"):
        return src[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace")
    for child in node.children:
        name = _identifier(child, src)
        if name:
            return name
    return None


def ts_symbols(path: Path) -> list[dict]:
    src = path.read_bytes()
    tree = Parser(_language(path)).parse(src)
    symbols = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            declarator = node.child_by_field_name("declarator")
            name = _identifier(declarator, src) if declarator else None
            if name:
                sig = src[node.start_byte:node.end_byte].split(b"{")[0]
                symbols.append({
                    "name": name, "kind": "function",
                    "line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "signature": sig.decode("utf-8",
                                            errors="replace").strip()})
        elif node.type in ("struct_specifier", "class_specifier"):
            name_node = node.child_by_field_name("name")
            if name_node is not None and node.child_by_field_name("body"):
                symbols.append({
                    "name": src[name_node.start_byte:name_node.end_byte]
                    .decode("utf-8", errors="replace"),
                    "kind": "struct" if node.type == "struct_specifier"
                    else "class",
                    "line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "signature": ""})
        stack.extend(node.children)
    return sorted(symbols, key=lambda s: s["line"])
