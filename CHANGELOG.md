## [Unreleased] - 2025-12-17

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
