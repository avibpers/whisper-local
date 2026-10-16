# agent/sandbox.py
# The check that decides whether an AI-written script may run, and running it
# in rehearsal. The check is an allow-list over the script's syntax tree: plain
# statements, calls of Koli's actions and a few built-ins by name, and reading
# the fields of file records. No imports, no other attributes, nothing with
# "__" in it, no lambdas or definitions. The run has no __builtins__, and a
# line counter stops loops and runaway scripts. This, not a second model, is
# what keeps a script to Koli's actions.

import ast
import sys
import time

from .actions import ActionError, Recorder

ATTRS = {'name', 'path', 'ext', 'size', 'modified', 'age_days', 'folder'}
MAX_LINES = 20000           # line events in one run
MAX_SECONDS = 10.0
# Operations in one request: a runaway guard, above what a search can find
# (actions.MAX_RESULTS, 500). Moving 495 pictures is one request; more than
# 50 files waits for your password anyway (policy.MAX_FREE_FILES).
MAX_OPS = 1000
TOO_MANY = ("That's more than {n} files or steps at once. Ask for part of it, "
            "like this month's pictures or the PDFs only.")
MAX_CONSTANT = 1_000_000
MAX_RANGE = 1000

_ALLOWED = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.For, ast.If, ast.Break, ast.Continue, ast.Pass,
    ast.Call, ast.keyword, ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.IfExp, ast.Subscript, ast.Slice, ast.JoinedStr, ast.FormattedValue, ast.Attribute,
)


class ScriptRejected(Exception):
    """The script asked for something outside Koli's actions."""


def _builtins() -> dict:
    def capped_range(*args):
        r = range(*[int(a) for a in args])
        if len(r) > MAX_RANGE:
            raise ActionError(f"range is limited to {MAX_RANGE} steps")
        return r
    return {'len': len, 'range': capped_range, 'str': str, 'int': int, 'min': min, 'max': max,
            'abs': abs, 'round': round, 'True': True, 'False': False, 'None': None}


# Assignment and loop targets: plain names (or a tuple of them), never one of
# Koli's actions or built-ins.
def _check_targets(targets, callable_names) -> None:
    for t in targets:
        names = [t] if isinstance(t, ast.Name) else list(t.elts) if isinstance(t, ast.Tuple) else [None]
        if any(not isinstance(n, ast.Name) for n in names):
            raise ScriptRejected('only plain names can be assigned')
        if any(n.id in callable_names for n in names):
            raise ScriptRejected('Koli\'s actions can\'t be replaced')


# Raises ScriptRejected with a reason when `script` has anything outside the
# allow-list. `names`: the functions it may call.
def check(script: str, names) -> ast.Module:
    if not isinstance(script, str) or not script.strip():
        raise ScriptRejected('the script is empty')
    if len(script) > 8000:
        raise ScriptRejected('the script is too long')
    try:
        tree = ast.parse(script, mode='exec')
    except SyntaxError as e:
        raise ScriptRejected(f"the script doesn't parse (line {e.lineno})")
    callable_names = set(names) | set(_builtins())
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ScriptRejected(f"{type(node).__name__} isn't allowed")
        if isinstance(node, ast.Name):
            if '__' in node.id or node.id.startswith('_'):
                raise ScriptRejected(f"the name {node.id} isn't allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr not in ATTRS:
                raise ScriptRejected(f".{node.attr} isn't allowed (only {', '.join(sorted(ATTRS))})")
            if not isinstance(node.ctx, ast.Load):
                raise ScriptRejected('fields can be read, not changed')
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and '__' in node.value:
                raise ScriptRejected('text with "__" isn\'t allowed')
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool) and abs(node.value) > MAX_CONSTANT:
                raise ScriptRejected('numbers that large aren\'t allowed')
            if isinstance(node.value, bytes):
                raise ScriptRejected('bytes aren\'t allowed')
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in callable_names:
                raise ScriptRejected('only Koli\'s actions can be called')
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                raise ScriptRejected('* and ** arguments aren\'t allowed')
        elif isinstance(node, ast.For):
            if node.orelse:
                raise ScriptRejected('for-else isn\'t allowed')
            _check_targets([node.target], callable_names)
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            _check_targets(node.targets if isinstance(node, ast.Assign) else [node.target], callable_names)
    return tree


# Runs `script` in rehearsal: returns the recorder with its ops, said and
# trace. Raises ScriptRejected (the check) or ActionError (an action refused,
# a limit reached).
def rehearse(script: str, recorder: Recorder) -> Recorder:
    functions = recorder.functions()
    tree = check(script, functions)
    code = compile(tree, '<koli script>', 'exec')
    env = {'__builtins__': {}}
    env.update(_builtins())
    env.update(functions)
    lines, started = [0], time.monotonic()

    def tracer(frame, event, arg):
        if frame.f_code is code or frame.f_code.co_filename == '<koli script>':
            if event == 'line':
                lines[0] += 1
                if lines[0] > MAX_LINES or time.monotonic() - started > MAX_SECONDS:
                    raise ActionError('the script ran too long')
                if len(recorder.ops) > MAX_OPS:
                    raise ActionError(TOO_MANY.format(n=MAX_OPS))
            return tracer
        return None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        exec(code, env)
    except (ActionError, ScriptRejected):
        raise
    except Exception as e:
        raise ActionError(f"the script failed: {e.__class__.__name__}: {e}")
    finally:
        sys.settrace(previous)
    if len(recorder.ops) > MAX_OPS:
        raise ActionError(TOO_MANY.format(n=MAX_OPS))
    return recorder
