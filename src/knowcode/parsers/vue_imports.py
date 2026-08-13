"""Import statement scanning for Vue Single File Component script blocks.

Vue components import two very different things through the same syntax. A bare
specifier such as ``'vue'`` or ``'pinia'`` names a package outside the
repository; a path specifier such as ``'./MyButton.vue'`` or ``'@/components'``
names project source whose bindings are usually the components the template
renders. Both stay external references until cross-file resolution exists, but
they are not the same kind of external thing, so they are classified here rather
than guessed at each call site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Specifier prefixes that denote project source rather than a package.
_PATH_PREFIXES = ("./", "../", "/", "@/", "~/")

# Anchored to the start of a line so a commented-out import, or the word
# "import" inside a string literal, cannot register a phantom dependency. A
# clause may still span lines, because the negated class matches newlines.
_IMPORT_STATEMENT = re.compile(
    r"^[ \t]*import\s+(?P<clause>[^'\";]*?)\s+from\s*['\"](?P<module>[^'\"]*)['\"]",
    re.MULTILINE,
)
_TYPE_ONLY_CLAUSE = re.compile(r"^type\b")
_NAMED_CLAUSE = re.compile(r"\{(?P<names>[^}]*)\}")
_NAMESPACE_CLAUSE = re.compile(r"\*\s+as\s+(?P<name>[A-Za-z_$][\w$]*)")
_IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*$")


@dataclass(frozen=True)
class VueImport:
    """One import statement and the local names it introduces."""

    module: str
    bindings: tuple[str, ...]
    is_type_only: bool = False

    @property
    def is_path_import(self) -> bool:
        """True when the specifier points at project source, not a package."""
        return self.module.startswith(_PATH_PREFIXES)

    @property
    def is_valid(self) -> bool:
        """True when the specifier names something at all."""
        return bool(self.module.strip())


def _binding_name(fragment: str) -> str | None:
    """Return the local name a single import clause fragment introduces."""
    name = fragment.strip()
    if " as " in name:
        name = name.split(" as ")[-1].strip()
    if not name or name == "type":
        return None
    # ``import { type Foo }`` binds ``Foo``; the modifier is not a name.
    name = name.removeprefix("type ").strip()
    return name if _IDENTIFIER.match(name) else None


def _clause_bindings(clause: str) -> tuple[str, ...]:
    """Return every local name declared by an import clause, in source order."""
    names: list[str] = []
    remainder = clause

    namespace = _NAMESPACE_CLAUSE.search(remainder)
    if namespace is not None:
        names.append(namespace.group("name"))
        remainder = remainder[: namespace.start()] + remainder[namespace.end() :]

    named = _NAMED_CLAUSE.search(remainder)
    if named is not None:
        remainder = remainder[: named.start()] + remainder[named.end() :]

    default_name = _binding_name(remainder.split(",")[0])
    if default_name is not None:
        names.append(default_name)

    if named is not None:
        for fragment in named.group("names").split(","):
            binding = _binding_name(fragment)
            if binding is not None:
                names.append(binding)

    deduplicated: list[str] = []
    for name in names:
        if name not in deduplicated:
            deduplicated.append(name)
    return tuple(deduplicated)


def scan_imports(script_content: str) -> list[VueImport]:
    """Return every ``import ... from '...'`` statement in source order."""
    return [
        VueImport(
            module=match.group("module"),
            bindings=_clause_bindings(match.group("clause")),
            is_type_only=_TYPE_ONLY_CLAUSE.match(match.group("clause").strip())
            is not None,
        )
        for match in _IMPORT_STATEMENT.finditer(script_content)
    ]
