"""Checagem estática do bundle: nenhum nome usado dentro de função pode estar indefinido (ex.: import com apelido perdido)."""
import ast
import builtins
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "market_ai_engine_v6.py")


def _targets(node, acc):
    if isinstance(node, ast.Name):
        acc.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            _targets(e, acc)
    elif isinstance(node, ast.Starred):
        _targets(node.value, acc)


def _bound_names(fn) -> set:
    names = set()
    a = fn.args
    for n in ast.walk(fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs:
                names.add(arg.arg)
            if a.vararg:
                names.add(a.vararg.arg)
            if a.kwarg:
                names.add(a.kwarg.arg)
        if isinstance(n, (ast.Assign,)):
            for t in n.targets:
                _targets(t, names)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            _targets(n.target, names)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            _targets(n.target, names)
        elif isinstance(n, ast.NamedExpr):
            _targets(n.target, names)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    _targets(it.optional_vars, names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                names.add((al.asname or al.name).split(".")[0])
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
    return names


def undefined_names(path: str) -> list:
    tree = ast.parse(open(path, encoding="utf-8").read())
    module = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                _targets(t, module)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            _targets(n.target, module)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                module.add((al.asname or al.name).split(".")[0])
        elif isinstance(n, (ast.Try, ast.If, ast.With, ast.For)):
            for m in ast.walk(n):
                if isinstance(m, (ast.Import, ast.ImportFrom)):
                    for al in m.names:
                        module.add((al.asname or al.name).split(".")[0])
                elif isinstance(m, ast.Assign):
                    for t in m.targets:
                        _targets(t, module)
                elif isinstance(m, (ast.FunctionDef, ast.ClassDef)):
                    module.add(m.name)
    bad = []

    def visit(fn):
        # funções aninhadas enxergam o escopo de quem as contém: _bound_names já inclui o que elas amarram
        bound = _bound_names(fn)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound and n.id not in module:
                bad.append((n.lineno, n.id))

    def top_level_functions(node):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield n
            elif isinstance(n, (ast.ClassDef, ast.If, ast.Try, ast.With, ast.For)):
                yield from top_level_functions(n)
    for fn in top_level_functions(tree):
        visit(fn)
    return sorted(set(bad))


def duplicated_definitions(path: str) -> list:
    tree = ast.parse(open(path, encoding="utf-8").read())
    seen, dup = {}, []
    for n in tree.body:
        names = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, ast.Assign) and not isinstance(n.value, ast.Name):   # CONSTANTES também (HORIZONS de um módulo sobrescreveu o do motor:
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]        # live caiu); `_atr = atr` é alias que o bundler injeta, igual em todos
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names = [n.target.id]
        for name in names:
            if name.startswith("_") and name.endswith("_"):     # __version__, __build__, __all__
                continue
            if name in seen:
                dup.append((name, seen[name], n.lineno))
            seen[name] = n.lineno
    return dup


class BundleNamesTests(unittest.TestCase):
    def test_bundle_is_fresh_and_has_no_undefined_names(self):
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_single_file.py")], check=True, capture_output=True)
        bad = undefined_names(BUNDLE)
        self.assertEqual(bad, [], f"nomes indefinidos no bundle: {bad[:20]}")

    def test_bundle_has_no_duplicated_top_level_definitions(self):
        dup = duplicated_definitions(BUNDLE)
        self.assertEqual(dup, [], f"definições duplicadas no bundle (a segunda sobrescreve a primeira): {dup}")
