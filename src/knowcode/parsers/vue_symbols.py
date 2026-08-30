"""Per-component symbol resolution for Vue Single File Components.

A Vue component exposes one flat name space to its template and to ``v-bind()``
in its style block. Composition API top-level bindings, Options API ``data``,
``computed``, and ``methods``, and ``defineProps`` results all land in that one
space, so ``@click="save"`` cannot be resolved by guessing an ID category.

This module records what the script section actually declared and resolves
template and style names against it. A name the component never declared becomes
an explicit unresolved reference rather than a fabricated internal ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from knowcode.data_models import EntityKind
from knowcode.utils.entity_identity import (
    build_external_reference_id,
    build_unresolved_reference_id,
)

VUE_LANGUAGE = "vue"


class BindingCategory(str, Enum):
    """Semantic category of a name exposed to a Vue component's template."""

    PROP = "prop"
    SETUP = "setup"
    DATA = "data"
    COMPUTED = "computed"
    METHOD = "method"


@dataclass(frozen=True)
class VueBinding:
    """One name a component declares.

    ``namespace`` is empty for template-facing bindings. A non-empty namespace
    such as ``emits.`` keeps a declaration out of template resolution while
    still guarding its canonical ID against a duplicate.
    """

    name: str
    entity_id: str
    kind: EntityKind
    category: BindingCategory
    namespace: str = ""

    @property
    def key(self) -> str:
        """The name this binding occupies in the component's symbol table."""
        return f"{self.namespace}{self.name}"


@dataclass(frozen=True)
class ResolvedReference:
    """The endpoint a template or style name resolves to, and why."""

    target_id: str
    resolution: str
    category: BindingCategory | None = None


class VueSymbolTable:
    """Names a single component declared, and how references resolve to them.

    Vue's template scope is flat: a component cannot bind one name as both a
    prop and a data property. One name therefore maps to at most one entity, and
    a second declaration of that name is a component defect rather than an
    ambiguity to resolve.
    """

    def __init__(self, file_path: str | Path, component_qualified_name: str) -> None:
        self._file_path = file_path
        self._component_qualified_name = component_qualified_name
        self._bindings: dict[str, VueBinding] = {}
        self._imports: dict[str, str] = {}

    # -- registration ------------------------------------------------------

    def declare(self, binding: VueBinding) -> bool:
        """Record a declaration, returning ``False`` if its name is taken."""
        if binding.key in self._bindings:
            return False
        self._bindings[binding.key] = binding
        return True

    def declare_import(self, local_name: str, module: str) -> None:
        """Record the module a name was imported from, for edge diagnostics."""
        self._imports.setdefault(local_name, module)

    def import_module_for(self, name: str) -> str | None:
        """Return the module ``name`` was imported from, if any."""
        return self._imports.get(name)

    def is_declared(self, name: str) -> bool:
        return name in self._bindings

    # -- resolution --------------------------------------------------------

    def resolve(self, name: str) -> ResolvedReference:
        """Resolve a template or style name against the declared bindings."""
        binding = self._bindings.get(name)
        if binding is None:
            return ResolvedReference(self._unresolved(name), "unresolved")
        return ResolvedReference(binding.entity_id, "resolved", binding.category)

    def resolve_call(self, name: str) -> ResolvedReference:
        """Resolve a called composable name.

        A component may define its own ``useX`` helper, so a local declaration
        wins. Everything else is an external composable: the component knows the
        symbol's name but not the entity behind it until cross-file resolution
        exists.
        """
        if self.is_declared(name):
            return self.resolve(name)
        return ResolvedReference(
            build_external_reference_id("composable", name), "external"
        )

    def _unresolved(self, symbol: str) -> str:
        return build_unresolved_reference_id(
            VUE_LANGUAGE,
            self._file_path,
            self._component_qualified_name,
            symbol,
        )
