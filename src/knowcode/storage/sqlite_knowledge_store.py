"""SQLite-backed knowledge store."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from knowcode.data_models import Entity, EntityKind, Location, Relationship, RelationshipKind
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.utils.entity_identity import ensure_entity_content_hash


class SqliteKnowledgeStore:
    """SQLite-backed knowledge store with recursive query support."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path) -> None:
        """Initialize SQLite knowledge store."""
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # auto-commit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema."""
        with self._lock:
            # We use isolation_level=None to manually control transactions when needed,
            # but for init we can just execute
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS entities (
                        entity_id TEXT UNIQUE NOT NULL,
                        kind TEXT NOT NULL,
                        name TEXT NOT NULL,
                        qualified_name TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        line_start INT NOT NULL,
                        line_end INT NOT NULL,
                        docstring TEXT,
                        signature TEXT,
                        source_code TEXT,
                        metadata_json TEXT,
                        content_hash TEXT
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS relationships (
                        source_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        metadata_json TEXT
                    )
                    """
                )
                # Indexes
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_qualified_name ON entities(qualified_name)")
                
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_relationships_source_kind ON relationships(source_id, kind)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_relationships_target_kind ON relationships(target_id, kind)")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """Close database connection."""
        self._conn.close()

    def add_entity(self, entity: Entity) -> None:
        """Add an entity to the store."""
        ensure_entity_content_hash(entity)
        metadata_json = json.dumps(entity.metadata) if entity.metadata else "{}"
        
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO entities (
                    entity_id, kind, name, qualified_name, file_path,
                    line_start, line_end, docstring, signature,
                    source_code, metadata_json, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.id,
                    entity.kind.value,
                    entity.name,
                    entity.qualified_name,
                    entity.location.file_path,
                    entity.location.line_start,
                    entity.location.line_end,
                    entity.docstring,
                    entity.signature,
                    entity.source_code,
                    metadata_json,
                    entity.metadata.get("content_hash"),
                ),
            )

    def add_relationship(self, rel: Relationship) -> None:
        """Add a relationship to the store."""
        metadata_json = json.dumps(rel.metadata) if rel.metadata else "{}"
        
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relationships (source_id, target_id, kind, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (rel.source_id, rel.target_id, rel.kind.value, metadata_json),
            )

    def _row_to_entity(self, row: sqlite3.Row) -> Entity:
        """Convert a database row to an Entity."""
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        if row["content_hash"]:
            metadata["content_hash"] = row["content_hash"]
            
        location = Location(
            file_path=row["file_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
        )
        return Entity(
            id=row["entity_id"],
            kind=EntityKind(row["kind"]),
            name=row["name"],
            qualified_name=row["qualified_name"],
            location=location,
            docstring=row["docstring"],
            signature=row["signature"],
            source_code=row["source_code"],
            metadata=metadata,
        )

    def _row_to_relationship(self, row: sqlite3.Row) -> Relationship:
        """Convert a database row to a Relationship."""
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        return Relationship(
            source_id=row["source_id"],
            target_id=row["target_id"],
            kind=RelationshipKind(row["kind"]),
            metadata=metadata,
        )

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Fetch an entity by ID."""
        cursor = self._conn.execute(
            "SELECT * FROM entities WHERE entity_id = ?", (entity_id,)
        )
        row = cursor.fetchone()
        if row:
            return self._row_to_entity(row)
        return None

    def search(self, pattern: str) -> list[Entity]:
        """Search entities by name or qualified name pattern."""
        like_pattern = f"%{pattern}%"
        cursor = self._conn.execute(
            """
            SELECT * FROM entities 
            WHERE name LIKE ? OR qualified_name LIKE ?
            """,
            (like_pattern, like_pattern),
        )
        return [self._row_to_entity(row) for row in cursor]

    def get_callers(self, entity_id: str) -> list[Entity]:
        """Return caller entities."""
        cursor = self._conn.execute(
            """
            SELECT e.* FROM entities e
            JOIN relationships r ON e.entity_id = r.source_id
            WHERE r.target_id = ? AND r.kind = ?
            """,
            (entity_id, RelationshipKind.CALLS.value),
        )
        return [self._row_to_entity(row) for row in cursor]

    def get_callees(self, entity_id: str) -> list[Entity]:
        """Return callee entities."""
        cursor = self._conn.execute(
            """
            SELECT e.* FROM entities e
            JOIN relationships r ON e.entity_id = r.target_id
            WHERE r.source_id = ? AND r.kind = ?
            """,
            (entity_id, RelationshipKind.CALLS.value),
        )
        return [self._row_to_entity(row) for row in cursor]

    def get_dependencies(self, entity_id: str) -> list[Entity]:
        """Return entities this entity depends on."""
        cursor = self._conn.execute(
            """
            SELECT DISTINCT e.* FROM entities e
            JOIN relationships r ON e.entity_id = r.target_id
            WHERE r.source_id = ? AND r.kind IN (?, ?)
            """,
            (entity_id, RelationshipKind.CALLS.value, RelationshipKind.IMPORTS.value),
        )
        return [self._row_to_entity(row) for row in cursor]

    def get_dependents(self, entity_id: str) -> list[Entity]:
        """Return entities depending on this entity."""
        cursor = self._conn.execute(
            """
            SELECT DISTINCT e.* FROM entities e
            JOIN relationships r ON e.entity_id = r.source_id
            WHERE r.target_id = ? AND r.kind IN (?, ?)
            """,
            (entity_id, RelationshipKind.CALLS.value, RelationshipKind.IMPORTS.value),
        )
        return [self._row_to_entity(row) for row in cursor]

    def trace_calls(
        self,
        entity_id: str,
        direction: str = "callees",
        depth: int = 1,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Multi-hop call graph traversal using Recursive CTE."""
        if direction not in ("callers", "callees"):
            raise ValueError(f"direction must be 'callers' or 'callees', got {direction}")

        if direction == "callees":
            start_col, next_col = "source_id", "target_id"
        else:
            start_col, next_col = "target_id", "source_id"

        query = f"""
            WITH RECURSIVE call_chain(entity_id, depth) AS (
                SELECT {next_col}, 1 FROM relationships
                WHERE {start_col} = ? AND kind = ?
              UNION ALL
                SELECT r.{next_col}, cc.depth + 1
                FROM relationships r
                JOIN call_chain cc ON r.{start_col} = cc.entity_id
                WHERE r.kind = ? AND cc.depth < ?
            )
            SELECT DISTINCT e.entity_id, e.name, e.qualified_name, e.kind, e.file_path, e.line_start, MIN(cc.depth) as call_depth
            FROM call_chain cc
            JOIN entities e ON e.entity_id = cc.entity_id
            GROUP BY e.entity_id
            ORDER BY call_depth, e.name
            LIMIT ?;
        """
        
        cursor = self._conn.execute(
            query,
            (entity_id, RelationshipKind.CALLS.value, RelationshipKind.CALLS.value, depth, max_results),
        )
        
        results = []
        for row in cursor:
            results.append({
                "entity_id": row["entity_id"],
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "file": row["file_path"],
                "line": row["line_start"],
                "call_depth": row["call_depth"],
            })
        return results

    def get_impact(self, entity_id: str, max_depth: int = 3) -> dict[str, Any]:
        """Analyze the impact of modifying or deleting an entity."""
        entity = self.get_entity(entity_id)
        if not entity:
            return {
                "entity_id": entity_id,
                "direct_dependents": [],
                "transitive_dependents": [],
                "risk_score": 0.0,
                "error": "Entity not found",
            }
        
        direct = self.trace_calls(entity_id, direction="callers", depth=1, max_results=100)
        transitive = self.trace_calls(entity_id, direction="callers", depth=max_depth, max_results=100)
        
        direct_ids = {d["entity_id"] for d in direct}
        transitive_only = [t for t in transitive if t["entity_id"] not in direct_ids]
        
        direct_count = len(direct)
        transitive_count = len(transitive_only)
        
        breadth_score = min(1.0, (direct_count + transitive_count * 0.5) / 20)
        
        affected_files = {d.get("file") for d in direct + transitive_only}
        file_score = min(1.0, len(affected_files) / 5)
        
        type_score = 0.3
        if entity.kind == EntityKind.CLASS:
            type_score = 0.6
        elif entity.kind == EntityKind.MODULE:
            type_score = 0.8
            
        risk_score = round((breadth_score + file_score + type_score) / 3, 2)
        
        return {
            "entity_id": entity_id,
            "entity_name": entity.qualified_name,
            "direct_dependents": direct,
            "transitive_dependents": transitive_only,
            "total_affected": direct_count + transitive_count,
            "affected_files": list(affected_files),
            "risk_score": min(1.0, risk_score),
        }

    @classmethod
    def from_json(cls, json_path: str | Path, db_path: str | Path) -> "SqliteKnowledgeStore":
        """Migrate from a JSON knowledge store to SQLite."""
        store = KnowledgeStore.load(json_path)
        sqlite_store = cls(db_path)
        
        sqlite_store._conn.execute("BEGIN")
        try:
            for entity in store.entities.values():
                sqlite_store.add_entity(entity)
            for rel in store.relationships:
                sqlite_store.add_relationship(rel)
            sqlite_store._conn.execute("COMMIT")
        except Exception:
            sqlite_store._conn.execute("ROLLBACK")
            raise
            
        return sqlite_store
