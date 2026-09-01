"""File-local receiver type knowledge for the Python parser.

This module is deliberately not a type checker. It answers one bounded
question per receiver name: *what does this file itself state about the
object bound to that name?* A receiver earns a type only from evidence in
its own file:

* a constructor call — ``x = Store()`` or ``x = mod.Store()``;
* an annotation — a parameter (``def f(store: Store)``), a local annotated
  assignment (``store: Store = ...``), or an in-file factory's return
  annotation reached through one hop;
* an instance attribute — ``self.attr = Store()`` or ``self.attr: Store``
  anywhere in the enclosing class, or a class-body assignment of the same
  shape;
* the enclosing class itself — behind ``cls.member()`` and as the fallback
  for ``self.member()`` when no sibling method matches.

Nothing here decides what a type *means* in the repository. The finding is
attached to the CALLS edge as ``receiver_type_*`` / ``receiver_member_module``
/ ``receiver_from_call_*`` metadata, and :class:`GraphBuilder` — which sees
every file — classifies it: an in-repo class links its method, an external
origin becomes an ``external::`` answer, and anything ambiguous keeps the
hole. A name the scope binds to two different types is ambiguous and stays
untyped; flow-sensitive dataflow and cross-file type knowledge are out of
scope by construction.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import Any, NamedTuple

_FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

# Annotation roots and bare constructors that name a builtin *type* rather
# than a callable: ``d: dict[str, int]`` and ``d = dict()`` both make the
# receiver a builtin dict — an external answer rather than a hole.
_BUILTIN_TYPE_NAMES = frozenset(
    {
        "bool",
        "bytearray",
        "bytes",
        "dict",
        "frozenset",
        "int",
        "list",
        "set",
        "str",
        "tuple",
        "type",
    }
)


class ImportBindings(NamedTuple):
    """Absolute-import bindings visible to scopes in one parsed file.

    ``modules`` maps the bound name of ``import X [as name]`` to the imported
    module path; ``import a.b`` binds only ``a``. ``members`` maps the bound
    name of ``from M import n [as name]`` to the origin module and the
    original member name. Relative imports (level > 0) resolve against the
    current package, which a single file cannot know; they stay unbound.
    """

    modules: dict[str, str]
    members: dict[str, tuple[str, str]]


class _Ambiguous:
    """Sentinel for a name the scope binds to more than one type."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- diagnostics only
        return "<ambiguous receiver>"


AMBIGUOUS = _Ambiguous()

#: A per-scope table of receiver names to their stated type, or AMBIGUOUS.
ReceiverTable = dict[str, "TypeRef | _Ambiguous"]

#: A class-attribute table: ``self.attr``/``cls.attr`` to the stated type or
#: the cross-module construction call that produced it, or AMBIGUOUS.
ClassAttributeTable = dict[str, "TypeRef | tuple[str, str] | _Ambiguous"]

#: A per-scope table of receiver names to the cross-module factory that
#: produced them, or AMBIGUOUS.
FromCallTable = dict[str, "tuple[str, str] | _Ambiguous"]


class TypeRef(NamedTuple):
    """What one file knows about a receiver's type.

    ``name`` is the type's last component (``GraphBuilder``, ``Path``,
    ``dict``). Exactly one of ``qname`` (a class lexically visible in this
    file) and ``module`` (the import origin, or ``builtins``) is set.
    """

    name: str
    module: str | None = None
    qname: str | None = None


class FileKnowledge(NamedTuple):
    """Whole-file declaration facts the per-scope tables resolve against.

    ``function_returns`` maps a function name to its return annotation only
    when every declaration of that name in the file agrees; disagreeing or
    missing annotations leave the name out.
    """

    class_names: frozenset[str]
    function_names: frozenset[str]
    function_returns: dict[str, ast.expr]

    @classmethod
    def build(cls, tree: ast.Module) -> "FileKnowledge":
        class_names: set[str] = set()
        function_names: set[str] = set()
        returns: dict[str, ast.expr | _Ambiguous] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_names.add(node.name)
            elif isinstance(node, _FUNCTION_DEFS):
                function_names.add(node.name)
                if node.returns is None:
                    continue
                previous = returns.get(node.name)
                if previous is None:
                    returns[node.name] = node.returns
                elif not isinstance(previous, _Ambiguous):
                    if ast.unparse(previous) != ast.unparse(node.returns):
                        returns[node.name] = AMBIGUOUS
        return cls(
            frozenset(class_names),
            frozenset(function_names),
            {
                name: annotation
                for name, annotation in returns.items()
                if not isinstance(annotation, _Ambiguous)
            },
        )


def _own_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Yield every node of one scope's own body, definitions excluded.

    The walk descends through compound statements without ever entering a
    nested function, class, or lambda — the same ownership discipline the
    call extractor uses, so a nested scope's bindings never leak outward.
    """
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES):
                continue
            stack.append(child)


class ReceiverKnowledge:
    """Receiver-type bindings for one function scope in one file.

    Built by :meth:`for_scope`; consulted by the parser's call resolver.
    Lookups return ``None`` (nothing known), a :class:`TypeRef`, or the
    ``AMBIGUOUS`` sentinel — metadata is emitted only for a real ``TypeRef``
    or a factory tuple, never for the ambiguous cases.
    """

    def __init__(
        self,
        scope_chain: list[dict[str, str]],
        import_bindings: ImportBindings,
        file_knowledge: FileKnowledge,
        receiver_types: ReceiverTable,
        receiver_from_calls: FromCallTable,
        class_attributes: ClassAttributeTable,
        cls_attributes: ClassAttributeTable,
        class_qname: str | None,
    ) -> None:
        self.scope_chain = scope_chain
        self.import_bindings = import_bindings
        self.file = file_knowledge
        self.receiver_types = receiver_types
        self.receiver_from_calls = receiver_from_calls
        self.class_attributes = class_attributes
        self.cls_attributes = cls_attributes
        self.class_qname = class_qname

    @classmethod
    def for_scope(
        cls,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        scope_chain: list[dict[str, str]],
        import_bindings: ImportBindings,
        file_knowledge: FileKnowledge,
        class_attributes: ClassAttributeTable,
        cls_attributes: ClassAttributeTable,
        class_qname: str | None,
    ) -> "ReceiverKnowledge":
        """Build the receiver table for one function scope.

        Parameters and the scope's own assignments (nested definitions
        excluded) each contribute bindings; a name bound to two different
        types becomes ambiguous. Aliases (``b = a`` after ``a = A()``) adopt
        the aliased binding after the direct pass.
        """
        receiver_types: ReceiverTable = {}
        receiver_from_calls: FromCallTable = {}
        knowledge = cls(
            scope_chain,
            import_bindings,
            file_knowledge,
            receiver_types,
            receiver_from_calls,
            class_attributes,
            cls_attributes,
            class_qname,
        )

        args = function.args
        for param in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if param.annotation is not None:
                knowledge._bind_assignment_name(
                    receiver_types,
                    param.arg,
                    knowledge.annotation_ref(param.annotation),
                )

        assignments: list[ast.Assign | ast.AnnAssign] = []
        rebinding_targets: set[str] = set()
        for node in _own_nodes(function.body):
            if isinstance(node, ast.For):
                rebinding_targets.update(_simple_targets(node.target))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                assignments.append(node)
                knowledge._bind_assignment(node, receiver_types, receiver_from_calls)

        # A name rebound by a loop after earning a stated type is no longer
        # single-typed in this scope.
        for name in rebinding_targets:
            receiver_types[name] = AMBIGUOUS
            receiver_from_calls.pop(name, None)

        for node in assignments:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name):
                continue
            source = node.value.id
            for target in _simple_targets_from_assignment(node):
                if target == source:
                    continue
                ref = receiver_types.get(source)
                if isinstance(ref, TypeRef):
                    knowledge._bind_assignment_name(receiver_types, target, ref)
                origin = receiver_from_calls.get(source)
                if isinstance(origin, tuple):
                    _bind(receiver_from_calls, target, origin)
        return knowledge

    # ------------------------------------------------------------------
    # Resolution of one expression to a TypeRef
    # ------------------------------------------------------------------

    def annotation_ref(self, annotation: ast.expr) -> TypeRef | None:
        """Resolve a type annotation to a TypeRef.

        ``Optional[X]`` and ``X | None`` peel to ``X``; other unions stay
        unknown. A subscripted generic resolves to its root (``dict[str,
        Any]`` to ``dict``); string annotations resolve nothing.
        """
        if isinstance(annotation, ast.Constant):
            return None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            left_none = isinstance(annotation.left, ast.Constant) and (
                annotation.left.value is None
            )
            right_none = isinstance(annotation.right, ast.Constant) and (
                annotation.right.value is None
            )
            if left_none:
                return self.annotation_ref(annotation.right)
            if right_none:
                return self.annotation_ref(annotation.left)
            return None
        if isinstance(annotation, ast.Subscript):
            value = annotation.value
            if isinstance(value, ast.Name) and value.id == "Optional":
                return self.annotation_ref(annotation.slice)
            return self.annotation_ref(value)
        if isinstance(annotation, ast.Attribute):
            return self._attribute_ref(annotation)
        if isinstance(annotation, ast.Name):
            return self._name_ref(annotation.id)
        return None

    def _name_ref(self, name: str) -> TypeRef | None:
        if self._lexically_visible(name):
            if self._is_visible_class(name):
                return TypeRef(name=name, qname=self._chain_qname(name))
            return None  # A visible non-class binding shadows any import.
        if name in self.file.class_names and name in self.file.function_names:
            return None  # The file declares both a class and a function here.
        origin = self.import_bindings.members.get(name)
        if origin is not None:
            return TypeRef(name=origin[1], module=origin[0])
        if name in _BUILTIN_TYPE_NAMES:
            return TypeRef(name=name, module="builtins")
        return None

    def _attribute_ref(self, attribute: ast.Attribute) -> TypeRef | None:
        """Resolve ``mod.Type`` / ``mod.sub.Type`` through a module import."""
        base = attribute.value
        if not isinstance(base, (ast.Name, ast.Attribute)):
            return None
        base_name = ast.unparse(base)
        root = base_name.split(".", 1)[0]
        module = self.import_bindings.modules.get(root)
        if module is None:
            return None
        return TypeRef(name=attribute.attr, module=f"{module}{base_name[len(root) :]}")

    def construction_ref(self, call: ast.Call) -> TypeRef | None:
        """Resolve ``x = <call>()``'s callee to the constructed type.

        Only spellings that *are* types in construction position count: a
        lexically visible class, an in-file factory whose return annotation
        names the type, or a builtin type callable. An imported name called
        here could be a class or a factory — that ambiguity is
        :meth:`factory_origin`'s to carry, not this method's to guess.
        """
        func = call.func
        if not isinstance(func, ast.Name):
            return None
        name = func.id
        if self._lexically_visible(name):
            if self._is_visible_class(name):
                return TypeRef(name=name, qname=self._chain_qname(name))
            if name in self.file.function_names and name not in self.file.class_names:
                annotation = self.file.function_returns.get(name)
                if annotation is not None:
                    return self.annotation_ref(annotation)
            return None
        if name in _BUILTIN_TYPE_NAMES:
            return TypeRef(name=name, module="builtins")
        return None

    def factory_origin(self, call: ast.Call) -> tuple[str, str] | None:
        """The cross-module factory behind ``x = f()`` the file cannot type.

        An in-file factory's return annotation is resolved locally by
        :meth:`construction_ref`, so what remains is ``from M import f`` /
        ``mod.f()`` — a callee that may be a factory or a class, a question
        only the graph can answer through the callee entity's kind and
        signature.
        """
        func = call.func
        if isinstance(func, ast.Name):
            if self._lexically_visible(func.id):
                return None
            return self.import_bindings.members.get(func.id)
        if isinstance(func, ast.Attribute):
            module = self._module_path(func.value)
            if module is not None:
                return (module, func.attr)
        return None

    def _module_path(self, base: ast.expr) -> str | None:
        """The import-bound module path of an attribute base, if any."""
        if not isinstance(base, (ast.Name, ast.Attribute)):
            return None
        base_name = ast.unparse(base)
        root = base_name.split(".", 1)[0]
        module = self.import_bindings.modules.get(root)
        if module is None:
            return None
        return f"{module}{base_name[len(root) :]}"

    # ------------------------------------------------------------------
    # Assignment binding
    # ------------------------------------------------------------------

    def _bind_assignment(
        self,
        node: ast.Assign | ast.AnnAssign,
        receiver_types: ReceiverTable,
        receiver_from_calls: FromCallTable,
    ) -> None:
        targets = _simple_targets_from_assignment(node)
        if not targets:
            return
        if isinstance(node, ast.AnnAssign):
            if node.annotation is None:  # pragma: no cover -- grammar requires one
                return
            ref = self.annotation_ref(node.annotation)
            if ref is not None:
                for target in targets:
                    self._bind_assignment_name(receiver_types, target, ref)
            return
        value = node.value
        if not isinstance(value, ast.Call):
            return
        ref = self.construction_ref(value)
        if ref is not None:
            for target in targets:
                self._bind_assignment_name(receiver_types, target, ref)
            return
        origin = self.factory_origin(value)
        if origin is not None:
            for target in targets:
                _bind(receiver_from_calls, target, origin)

    @staticmethod
    def _bind_assignment_name(
        table: ReceiverTable, name: str, ref: TypeRef | None
    ) -> None:
        if ref is not None:
            _bind(table, name, ref)

    # ------------------------------------------------------------------
    # Scope visibility
    # ------------------------------------------------------------------

    def _lexically_visible(self, name: str) -> bool:
        return any(name in symbols for symbols in reversed(self.scope_chain))

    def _is_visible_class(self, name: str) -> bool:
        return (
            name in self.file.class_names
            and name not in self.file.function_names
            and self._lexically_visible(name)
        )

    def _chain_qname(self, name: str) -> str:
        for symbols in reversed(self.scope_chain):
            if name in symbols:
                return symbols[name]
        raise AssertionError(  # pragma: no cover -- callers check visibility first
            f"{name!r} checked for a qname without being visible"
        )

    # ------------------------------------------------------------------
    # Edge metadata
    # ------------------------------------------------------------------

    def receiver_metadata(self, callee: str) -> dict[str, Any] | None:
        """Binding metadata for one dotted callee, or ``None``.

        The dotted callee arrives as the parser's flattened receiver chain
        (``store.get``, ``self.registry.resolve``, ``cls.create``). The
        returned metadata states the receiver's identity and which member is
        called; classification is the builder's job. Object receivers need a
        single-component member — ``x.attr.method()`` says nothing about
        ``attr`` — while a member-imported receiver may name a nested path
        (``parsers.python_parser.PythonParser``).
        """
        if callee.startswith("self."):
            return self._self_metadata(callee)
        if callee.startswith("cls."):
            return self._cls_metadata(callee)
        receiver, _, member = callee.partition(".")
        if not member:
            return None
        ref = self.receiver_types.get(receiver)
        if isinstance(ref, TypeRef):
            if "." in member:
                return None
            return self._type_metadata(ref, member)
        origin = self.receiver_from_calls.get(receiver)
        if isinstance(origin, tuple):
            if "." in member:
                return None
            return {
                "receiver_from_call_name": origin[1],
                "receiver_from_call_module": origin[0],
                "receiver_method": member,
            }
        if self._lexically_visible(receiver):
            # A class visible in the scope chain makes the receiver a
            # class-object call (``ClassName.method()``); a visible binding
            # of any other kind shadows every import below.
            if self._is_visible_class(receiver) and receiver not in (
                self.import_bindings.members
            ):
                qname = self._chain_qname(receiver)
                if "." in member:
                    return None
                return self._type_metadata(TypeRef(name=receiver, qname=qname), member)
            return None
        member_origin = self.import_bindings.members.get(receiver)
        if member_origin is not None:
            return {
                "receiver_member_module": f"{member_origin[0]}.{member_origin[1]}",
                "receiver_method": member,
            }
        return None

    def _self_metadata(self, callee: str) -> dict[str, Any] | None:
        rest = callee[len("self.") :]
        attr, sep, member = rest.partition(".")
        if self.class_qname is None:
            return None
        class_name = self.class_qname.rsplit(".", 1)[-1]
        if not sep:
            # ``self.member()`` with no sibling match: the enclosing class is
            # the receiver's type, which lets the builder walk base classes.
            return self._type_metadata(
                TypeRef(name=class_name, qname=self.class_qname), attr
            )
        if "." in member:
            return None
        return _attribute_metadata(self.class_attributes.get(attr), member)

    def _cls_metadata(self, callee: str) -> dict[str, Any] | None:
        rest = callee[len("cls.") :]
        attr, sep, member = rest.partition(".")
        if self.class_qname is None:
            return None
        class_name = self.class_qname.rsplit(".", 1)[-1]
        if not sep:
            return self._type_metadata(
                TypeRef(name=class_name, qname=self.class_qname), attr
            )
        if "." in member:
            return None
        return _attribute_metadata(self.cls_attributes.get(attr), member)

    @staticmethod
    def _type_metadata(ref: TypeRef, member: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "receiver_type_name": ref.name,
            "receiver_method": member,
        }
        if ref.module is not None:
            metadata["receiver_type_module"] = ref.module
        if ref.qname is not None:
            metadata["receiver_type_qname"] = ref.qname
        return metadata


def build_class_attributes(
    class_node: ast.ClassDef,
    scope_chain: list[dict[str, str]],
    import_bindings: ImportBindings,
    file_knowledge: FileKnowledge,
) -> tuple[ClassAttributeTable, ClassAttributeTable]:
    """Attribute-type tables for one class.

    ``self.*`` receivers may be bound by a class-body assignment or by
    ``self.attr = ...`` in any method; ``cls.*`` receivers only by class-body
    assignments, because an instance attribute assigned in a method is not
    reachable through the class object. Two different bindings for one
    attribute anywhere in the class leave it ambiguous.
    """
    self_table: ClassAttributeTable = {}
    cls_table: ClassAttributeTable = {}
    knowledge = ReceiverKnowledge(
        scope_chain,
        import_bindings,
        file_knowledge,
        {},
        {},
        {},
        {},
        None,
    )

    def bind_tables(
        node: ast.Assign | ast.AnnAssign, table: ClassAttributeTable
    ) -> None:
        for target in _simple_targets_from_assignment(node):
            if isinstance(node, ast.AnnAssign):
                if node.annotation is not None:  # pragma: no cover -- grammar
                    _bind(table, target, knowledge.annotation_ref(node.annotation))
                continue
            value = node.value
            if isinstance(value, ast.Call):
                _bind(table, target, _construction_binding(knowledge, value))

    for stmt in class_node.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            bind_tables(stmt, self_table)
            bind_tables(stmt, cls_table)
        elif isinstance(stmt, _FUNCTION_DEFS):
            for node in _own_nodes(stmt.body):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        _bind_self_target(target, node.value, self_table, knowledge)
                elif isinstance(node, ast.AnnAssign):
                    _bind_self_target(node.target, node.value, self_table, knowledge)

    return self_table, cls_table


def _construction_binding(
    knowledge: ReceiverKnowledge, call: ast.Call
) -> TypeRef | tuple[str, str] | None:
    """The stated binding of one construction call: a local type, or the
    cross-module callee identity the graph must resolve."""
    ref = knowledge.construction_ref(call)
    if ref is not None:
        return ref
    return knowledge.factory_origin(call)


def _attribute_metadata(binding: object, member: str) -> dict[str, Any] | None:
    """Edge metadata for one class-attribute binding."""
    if isinstance(binding, TypeRef):
        return ReceiverKnowledge._type_metadata(binding, member)
    if isinstance(binding, tuple):
        return {
            "receiver_from_call_name": binding[1],
            "receiver_from_call_module": binding[0],
            "receiver_method": member,
        }
    return None


def _bind_self_target(
    target: ast.expr,
    value: ast.expr | None,
    table: ClassAttributeTable,
    knowledge: ReceiverKnowledge,
) -> None:
    if not isinstance(target, ast.Attribute) or target.attr.startswith("__"):
        return
    if not isinstance(target.value, ast.Name) or target.value.id != "self":
        return
    if isinstance(value, ast.Call):
        _bind(table, target.attr, _construction_binding(knowledge, value))


def _bind(table: dict[str, Any], name: str, value: Any) -> None:
    if value is None:
        return
    previous = table.get(name)
    if previous is None:
        table[name] = value
    elif previous != value:
        table[name] = AMBIGUOUS


def _simple_targets(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _simple_targets(element)
    elif isinstance(target, ast.Starred):
        yield from _simple_targets(target.value)


def _simple_targets_from(targets: list[ast.expr]) -> Iterator[str]:
    for target in targets:
        yield from _simple_targets(target)


def _simple_targets_from_assignment(node: ast.Assign | ast.AnnAssign) -> list[str]:
    if isinstance(node, ast.Assign):
        return list(_simple_targets_from(node.targets))
    return list(_simple_targets(node.target))
