"""Complete unit tests for Vue parser (all tests merged)."""

from pathlib import Path
import tempfile

from knowcode.parsers.vue_parser import VueParser
from knowcode.data_models import EntityKind, ParseResult, RelationshipKind
from knowcode.utils.entity_identity import build_external_reference_id


# ============================================================================
# SHARED ASSERTION HELPERS
# ============================================================================
#
# Vue entity IDs are canonical ``<file>::<qualified name>`` values, so a test
# cannot identify a declaration by an ID substring. Declarations are selected by
# their recorded semantic category, and edges are checked against the entity the
# same parse produced rather than against a name appearing somewhere in a target
# string.


def declarations_of(result: ParseResult, declaration_type: str) -> list:
    """Return entities the parser classified as ``declaration_type``."""
    return [
        entity
        for entity in result.entities
        if entity.metadata.get("declaration_type") == declaration_type
    ]


def declaration_names(result: ParseResult, declaration_type: str) -> set[str]:
    return {entity.name for entity in declarations_of(result, declaration_type)}


def entity_named(result: ParseResult, name: str):
    """Return the single entity called ``name``, failing if it is not unique."""
    matches = [entity for entity in result.entities if entity.name == name]
    assert len(matches) == 1, (
        f"expected exactly one entity named {name!r}, got "
        f"{[e.qualified_name for e in result.entities]}"
    )
    return matches[0]


def binding_targets(result: ParseResult, kind: RelationshipKind, binding_type: str) -> set[str]:
    """Return the endpoints of every template or style edge of one binding type."""
    return {
        relationship.target_id
        for relationship in result.relationships
        if relationship.kind is kind
        and relationship.metadata.get("binding_type") == binding_type
    }


def assert_binds_to_entities(
    result: ParseResult,
    kind: RelationshipKind,
    binding_type: str,
    names: set[str],
) -> None:
    """Assert one edge per name, each resolving to that name's real entity."""
    assert binding_targets(result, kind, binding_type) == {
        entity_named(result, name).id for name in names
    }


# ============================================================================
# CORE FUNCTIONALITY TESTS
# ============================================================================

def test_parse_simple_vue_component():
    """Test parsing a simple Vue component."""
    vue_code = """
<template>
  <div>Hello World</div>
</template>

<script>
export default {
  name: 'HelloWorld'
}
</script>

<style scoped>
div { color: blue; }
</style>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Should have component entity
        assert len(result.entities) >= 1
        components = [e for e in result.entities if e.kind == EntityKind.CLASS]
        assert len(components) >= 1

        # Component name should be derived from filename or component definition
        assert len(result.errors) == 0
    finally:
        temp_path.unlink()


def test_parse_composition_api_with_script_setup():
    """Test parsing Vue 3 Composition API with <script setup>."""
    vue_code = """
<template>
  <div @click="handleClick">{{ message }}</div>
</template>

<script setup>
import { ref } from 'vue'

const message = ref('Hello')
const count = ref(0)

function handleClick() {
  count.value++
}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find ref entities
        refs = {
            entity.name
            for entity in result.entities
            if entity.kind == EntityKind.VARIABLE
            and entity.metadata.get("is_reactive") == "true"
        }
        assert refs == {"message", "count"}

        # The event handler must resolve to the extracted function itself.
        assert_binds_to_entities(
            result, RelationshipKind.CALLS, "event", {"handleClick"}
        )
    finally:
        temp_path.unlink()


def test_parse_composition_api_with_define_props():
    """Test parsing defineProps in <script setup>."""
    vue_code = """
<template>
  <div>{{ title }}</div>
</template>

<script setup>
const props = defineProps({
  title: String,
  count: Number
})
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        assert declaration_names(result, "prop") == {"title", "count"}
    finally:
        temp_path.unlink()


def test_parse_composition_api_with_define_emits():
    """Test parsing defineEmits in <script setup>."""
    vue_code = """
<template>
  <button @click="emit('update', value)">Update</button>
</template>

<script setup>
const emit = defineEmits(['update', 'close'])
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        assert declaration_names(result, "emit") == {"update", "close"}
        # Events live in their own name space so they cannot collide with a
        # method or ref of the same name.
        component = result.entities[0].qualified_name
        assert {
            entity.qualified_name for entity in declarations_of(result, "emit")
        } == {f"{component}.emits.update", f"{component}.emits.close"}
    finally:
        temp_path.unlink()


def test_parse_options_api_with_data():
    """Test parsing Options API with data properties."""
    vue_code = """
<template>
  <div>{{ message }}</div>
</template>

<script>
export default {
  data() {
    return {
      message: 'Hello',
      count: 0
    }
  }
}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        assert declaration_names(result, "data") == {"message", "count"}
    finally:
        temp_path.unlink()


def test_parse_options_api_with_methods():
    """Test parsing Options API with methods."""
    vue_code = """
<template>
  <button @click="increment">{{ count }}</button>
</template>

<script>
export default {
  data() {
    return { count: 0 }
  },
  methods: {
    increment() {
      this.count++
    },
    reset() {
      this.count = 0
    }
  }
}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Options API methods are class-body callables, so they are methods.
        methods = declarations_of(result, "method")
        assert {method.name for method in methods} == {"increment", "reset"}
        assert {method.kind for method in methods} == {EntityKind.METHOD}

        assert_binds_to_entities(
            result, RelationshipKind.CALLS, "event", {"increment"}
        )
    finally:
        temp_path.unlink()


def test_parse_options_api_with_computed():
    """Test parsing Options API with computed properties."""
    vue_code = """
<script>
export default {
  data() {
    return { firstName: 'John', lastName: 'Doe' }
  },
  computed: {
    fullName() {
      return this.firstName + ' ' + this.lastName
    },
    reversedName: {
      get() {
        return this.lastName + ', ' + this.firstName
      }
    }
  }
}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        computed_names = declaration_names(result, "computed")
        assert {"fullName", "reversedName"} <= computed_names
    finally:
        temp_path.unlink()


def test_parse_component_imports():
    """Test parsing component imports."""
    vue_code = """
<template>
  <div>
    <MyButton />
    <UserCard />
  </div>
</template>

<script setup>
import MyButton from './components/MyButton.vue'
import UserCard from './components/UserCard.vue'
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find import relationships
        imports = [r for r in result.relationships if r.kind == RelationshipKind.IMPORTS]
        import_targets = {r.target_id for r in imports}
        assert any("MyButton" in target for target in import_targets)
        assert any("UserCard" in target for target in import_targets)
    finally:
        temp_path.unlink()


def test_parse_composables():
    """Test parsing composable usage (useXxx)."""
    vue_code = """
<script setup>
import { useRouter } from 'vue-router'
import { useStore } from 'vuex'
import { useCustom } from '@/composables/useCustom'

const router = useRouter()
const store = useStore()
const { data } = useCustom()
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Find composable call relationships
        composable_calls = [r for r in result.relationships if r.kind == RelationshipKind.CALLS and "composable::" in r.target_id]
        composable_names = {r.target_id.split("::")[-1] for r in composable_calls}
        assert "useRouter" in composable_names
        assert "useStore" in composable_names
        assert "useCustom" in composable_names
    finally:
        temp_path.unlink()


def test_parse_v_model():
    """Test parsing v-model bindings."""
    vue_code = """
<template>
  <div>
    <input v-model="username" />
    <input v-model:value="email" />
  </div>
</template>

<script setup>
import { ref } from 'vue'
const username = ref('')
const email = ref('')
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Both bindings must resolve to the refs the same parse extracted.
        assert_binds_to_entities(
            result, RelationshipKind.REFERENCES, "model", {"username", "email"}
        )
    finally:
        temp_path.unlink()


def test_parse_typescript_script():
    """Test parsing Vue component with TypeScript."""
    vue_code = """
<template>
  <div>{{ message }}</div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

interface User {
  name: string
  age: number
}

const message = ref<string>('Hello')
const user = ref<User>({ name: 'John', age: 30 })
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Should parse without errors
        assert len(result.errors) == 0

        # Find ref entities
        refs = {
            entity.name
            for entity in result.entities
            if entity.metadata.get("is_reactive") == "true"
        }
        assert {"message", "user"} <= refs
    finally:
        temp_path.unlink()


def test_parse_component_name_from_filename():
    """Test that component name is derived from filename."""
    vue_code = """
<template>
  <div>Component</div>
</template>
"""
    parser = VueParser()

    # Create file with kebab-case name
    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False, prefix='my-custom-component-') as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Component should exist
        components = [e for e in result.entities if e.kind == EntityKind.CLASS]
        assert len(components) >= 1

        # Name should be in PascalCase
        component = components[0]
        # Should be some variation of MyCustomComponent
        assert component.name[0].isupper()  # First letter capitalized
    finally:
        temp_path.unlink()


def test_parse_empty_vue_file():
    """Test parsing an empty Vue file."""
    vue_code = ""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Should have at least a component entity
        assert len(result.entities) >= 1
        assert len(result.errors) == 0
    finally:
        temp_path.unlink()


def test_parse_complex_vue_component():
    """Test parsing a complex real-world Vue component."""
    vue_code = """
<template>
  <div class="user-profile">
    <h1>{{ fullName }}</h1>
    <p>Age: {{ age }}</p>
    <button @click="incrementAge">Birthday</button>
    <input v-model="nickname" placeholder="Nickname" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'

// Props
const props = defineProps({
  firstName: String,
  lastName: String,
  initialAge: Number
})

// Emits
const emit = defineEmits(['update', 'navigate'])

// Composables
const router = useRouter()

// State
const age = ref(props.initialAge || 0)
const nickname = ref('')

// Computed
const fullName = computed(() => {
  return `${props.firstName} ${props.lastName}`
})

// Methods
function incrementAge() {
  age.value++
  emit('update', age.value)
}

function goBack() {
  router.back()
  emit('navigate', 'back')
}
</script>

<style scoped>
.user-profile { padding: 20px; }
</style>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check component entity
        assert len(result.entities) > 5

        # Check props
        assert {"firstName", "lastName", "initialAge"} <= declaration_names(
            result, "prop"
        )

        # Check emits
        assert {"update", "navigate"} <= declaration_names(result, "emit")

        # Check refs
        refs = {
            entity.name
            for entity in result.entities
            if entity.metadata.get("is_reactive") == "true"
        }
        assert {"age", "nickname"} <= refs

        # Check composable usage. useRouter is imported, not declared here, so
        # it stays an explicit external reference.
        assert build_external_reference_id("composable", "useRouter") in {
            relationship.target_id for relationship in result.relationships
        }

        # Check event handlers and v-model resolve to real entities
        assert (
            entity_named(result, "incrementAge").id
            in binding_targets(result, RelationshipKind.CALLS, "event")
        )
        assert (
            entity_named(result, "nickname").id
            in binding_targets(result, RelationshipKind.REFERENCES, "model")
        )
    finally:
        temp_path.unlink()


# ============================================================================
# ENHANCEMENT TESTS
# ============================================================================

def test_comprehensive_script_setup_declarations():
    """Test that ALL top-level declarations in <script setup> are captured."""
    vue_code = """
<template>
  <div>{{ count }} {{ message }}</div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'

// All these should be captured
const count = ref(0)
let isActive = true
var oldStyle = 'test'

const increment = () => { count.value++ }
const decrement = () => { count.value-- }

function greet() {
  console.log('Hello')
}

async function fetchData() {
  return await fetch('/api')
}

interface User {
  name: string
  age: number
}

type Status = 'active' | 'inactive'

const { x, y } = reactive({ x: 0, y: 0 })
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check that all variables are captured
        var_entities = [e for e in result.entities if e.kind == EntityKind.VARIABLE]
        var_names = {e.name for e in var_entities}

        assert "count" in var_names
        assert "isActive" in var_names
        assert "oldStyle" in var_names
        assert "x" in var_names
        assert "y" in var_names

        # Check that functions are captured
        func_entities = [e for e in result.entities if e.kind == EntityKind.FUNCTION]
        func_names = {e.name for e in func_entities}

        assert "increment" in func_names
        assert "decrement" in func_names
        assert "greet" in func_names
        assert "fetchData" in func_names

        # Check metadata
        count_entity = [e for e in result.entities if e.name == "count"][0]
        assert count_entity.metadata["vue_api"] == "composition"
        assert count_entity.metadata["is_reactive"] == "true"
        assert count_entity.metadata["exposed_to_template"] == "true"

        greet_entity = [e for e in result.entities if e.name == "greet"][0]
        assert greet_entity.metadata["vue_api"] == "composition"
        assert greet_entity.metadata["declaration_type"] == "function"

    finally:
        temp_path.unlink()


def test_extension_less_imports():
    """Test that imports work without .vue extension and with aliases."""
    vue_code = """
<template>
  <div>Test</div>
</template>

<script setup>
import MyButton from './components/MyButton'
import UserCard from '@/components/UserCard'
import { FormInput, FormSelect } from './forms'
import Modal from './Modal.vue'
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check imports
        import_rels = [r for r in result.relationships if r.kind == RelationshipKind.IMPORTS]
        imported_components = {r.target_id.split("::")[-1] for r in import_rels}

        assert "MyButton" in imported_components
        assert "UserCard" in imported_components
        assert "FormInput" in imported_components
        assert "FormSelect" in imported_components
        assert "Modal" in imported_components

        # Check metadata
        button_import = [r for r in import_rels if "MyButton" in r.target_id][0]
        assert "import_path" in button_import.metadata
        assert button_import.metadata["import_path"] == "./components/MyButton"

    finally:
        temp_path.unlink()


def test_nested_templates():
    """Test that nested templates are handled correctly."""
    vue_code = """
<template>
  <div>
    <template v-if="isActive">
      <span>Active</span>
      <template v-for="item in items">
        <p>{{ item }}</p>
      </template>
    </template>
  </div>
</template>

<script setup>
const isActive = true
const items = ['a', 'b', 'c']
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check that template section was extracted correctly
        # If this doesn't crash, the nested template handling works
        assert len(result.entities) > 0

        # Check that variables are found
        var_names = {e.name for e in result.entities if e.kind == EntityKind.VARIABLE}
        assert "isActive" in var_names
        assert "items" in var_names

    finally:
        temp_path.unlink()


def test_component_usage_extraction():
    """Test that component usage in templates is detected and linked."""
    vue_code = """
<template>
  <div>
    <MyButton @click="handleClick" />
    <UserCard :user="currentUser" />
    <form-input v-model="email" />
    <Modal>
      <template #header>
        <h1>Title</h1>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import MyButton from './MyButton'
import UserCard from './UserCard'
import Modal from './Modal'

const email = ref('')
const currentUser = ref(null)

function handleClick() {
  console.log('clicked')
}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check component usage relationships
        usage_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.REFERENCES
            and "vue_component" in r.target_id
            and r.metadata.get("usage_type") == "template"
        ]

        used_components = {r.target_id.split("::")[-1] for r in usage_rels}

        assert "MyButton" in used_components
        assert "UserCard" in used_components
        assert "FormInput" in used_components  # kebab-case converted to PascalCase
        assert "Modal" in used_components

    finally:
        temp_path.unlink()


def test_multiline_typescript_defineprops():
    """Test that multi-line TypeScript interfaces in defineProps are parsed."""
    vue_code = """
<template>
  <div>{{ title }} - {{ count }}</div>
</template>

<script setup lang="ts">
const props = defineProps<{
  title: string
  count: number
  isActive?: boolean
  user: {
    name: string
    age: number
  }
}>()
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check that props are extracted
        assert {"title", "count", "isActive", "user"} <= declaration_names(
            result, "prop"
        )

    finally:
        temp_path.unlink()


def test_enhanced_metadata():
    """Test that enhanced metadata is added to all entities."""
    vue_code = """
<template>
  <div>{{ message }}</div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const message = ref('Hello')
const count = 0
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check component metadata
        component = result.entities[0]
        assert component.metadata["component_type"] == "vue_sfc"
        assert component.metadata["vue_api"] == "composition"
        assert component.metadata["has_template"] == "true"
        assert component.metadata["has_script"] == "true"
        assert component.metadata["script_lang"] == "ts"

        # Check variable metadata
        message_entity = [e for e in result.entities if e.name == "message"][0]
        assert message_entity.metadata["vue_api"] == "composition"
        assert message_entity.metadata["is_reactive"] == "true"

    finally:
        temp_path.unlink()


def test_options_api_metadata():
    """Test that Options API entities have correct metadata."""
    vue_code = """
<template>
  <div>{{ count }}</div>
</template>

<script>
export default {
  data() {
    return {
      count: 0,
      message: 'Hello'
    }
  },
  methods: {
    increment() {
      this.count++
    }
  },
  computed: {
    doubled() {
      return this.count * 2
    }
  }
}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check component metadata
        component = result.entities[0]
        assert component.metadata["vue_api"] == "options"

        # Check data property metadata
        count_entity = entity_named(result, "count")
        assert count_entity.metadata["vue_api"] == "options"
        assert count_entity.metadata["declaration_type"] == "data"
        assert count_entity.metadata["is_reactive"] == "true"

        # Check method metadata
        increment_entity = [e for e in result.entities if e.name == "increment"][0]
        assert increment_entity.metadata["vue_api"] == "options"
        assert increment_entity.metadata["declaration_type"] == "method"

        # Check computed metadata
        doubled_entity = [e for e in result.entities if e.name == "doubled"][0]
        assert doubled_entity.metadata["vue_api"] == "options"
        assert doubled_entity.metadata["declaration_type"] == "computed"
        assert doubled_entity.metadata["is_reactive"] == "true"

    finally:
        temp_path.unlink()


def test_component_tree_building():
    """Test that the full component tree can be built from imports and usage."""
    vue_code = """
<template>
  <div>
    <Header />
    <main>
      <UserProfile :user="user" />
      <ActionButton @click="save" />
    </main>
    <Footer />
  </div>
</template>

<script setup>
import Header from '@/components/Header'
import Footer from '@/components/Footer'
import UserProfile from './UserProfile'
import ActionButton from './ActionButton'

const user = ref({ name: 'John' })

function save() {
  console.log('saving')
}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check imports
        import_rels = [r for r in result.relationships if r.kind == RelationshipKind.IMPORTS]
        assert len(import_rels) == 4

        # Check usage
        usage_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.REFERENCES
            and "vue_component" in r.target_id
            and r.metadata.get("usage_type") == "template"
        ]
        assert len(usage_rels) == 4

        # Verify that we can build UI component tree
        # (Both imports and usage should exist for each component)
        imported = {r.target_id.split("::")[-1] for r in import_rels}
        used = {r.target_id.split("::")[-1] for r in usage_rels}

        assert imported == used
        assert imported == {"Header", "Footer", "UserProfile", "ActionButton"}

    finally:
        temp_path.unlink()


# ============================================================================
# REFINEMENT TESTS
# ============================================================================

def test_expression_based_event_handlers_filtered():
    """Test that inline expressions in event handlers are filtered out."""
    vue_code = """
<template>
  <div>
    <!-- These should NOT be captured as method handlers -->
    <button @click="count++">Increment</button>
    <button @click="count--">Decrement</button>
    <button @click="isActive = true">Activate</button>
    <button @click="value += 5">Add</button>
    <button @click="item.show = false">Hide</button>
    <button @click="$emit('close')">Emit</button>

    <!-- These SHOULD be captured as method handlers -->
    <button @click="handleClick">Handle</button>
    <button v-on:submit="onSubmit">Submit</button>

    <!-- Function calls with args should capture function name -->
    <button @click="doSomething(123)">Do</button>
  </div>
</template>

<script setup>
const count = ref(0)
const isActive = ref(false)
const value = ref(0)

function handleClick() {}
function onSubmit() {}
function doSomething(arg) {}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Inline expressions are not handlers, so exactly the three declared
        # functions may be event targets, each resolved to its own entity.
        assert_binds_to_entities(
            result,
            RelationshipKind.CALLS,
            "event",
            {"handleClick", "onSubmit", "doSomething"},
        )

    finally:
        temp_path.unlink()


def test_css_variable_binding():
    """Test that v-bind() in CSS creates REFERENCES relationships."""
    vue_code = """
<template>
  <div class="themed">{{ message }}</div>
</template>

<script setup>
import { ref } from 'vue'

const themeColor = ref('#42b983')
const fontSize = ref('16px')
const primaryColor = ref('#409eff')
</script>

<style scoped>
.themed {
  color: v-bind(themeColor);
  font-size: v-bind(fontSize);
  background: v-bind('primaryColor');
}
</style>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Each bound name must resolve to the ref the same parse extracted.
        assert_binds_to_entities(
            result,
            RelationshipKind.REFERENCES,
            "css_variable",
            {"themeColor", "fontSize", "primaryColor"},
        )

    finally:
        temp_path.unlink()


def test_css_variable_kebab_case_conversion():
    """Test that kebab-case CSS variables are converted to camelCase."""
    vue_code = """
<template>
  <div>Test</div>
</template>

<script setup>
const primaryColor = ref('#409eff')
const borderWidth = ref('2px')
</script>

<style scoped>
.box {
  color: v-bind(primary-color);
  border-width: v-bind(border-width);
}
</style>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Kebab-case is converted to camelCase, so both bindings resolve to the
        # camelCase refs rather than becoming unresolved references.
        assert_binds_to_entities(
            result,
            RelationshipKind.REFERENCES,
            "css_variable",
            {"primaryColor", "borderWidth"},
        )

    finally:
        temp_path.unlink()


def test_arrow_function_entity_kind():
    """Test that arrow functions are classified as FUNCTION entities, not VARIABLE."""
    vue_code = """
<template>
  <div>Test</div>
</template>

<script setup>
// Regular functions
function regularFunc() {}
async function asyncFunc() {}

// Arrow functions (should be FUNCTION entities)
const arrowFunc = () => {}
const asyncArrow = async () => {}
const arrowWithArgs = (a, b) => a + b

// Regular variables (should be VARIABLE entities)
const count = ref(0)
const message = 'Hello'
let isActive = true
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Check function entities
        function_entities = [e for e in result.entities if e.kind == EntityKind.FUNCTION]
        func_names = {f.name for f in function_entities}

        # Regular functions
        assert "regularFunc" in func_names
        assert "asyncFunc" in func_names

        # Arrow functions should be FUNCTION entities
        assert "arrowFunc" in func_names
        assert "asyncArrow" in func_names
        assert "arrowWithArgs" in func_names

        # Check that arrow functions have correct metadata
        arrow_func = [f for f in function_entities if f.name == "arrowFunc"][0]
        assert arrow_func.metadata["vue_api"] == "composition"
        assert arrow_func.metadata["declaration_type"] == "function"
        assert arrow_func.metadata["exposed_to_template"] == "true"

        # Check variable entities
        variable_entities = [e for e in result.entities if e.kind == EntityKind.VARIABLE]
        var_names = {v.name for v in variable_entities}

        # These should be VARIABLE entities
        assert "count" in var_names
        assert "message" in var_names
        assert "isActive" in var_names

        # Arrow functions should NOT be in variables
        assert "arrowFunc" not in var_names
        assert "asyncArrow" not in var_names
        assert "arrowWithArgs" not in var_names

    finally:
        temp_path.unlink()


def test_declaration_kind_is_metadata_not_id_syntax():
    """Arrow functions and plain constants share the canonical ID shape.

    The declaration category used to be encoded in the ID as ``::function::`` or
    ``::const::``, which produced internal-looking endpoints that matched no
    entity. Category now travels in the entity's kind and metadata, while the ID
    stays ``<file>::<qualified name>``.
    """
    vue_code = """
<template>
  <div>Test</div>
</template>

<script setup>
const myArrowFunc = () => {}
const regularVar = 123
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        component = result.entities[0]

        arrow_func = entity_named(result, "myArrowFunc")
        assert arrow_func.kind == EntityKind.FUNCTION
        assert arrow_func.metadata["declaration_type"] == "function"
        assert arrow_func.id == f"{component.id}.myArrowFunc"

        regular_var = entity_named(result, "regularVar")
        assert regular_var.kind == EntityKind.VARIABLE
        assert regular_var.metadata["declaration_type"] == "const"
        assert regular_var.id == f"{component.id}.regularVar"

    finally:
        temp_path.unlink()


def test_complex_event_handler_expressions():
    """Test filtering of complex event handler expressions."""
    vue_code = """
<template>
  <div>
    <!-- Complex expressions that should be filtered -->
    <button @click="count > 5 ? reset() : increment()">Conditional</button>
    <input @input="search = $event.target.value">Input</input>
    <div @click="items.push({ id: 1 })">Add</div>
    <button @click="console.log('test')">Log</button>

    <!-- Valid method handlers -->
    <button @click="reset()">Reset</button>
    <button @click="increment">Increment</button>
  </div>
</template>

<script setup>
const count = ref(0)

function reset() {}
function increment() {}
</script>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # Only the two declared functions are handlers; every inline expression
        # is filtered out rather than becoming an edge to an invented endpoint.
        assert_binds_to_entities(
            result, RelationshipKind.CALLS, "event", {"reset", "increment"}
        )

    finally:
        temp_path.unlink()


def test_full_stack_component_with_all_refinements():
    """Test a full-stack component using all three refinements together."""
    vue_code = """
<template>
  <div class="card">
    <h1>{{ title }}</h1>
    <p>Count: {{ count }}</p>

    <!-- Valid method handlers -->
    <button @click="increment">+</button>
    <button @click="handleReset">Reset</button>

    <!-- Inline expressions (should be filtered) -->
    <button @click="count = 0">Clear</button>
    <button @click="$emit('update', count)">Emit</button>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const title = ref('Counter')
const count = ref(0)
const themeColor = ref('#42b983')

// Arrow functions (should be FUNCTION entities)
const increment = () => { count.value++ }
const handleReset = async () => {
  count.value = 0
}

// Regular function
function multiply(a, b) {
  return a * b
}
</script>

<style scoped>
.card {
  color: v-bind(themeColor);
  border: 1px solid v-bind('themeColor');
}
</style>
"""
    parser = VueParser()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.vue', delete=False) as f:
        f.write(vue_code)
        f.flush()
        temp_path = Path(f.name)

    try:
        result = parser.parse_file(temp_path)

        # 1. Check arrow functions are FUNCTION entities
        function_entities = [e for e in result.entities if e.kind == EntityKind.FUNCTION]
        func_names = {f.name for f in function_entities}
        assert "increment" in func_names
        assert "handleReset" in func_names
        assert "multiply" in func_names

        # 2. Check event handlers are filtered and resolved correctly
        assert_binds_to_entities(
            result, RelationshipKind.CALLS, "event", {"increment", "handleReset"}
        )

        # 3. Check CSS variable bindings. The same name is bound twice, so it
        # must produce exactly one edge to the ref's entity.
        assert_binds_to_entities(
            result, RelationshipKind.REFERENCES, "css_variable", {"themeColor"}
        )

    finally:
        temp_path.unlink()
