"""Complete unit tests for Vue parser (all tests merged)."""

from pathlib import Path
import tempfile

from knowcode.parsers.vue_parser import VueParser
from knowcode.data_models import EntityKind, RelationshipKind


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
        refs = [e for e in result.entities if e.kind == EntityKind.VARIABLE and "ref::" in e.id]
        ref_names = {r.name for r in refs}
        assert "message" in ref_names
        assert "count" in ref_names

        # Find event handler relationship
        event_rels = [r for r in result.relationships if r.kind == RelationshipKind.CALLS and "handleClick" in r.target_id]
        assert len(event_rels) >= 1
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

        # Find prop entities
        props = [e for e in result.entities if "prop::" in e.id]
        prop_names = {p.name for p in props}
        assert "title" in prop_names
        assert "count" in prop_names
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

        # Find emit entities
        emits = [e for e in result.entities if "emit::" in e.id]
        emit_names = {e.name for e in emits}
        assert "update" in emit_names
        assert "close" in emit_names
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

        # Find data properties
        data_props = [e for e in result.entities if "data::" in e.id]
        data_names = {d.name for d in data_props}
        assert "message" in data_names
        assert "count" in data_names
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

        # Find method entities
        methods = [e for e in result.entities if e.kind == EntityKind.FUNCTION]
        method_names = {m.name for m in methods}
        assert "increment" in method_names
        assert "reset" in method_names

        # Check event handler relationship
        event_rels = [r for r in result.relationships if r.kind == RelationshipKind.CALLS and "increment" in r.target_id]
        assert len(event_rels) >= 1
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

        # Find computed properties
        computed = [e for e in result.entities if "computed::" in e.id]
        computed_names = {c.name for c in computed}
        assert "fullName" in computed_names
        assert "reversedName" in computed_names
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

        # Find v-model relationships
        model_rels = [r for r in result.relationships if r.kind == RelationshipKind.REFERENCES and "data::" in r.target_id]
        # At least username and email should be referenced
        assert len(model_rels) >= 2
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
        refs = [e for e in result.entities if "ref::" in e.id]
        ref_names = {r.name for r in refs}
        assert "message" in ref_names
        assert "user" in ref_names
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
        props = [e for e in result.entities if "prop::" in e.id]
        prop_names = {p.name for p in props}
        assert "firstName" in prop_names
        assert "lastName" in prop_names
        assert "initialAge" in prop_names

        # Check emits
        emits = [e for e in result.entities if "emit::" in e.id]
        emit_names = {e.name for e in emits}
        assert "update" in emit_names
        assert "navigate" in emit_names

        # Check refs
        refs = [e for e in result.entities if "ref::" in e.id]
        ref_names = {r.name for r in refs}
        assert "age" in ref_names
        assert "nickname" in ref_names

        # Check composable usage
        composable_calls = [r for r in result.relationships if "composable::useRouter" in r.target_id]
        assert len(composable_calls) >= 1

        # Check event handlers
        event_handlers = [r for r in result.relationships if r.kind == RelationshipKind.CALLS and "incrementAge" in r.target_id]
        assert len(event_handlers) >= 1

        # Check v-model
        v_models = [r for r in result.relationships if r.kind == RelationshipKind.REFERENCES and "nickname" in r.target_id]
        assert len(v_models) >= 1
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
        prop_entities = [e for e in result.entities if "::prop::" in e.id]
        prop_names = {e.name for e in prop_entities}

        assert "title" in prop_names
        assert "count" in prop_names
        assert "isActive" in prop_names
        assert "user" in prop_names

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
        count_entity = [e for e in result.entities if e.name == "count" and "::data::" in e.id][0]
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

        # Check event handler relationships
        handler_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.CALLS
            and "::method::" in r.target_id or "::function::" in r.target_id
        ]

        # Should only have 3 valid method handlers
        handler_names = [r.target_id.split("::")[-1] for r in handler_rels]

        assert "handleClick" in handler_names
        assert "onSubmit" in handler_names
        assert "doSomething" in handler_names

        # Should NOT have these
        assert "count" not in handler_names
        assert "isActive" not in handler_names
        assert "value" not in handler_names
        assert "item" not in handler_names
        assert "$emit" not in handler_names

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

        # Check CSS binding relationships
        css_binding_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.REFERENCES
            and r.metadata.get("binding_type") == "css_variable"
        ]

        # Should have 3 CSS bindings
        assert len(css_binding_rels) >= 3

        bound_vars = [r.target_id.split("::")[-1] for r in css_binding_rels]

        assert "themeColor" in bound_vars
        assert "fontSize" in bound_vars
        assert "primaryColor" in bound_vars

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

        # Check that kebab-case is converted to camelCase
        css_binding_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.REFERENCES
            and r.metadata.get("binding_type") == "css_variable"
        ]

        bound_vars = [r.target_id.split("::")[-1] for r in css_binding_rels]

        # Should be camelCase
        assert "primaryColor" in bound_vars
        assert "borderWidth" in bound_vars

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


def test_arrow_function_id_format():
    """Test that arrow functions have correct ID format (::function:: not ::const::)."""
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

        # Check arrow function ID
        arrow_funcs = [e for e in result.entities if e.name == "myArrowFunc"]
        assert len(arrow_funcs) == 1
        assert "::function::" in arrow_funcs[0].id

        # Check regular variable ID
        regular_vars = [e for e in result.entities if e.name == "regularVar"]
        assert len(regular_vars) == 1
        assert "::const::" in regular_vars[0].id

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

        # Check event handler relationships
        handler_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.CALLS
        ]

        handler_names = [r.target_id.split("::")[-1] for r in handler_rels]

        # Should only capture valid method handlers
        assert "reset" in handler_names
        assert "increment" in handler_names

        # Should NOT capture these complex expressions
        assert "search" not in handler_names
        assert "items" not in handler_names
        assert "console" not in handler_names
        assert "$event" not in handler_names

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

        # 2. Check event handlers are filtered correctly
        handler_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.CALLS
        ]
        handler_names = [r.target_id.split("::")[-1] for r in handler_rels]
        assert "increment" in handler_names
        assert "handleReset" in handler_names
        # Should NOT have inline expressions
        assert "count" not in handler_names
        assert "$emit" not in handler_names

        # 3. Check CSS variable bindings
        css_binding_rels = [
            r for r in result.relationships
            if r.kind == RelationshipKind.REFERENCES
            and r.metadata.get("binding_type") == "css_variable"
        ]
        # Should have 2 CSS bindings (both themeColor)
        assert len(css_binding_rels) >= 1

    finally:
        temp_path.unlink()
