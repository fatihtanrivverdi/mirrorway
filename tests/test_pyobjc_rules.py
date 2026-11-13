"""pyobjc turns every method of an Objective-C subclass into a selector (underscores become colons), so a plain
Python helper with the wrong number of arguments stops the class from being defined on a real Mac (3.5.0's
arrangement view failed this way in CI). Checked here without a Mac: such helpers need @objc.python_method."""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBJC_BASES = {"NSObject", "NSView", "NSWindow", "NSPanel", "NSButton", "NSTextField", "NSStatusItem"}


def _decorated(fn) -> bool:
    for d in fn.decorator_list:
        name = d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", None)
        if isinstance(d, ast.Call):
            f = d.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name in ("python_method", "typedSelector", "IBAction", "signature", "selector"):
            return True
    return False


def test_objective_c_methods_match_their_selectors():
    bad = []
    for folder, _dirs, files in os.walk(os.path.join(ROOT, "mirrorway", "mac")):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(folder, fname)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in cls.bases}
                if not bases & OBJC_BASES:
                    continue
                for fn in cls.body:
                    if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("__") or _decorated(fn):
                        continue
                    nargs = len(fn.args.args) - 1
                    if fn.name.lstrip("_").count("_") != nargs:      # a leading "_" is kept as is
                        bad.append(f"{fname}:{fn.lineno} {cls.name}.{fn.name} ({nargs} arguments)")
    assert not bad, "methods pyobjc cannot turn into selectors (add @objc.python_method):\n" + "\n".join(bad)
