## [2.1.0] - 2025-12-19

**Focus:** Semantic Search & Retrieval Quality

### 🚀 Features
* **Semantic Search**: Implemented dense vector retrieval using FAISS and OpenAI embeddings.
* **Hybrid Retrieval**: Combined BM25 sparse search with dense embeddings using Reciprocal Rank Fusion (RRF).
* **Code Chunking**: Added intelligent code chunking for modules, imports, and entities.
* **Watch Mode**: Integrated file system monitoring for real-time background re-indexing.
* **Dependency Expansion**: Improved context quality by automatically including caller/callee dependencies in search results.
* **New CLI Commands**: Added `knowcode index` and `knowcode semantic-search`.
* **API Enhancement**: Added `/api/v1/context/query` endpoint for rich semantic queries.

### 🐛 Fixes
* Fixed `VectorStore` persistence bug where `id_map` was reset after loading.
* Fixed `Chunker` instability issue where collected chunks were reset mid-parsing.
* Resolved stubbed implementation in `completeness.py`.

### 🏗️ Architectural Impact
* Introduced a new retrieval pipeline: `Indexer` -> `ChunkRepository` -> `VectorStore` -> `HybridIndex` -> `SearchEngine`.
* Added background processing and file monitoring for improved live updates.

---


**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* Add Runtime Signals to the knowledge graph: Cobertura test coverage report processing, associated new entity and relationship types, and integrate into graph building. (`73d8ef1`)


---

## [Unreleased] - 2025-12-18

**Focus:** Bug Fixes

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* Added manual manual changelog generation capability in CI pipeline. Added flexibility in content scoping for the changelog (`d8207a4`)

#### 🐛 Fixes
* fix linting errors add future annotations import to all modules (`d6559d2`)
* Changes to address tree-sitter-languages dependency issues: - Restricted `requires-python` in `pyproject.toml` to `<3.13`. - User removed 3.13 from `ci-cd.yml`. (`745b38c`)

#### 🔨 Refactoring
* Remove unused imports and update Python version range in lock file. Also, fixed linting issues. (`72a63ec`)

#### 📦 Other Changes
* ci: add step to commit and push generated changelog. (`4039499`)


---

## [Unreleased] - 2025-12-18

**Focus:** Bug Fixes

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🐛 Fixes
* fix linting errors add future annotations import to all modules (`d6559d2`)

#### 📚 Documentation
* Enhance README with CI/CD badge, updated Python version support, refined architecture descriptions, and an updated release roadmap. (`754fa6b`)
* update changelog [skip ci] (`06dbf4c`)

#### 🔨 Refactoring
* Remove unused imports and update Python version range in lock file. Also, fixed linting issues. (`72a63ec`)

#### 📦 Other Changes
* ci: add step to commit and push generated changelog. (`4039499`)


---

## [Unreleased] - 2025-12-18

**Focus:** Routine Maintenance

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🔧 Maintenance
* ignore CHANGELOG.md (`82189f9`)

#### 📚 Documentation
* update changelog [skip ci] (`de306d0`)
* Enhance README with CI/CD badge, updated Python version support, refined architecture descriptions, and an updated release roadmap. (`754fa6b`)
* update changelog [skip ci] (`06dbf4c`)

#### 📦 Other Changes
* ci: add step to commit and push generated changelog. (`4039499`)


---

## [Unreleased] - 2025-12-18

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* Introduce KnowCode server with a new service layer and API endpoints, add server tests, and update project roadmap documentation. (`9af0e3f`)

#### 🔧 Maintenance
* ignore CHANGELOG.md (`82189f9`)

#### 📚 Documentation
* update changelog [skip ci] (`c4b5e02`)
* update changelog [skip ci] (`de306d0`)
* Enhance README with CI/CD badge, updated Python version support, refined architecture descriptions, and an updated release roadmap. (`754fa6b`)


---

## [Unreleased] - 2025-12-18

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* Introduce KnowCode server with a new service layer and API endpoints, add server tests, and update project roadmap documentation. (`9af0e3f`)

#### 🔧 Maintenance
* ignore CHANGELOG.md (`82189f9`)

#### 📚 Documentation
* Add comments explaining entity to dictionary conversion logic. (`578f886`)
* update changelog [skip ci] (`4e6f30f`)
* update changelog [skip ci] (`c4b5e02`)


---

## [Unreleased] - 2025-12-20

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* Introduce core RAG system for code, including agent, indexer, chunker, embedding, and vector store components, with updated service and API integrations. (`6bf3194`)
* Introduce KnowCode server with a new service layer and API endpoints, add server tests, and update project roadmap documentation. (`9af0e3f`)

#### 📚 Documentation
* update changelog [skip ci] (`8a63644`)
* Add comments explaining entity to dictionary conversion logic. (`578f886`)
* update changelog [skip ci] (`4e6f30f`)


---

## [Unreleased] - 2025-12-20

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* Enhance chunking metadata and add comprehensive docstrings to core components. (`30c0fb1`)
* Introduce core RAG system for code, including agent, indexer, chunker, embedding, and vector store components, with updated service and API integrations. (`6bf3194`)

#### 📚 Documentation
* update changelog [skip ci] (`fb54235`)
* update changelog [skip ci] (`8a63644`)
* Add comments explaining entity to dictionary conversion logic. (`578f886`)


---

## [Unreleased] - 2025-12-20

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* Enhance chunking metadata and add comprehensive docstrings to core components. (`30c0fb1`)
* Introduce core RAG system for code, including agent, indexer, chunker, embedding, and vector store components, with updated service and API integrations. (`6bf3194`)

#### 📚 Documentation
* update changelog [skip ci] (`9bc659c`)
* update changelog [skip ci] (`fb54235`)

#### 🔨 Refactoring
* Reorganize codebase into modular subpackages for core components and introduce new tokenizer utility. (`f29933b`)


---

## [Unreleased] - 2025-12-20

**Focus:** Routine Maintenance

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 📚 Documentation
* update changelog [skip ci] (`83739d8`)
* update changelog [skip ci] (`9bc659c`)

#### 🔨 Refactoring
* Introduce new module-specific unit tests for various components and remove outdated general test files. (`f083e11`)
* Reorganize codebase into modular subpackages for core components and introduce new tokenizer utility. (`f29933b`)

#### ✅ Testing
* Add unit tests for retrieval, analysis, LLM, CLI, storage, models, utils, API, and indexing components. Reorganize all tests into appropriate folder structure. (`e9b4a24`)


---

## [Unreleased] - 2025-12-20

**Focus:** Bug Fixes

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🐛 Fixes
* Fixed some test suite bugs (`efc66c1`)

#### 📚 Documentation
* update changelog [skip ci] (`3ea35e1`)
* update changelog [skip ci] (`83739d8`)

#### 🔨 Refactoring
* Introduce new module-specific unit tests for various components and remove outdated general test files. (`f083e11`)

#### ✅ Testing
* Add unit tests for retrieval, analysis, LLM, CLI, storage, models, utils, API, and indexing components. Reorganize all tests into appropriate folder structure. (`e9b4a24`)


---

## [Unreleased] - 2025-12-20

**Focus:** Bug Fixes

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🐛 Fixes
* Fixed some test suite bugs (`efc66c1`)

#### 📚 Documentation
* update changelog [skip ci] (`bdba35c`)
* update changelog [skip ci] (`3ea35e1`)

#### 🔨 Refactoring
* renamed models.py to data_models.py and updated the dependencies (`91cb347`)
* Introduce new module-specific unit tests for various components and remove outdated general test files. (`f083e11`)


---

## [Unreleased] - 2026-01-07

**Focus:** Bug Fixes

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🐛 Fixes
* Fixed some test suite bugs (`efc66c1`)

#### 📚 Documentation
* update changelog [skip ci] (`9a69740`)
* update changelog [skip ci] (`bdba35c`)

#### 🔨 Refactoring
* update .gitignore and remove obsolete documentation files; enhance README and KnowCode.md for clarity (`15cda76`)
* renamed models.py to data_models.py and updated the dependencies (`91cb347`)


---

## [Unreleased] - 2026-01-07

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 5
**Confidence:** high

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* enhance changelog generation with optional summary input and improved commit parsing (`9fdebad`)

#### 📚 Documentation
* update changelog [skip ci] (`b84bf72`)
* update changelog [skip ci] (`9a69740`)

#### 🔨 Refactoring
* update .gitignore and remove obsolete documentation files; enhance README and KnowCode.md for clarity (`15cda76`)
* renamed models.py to data_models.py and updated the dependencies (`91cb347`)


---

## [Unreleased] - 2026-01-10

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 5
**Confidence:** medium

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* enhance changelog generation with optional summary input and improved commit parsing (`9fdebad`)

#### 📚 Documentation
* update changelog [skip ci] (`a4bf068`)
* update changelog [skip ci] (`b84bf72`)

#### 🔨 Refactoring
* update .gitignore and remove obsolete documentation files; enhance README and KnowCode.md for clarity (`15cda76`)

#### 📦 Other Changes
* Replaced OpenAI LLM API Key with Gemini API Key for the ability to ask questions in plain English. Updated documentation and project version to cover missing items. (`6e6d1e6`)


---

## [Unreleased] - 2026-01-11

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 5
**Confidence:** medium

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* enhance changelog generation with optional summary input and improved commit parsing (`9fdebad`)

#### 📚 Documentation
* update changelog [skip ci] (`374cfb5`)
* update changelog [skip ci] (`a4bf068`)

#### 📦 Other Changes
* Built a mature search and retrieval soln. Enabled tool-based usage via MCP server. Introduced AI models - Devstral 2 and Gemini for natural language interactions, VoyageAI for embedding and reranking. (`7cf4de5`)
* Replaced OpenAI LLM API Key with Gemini API Key for the ability to ask questions in plain English. Updated documentation and project version to cover missing items. (`6e6d1e6`)


---

## [Unreleased] - 2026-01-11

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 5
**Confidence:** medium

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* complete agentic capabilities and IDE integration; update README with MCP server details and configuration (`ab3a3c9`)

#### 📚 Documentation
* update changelog [skip ci] (`75b9b8e`)
* update changelog [skip ci] (`374cfb5`)

#### 📦 Other Changes
* Built a mature search and retrieval soln. Enabled tool-based usage via MCP server. Introduced AI models - Devstral 2 and Gemini for natural language interactions, VoyageAI for embedding and reranking. (`7cf4de5`)
* Replaced OpenAI LLM API Key with Gemini API Key for the ability to ask questions in plain English. Updated documentation and project version to cover missing items. (`6e6d1e6`)


---

## [Unreleased] - 2026-01-12

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 5
**Confidence:** medium

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* complete agentic capabilities and IDE integration; update README with MCP server details and configuration (`ab3a3c9`)

#### 📚 Documentation
* update changelog [skip ci] (`5729c32`)
* update changelog [skip ci] (`75b9b8e`)

#### 📦 Other Changes
* Refactor Python parser type checks, remove unused imports, and enhance search engine functionality. Refactored the codebase so that User queries and IDE integration use the same central search and retrieval logic. (`203362b`)
* Built a mature search and retrieval soln. Enabled tool-based usage via MCP server. Introduced AI models - Devstral 2 and Gemini for natural language interactions, VoyageAI for embedding and reranking. (`7cf4de5`)


---

## [Unreleased] - 2026-01-12

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 4
**Confidence:** low

**Focus:** Feature Development

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🚀 Features
* complete agentic capabilities and IDE integration; update README with MCP server details and configuration (`ab3a3c9`)

#### 📚 Documentation
* update changelog [skip ci] (`5729c32`)

#### 📦 Other Changes
* Revise README to enhance project description (`df49021`)
* Refactor Python parser type checks, remove unused imports, and enhance search engine functionality. Refactored the codebase so that User queries and IDE integration use the same central search and retrieval logic. (`203362b`)


---

## [Unreleased] - 2026-01-12

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 4
**Confidence:** low

**Focus:** Routine Maintenance

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 📚 Documentation
* update changelog [skip ci] (`1556b06`)

#### 📦 Other Changes
* Update README to improve clarity of description (`6350aa6`)
* Revise README to enhance project description (`df49021`)
* Refactor Python parser type checks, remove unused imports, and enhance search engine functionality. Refactored the codebase so that User queries and IDE integration use the same central search and retrieval logic. (`203362b`)


---

## [Unreleased] - 2026-01-12

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 5
**Confidence:** medium

**Focus:** Routine Maintenance

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🔧 Maintenance
* update .gitignore to include aimodels.yaml and .knowcode directories (`e533b0c`)

#### 📚 Documentation
* update changelog [skip ci] (`6272b24`)
* update changelog [skip ci] (`1556b06`)

#### 📦 Other Changes
* Update README to improve clarity of description (`6350aa6`)
* Revise README to enhance project description (`df49021`)


---

## [Unreleased] - 2026-01-12

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 4
**Confidence:** low

**Focus:** Routine Maintenance

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🔧 Maintenance
* update .gitignore to include aimodels.yaml and .knowcode directories (`e533b0c`)

#### 📚 Documentation
* update changelog [skip ci] (`6272b24`)

#### 📦 Other Changes
* Revise guiding principle in KnowCode documentation (`5677d0d`)
* Update README to improve clarity of description (`6350aa6`)


---

## [Unreleased] - 2026-01-12

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 4
**Confidence:** low

**Focus:** Routine Maintenance

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 🔧 Maintenance
* update .gitignore to include aimodels.yaml and .knowcode directories (`e533b0c`)

#### 📚 Documentation
* update changelog [skip ci] (`e0d0b0c`)

#### 📦 Other Changes
* Fix grammatical error in KnowCode.md (`95f1d25`)
* Revise guiding principle in KnowCode documentation (`5677d0d`)


---

## [Unreleased] - 2026-01-12

**Origin:** Generated
**Range:** `HEAD~5..HEAD`
**Commit Count:** 4
**Confidence:** low

**Focus:** Routine Maintenance

### 🧠 Temporal Context & Intent
> *Auto-generated: Add context about why these changes were made.*

### 🏗️ Architectural Impact
> *Auto-generated: Describe high-level architectural shifts.*

### 📝 Delta Changes

#### 📚 Documentation
* update changelog [skip ci] (`e0d0b0c`)

#### 📦 Other Changes
* Clean up duplicate model configurations (`d787409`)
* Fix grammatical error in KnowCode.md (`95f1d25`)
* Revise guiding principle in KnowCode documentation (`5677d0d`)
