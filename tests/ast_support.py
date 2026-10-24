"""Load selected definitions, never execute application-level startup code."""
import ast
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=4)
def parsed(path):
    return ast.parse(Path(path).read_text(encoding='utf-8-sig'))


def load_definitions(names, namespace=None, path=None):
    path = path or ROOT / 'BOT5-2-414 .py'
    tree = parsed(str(path))
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    assert {n.name for n in selected} == set(names), set(names) - {n.name for n in selected}
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    code = compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])), str(path), 'exec')
    env = {} if namespace is None else namespace
    exec(code, env)
    return env
