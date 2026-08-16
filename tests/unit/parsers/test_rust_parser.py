"""Complete unit tests for Rust parser (all tests merged)."""

from pathlib import Path
import tempfile

import pytest

from knowcode.parsers.rust_parser import RustParser
from knowcode.data_models import EntityKind, ParseResult, RelationshipKind
from knowcode.utils.entity_identity import (
    EndpointKind,
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
    classify_endpoint_id,
)
from tests.helpers.parser_assertions import (
    assert_exact_parse_result,
    assert_relationship_endpoints_classified,
    load_parser_fixture_contract,
)


# ============================================================================
# CORE FUNCTIONALITY TESTS
# ============================================================================


def test_parse_simple_function():
    """Test parsing a simple Rust function."""
    rust_code = """
fn hello_world() {
    println!("Hello, world!");
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Should have module + function
        assert len(result.entities) >= 2

        # Find function entity
        functions = [e for e in result.entities if e.kind == EntityKind.FUNCTION]
        assert len(functions) == 1
        assert functions[0].name == "hello_world"
    finally:
        temp_path.unlink()


def test_parse_struct_with_fields():
    """Test parsing a Rust struct with fields."""
    rust_code = """
/// A person with a name and age
struct Person {
    name: String,
    age: u32,
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find struct entity
        structs = [
            e
            for e in result.entities
            if e.kind == EntityKind.CLASS and e.name == "Person"
        ]
        assert len(structs) == 1
        struct = structs[0]
        assert struct.docstring == "A person with a name and age"

        # Find field entities
        fields = [
            e
            for e in result.entities
            if e.kind == EntityKind.VARIABLE and "." in e.qualified_name
        ]
        assert len(fields) == 2
        field_names = {f.name for f in fields}
        assert "name" in field_names
        assert "age" in field_names

        # Check CONTAINS relationships
        contains_rels = [
            r for r in result.relationships if r.kind == RelationshipKind.CONTAINS
        ]
        assert len(contains_rels) >= 3  # module->struct, struct->field1, struct->field2
    finally:
        temp_path.unlink()


def test_parse_enum():
    """Test parsing a Rust enum."""
    rust_code = """
enum Color {
    Red,
    Green,
    Blue,
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find enum entity
        enums = [
            e
            for e in result.entities
            if e.kind == EntityKind.CLASS and e.name == "Color"
        ]
        assert len(enums) == 1
    finally:
        temp_path.unlink()


def test_parse_trait():
    """Test parsing a Rust trait."""
    rust_code = """
/// Trait for drawable objects
trait Drawable {
    fn draw(&self);
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find trait entity
        traits = [e for e in result.entities if e.kind == EntityKind.CLASS]
        assert len(traits) == 1
        assert traits[0].name == "Drawable"
        assert traits[0].docstring == "Trait for drawable objects"
    finally:
        temp_path.unlink()


def test_parse_impl_block():
    """Test parsing a Rust impl block."""
    rust_code = """
struct Point {
    x: i32,
    y: i32,
}

impl Point {
    fn new(x: i32, y: i32) -> Point {
        Point { x, y }
    }

    fn distance(&self) -> f64 {
        ((self.x * self.x + self.y * self.y) as f64).sqrt()
    }
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find method entities
        methods = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and "Point" in e.qualified_name
        ]
        assert len(methods) == 2
        method_names = {m.name for m in methods}
        assert "new" in method_names
        assert "distance" in method_names

        # Methods must be contained by the real Point entity, not a pseudo type:: id
        point = [
            e
            for e in result.entities
            if e.name == "Point" and e.kind == EntityKind.CLASS
        ]
        assert len(point) == 1
        method_ids = {m.id for m in methods}
        type_contains_rels = [
            r
            for r in result.relationships
            if r.kind == RelationshipKind.CONTAINS
            and r.source_id == point[0].id
            and r.target_id in method_ids
        ]
        assert len(type_contains_rels) == 2
        assert not any(r.source_id.startswith("type::") for r in result.relationships)
    finally:
        temp_path.unlink()


def test_parse_use_declarations():
    """Test parsing use declarations (imports)."""
    rust_code = """
use std::collections::HashMap;
use std::io::{self, Write};

fn main() {
    println!("Hello");
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find import relationships
        imports = [
            r for r in result.relationships if r.kind == RelationshipKind.IMPORTS
        ]
        assert len(imports) >= 1

        # Check that imports point to external modules
        assert any("external::" in r.target_id for r in imports)
    finally:
        temp_path.unlink()


def test_parse_function_calls():
    """Test extracting function call relationships."""
    rust_code = """
fn helper() -> i32 {
    42
}

fn main() {
    let x = helper();
    println!("{}", x);
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find call relationships
        calls = [r for r in result.relationships if r.kind == RelationshipKind.CALLS]
        assert len(calls) >= 1

        # Check that main calls helper
        helper_calls = [r for r in calls if "helper" in r.target_id]
        assert len(helper_calls) >= 1
    finally:
        temp_path.unlink()


def test_parse_const_and_static():
    """Test parsing const and static declarations."""
    rust_code = """
const MAX_SIZE: usize = 100;
static GLOBAL_COUNTER: i32 = 0;
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find const and static entities
        variables = [e for e in result.entities if e.kind == EntityKind.VARIABLE]
        var_names = {v.name for v in variables}
        assert "MAX_SIZE" in var_names
        assert "GLOBAL_COUNTER" in var_names
    finally:
        temp_path.unlink()


def test_parse_type_alias():
    """Test parsing type aliases."""
    rust_code = """
type Result<T> = std::result::Result<T, Box<dyn Error>>;
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find type alias (maps to CLASS)
        type_aliases = [
            e
            for e in result.entities
            if e.kind == EntityKind.CLASS and e.name == "Result"
        ]
        assert len(type_aliases) == 1
    finally:
        temp_path.unlink()


def test_parse_module():
    """Test parsing module declarations."""
    rust_code = """
mod utils {
    pub fn helper() {}
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find module entity
        modules = [
            e
            for e in result.entities
            if e.kind == EntityKind.MODULE and e.name == "utils"
        ]
        assert len(modules) == 1
    finally:
        temp_path.unlink()


def test_parse_doc_comments():
    """Test that doc comments are extracted."""
    rust_code = """
/// This is a documented function
/// that does something important.
fn documented_function() {
    // Regular comment
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find function and check docstring
        functions = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "documented_function"
        ]
        assert len(functions) == 1
        assert functions[0].docstring is not None
        assert "documented function" in functions[0].docstring
        assert "something important" in functions[0].docstring
    finally:
        temp_path.unlink()


def test_parse_empty_file():
    """Test parsing an empty Rust file."""
    rust_code = ""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Should have at least a module entity
        assert len(result.entities) >= 1
        assert len(result.errors) == 0
    finally:
        temp_path.unlink()


def test_parse_complex_rust_file():
    """Test parsing a more complex Rust file with multiple constructs."""
    rust_code = """
use std::fmt;

/// A point in 2D space
#[derive(Debug)]
struct Point {
    x: f64,
    y: f64,
}

impl Point {
    /// Create a new point
    fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }

    fn distance_from_origin(&self) -> f64 {
        (self.x * self.x + self.y * self.y).sqrt()
    }
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "({}, {})", self.x, self.y)
    }
}

fn main() {
    let p = Point::new(3.0, 4.0);
    println!("Distance: {}", p.distance_from_origin());
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check entities
        assert len(result.entities) > 5

        # Check struct
        structs = [
            e
            for e in result.entities
            if e.kind == EntityKind.CLASS and e.name == "Point"
        ]
        assert len(structs) == 1
        assert "point in 2d space" in structs[0].docstring.lower()

        # Check methods
        methods = [e for e in result.entities if e.kind == EntityKind.FUNCTION]
        method_names = {m.name for m in methods}
        assert "new" in method_names
        assert "distance_from_origin" in method_names
        assert "main" in method_names

        # Check import
        imports = [
            r for r in result.relationships if r.kind == RelationshipKind.IMPORTS
        ]
        assert len(imports) >= 1

        # Check impl relationship (Point implements fmt::Display)
        impls = [
            r for r in result.relationships if r.kind == RelationshipKind.IMPLEMENTS
        ]
        assert len(impls) >= 1
    finally:
        temp_path.unlink()


# ============================================================================
# ENHANCEMENT TESTS
# ============================================================================


def test_visibility_modifiers():
    """Test that visibility modifiers are captured correctly."""
    rust_code = """
pub struct PublicStruct {
    pub public_field: i32,
    private_field: i32,
}

pub(crate) struct CrateStruct {}

pub(super) fn super_function() {}

pub fn public_function() {}

fn private_function() {}

pub const PUBLIC_CONST: i32 = 42;
const PRIVATE_CONST: i32 = 42;
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check public struct
        public_structs = [
            e
            for e in result.entities
            if e.kind == EntityKind.CLASS and e.name == "PublicStruct"
        ]
        assert len(public_structs) == 1
        assert public_structs[0].metadata["visibility"] == "public"

        # Check pub(crate) struct
        crate_structs = [
            e
            for e in result.entities
            if e.kind == EntityKind.CLASS and e.name == "CrateStruct"
        ]
        assert len(crate_structs) == 1
        assert crate_structs[0].metadata["visibility"] == "pub(crate)"

        # Check public function
        public_funcs = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "public_function"
        ]
        assert len(public_funcs) == 1
        assert public_funcs[0].metadata["visibility"] == "public"

        # Check private function
        private_funcs = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "private_function"
        ]
        assert len(private_funcs) == 1
        assert private_funcs[0].metadata["visibility"] == "private"

        # Check pub(super) function
        super_funcs = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "super_function"
        ]
        assert len(super_funcs) == 1
        assert super_funcs[0].metadata["visibility"] == "pub(super)"

        # Check public const
        public_consts = [
            e
            for e in result.entities
            if e.kind == EntityKind.VARIABLE and e.name == "PUBLIC_CONST"
        ]
        assert len(public_consts) == 1
        assert public_consts[0].metadata["visibility"] == "public"

    finally:
        temp_path.unlink()


def test_external_module_detection():
    """Test that external module declarations are detected with file path hints."""
    rust_code = """
mod utils;
mod models;

mod inline_module {
    fn helper() {}
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check external module (utils)
        utils_mods = [
            e
            for e in result.entities
            if e.kind == EntityKind.MODULE and e.name == "utils"
        ]
        assert len(utils_mods) == 1
        assert utils_mods[0].metadata["is_external"] == "true"
        assert "possible_file_paths" in utils_mods[0].metadata
        # Should suggest utils.rs or utils/mod.rs
        paths = utils_mods[0].metadata["possible_file_paths"]
        assert "utils.rs" in paths or "utils/mod.rs" in paths

        # Check inline module
        inline_mods = [
            e
            for e in result.entities
            if e.kind == EntityKind.MODULE and e.name == "inline_module"
        ]
        assert len(inline_mods) == 1
        assert inline_mods[0].metadata["is_external"] == "false"

    finally:
        temp_path.unlink()


def test_inherent_vs_trait_impl():
    """Test that inherent and trait implementations are distinguished."""
    rust_code = """
struct Point {
    x: i32,
    y: i32,
}

trait Display {
    fn display(&self);
}

// Inherent impl
impl Point {
    fn new(x: i32, y: i32) -> Self {
        Point { x, y }
    }
}

// Trait impl
impl Display for Point {
    fn display(&self) {
        println!("({}, {})", self.x, self.y);
    }
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find methods
        methods = [e for e in result.entities if e.kind == EntityKind.FUNCTION]

        # Check inherent impl method
        new_methods = [m for m in methods if m.name == "new"]
        assert len(new_methods) == 1
        assert new_methods[0].metadata["impl_type"] == "inherent"
        assert new_methods[0].metadata["associated_type"] == "Point"

        # Check trait impl method
        display_methods = [m for m in methods if m.name == "display"]
        assert len(display_methods) == 1
        assert display_methods[0].metadata["impl_type"] == "trait"
        assert display_methods[0].metadata["associated_type"] == "Point"

        # Verify trait implementation relationships
        # Should have TWO IMPLEMENTS relationships now:
        # 1. type::Point -> trait::Display (type-level)
        # 2. method -> trait::Display (method-level for "Where is Display implemented?")
        impl_rels = [
            r for r in result.relationships if r.kind == RelationshipKind.IMPLEMENTS
        ]
        assert len(impl_rels) >= 2
        point_impl = [
            r for r in impl_rels if "Point" in r.source_id and "Display" in r.target_id
        ]
        assert len(point_impl) >= 1

    finally:
        temp_path.unlink()


def test_method_containment_relationships():
    """Test that methods are always linked to their type (never orphaned)."""
    rust_code = """
struct Calculator {}

impl Calculator {
    pub fn add(a: i32, b: i32) -> i32 {
        a + b
    }

    fn subtract(a: i32, b: i32) -> i32 {
        a - b
    }
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find methods
        methods = [e for e in result.entities if e.kind == EntityKind.FUNCTION]
        method_names = {m.name for m in methods}
        assert "add" in method_names
        assert "subtract" in method_names

        # Verify BOTH methods have CONTAINS relationships from Calculator
        contains_rels = [
            r
            for r in result.relationships
            if r.kind == RelationshipKind.CONTAINS and "Calculator" in r.source_id
        ]
        # Should have at least 2 CONTAINS relationships (one for each method)
        method_contains = [
            r for r in contains_rels if any(m.id == r.target_id for m in methods)
        ]
        assert len(method_contains) == 2

        # Verify visibility metadata
        add_methods = [m for m in methods if m.name == "add"]
        assert len(add_methods) == 1
        assert add_methods[0].metadata["visibility"] == "public"

        subtract_methods = [m for m in methods if m.name == "subtract"]
        assert len(subtract_methods) == 1
        assert subtract_methods[0].metadata["visibility"] == "private"

    finally:
        temp_path.unlink()


def test_rust_type_metadata():
    """Test that rust_type metadata is added to distinguish structs, enums, traits, etc."""
    rust_code = """
pub struct MyStruct {}
pub enum MyEnum { A, B }
pub trait MyTrait {}
pub type MyType = i32;
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check struct metadata
        structs = [e for e in result.entities if e.name == "MyStruct"]
        assert len(structs) == 1
        assert structs[0].metadata["rust_type"] == "struct"

        # Check enum metadata
        enums = [e for e in result.entities if e.name == "MyEnum"]
        assert len(enums) == 1
        assert enums[0].metadata["rust_type"] == "enum"

        # Check trait metadata
        traits = [e for e in result.entities if e.name == "MyTrait"]
        assert len(traits) == 1
        assert traits[0].metadata["rust_type"] == "trait"

        # Check type alias metadata
        types = [e for e in result.entities if e.name == "MyType"]
        assert len(types) == 1
        assert types[0].metadata["rust_type"] == "type_alias"

    finally:
        temp_path.unlink()


# ============================================================================
# EDGE CASE TESTS
# ============================================================================


def test_attribute_capture():
    """Test that attributes like #[derive], #[test], #[inline] are captured."""
    rust_code = """
#[derive(Debug, Clone)]
pub struct Point {
    x: i32,
    y: i32,
}

#[test]
fn test_addition() {
    assert_eq!(2 + 2, 4);
}

#[tokio::test]
async fn test_async() {
    assert!(true);
}

#[inline]
fn fast_function() -> i32 {
    42
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check struct attributes
        structs = [
            e
            for e in result.entities
            if e.kind == EntityKind.CLASS and e.name == "Point"
        ]
        assert len(structs) == 1
        assert "attributes" in structs[0].metadata
        assert "#[derive(Debug, Clone)]" in structs[0].metadata["attributes"]

        # Check test function attributes and is_test flag
        test_funcs = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "test_addition"
        ]
        assert len(test_funcs) == 1
        assert "attributes" in test_funcs[0].metadata
        assert "#[test]" in test_funcs[0].metadata["attributes"]
        assert test_funcs[0].metadata.get("is_test") == "true"

        # Check async test
        async_tests = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "test_async"
        ]
        assert len(async_tests) == 1
        assert "is_test" in async_tests[0].metadata
        assert async_tests[0].metadata["is_test"] == "true"

        # Check inline function
        inline_funcs = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "fast_function"
        ]
        assert len(inline_funcs) == 1
        assert "attributes" in inline_funcs[0].metadata
        assert "#[inline]" in inline_funcs[0].metadata["attributes"]

    finally:
        temp_path.unlink()


def test_generic_type_stripping():
    """Test that generic types are stripped for USES_TYPE relationships."""
    rust_code = """
use std::collections::HashMap;

struct Container {
    map: HashMap<String, i32>,
    vec: Vec<String>,
    opt: Option<i32>,
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check type relationships - should link to base types without generics
        type_rels = [
            r for r in result.relationships if r.kind == RelationshipKind.USES_TYPE
        ]

        # Should have relationships to HashMap, Vec, Option (not HashMap<String, i32>)
        target_types = {r.target_id.split("::")[-1] for r in type_rels}
        assert "HashMap" in target_types
        assert "Vec" in target_types
        assert "Option" in target_types

        # Should NOT have the full generic types
        for rel in type_rels:
            assert "<" not in rel.target_id

    finally:
        temp_path.unlink()


def test_trait_method_tracking():
    """Test that trait implementation methods are linked to both type and trait."""
    rust_code = """
trait Display {
    fn display(&self) -> String;
}

struct Point {
    x: i32,
    y: i32,
}

impl Display for Point {
    fn display(&self) -> String {
        format!("({}, {})", self.x, self.y)
    }
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find the display method
        display_methods = [
            e
            for e in result.entities
            if e.kind == EntityKind.FUNCTION and e.name == "display"
        ]
        assert len(display_methods) == 1
        display_method = display_methods[0]

        # Check metadata
        assert display_method.metadata.get("impl_type") == "trait"
        assert display_method.metadata.get("associated_type") == "Point"
        assert display_method.metadata.get("implemented_trait") == "Display"

        # Check that method implements the trait (direct link for queries like "Where is Display implemented?")
        method_impl_rels = [
            r
            for r in result.relationships
            if r.kind == RelationshipKind.IMPLEMENTS
            and r.source_id == display_method.id
            and "Display" in r.target_id
        ]
        assert len(method_impl_rels) == 1

        # Also check that type implements trait
        # Should have TWO relationships with Display:
        # 1. type::Point -> trait::Display
        # 2. method display -> trait::Display
        type_impl_rels = [
            r
            for r in result.relationships
            if r.kind == RelationshipKind.IMPLEMENTS
            and "Point" in r.source_id
            and "Display" in r.target_id
        ]
        assert len(type_impl_rels) >= 1

    finally:
        temp_path.unlink()


def test_generic_trait_stripping():
    """Test that generic trait implementations are stripped for cleaner linking."""
    rust_code = """
use std::fmt;

struct Wrapper<T> {
    value: T,
}

impl<T: fmt::Display> fmt::Display for Wrapper<T> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.value)
    }
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check that impl relationship strips generics from trait name
        impl_rels = [
            r for r in result.relationships if r.kind == RelationshipKind.IMPLEMENTS
        ]

        # Should have at least one IMPLEMENTS relationship
        assert len(impl_rels) >= 1

        # The trait target should be fmt::Display, not fmt::Display<something>
        trait_impl = [
            r
            for r in impl_rels
            if "Display" in r.target_id and "Wrapper" in r.source_id
        ]
        assert len(trait_impl) >= 1
        # Ensure no generics in trait target
        assert "<" not in trait_impl[0].target_id

    finally:
        temp_path.unlink()


def test_test_detection_comprehensive():
    """Test that various test patterns are detected."""
    rust_code = """
#[test]
fn basic_test() {
    assert!(true);
}

#[tokio::test]
async fn async_test() {
    assert!(true);
}

#[async_test]
async fn another_async_test() {
    assert!(true);
}

// Regular function (not a test)
fn helper() {
    println!("Not a test");
}

#[cfg(test)]
mod tests {
    #[test]
    fn nested_test() {
        assert!(true);
    }
}
"""
    parser = RustParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(rust_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find all functions
        functions = [e for e in result.entities if e.kind == EntityKind.FUNCTION]

        # Find test functions
        test_funcs = [f for f in functions if f.metadata.get("is_test") == "true"]
        test_names = {f.name for f in test_funcs}

        # Should detect all test patterns
        assert "basic_test" in test_names
        assert "async_test" in test_names
        assert "another_async_test" in test_names
        assert "nested_test" in test_names

        # Should NOT flag helper as a test
        assert "helper" not in test_names

        # Helper should not have is_test metadata
        helpers = [f for f in functions if f.name == "helper"]
        assert len(helpers) == 1
        assert (
            "is_test" not in helpers[0].metadata
            or helpers[0].metadata["is_test"] != "true"
        )

    finally:
        temp_path.unlink()


# ============================================================================
# STEP 06 — CANONICAL IMPL, TRAIT, AND TYPE IDENTITY
# ============================================================================

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts" / "rust"


def _write_rust(tmp_path: Path, source: str, name: str = "sample.rs") -> Path:
    """Write a Rust source fixture and return its path."""
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _parse(
    tmp_path: Path, source: str, name: str = "sample.rs"
) -> tuple[ParseResult, Path]:
    """Parse an inline Rust source fixture."""
    path = _write_rust(tmp_path, source, name)
    return RustParser().parse_file(path), path


def _internal(path: Path, qualified_name: str) -> str:
    return build_internal_entity_id(path, qualified_name)


def _unresolved(path: Path, scope: str, symbol: str) -> str:
    return build_unresolved_reference_id("rust", path, scope, symbol)


def _edges(result: ParseResult, kind: RelationshipKind) -> set[tuple[str, str]]:
    return {
        (rel.source_id, rel.target_id)
        for rel in result.relationships
        if rel.kind is kind
    }


def _entity(result: ParseResult, entity_id: str):
    matches = [entity for entity in result.entities if entity.id == entity_id]
    assert len(matches) == 1, f"expected exactly one {entity_id!r}, got {len(matches)}"
    return matches[0]


def test_rust_impl_identity_fixture_matches_exactly() -> None:
    """The committed Rust fixture must match entity and edge identity exactly."""
    contract = load_parser_fixture_contract(FIXTURE_ROOT / "impl_identity.rs")

    assert_exact_parse_result(RustParser().parse_file(contract.source_path), contract)


def test_inherent_impl_methods_are_contained_by_the_local_type(tmp_path: Path) -> None:
    """An inherent impl gives each method exactly one real structural parent."""
    result, path = _parse(
        tmp_path,
        "struct Point;\n\nimpl Point {\n    fn new() -> Point { Point }\n}\n",
    )

    method_id = _internal(path, "sample.Point.new")
    assert {entity.id for entity in result.entities} == {
        _internal(path, "sample"),
        _internal(path, "sample.Point"),
        method_id,
    }
    containing = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CONTAINS and rel.target_id == method_id
    ]
    assert [rel.source_id for rel in containing] == [_internal(path, "sample.Point")]
    assert _edges(result, RelationshipKind.IMPLEMENTS) == set()
    assert_relationship_endpoints_classified(result)


def test_local_trait_impl_resolves_to_the_trait_entity(tmp_path: Path) -> None:
    """A locally declared trait is a real endpoint for type and method edges."""
    result, path = _parse(
        tmp_path,
        "trait Draw {\n}\n\nstruct Point;\n\nimpl Draw for Point {\n    fn draw(&self) {}\n}\n",
    )

    assert _edges(result, RelationshipKind.IMPLEMENTS) == {
        (_internal(path, "sample.Point"), _internal(path, "sample.Draw")),
        (_internal(path, "sample.Point.draw"), _internal(path, "sample.Draw")),
    }
    assert _edges(result, RelationshipKind.CONTAINS) == {
        (_internal(path, "sample"), _internal(path, "sample.Draw")),
        (_internal(path, "sample"), _internal(path, "sample.Point")),
        (_internal(path, "sample.Point"), _internal(path, "sample.Point.draw")),
    }
    assert_relationship_endpoints_classified(result)


def test_qualified_trait_path_becomes_a_scoped_unresolved_reference(
    tmp_path: Path,
) -> None:
    """A trait reached through a path is unresolved, never a fabricated trait:: id."""
    result, path = _parse(
        tmp_path,
        "struct Point;\n\nimpl vendor::Render for Point {\n    fn render(&self) {}\n}\n",
    )

    assert _edges(result, RelationshipKind.IMPLEMENTS) == {
        (
            _internal(path, "sample.Point"),
            _unresolved(path, "sample.Point", "vendor::Render"),
        ),
        (
            _internal(path, "sample.Point.render"),
            _unresolved(path, "sample.Point.render", "vendor::Render"),
        ),
    }
    assert not any(
        rel.target_id.startswith(("trait::", "type::", "ref::"))
        for rel in result.relationships
    )
    assert_relationship_endpoints_classified(result)


GENERIC_IMPLS = """\
trait Show {
}

struct Wrapper<T> {
    value: T,
}

impl<T: Show> Wrapper<T> {
    fn get(&self) -> &T { &self.value }
}

impl<T> Show for Wrapper<T> {
    fn show(&self) {}
}
"""


def test_generic_impl_types_do_not_create_malformed_ids(tmp_path: Path) -> None:
    """Generic impl headers resolve to the base type without leaking type arguments."""
    result, path = _parse(tmp_path, GENERIC_IMPLS)

    assert {entity.id for entity in result.entities} == {
        _internal(path, "sample"),
        _internal(path, "sample.Show"),
        _internal(path, "sample.Wrapper"),
        _internal(path, "sample.Wrapper.value"),
        _internal(path, "sample.Wrapper.get"),
        _internal(path, "sample.Wrapper.show"),
    }
    assert _edges(result, RelationshipKind.IMPLEMENTS) == {
        (_internal(path, "sample.Wrapper"), _internal(path, "sample.Show")),
        (_internal(path, "sample.Wrapper.show"), _internal(path, "sample.Show")),
    }
    for rel in result.relationships:
        assert "<" not in rel.source_id and "<" not in rel.target_id
    assert_relationship_endpoints_classified(result)


MULTIPLE_IMPLS = """\
trait Alpha {
}

trait Beta {
}

struct Point;

impl Point {
    fn new() -> Point { Point }
}

impl Alpha for Point {
    fn alpha(&self) {}
}

impl Beta for Point {
    fn beta(&self) {}
}
"""


def test_multiple_impls_share_one_structural_parent(tmp_path: Path) -> None:
    """Every method of every impl block is contained exactly once, by its type."""
    result, path = _parse(tmp_path, MULTIPLE_IMPLS)

    point_id = _internal(path, "sample.Point")
    for method in ("new", "alpha", "beta"):
        method_id = _internal(path, f"sample.Point.{method}")
        parents = [
            rel.source_id
            for rel in result.relationships
            if rel.kind is RelationshipKind.CONTAINS and rel.target_id == method_id
        ]
        assert parents == [point_id], f"{method} must have one parent, got {parents}"

    assert _edges(result, RelationshipKind.IMPLEMENTS) == {
        (point_id, _internal(path, "sample.Alpha")),
        (_internal(path, "sample.Point.alpha"), _internal(path, "sample.Alpha")),
        (point_id, _internal(path, "sample.Beta")),
        (_internal(path, "sample.Point.beta"), _internal(path, "sample.Beta")),
    }
    assert_relationship_endpoints_classified(result)


INLINE_MODULE_IMPLS = """\
mod first {
    pub struct Shared;

    impl Shared {
        pub fn ping() {}
    }
}

mod second {
    pub struct Shared;

    impl Shared {
        pub fn pong() {}
    }
}
"""


def test_inline_module_impls_resolve_within_their_own_scope(tmp_path: Path) -> None:
    """A duplicated type name in two modules never cross-resolves by first match."""
    result, path = _parse(tmp_path, INLINE_MODULE_IMPLS)

    assert {entity.id for entity in result.entities} == {
        _internal(path, "sample"),
        _internal(path, "sample.first"),
        _internal(path, "sample.first.Shared"),
        _internal(path, "sample.first.Shared.ping"),
        _internal(path, "sample.second"),
        _internal(path, "sample.second.Shared"),
        _internal(path, "sample.second.Shared.pong"),
    }
    assert _edges(result, RelationshipKind.CONTAINS) == {
        (_internal(path, "sample"), _internal(path, "sample.first")),
        (_internal(path, "sample"), _internal(path, "sample.second")),
        (_internal(path, "sample.first"), _internal(path, "sample.first.Shared")),
        (_internal(path, "sample.second"), _internal(path, "sample.second.Shared")),
        (
            _internal(path, "sample.first.Shared"),
            _internal(path, "sample.first.Shared.ping"),
        ),
        (
            _internal(path, "sample.second.Shared"),
            _internal(path, "sample.second.Shared.pong"),
        ),
    }
    assert_relationship_endpoints_classified(result)


def test_impl_for_nonlocal_type_does_not_fabricate_a_type_endpoint(
    tmp_path: Path,
) -> None:
    """A foreign implementing type is recorded as metadata, never as an edge source."""
    result, path = _parse(
        tmp_path,
        "trait Ping {\n}\n\nimpl Ping for Vec<u8> {\n    fn ping(&self) {}\n}\n",
    )

    method_id = _internal(path, "sample.Vec.ping")
    assert _edges(result, RelationshipKind.IMPLEMENTS) == {
        (method_id, _internal(path, "sample.Ping")),
    }
    assert [
        rel.source_id
        for rel in result.relationships
        if rel.kind is RelationshipKind.CONTAINS and rel.target_id == method_id
    ] == [_internal(path, "sample")]
    method = _entity(result, method_id)
    assert method.metadata["associated_type"] == "Vec"
    assert method.metadata["associated_type_endpoint"] == _unresolved(
        path, "sample.Vec.ping", "Vec"
    )
    assert_relationship_endpoints_classified(result)


def test_struct_field_types_resolve_locally_or_stay_unresolved(tmp_path: Path) -> None:
    """Field type usage points at a real local type or an explicit unresolved id."""
    result, path = _parse(
        tmp_path,
        "struct Inner;\n\nstruct Outer {\n    inner: Inner,\n    items: Vec<Inner>,\n}\n",
    )

    assert _edges(result, RelationshipKind.USES_TYPE) == {
        (_internal(path, "sample.Outer.inner"), _internal(path, "sample.Inner")),
        (
            _internal(path, "sample.Outer.items"),
            _unresolved(path, "sample.Outer.items", "Vec"),
        ),
    }
    assert_relationship_endpoints_classified(result)


def test_use_declarations_emit_external_rust_references(tmp_path: Path) -> None:
    """Imports use the canonical external namespace rather than a bare prefix."""
    result, path = _parse(tmp_path, "use std::collections::HashMap;\n")

    assert _edges(result, RelationshipKind.IMPORTS) == {
        (
            _internal(path, "sample"),
            build_external_reference_id("rust", "std::collections::HashMap"),
        ),
    }
    assert_relationship_endpoints_classified(result)


CALL_RESOLUTION = """\
fn helper() -> i32 { 1 }

struct Point;

impl Point {
    fn new() -> Point { Point }
    fn build() -> Point { Point::new() }
}

fn main() {
    let value = helper();
    let point = Point::new();
    let other = missing_helper();
}
"""


def test_calls_resolve_locally_or_become_scoped_unresolved_references(
    tmp_path: Path,
) -> None:
    """Unambiguous local callees resolve; unknown callees stay explicitly unresolved."""
    result, path = _parse(tmp_path, CALL_RESOLUTION)

    assert _edges(result, RelationshipKind.CALLS) == {
        (_internal(path, "sample.Point.build"), _internal(path, "sample.Point.new")),
        (_internal(path, "sample.main"), _internal(path, "sample.helper")),
        (_internal(path, "sample.main"), _internal(path, "sample.Point.new")),
        (
            _internal(path, "sample.main"),
            _unresolved(path, "sample.main", "missing_helper"),
        ),
    }
    assert_relationship_endpoints_classified(result)


DUPLICATE_METHOD_NAMES = """\
trait Alpha {
}

trait Beta {
}

struct Point;

impl Alpha for Point {
    fn run(&self) {}
}

impl Beta for Point {
    fn run(&self) {}
}
"""


def test_duplicate_method_identity_is_reported_not_duplicated(tmp_path: Path) -> None:
    """Two impls declaring one method name keep one entity and report the loss."""
    result, path = _parse(tmp_path, DUPLICATE_METHOD_NAMES)

    entity_ids = [entity.id for entity in result.entities]
    assert len(entity_ids) == len(set(entity_ids))
    assert entity_ids.count(_internal(path, "sample.Point.run")) == 1
    assert any("sample.Point.run" in error for error in result.errors)
    assert_relationship_endpoints_classified(result)


AMBIGUOUS_TYPE_DECLARATIONS = """\
struct Shape;

enum Shape {
    Square,
}

impl Shape {
    fn area() {}
}
"""


def test_ambiguous_type_declaration_is_never_resolved_by_first_match(
    tmp_path: Path,
) -> None:
    """A name declared twice in one scope cannot silently claim impl methods."""
    result, path = _parse(tmp_path, AMBIGUOUS_TYPE_DECLARATIONS)

    entity_ids = [entity.id for entity in result.entities]
    assert len(entity_ids) == len(set(entity_ids))
    assert any("sample.Shape" in error for error in result.errors)
    method_id = _internal(path, "sample.Shape.area")
    assert [
        rel.source_id
        for rel in result.relationships
        if rel.kind is RelationshipKind.CONTAINS and rel.target_id == method_id
    ] == [_internal(path, "sample")]
    assert_relationship_endpoints_classified(result)


def test_entity_ids_use_canonical_file_identity(tmp_path: Path) -> None:
    """Symlinked and unresolved paths still produce one canonical entity identity."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    source = real_dir / "sample.rs"
    source.write_text("struct Point;\n", encoding="utf-8")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    result = RustParser().parse_file(link_dir / "sample.rs")

    assert {entity.id for entity in result.entities} == {
        build_internal_entity_id(source, "sample"),
        build_internal_entity_id(source, "sample.Point"),
    }
    assert_relationship_endpoints_classified(result)


RICH_RUST_SOURCE = """\
use std::fmt;

trait Draw {
}

#[derive(Debug)]
pub struct Point {
    x: f64,
    y: f64,
}

impl Point {
    /// Build a point.
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }
}

impl Draw for Point {
    fn draw(&self) {}
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "point")
    }
}

mod helpers {
    pub struct Helper;

    impl Helper {
        pub fn assist() {}
    }
}

fn main() {
    let point = Point::new(1.0, 2.0);
}
"""


def test_rich_rust_file_has_only_classified_endpoints(tmp_path: Path) -> None:
    """Every endpoint of a realistic Rust file is internal, external, or unresolved."""
    result, _ = _parse(tmp_path, RICH_RUST_SOURCE)

    assert_relationship_endpoints_classified(result)
    entity_ids = [entity.id for entity in result.entities]
    assert len(entity_ids) == len(set(entity_ids))
    assert result.errors == []


@pytest.mark.parametrize("source", [GENERIC_IMPLS, MULTIPLE_IMPLS, RICH_RUST_SOURCE])
def test_rust_parsing_is_deterministic(tmp_path: Path, source: str) -> None:
    """Repeated parses of one file produce identical entities and edges."""
    path = _write_rust(tmp_path, source)
    parser = RustParser()

    first = parser.parse_file(path)
    second = parser.parse_file(path)

    assert [entity.id for entity in first.entities] == [
        entity.id for entity in second.entities
    ]
    assert [
        (rel.source_id, rel.target_id, rel.kind) for rel in first.relationships
    ] == [(rel.source_id, rel.target_id, rel.kind) for rel in second.relationships]
    assert all(
        classify_endpoint_id(rel.target_id) is not EndpointKind.INVALID
        for rel in first.relationships
    )


def test_module_level_macro_invocation_is_an_unresolved_reference(
    tmp_path: Path,
) -> None:
    """A macro call is explicit unresolved data, not a fabricated macro:: id."""
    result, path = _parse(
        tmp_path,
        "thread_local! {\n    static COUNTER: i32 = 0;\n}\n",
    )

    calls = [rel for rel in result.relationships if rel.kind is RelationshipKind.CALLS]
    assert [(rel.source_id, rel.target_id) for rel in calls] == [
        (
            _internal(path, "sample"),
            _unresolved(path, "sample", "thread_local!"),
        )
    ]
    assert calls[0].metadata["rust_reference"] == "macro"
    assert_relationship_endpoints_classified(result)


def test_use_lists_import_paths_not_punctuation_or_aliases(tmp_path: Path) -> None:
    """Grouped, self, and aliased imports name real paths, never braces or aliases."""
    result, path = _parse(
        tmp_path,
        "use std::fmt::{self, Display};\nuse a::{b::C, d as e};\n",
    )

    module_id = _internal(path, "sample")
    assert _edges(result, RelationshipKind.IMPORTS) == {
        (module_id, build_external_reference_id("rust", "std::fmt")),
        (module_id, build_external_reference_id("rust", "std::fmt::Display")),
        (module_id, build_external_reference_id("rust", "a::b::C")),
        (module_id, build_external_reference_id("rust", "a::d")),
    }
    assert_relationship_endpoints_classified(result)
