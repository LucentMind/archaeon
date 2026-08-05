from pathlib import Path


def clang_symbols(file_path: Path, compile_db_dir: Path) -> list[dict]:
    try:
        from clang import cindex
        index = cindex.Index.create()
        compdb = cindex.CompilationDatabase.fromDirectory(str(compile_db_dir))
    except Exception as e:  # library load or DB load failure
        raise RuntimeError(f"clang init failed: {e}") from e
    try:
        commands = compdb.getCompileCommands(str(file_path))
    except Exception as e:
        raise RuntimeError(f"getCompileCommands failed: {e}") from e
    if not commands:
        raise RuntimeError(f"no compile command for {file_path}")
    args = [a for a in list(commands[0].arguments)[1:]
            if a not in ("-c", str(file_path))]
    try:
        tu = index.parse(str(file_path), args=args)
    except Exception as e:
        raise RuntimeError(f"clang parse failed: {e}") from e
    kinds = {
        "FUNCTION_DECL": "function",
        "CXX_METHOD": "function",
        "STRUCT_DECL": "struct",
        "CLASS_DECL": "class",
    }
    try:
        symbols = []
        for cursor in tu.cursor.walk_preorder():
            kind = kinds.get(cursor.kind.name)
            if not kind or not cursor.is_definition():
                continue
            if cursor.location.file is None or \
                    Path(str(cursor.location.file)) != file_path:
                continue
            symbols.append({
                "name": cursor.spelling, "kind": kind,
                "line": cursor.extent.start.line,
                "end_line": cursor.extent.end.line,
                "signature": cursor.displayname})
    except Exception as e:
        raise RuntimeError(f"clang cursor walk failed: {e}") from e
    return sorted(symbols, key=lambda s: s["line"])
