# Knowledge Store API

The `KnowledgeStore` is the central repository for the semantic graph derived from the codebase. It persists the graph to a JSON file and provides query mechanisms.

## Class: `KnowledgeStore`

**Module:** `knowcode.storage.knowledge_store`

### Initialization

```python
store = KnowledgeStore()
```

### Persistence

#### `save(path: str | Path)`
Saves the current graph, including entities, relationships, and metadata, to a JSON file (default `knowcode_knowledge.json`).

#### `load(path: str | Path) -> KnowledgeStore`
Class method to load a store from a JSON file (or directory containing the file).

### Core Properties

- **`entities`**: A dictionary mapping entity IDs to `Entity` objects.
- **`relationships`**: A list of `Relationship` objects.
- **`metadata`**: A dictionary containing scan statistics (scan time, file count) and errors.

### Query Methods

#### `get_entity(entity_id: str) -> Optional[Entity]`
Retrieve an entity object by its unique ID.

#### `search(pattern: str) -> list[Entity]`
Search for entities where the name or qualified name matches the substring pattern (case-insensitive).

#### `get_callers(entity_id: str) -> list[Entity]`
Find all entities that call the target entity (incoming `CALLS` edges).

#### `get_callees(entity_id: str) -> list[Entity]`
Find all entities that are called by the source entity (outgoing `CALLS` edges).

#### `get_children(entity_id: str) -> list[Entity]`
Find all entities contained within the source entity (e.g., methods within a class).

#### `get_parent(entity_id: str) -> Optional[Entity]`
Find the container of an entity (e.g., the class containing a method).

#### `get_dependencies(entity_id: str) -> list[Entity]`
Get all entities that the target entity depends on via calls or imports.

#### `get_dependents(entity_id: str) -> list[Entity]`
Get all entities that depend on the target entity via calls or imports.

#### `get_entities_by_kind(kind: EntityKind | str) -> list[Entity]`
List all entities of a specific kind (e.g., `EntityKind.CLASS`, "function").

#### `trace_calls(entity_id: str, direction: str = "callees", depth: int = 1, max_results: int = 50) -> list[dict[str, Any]]`
Multi-hop call graph traversal starting from an entity.

- `direction="callees"`: what the entity calls
- `direction="callers"`: what calls the entity

Each result includes `call_depth` (hops from the starting entity) plus basic location metadata.

#### `get_impact(entity_id: str, max_depth: int = 3) -> dict[str, Any]`
Impact analysis for modifying/deleting an entity.

Returns:
- `direct_dependents`: 1-hop callers/importers
- `transitive_dependents`: multi-hop dependents up to `max_depth`
- `affected_files`: files likely requiring review
- `risk_score`: 0.0–1.0 risk estimate

### Data Models

#### `Entity`
- `id`: Unique identifier (path + :: + qualified name)
- `kind`: `EntityKind` (module, class, function, method, etc.)
- `name`: Short name
- `qualified_name`: Full dotted path
- `location`: File path and line range
- `source_code`: Raw source code (optional)
- `docstring`: Extracted docstring (optional)

#### `Relationship`
- `source_id`: Origin entity ID
- `target_id`: Destination entity ID
- `kind`: `RelationshipKind` (calls, imports, contains, inherits, etc.)
