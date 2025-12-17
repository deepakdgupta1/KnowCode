# Project Evolution & Delta Log

This document tracks the incremental evolution of the codebase. It serves as a high-level narrative for human developers and a semantic anchor for LLMs.

---

## [v0.1.0] - 2023-10-27

**Commit Range:** `init...a1b2c3d`
**Focus:** MVP Release - Core Parsing and Graph Building

### 🧠 Temporal Context & Intent
Initial release of the KnowCode MVP. The primary focus was establishing the pipeline to convert raw source code into a queryable knowledge graph. The scope was strictly limited to Python, Markdown, and YAML to prove the concept of "token-efficient context generation" without tracking temporal changes (git history) yet.

### 🏗️ Architectural Impact
* **Pipeline Established:** Implemented the core `Scanner` -> `Parser` -> `GraphBuilder` -> `ContextSynthesizer` flow.
* **Graph Model:** Entities (Class, Function, Module) are now linked via simple relationships (Calls, Imports, Inherits) in an in-memory store.
* **CLI:** Established `click`-based CLI as the sole entry point.

### 📝 Delta Changes

#### 🚀 Features (`feat`)
* **parser:** Implement Python AST parser for function/class extraction
    * *Context:* Uses standard `ast` library to identify nodes and edges.
* **graph:** Create in-memory KnowledgeStore with JSON export
* **cli:** Add `analyze` and `query` commands
    * *Context:* Allows users to build the graph and perform relationship lookups (callers/callees).

#### 🐛 Fixes (`fix`)
* **scanner:** Correctly ignore files listed in `.gitignore`
    * *Resolution:* Integrated `pathspec` to parse gitignore patterns before scanning.

#### 🔧 Maintenance (`chore`, `docs`)
* **docs:** Add comprehensive project documentation and CLI reference
* **ci:** Configure basic GitHub Actions for linting (ruff)