import os
import ast
import builtins
from .database import save_code_graph_to_db

# Extract standard Python built-in names
BUILTIN_NAMES = set(dir(builtins))

# Decorators that only modify a function in place, rather than handing it to
# something that will call it later. Everything NOT on this list is treated as
# registration — `@app.route`, `@mcp.tool`, `@pytest.fixture`, `@celery.task`
# and every framework's equivalent all mean "something outside this codebase
# holds a reference to this function and will invoke it". Enumerating inert
# decorators is tractable; enumerating every framework's registering ones is
# not.
INERT_DECORATORS = {
    "staticmethod", "classmethod", "abstractmethod", "abstractproperty",
    "override", "overload", "final", "wraps", "lru_cache", "cache",
    "dataclass", "total_ordering", "contextmanager", "asynccontextmanager",
    "singledispatch", "singledispatchmethod", "runtime_checkable",
}

# Decorators making a function reachable by attribute access rather than a
# call, so no call edge will ever point at it.
DESCRIPTOR_DECORATORS = {
    "property", "cached_property", "setter", "getter", "deleter",
}

# Names that conventionally mean "this is where execution starts". Applied
# only to module-level functions: `run` as a method is far too common to treat
# as an entrypoint.
ENTRYPOINT_FUNCTION_NAMES = {"main", "cli"}

# Fixture and lifecycle hooks a test runner invokes directly.
TEST_LIFECYCLE_NAMES = {
    "setUp", "tearDown", "setUpClass", "tearDownClass",
    "setUpModule", "tearDownModule",
    "setup_method", "teardown_method", "setup_class", "teardown_class",
    "setup_module", "teardown_module", "setup_function", "teardown_function",
}

# Generic containers whose subscript names the type that matters:
# `Optional[TraceManager]` is a TraceManager as far as attribute access goes.
UNWRAPPED_GENERICS = {
    "Optional", "List", "list", "Set", "set", "Sequence", "Iterable",
    "Iterator", "Awaitable", "Coroutine", "ClassVar", "Final",
}


def is_environment_dir(path):
    """
    True for a virtualenv or conda environment.

    Name matching alone is not enough: environments get called anything —
    autogen-env, .direnv, myproject-venv — and indexing one silently floods
    the graph with thousands of third-party symbols. These markers are what
    the tools themselves write, so they hold whatever the directory is named.
    """
    return (
        os.path.isfile(os.path.join(path, "pyvenv.cfg"))        # stdlib venv
        or os.path.isdir(os.path.join(path, "conda-meta"))      # conda
        or os.path.isdir(os.path.join(path, "site-packages"))   # bare prefix
    )


def module_name_for(rel_path, package_prefix=()):
    """
    The module name for a source file, given its path relative to the indexed
    root. Shared so history analysis names symbols exactly as parsing does —
    otherwise the two produce names that never join up.

    Returns (module_name, is_package).
    """
    parts = rel_path[:-3].replace(os.sep, ".").split(".")
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    parts = list(package_prefix) + parts
    name = ".".join(parts)
    if not name:
        name = os.path.basename(rel_path)[:-3]
    return name, is_package


def package_prefix_for(root_dir):
    """A directory that is itself a package contributes its own name."""
    if os.path.isfile(os.path.join(root_dir, "__init__.py")):
        return [os.path.basename(os.path.normpath(root_dir))]
    return []


def src_layout_dirs(root_dir):
    """
    Top-level directories pyproject.toml declares as a src/ layout - a
    `packages` field pointing under a subdirectory (`src/mypackage`), via
    either setuptools or hatchling. A file under one of these carries the
    directory's name in its path but not in its import path, so the parser
    must drop it too: otherwise `from mypackage.x import y` inside
    src/mypackage/x.py never matches the symbol parsed from that file, and
    every absolute self-import in the package silently fails to resolve.

    Returns the set of such directory names (usually just {"src"}), or an
    empty set if pyproject.toml is absent, unreadable, or names no src dir.
    """
    pyproject_path = os.path.join(root_dir, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return set()

    try:
        import tomllib
    except ImportError:
        return set()

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return set()

    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return set()

    package_lists = []

    setuptools_cfg = tool.get("setuptools", {})
    if isinstance(setuptools_cfg, dict):
        package_lists.append(setuptools_cfg.get("packages"))
        package_dir = setuptools_cfg.get("package-dir")
        if isinstance(package_dir, dict):
            root_mapping = package_dir.get("")
            if isinstance(root_mapping, str) and root_mapping:
                package_lists.append([root_mapping])

    hatch_cfg = tool.get("hatch", {})
    if isinstance(hatch_cfg, dict):
        wheel_cfg = hatch_cfg.get("build", {}).get("targets", {}).get("wheel", {})
        if isinstance(wheel_cfg, dict):
            package_lists.append(wheel_cfg.get("packages"))

    dirs = set()
    for packages in package_lists:
        if not isinstance(packages, list):
            continue
        for entry in packages:
            if isinstance(entry, str) and "/" in entry:
                dirs.add(entry.split("/")[0])

    return dirs


def strip_src_layout(rel_path, src_dirs):
    """Drops a leading src-layout directory (see `src_layout_dirs`) from a
    path already relative to the indexed root, so the parsed module name
    matches how the code actually imports itself."""
    if not src_dirs:
        return rel_path
    parts = rel_path.split(os.sep)
    if len(parts) > 1 and parts[0] in src_dirs:
        return os.sep.join(parts[1:])
    return rel_path


def absolute_import_module(module_name, is_package, node):
    """
    Turns a relative import into the module it actually names.

    `from .base_test import X` inside a.b.tests.test_mixing means
    a.b.tests.base_test; reading node.module alone yields "base_test" and the
    symbol never resolves. Package-structured projects use these everywhere.
    """
    if not node.level:
        return node.module or ""

    parts = module_name.split(".")
    if not is_package:
        parts = parts[:-1]          # a module resolves against its package
    climb = node.level - 1
    if climb:
        parts = parts[:-climb] if climb < len(parts) else []

    base = ".".join(parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def annotation_raw_name(node):
    """
    The type name written in an annotation, before resolution.

    Unwraps the generics above so `Optional[TraceManager]` yields
    "TraceManager". Returns None for anything with no single obvious type.
    """
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = annotation_raw_name(node.value)
        return f"{inner}.{node.attr}" if inner else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # Forward reference: `-> "SessionMemory"`.
        return node.value
    if isinstance(node, ast.Subscript):
        outer = annotation_raw_name(node.value)
        if outer and outer.split(".")[-1] in UNWRAPPED_GENERICS:
            inner = node.slice
            if isinstance(inner, ast.Tuple) and inner.elts:
                inner = inner.elts[0]
            return annotation_raw_name(inner)
        return outer
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # PEP 604: `Cache | None` is the modern spelling of Optional[Cache],
        # and reads as a BinOp rather than a Subscript. Typed code uses it
        # everywhere, so missing it loses most attribute types in such a
        # codebase.
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and side.value is None:
                continue
            name = annotation_raw_name(side)
            if name:
                return name
        return None
    return None

class DefinitionVisitor(ast.NodeVisitor):
    """
    Pass 1 Visitor: Extracts all local symbols (classes, methods, functions)
    defined within a module to build the codebase symbol table.
    """
    def __init__(self, module_name, is_package=False):
        self.module_name = module_name
        self.is_package = is_package
        self.current_class = None
        self.current_function = None
        self.symbols = set()

        # Collected for pass 2, which needs to know a call's return type to
        # resolve `trace = get_current_trace()` - and needs to tell a
        # constructor apart from a plain function returning something else.
        self.class_symbols = set()
        self.raw_returns = {}
        self.imports = {}
        self.spans = {}   # symbol -> (first line, last line)

    def visit_Import(self, node):
        for alias in node.names:
            self.imports[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = absolute_import_module(self.module_name, self.is_package, node)
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            self.imports[name] = f"{module}.{alias.name}" if module else alias.name
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        class_fqn = f"{self.module_name}.{node.name}"
        self.symbols.add(class_fqn)
        self.class_symbols.add(class_fqn)
        self.spans[class_fqn] = (node.lineno, getattr(node, "end_lineno", node.lineno))

        old_class = self.current_class
        self.current_class = class_fqn
        self.generic_visit(node)
        self.current_class = old_class

    def visit_FunctionDef(self, node):
        self.visit_any_function(node)

    def visit_AsyncFunctionDef(self, node):
        self.visit_any_function(node)

    def visit_any_function(self, node):
        # Nested functions are qualified by the function enclosing them.
        # Naming them after the enclosing class instead collapses every
        # same-named closure in that class into one symbol.
        parent = self.current_function or self.current_class or self.module_name
        func_name = f"{parent}.{node.name}"
        self.symbols.add(func_name)

        self.spans[func_name] = (node.lineno, getattr(node, "end_lineno", node.lineno))

        raw_return = annotation_raw_name(node.returns)
        if raw_return:
            self.raw_returns[func_name] = raw_return

        old_func = self.current_function
        self.current_function = func_name
        self.generic_visit(node)
        self.current_function = old_func

class CodeASTVisitor(ast.NodeVisitor):
    """
    Pass 2 Visitor: Maps structural relationships (calls, containment, imports, inherits)
    and classifies nodes against the codebase symbol table.
    """
    def __init__(self, module_name, local_symbols, class_symbols=None, return_types=None,
                 is_package=False):
        self.module_name = module_name
        self.is_package = is_package
        self.local_symbols = local_symbols
        self.class_symbols = class_symbols or set()
        self.return_types = return_types or {}
        self.current_class = None
        self.current_function = None

        self.nodes = [] # List of dicts: {"title": ..., "summary": ..., "node_type": ...}
        self.edges = [] # List of dicts: {"source": ..., "target": ..., "edge_type": ...}

        self.imports = {}
        self.star_imports = []

        # ─── Local type environment ───
        # Without knowing what a variable holds, `db = Database()` followed by
        # `db.save()` yields no usable edge, which is most real Python. These
        # track the little that can be inferred from constructor calls and
        # annotations, which covers the common cases without a type checker.
        self.scope_stack = [{}]          # stack of {variable name: type FQN}
        self.class_attrs = {}            # class FQN -> {attribute name: type FQN}

        # ─── Roles ───
        # Which symbols are tests, and which are reachable from outside the
        # codebase. Together these give the graph its roots: anything no test
        # and no entrypoint can reach is a dead-code candidate.
        self.module_roles = set()
        self.is_test_module = self._looks_like_test_module(module_name)

        # Call targets this visitor could not tie to any known symbol.
        self.unresolved = set()

        # Name nodes sitting in the callee position of a Call, so that
        # `foo()` is recorded once as a call and not again as a reference.
        self.call_positions = set()

    @staticmethod
    def _looks_like_test_module(module_name):
        leaf = module_name.split(".")[-1]
        return (
            leaf.startswith("test_")
            or leaf.endswith("_test")
            or "tests" in module_name.split(".")
            or leaf == "conftest"
        )

    def decorator_names(self, node):
        """Final attribute of each decorator, e.g. `app.route` -> 'route'."""
        names = []
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            full = self.get_full_attr_name(target)
            if full:
                names.append(full.split(".")[-1])
        return names

    def function_roles(self, node, name, is_method):
        """Classifies a function as test and/or entrypoint."""
        roles = set()
        decorators = self.decorator_names(node)

        # Name-based detection is confined to test modules, matching what a
        # runner actually collects. Without that guard any ordinary function
        # named `test_coverage` is mistaken for a test, and its dependencies
        # are then reported as covered when nothing tests them at all.
        if self.is_test_module:
            if name.startswith("test"):
                roles.add("test")
            elif name in TEST_LIFECYCLE_NAMES:
                # The runner calls these itself, and they appear on shared
                # base classes that are not named Test* at all.
                roles.add("test")
        if "fixture" in decorators:
            roles.add("test")

        if any(d in DESCRIPTOR_DECORATORS for d in decorators):
            roles.add("implicit")
        elif any(d not in INERT_DECORATORS for d in decorators):
            # Decorated by something that is not purely a modifier, so a
            # framework is holding this function and will call it.
            roles.add("entrypoint")

        if not is_method and name in ENTRYPOINT_FUNCTION_NAMES:
            roles.add("entrypoint")

        # pytest discovers its hooks by name - pytest_configure,
        # pytest_addoption, pytest_collection_modifyitems - and calls them
        # itself, so nothing in the codebase ever references them.
        if not is_method and name.startswith("pytest_"):
            roles.add("entrypoint")

        return roles

    def visit_If(self, node):
        """Detects the `if __name__ == "__main__":` guard, which makes the
        enclosing module an execution entrypoint."""
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            self.module_roles.add("entrypoint")
        self.generic_visit(node)
        
    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname or alias.name
            self.imports[name] = alias.name
            self.edges.append({
                "source": self.module_name,
                "target": alias.name,
                "edge_type": "imports"
            })
        self.generic_visit(node)
        
    def visit_ImportFrom(self, node):
        module = absolute_import_module(self.module_name, self.is_package, node)
        for alias in node.names:
            if alias.name == "*":
                self.star_imports.append(module)
                self.edges.append({
                    "source": self.module_name,
                    "target": module,
                    "edge_type": "imports"
                })
            else:
                name = alias.asname or alias.name
                target = f"{module}.{alias.name}" if module else alias.name
                self.imports[name] = target
                self.edges.append({
                    "source": self.module_name,
                    "target": target,
                    "edge_type": "imports"
                })
        self.generic_visit(node)
        
    def get_base_class_name(self, node):
        """Recursively resolves class inheritance parents."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            val_str = self.get_base_class_name(node.value)
            if val_str:
                return f"{val_str}.{node.attr}"
        elif isinstance(node, ast.Subscript):
            return self.get_base_class_name(node.value)
        return None

    # ─── Type inference ─────────────────────────────────────────────────────

    def resolve_type_name(self, name):
        """
        Turns a type reference as written (`Database`, `nx.DiGraph`) into the
        fully-qualified name the graph uses. Returns None when the name cannot
        be tied to anything, so callers can skip recording a guess.
        """
        if not name:
            return None

        if name in self.imports:
            return self.imports[name]

        parts = name.split(".")
        if parts[0] in self.imports:
            return f"{self.imports[parts[0]]}.{'.'.join(parts[1:])}"

        # A class defined in this module.
        local_candidate = f"{self.module_name}.{name}"
        if local_candidate in self.local_symbols:
            return local_candidate

        if name in self.local_symbols:
            return name

        return None

    def infer_type(self, value):
        """
        Infers the type of an assigned expression.

        A call needs care: `Database()` yields a Database, but
        `get_current_trace()` yields whatever that function returns, not the
        function itself. Treating every resolvable call as a constructor —
        as this once did — types the variable as the function and makes every
        method called on it unresolvable.
        """
        if isinstance(value, ast.Call):
            callee = self.get_full_attr_name(value.func)
            if not callee:
                return None

            resolved = self.resolve_call(callee, record_unresolved=False)
            if resolved in self.return_types:
                return self.return_types[resolved]
            if resolved in self.class_symbols:
                return resolved

            # Falls back to name-shaped resolution for classes outside this
            # codebase, where there is no symbol table to consult.
            direct = self.resolve_type_name(callee)
            if direct and direct not in self.local_symbols:
                return direct
            return None

        if isinstance(value, ast.Name):
            return self.lookup_variable(value.id)

        if isinstance(value, ast.Attribute):
            # `cache = self._cache` - reading a typed attribute into a local.
            # Without this the attribute's type is known but stops at the
            # assignment, and every call on the local goes unresolved.
            parts = (self.get_full_attr_name(value) or "").split(".")
            if len(parts) == 2 and parts[0] == "self" and self.current_class:
                return self.class_attrs.get(self.current_class, {}).get(parts[1])
            if len(parts) > 1:
                base = self.lookup_variable(parts[0])
                if base:
                    return self.class_attrs.get(base, {}).get(parts[1])
        return None

    def annotation_type(self, annotation):
        """
        Resolves a type annotation to a fully-qualified symbol.

        Reading the annotation is shared with pass 1 rather than reimplemented
        here: keeping two parsers in step failed silently once already, when
        `X | None` was taught to one of them and not the other.
        """
        return self.resolve_type_name(annotation_raw_name(annotation))

    def lookup_variable(self, name):
        """Finds a variable's type in the innermost scope that defines it."""
        for scope in reversed(self.scope_stack):
            if name in scope:
                return scope[name]
        return None

    def record_assignment(self, target, inferred):
        """Stores an inferred type against a simple name or a `self.attr`."""
        if inferred is None:
            return
        if isinstance(target, ast.Name):
            self.scope_stack[-1][target.id] = inferred
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and self.current_class
        ):
            self.class_attrs.setdefault(self.current_class, {})[target.attr] = inferred

    def visit_Assign(self, node):
        inferred = self.infer_type(node.value)
        if inferred:
            for target in node.targets:
                self.record_assignment(target, inferred)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        inferred = self.annotation_type(node.annotation) or self.infer_type(node.value)
        self.record_assignment(node.target, inferred)
        self.generic_visit(node)

    def collect_self_attributes(self, class_node):
        """
        Pre-scans a class body for `self.x = Thing()` before visiting its
        methods. A method that uses `self.x` may be defined above the
        `__init__` that creates it, so these cannot be learned in traversal
        order.
        """
        # Class-body annotations: `cache: ClientResponseCache | None = ...`.
        # Dataclasses, pydantic models and attrs classes declare their state
        # this way and never write `self.cache = ...` at all, so scanning only
        # for self-assignment misses every attribute they have. Only direct
        # children of the class body count - the same syntax inside a method
        # is an ordinary local variable.
        for child in class_node.body:
            if (
                isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
            ):
                inferred = self.annotation_type(child.annotation)
                if inferred:
                    self.class_attrs.setdefault(self.current_class, {})[child.target.id] = inferred

        for child in ast.walk(class_node):
            if isinstance(child, ast.Assign):
                inferred = self.infer_type(child.value)
                targets = child.targets
            elif isinstance(child, ast.AnnAssign):
                inferred = self.annotation_type(child.annotation) or self.infer_type(child.value)
                targets = [child.target]
            else:
                continue

            if not inferred:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    self.class_attrs.setdefault(self.current_class, {})[target.attr] = inferred

    def visit_ClassDef(self, node):
        class_name = f"{self.module_name}.{node.name}"
        bases = []
        for base in node.bases:
            base_name = self.get_base_class_name(base)
            if base_name:
                bases.append(base_name)
        
        summary = f"class {node.name}(" + ", ".join(bases) + "):"
        class_roles = set()
        if node.name.startswith("Test") or (self.is_test_module and node.name.endswith("Test")):
            class_roles.add("test")

        # A base class defined outside this codebase hides its own contract:
        # any method here may be an override the framework invokes, with no
        # call edge to show for it (ast.NodeVisitor dispatching to visit_Call
        # is the case in point). Recorded so reachability can allow for it.
        for base in bases:
            resolved = self.resolve_type_name(base)
            if resolved is None or resolved not in self.local_symbols:
                class_roles.add("external_base")
                break
        self.nodes.append({
            "title": class_name,
            "summary": summary,
            "node_type": "class",
            "roles": sorted(class_roles),
        })
        
        self.edges.append({
            "source": self.module_name,
            "target": class_name,
            "edge_type": "contains"
        })
        
        for base in bases:
            # resolve_type_name also covers a base defined in this same
            # module, which `imports` alone never contains - the common case,
            # and one that otherwise points the edge at a bare name that
            # matches no symbol and invents a phantom node.
            resolved_base = self.resolve_type_name(base) or self.imports.get(base, base)
            self.edges.append({
                "source": class_name,
                "target": resolved_base,
                "edge_type": "inherits"
            })
            
        old_class = self.current_class
        self.current_class = class_name
        self.collect_self_attributes(node)
        self.generic_visit(node)
        self.current_class = old_class

    def visit_FunctionDef(self, node):
        self.visit_any_function(node)
        
    def visit_AsyncFunctionDef(self, node):
        self.visit_any_function(node)
        
    def visit_any_function(self, node):
        if self.current_function:
            # A closure belongs to the function that defines it, not to the
            # enclosing class: two same-named closures in one class are
            # different functions and must not share a node.
            func_name = f"{self.current_function}.{node.name}"
            node_type = "function"
        elif self.current_class:
            func_name = f"{self.current_class}.{node.name}"
            node_type = "method"
        else:
            func_name = f"{self.module_name}.{node.name}"
            node_type = "function"
            
        args_list = [arg.arg for arg in node.args.args]
        summary = f"def {node.name}(" + ", ".join(args_list) + "):"

        roles = self.function_roles(node, node.name, is_method=bool(self.current_class))
        self.nodes.append({
            "title": func_name,
            "summary": summary,
            "node_type": node_type,
            "roles": sorted(roles),
        })
        
        parent = self.current_function or self.current_class or self.module_name
        self.edges.append({
            "source": parent,
            "target": func_name,
            "edge_type": "contains"
        })
        
        old_func = self.current_function
        self.current_function = func_name

        # Parameter annotations are the other reliable source of variable
        # types, and cost nothing to read.
        scope = {}
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            annotated = self.annotation_type(arg.annotation)
            if annotated:
                scope[arg.arg] = annotated
        self.scope_stack.append(scope)

        self.generic_visit(node)

        self.scope_stack.pop()
        self.current_function = old_func
        
    def get_full_attr_name(self, node):
        """Resolves nested attribute accesses (e.g. os.path.join)."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            val_str = self.get_full_attr_name(node.value)
            if val_str:
                return f"{val_str}.{node.attr}"
        elif isinstance(node, ast.Call):
            return self.get_full_attr_name(node.func)
        elif isinstance(node, ast.Subscript):
            # `graph[node].iter_neighbors()` - the element's type is unknown,
            # but naming the container still records that *some* method by
            # this name was called, which is what keeps it off the dead list.
            return self.get_full_attr_name(node.value)
        return None

    def resolve_reference(self, name):
        """
        Resolves a bare name to a local symbol, or None.

        Deliberately stricter than resolve_call: no fallbacks and no guessing,
        because every ordinary variable passes through here and inventing
        targets would flood the graph.
        """
        if name in self.imports:
            target = self.imports[name]
            return target if target in self.local_symbols else None

        scope = self.current_function
        while scope:
            candidate = f"{scope}.{name}"
            if candidate in self.local_symbols:
                return candidate
            scope = scope.rsplit(".", 1)[0] if "." in scope else None

        if self.current_class:
            candidate = f"{self.current_class}.{name}"
            if candidate in self.local_symbols:
                return candidate

        candidate = f"{self.module_name}.{name}"
        return candidate if candidate in self.local_symbols else None

    def visit_Name(self, node):
        """
        Records a function or class named without being called —
        `render_login_form(handle_login)`, `handlers = {"a": run_a}`,
        `return build`.

        These are real dependencies: change the referenced function's
        signature and the code handing it around is affected. Without them,
        every callback looks dead.
        """
        if isinstance(node.ctx, ast.Load) and id(node) not in self.call_positions:
            target = self.resolve_reference(node.id)
            source = self.current_function or self.module_name
            if target and target != source:
                self.edges.append({
                    "source": source,
                    "target": target,
                    "edge_type": "references",
                })
        self.generic_visit(node)

    def visit_Attribute(self, node):
        """
        Records a method handed somewhere without being called, such as
        `FunctionTool.from_defaults(fn=self.tools.authenticate_customer)`.

        Registering a bound method with a framework is how agent tools, signal
        handlers, and callbacks are wired; without this they all read as dead.
        """
        if isinstance(node.ctx, ast.Load) and id(node) not in self.call_positions:
            full = self.get_full_attr_name(node)
            if full:
                target = self.resolve_call(full, record_unresolved=False)
                source = self.current_function or self.module_name
                if target in self.local_symbols and target != source:
                    self.edges.append({
                        "source": source,
                        "target": target,
                        "edge_type": "references",
                    })
        self.generic_visit(node)

    def visit_Call(self, node):
        # Calls made at module level belong to the module itself. Script-style
        # files — Streamlit apps, main.py, settings modules — put nearly all
        # their logic there, and skipping those calls makes such a file appear
        # to depend on nothing at all.
        caller = self.current_function or self.module_name

        # The callee is handled here as a call; keep the reference visitors
        # from recording it a second time.
        if isinstance(node.func, (ast.Name, ast.Attribute)):
            self.call_positions.add(id(node.func))

        called_name = self.get_full_attr_name(node.func)
        if called_name:
            resolved = self.resolve_call(called_name)
            if resolved and resolved != caller:
                self.edges.append({
                    "source": caller,
                    "target": resolved,
                    "edge_type": "calls"
                })

        self.generic_visit(node)
        
    def resolve_call(self, name, record_unresolved=True):
        if name in self.imports:
            return self.imports[name]
        parts = name.split(".")
        if parts[0] in self.imports:
            resolved_module = self.imports[parts[0]]
            return f"{resolved_module}.{'.'.join(parts[1:])}"

        if parts[0] == "self" and self.current_class:
            if len(parts) == 1:
                # `self(...)` - calling the instance itself, which runs the
                # class's __call__. Resolved to the class, the same way a
                # constructor call is. Without this the parts[1] below raises
                # and the whole file is lost.
                return self.current_class
            if len(parts) == 2:
                return f"{self.current_class}.{parts[1]}"
            # `self.engine.run()` — resolve the attribute to its type first, so
            # the call lands on the owning class rather than being dropped.
            attr_type = self.class_attrs.get(self.current_class, {}).get(parts[1])
            if attr_type:
                return f"{attr_type}.{'.'.join(parts[2:])}"
            return f"{self.current_class}.{parts[1]}"

        # `db = Database()` ... `db.save()` — the case that string matching
        # alone can never see.
        if len(parts) > 1:
            var_type = self.lookup_variable(parts[0])
            if var_type:
                return f"{var_type}.{'.'.join(parts[1:])}"

            # `SessionOrchestrator.helper()` — a class naming its own static
            # or class method, or any class referenced by name rather than
            # through an instance.
            prefix_type = self.resolve_type_name(parts[0])
            if prefix_type:
                candidate = f"{prefix_type}.{'.'.join(parts[1:])}"
                if candidate in self.local_symbols:
                    return candidate

        if "." in name:
            # An attribute call on something whose type could not be inferred:
            # `mystery.save()` where mystery is an unannotated parameter. The
            # call is real, so the edge is kept, but the target is recorded as
            # unresolved rather than dressed up as a module path. Counting
            # these is what makes the parser's blind spots measurable instead
            # of invisible.
            if record_unresolved:
                self.unresolved.add(name)
            return name

        # Bare, unqualified call (e.g. `print(x)`, or a nested closure like
        # `dfs(...)` invoked from inside a method). Check for a real Python
        # builtin first, then a same-class member (a function nested inside
        # a method belongs to the enclosing class's scope, not the module),
        # before falling back to assuming a module-level symbol.
        if is_builtin_name(name):
            return name

        # Walk outwards through the enclosing scopes: a closure calls its
        # sibling closure, or itself, by bare name, and those live under the
        # defining function rather than the module.
        scope = self.current_function
        while scope:
            candidate = f"{scope}.{name}"
            if candidate in self.local_symbols:
                return candidate
            scope = scope.rsplit(".", 1)[0] if "." in scope else None

        if self.current_class:
            class_candidate = f"{self.current_class}.{name}"
            if class_candidate in self.local_symbols:
                return class_candidate

        return f"{self.module_name}.{name}"

def is_builtin_name(name):
    """Determines if a resolved symbol name matches standard Python built-ins or properties."""
    if name in BUILTIN_NAMES:
        return True
    parts = name.split(".")
    if parts[0] in BUILTIN_NAMES:
        return True
        
    common_builtins_attrs = {
        "append", "extend", "insert", "pop", "remove", "clear", "copy", "count", "index",
        "get", "keys", "values", "items", "update", "split", "strip", "lower", "upper",
        "join", "replace", "find", "add", "difference", "intersection", "union", "discard",
        "read", "write", "close", "format", "encode", "decode", "startswith", "endswith"
    }
    if parts[-1] in common_builtins_attrs:
        return True
    return False

def parse_repository(root_dir):
    """
    Performs a two-pass static AST codebase parsing:
    1. Pass 1: Crawl all source files and compile a global local_symbols table.
    2. Pass 2: Traverse files to map architectural calls, resolve star-imports, and classify node types.
    """
    # Indexing a package directory directly (networkx/, or the common
    # src/mypackage/) would otherwise drop the package's own name, so the
    # code's absolute self-imports never match the symbols parsed from it.
    package_prefix = package_prefix_for(root_dir)
    src_dirs = src_layout_dirs(root_dir)

    exclude_dirs = {
        ".git", "__pycache__", ".agents", "scratch", "node_modules",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "site-packages", "build", "dist", ".eggs",
    }
    
    # ─── Pass 1: Collect Defined Symbols ───
    # A file that fails to parse contributes nothing at all - every symbol
    # in it vanishes from the graph. Surfaced rather than printed and
    # forgotten, because silently losing a file quietly corrupts every
    # answer that follows.
    parse_failures = []

    local_symbols = set()
    class_symbols = set()
    raw_returns = {}
    module_imports = {}
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [
            d for d in dirs
            if d not in exclude_dirs
            and not is_environment_dir(os.path.join(root, d))
        ]
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                
                rel_path = strip_src_layout(os.path.relpath(file_path, root_dir), src_dirs)
                module_name, is_package = module_name_for(rel_path, package_prefix)
                    
                local_symbols.add(module_name)
                
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        source = f.read()
                    tree = ast.parse(source, filename=file_path)
                    visitor = DefinitionVisitor(module_name, is_package)
                    visitor.visit(tree)
                    local_symbols.update(visitor.symbols)
                    class_symbols.update(visitor.class_symbols)
                    raw_returns.update(visitor.raw_returns)
                    module_imports[module_name] = visitor.imports
                except Exception as e:
                    parse_failures.append((file_path, "pass 1", str(e)))

    # Resolve return annotations now that every module's symbols are known.
    # A raw name like "TraceManager" means whatever it means in the module
    # that wrote it, so each is resolved against its own module's imports.
    return_types = {}
    for func_fqn, raw in raw_returns.items():
        owner = func_fqn.rsplit(".", 1)[0]
        module_of = owner
        while module_of and module_of not in module_imports:
            module_of = module_of.rsplit(".", 1)[0] if "." in module_of else None
        imports = module_imports.get(module_of, {})

        resolved = None
        if raw in imports and imports[raw] in class_symbols:
            resolved = imports[raw]
        elif module_of and f"{module_of}.{raw}" in class_symbols:
            resolved = f"{module_of}.{raw}"
        elif raw in class_symbols:
            resolved = raw
        if resolved:
            return_types[func_fqn] = resolved

    # ─── Pass 2: Map AST Structural Call Edges & Classify Nodes ───
    all_nodes = []
    all_edges = []
    star_imports_map = {}
    all_unresolved = set()
    explicit_exports = set()
    star_exports = set()
    
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [
            d for d in dirs
            if d not in exclude_dirs
            and not is_environment_dir(os.path.join(root, d))
        ]
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                
                rel_path = strip_src_layout(os.path.relpath(file_path, root_dir), src_dirs)
                module_name, is_package = module_name_for(rel_path, package_prefix)
                    
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        source = f.read()

                    tree = ast.parse(source, filename=file_path)
                    visitor = CodeASTVisitor(module_name, local_symbols, class_symbols,
                                             return_types, is_package)
                    visitor.visit(tree)

                    # Appended after the walk: the `__main__` guard that makes
                    # a module an entrypoint is only found during visiting.
                    module_roles = set(visitor.module_roles)
                    if visitor.is_test_module:
                        module_roles.add("test")
                    all_nodes.append({
                        "title": module_name,
                        "summary": f"module {file}",
                        "node_type": "module",
                        "roles": sorted(module_roles),
                    })

                    all_nodes.extend(visitor.nodes)
                    all_edges.extend(visitor.edges)
                    all_unresolved |= visitor.unresolved
                    if visitor.star_imports:
                        star_imports_map[module_name] = visitor.star_imports

                    # A package's __init__ re-exports are its public surface.
                    # For a library that surface IS the entrypoint: callers
                    # live outside the codebase, so without this every public
                    # function reads as dead.
                    if is_package:
                        explicit_exports.update(visitor.imports.values())
                        star_exports.update(visitor.star_imports)
                except Exception as e:
                    parse_failures.append((file_path, "pass 2", str(e)))
                    
    # Post-Parse Star Imports Resolution
    for edge in all_edges:
        target = edge["target"]
        if target not in local_symbols:
            parts = target.split(".")
            if len(parts) > 1:
                module_prefix = ".".join(parts[:-1])
                rel_name = parts[-1]
                if module_prefix in star_imports_map:
                    for star_mod in star_imports_map[module_prefix]:
                        possible_target = f"{star_mod}.{rel_name}"
                        if possible_target in local_symbols:
                            edge["target"] = possible_target
                            break

    # Dynamic Classification of Call Targets
    final_nodes = []
    added_titles = set()
    
    for node in all_nodes:
        if node["title"] not in added_titles:
            added_titles.add(node["title"])
            final_nodes.append(node)
            
    # Mark public exports as entrypoints. A star-exported module publishes
    # everything defined directly inside it.
    public = {e for e in explicit_exports if e in local_symbols}
    if star_exports:
        for symbol in local_symbols:
            owner = symbol.rsplit(".", 1)[0] if "." in symbol else None
            if owner in star_exports:
                public.add(symbol)
    # An exported class publishes its public methods along with itself: a
    # library user calls them, and no call inside the codebase need exist.
    for exported in list(public):
        prefix = exported + "."
        for symbol in local_symbols:
            if not symbol.startswith(prefix):
                continue
            leaf = symbol[len(prefix):]
            if "." not in leaf and not leaf.startswith("_"):
                public.add(symbol)

    for node in final_nodes:
        if node["title"] in public and "entrypoint" not in node["roles"]:
            node["roles"] = sorted(set(node["roles"]) | {"entrypoint"})

    # Resolve node_type for target edges not yet defined in final_nodes
    for edge in all_edges:
        target = edge["target"]
        if target not in added_titles:
            added_titles.add(target)
            
            # Categorize the node type
            if target in local_symbols:
                # Local symbol that was parsed but node description wasn't created yet
                # E.g. class methods called dynamically
                node_type = "method" if "." in target else "function"
                summary = "Local codebase component definition"
            elif is_builtin_name(target):
                node_type = "builtin"
                summary = "Python runtime built-in function or collection method"
            elif target in all_unresolved:
                # A call on a value whose type could not be inferred. Recorded
                # so the gap is countable rather than misfiled as a dependency.
                node_type = "unresolved"
                summary = "Call target that static resolution could not identify"
            else:
                node_type = "external_library"
                summary = "External dependency or package import reference"
                
            final_nodes.append({
                "title": target,
                "summary": summary,
                "node_type": node_type,
                "roles": [],
            })
            
    parse_repository.last_failures = parse_failures
    return final_nodes, all_edges

def ingest_codebase(repo_path, db_path="epicenter.db"):
    """Parses codebase using 2-pass AST parsing and saves the architecture graph to SQLite."""
    nodes, edges = parse_repository(repo_path)
    if not nodes:
        return 0
    save_code_graph_to_db(repo_path, nodes, edges, db_path)
    return len(edges)
