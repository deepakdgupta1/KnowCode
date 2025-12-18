# **Reference Architecture: KnowCode**

## **0\. Guiding Principle (sets the tone)**

The system does not *retrieve text*.  
It *constructs and maintains a continuously evolving semantic model* of the codebase and its intent, behavior, and constraints.

Everything else flows from that.

---

## **1\. Source Ingestion & Canonicalization Layer**

### **Purpose**

Ingest all raw artifacts that define or influence the codebase and normalize them into stable, traceable representations.

### **Responsibilities**

* Discover and ingest:
  * Source code
  * Build files
  * Configuration
  * Tests
  * Documentation
  * Version history metadata
* Normalize formats (e.g., line endings, encodings)
* Maintain identity and versioning of artifacts
* Track provenance (where did this come from, when, and why)
* **[HARDENED]** Content-addressable hashing (SHA-256) for delta detection
* **[HARDENED]** Streaming ingestion via VCS webhooks (push) or polling (pull)
* **[HARDENED]** Distinguish monorepo vs. polyrepo topology
* **[HARDENED]** Handle binary artifacts (compiled code, Docker images, WASM)

### **Inputs**

* Source repositories
* VCS metadata
* Documentation files
* Configuration manifests
* **[HARDENED]** Change events (file added/modified/deleted/renamed)
* **[HARDENED]** Webhook payloads (GitHub, GitLab, Bitbucket)

### **Outputs**

* Canonical artifact store
* Artifact metadata graph (file → repo → commit → author)
* **[HARDENED]** Delta artifact stream (incremental updates, not full snapshots)
* **[HARDENED]** Provenance chain with cryptographic verification

### **Downstream Consumers**

* Parsing & analysis layer
* Intent extraction layer

---

## **2\. Structural Parsing & Intermediate Representation Layer**

### **Purpose**

Convert raw code into **language-aware, lossless structural representations** suitable for higher-order reasoning.

### **Responsibilities**

* Parse source code into ASTs or equivalent IRs
* Preserve:
  * Symbol definitions
  * Scopes
  * Types
  * Control structures
* Maintain bidirectional mappings:
  * Source text ↔ structural nodes
* **[HARDENED]** Support language-agnostic intermediate representation
* **[HARDENED]** Detect cross-language boundaries (FFI, RPC, WASM)
* **[HARDENED]** Graceful degradation on parse errors (partial ASTs)
* **[HARDENED]** Track macro/metaprogramming expansion (Rust, C, Lisp)
* **[HARDENED]** Flag generated code vs. authored code (protobuf, codegen)

### **Inputs**

* Canonical source artifacts

### **Outputs**

* Parsed structural representations
* Symbol tables
* Source-to-structure mappings
* **[HARDENED]** Parse error entities (first-class, not silent failures)
* **[HARDENED]** Generated-code markers

### **Downstream Consumers**

* Semantic graph builder
* Static analysis
* Documentation synthesis

---

## **3\. Semantic Graph Construction Layer**

### **Purpose**

Build a **unified semantic graph** representing what the code *is*, *does*, and *depends on*.
This is the heart of the system.

### **Responsibilities**

* Create semantic entities:
  * Functions, classes, modules
  * Types, interfaces
  * APIs, schemas
  * Config keys, feature flags
* Establish relationships:
  * Call relationships
  * Import / dependency edges
  * Type usage
  * Data flow edges
  * Ownership / domain boundaries
* Encode semantic attributes:
  * Visibility
  * Mutability
  * Side effects
  * Error behavior
* **[HARDENED]** Schema versioning with migration strategy
* **[HARDENED]** Conflict resolution for parallel analysis
* **[HARDENED]** Temporal modeling (entity history, not just current state)
* **[HARDENED]** Confidence scores on all entities and edges
* **[HARDENED]** Entity lifecycle tracking (birth, deprecation, death)

### **Enhanced Entity Model**

```yaml
Entity:
  id: UUID
  kind: function | class | module | config_key | feature_flag | api_endpoint
  source_location: Location
  confidence: float (0.0-1.0)
  provenance: static_analysis | runtime_trace | llm_inference | human_annotation
  created_at: timestamp
  deprecated_at: timestamp?
  superseded_by: UUID?
```

### **Inputs**

* Structural representations
* Symbol tables

### **Outputs**

* Unified semantic graph
* Entity and relationship metadata
* **[HARDENED]** Versioned graph snapshots
* **[HARDENED]** Confidence-annotated edges

### **Downstream Consumers**

* Documentation generation
* Impact analysis
* Query & reasoning engines
* Context synthesis

---

## **4\. Static Behavioral Analysis Layer**

### **Purpose**

Extract *behavioral meaning* that is not explicit in structure alone.

### **Responsibilities**

* Identify:
  * Control flow
  * Data flow
  * State transitions
  * Invariants and assumptions
* Derive:
  * Pre-conditions / post-conditions
  * Error propagation paths
  * Resource lifecycles
* **[HARDENED]** Analysis depth tiers (quick/shallow vs. deep/expensive)
* **[HARDENED]** Side effect classification: pure, IO, state-mutating, non-deterministic
* **[HARDENED]** Termination analysis (infinite loops, unbounded recursion)
* **[HARDENED]** Thread safety markers (unsafe patterns, lock requirements)
* **[HARDENED]** Memory ownership tracking (C/C++, Rust)

### **Inputs**

* Semantic graph
* Structural IRs

### **Outputs**

* Behavioral annotations attached to semantic graph
* Explicit control/data flow subgraphs
* **[HARDENED]** Complexity metrics (cyclomatic, cognitive)
* **[HARDENED]** Purity annotations (pure/impure/unknown)
* **[HARDENED]** Resource acquisition/release graph (RAII tracking)

### **Downstream Consumers**

* Debugging context synthesis
* Change impact reasoning
* Q\&A engine

---

## **5\. Runtime & Execution Signal Layer (Optional but Powerful)**

### **Purpose**

Augment static understanding with **ground truth about execution**.
Accuracy beats theory.

### **Responsibilities**

* Ingest:
  * Test execution traces
  * Logs
  * Runtime metrics
* Map runtime events back to semantic entities
* Capture:
  * Hot paths
  * Rare branches
  * Exception patterns
* **[HARDENED]** Define max overhead budget (<5% latency impact)
* **[HARDENED]** Adaptive sampling based on path frequency
* **[HARDENED]** Data sanitization for sensitive runtime values
* **[HARDENED]** OpenTelemetry-compatible trace format
* **[HARDENED]** Deterministic correlation IDs to semantic graph

### **Inputs**

* Execution traces
* Logs
* Runtime metadata

### **Outputs**

* Execution-annotated semantic graph
* Observed behavior overlays
* **[HARDENED]** Trace retention policies
* **[HARDENED]** Anonymized/redacted sensitive data

### **Downstream Consumers**

* Debugging workflows
* Behavior explanation
* Confidence scoring

---

## **6\. Intent & Rationale Extraction Layer**

### **Purpose**

Answer the hardest question in codebases: **"Why does this exist?"**

### **Responsibilities**

* Extract intent from:
  * Commit messages
  * PR descriptions
  * ADRs
  * Comments
* Associate intent with:
  * Code entities
  * Architectural decisions
* Track evolution of intent over time
* **[HARDENED]** Staleness detection (doc age vs. code modification time)
* **[HARDENED]** Intent conflict resolution with precedence rules
* **[HARDENED]** Implicit intent inference from code patterns
* **[HARDENED]** Organizational signals (CODEOWNERS, team boundaries)
* **[HARDENED]** Regulatory intent markers (HIPAA, PCI compliance)

### **Intent Source Ranking**

```
ADR > PR Description > Commit Message > Inline Comment > Inferred
```

### **Inputs**

* Version control metadata
* Documentation artifacts
* Semantic graph

### **Outputs**

* Intent annotations
* Decision-to-code mappings
* **[HARDENED]** Intent freshness score (0.0-1.0)
* **[HARDENED]** Unintentional behavior warnings (code ≠ docs)

### **Downstream Consumers**

* Documentation synthesis
* Q\&A
* Change risk analysis

---

## **7\. Documentation Synthesis Layer (Multi-Level)**

### **Purpose**

Generate **explainable, navigable, abstraction-aware documentation** directly from the semantic model.

### **Responsibilities**

* Produce documentation at multiple levels:
  * System architecture
  * Subsystem/module
  * Component
  * Function/class
* Ensure:
  * Consistency with actual code
  * Traceability back to source
* Keep docs incrementally up-to-date
* **[HARDENED]** Multiple output formats: Markdown, HTML, IDE tooltips, OpenAPI, AsyncAPI
* **[HARDENED]** Audience targeting: new engineers, domain experts, API consumers
* **[HARDENED]** Auto-generate "what changed since version X" summaries
* **[HARDENED]** Dead documentation detection and removal
* **[HARDENED]** Example synthesis from test cases

### **Abstraction Levels**

```
1. Executive    (1 paragraph per system)
2. Architectural (component interaction diagrams)
3. Module       (API contracts, dependencies)
4. Function     (signature, behavior, edge cases)
5. Code-inline  (contextual tooltips)
```

### **Inputs**

* Semantic graph
* Behavioral annotations
* Intent metadata

### **Outputs**

* Structured documentation artifacts
* Machine-readable documentation indices

### **Downstream Consumers**

* Human readers
* Local Q\&A
* Context synthesis

---

## **8\. Local Knowledge Store & Reasoning Substrate**

### **Purpose**

Provide a **local, authoritative knowledge base** for answering questions *without frontier LLMs*.

### **Responsibilities**

* Store:
  * Semantic graph
  * Documentation
  * Intent
  * Behavioral data
* Support:
  * Graph queries
  * Symbolic reasoning
  * Evidence-backed answers
* Provide explanations with traceability
* **[HARDENED]** Define query language (Cypher-like graph queries)
* **[HARDENED]** Indexing strategy for common query patterns
* **[HARDENED]** Storage backend specification (embedded vs. graph DB)
* **[HARDENED]** Team-wide replication vs. local-only modes
* **[HARDENED]** Cache invalidation on code changes

### **Supported Query Types**

```yaml
Queries:
  - kind: reachability
    example: "What functions can call X?"
  - kind: impact
    example: "What breaks if I delete Y?"
  - kind: dependency
    example: "What does Z depend on?"
  - kind: similarity
    example: "What functions are similar to W?"
  - kind: invariant
    example: "What assumptions does V make?"
```

### **Inputs**

* All enriched semantic artifacts

### **Outputs**

* Verified answers
* Supporting evidence paths

### **Downstream Consumers**

* Developer Q\&A interface
* Context distillation layer

---

## **9\. Task-Aware Context Synthesis Layer**

### **Purpose**

Create **high-signal, minimal context bundles** tailored to the developer's current task.

### **Responsibilities**

* Identify task intent (debug, refactor, extend, review)
* Traverse semantic graph selectively
* Synthesize:
  * Relevant entities
  * Key behaviors
  * Critical invariants
  * Known risks
* Produce compact, lossless summaries
* **[HARDENED]** Token budget allocation (e.g., max 8K context tokens)
* **[HARDENED]** Priority ranking when budget constrained
* **[HARDENED]** Context compression (summarization, deduplication, reference-by-ID)
* **[HARDENED]** Multi-turn conversation memory
* **[HARDENED]** Negative context (what to explicitly exclude)

### **Task-Specific Templates**

```yaml
debug:
  priority: [stack_trace, related_functions, recent_changes, known_bugs]
  exclude: [unrelated_modules, documentation]
  
refactor:
  priority: [code_structure, dependencies, tests, invariants]
  exclude: [implementation_details_of_dependencies]
  
extend:
  priority: [patterns_used, architecture_constraints, related_features]
  exclude: [deprecated_code]

review:
  priority: [changed_lines, test_coverage, impact_analysis, security_concerns]
  exclude: [unchanged_dependencies]
```

### **Inputs**

* Semantic graph
* Knowledge store
* Task metadata

### **Outputs**

* Task-specific context artifacts
* **[HARDENED]** Token usage reports
* **[HARDENED]** Context compression metrics

### **Downstream Consumers**

* Frontier LLM interface
* Human-in-the-loop workflows

---

## **10\. Frontier LLM Interface Layer**

### **Purpose**

Use frontier LLMs **only where they add leverage**, not as a crutch.

### **Responsibilities**

* Inject synthesized context
* Frame precise tasks/questions
* Consume minimal tokens
* Capture and ground responses
* **[HARDENED]** Model abstraction (OpenAI, Anthropic, local Ollama)
* **[HARDENED]** Cost tracking per query (tokens, dollars)
* **[HARDENED]** Latency SLAs with fallback strategy
* **[HARDENED]** Response validation against known constraints
* **[HARDENED]** Prompt injection prevention (sandboxed prompts)

### **Inputs**

* Task-specific context bundles

### **Outputs**

* Generated code
* Explanations
* Recommendations (always traceable)
* **[HARDENED]** Token usage metrics
* **[HARDENED]** Validation results (alignment with known facts)
* **[HARDENED]** Cost attribution per query

### **Downstream Consumers**

* Developer
* Knowledge store (optional feedback loop)

---

## **11\. Feedback, Validation & Evolution Layer**

### **Purpose**

Continuously improve accuracy and completeness over time.

### **Responsibilities**

* Capture:
  * Developer corrections
  * False assumptions
  * Knowledge gaps
* Validate:
  * Documentation vs reality
  * Static vs runtime behavior
* Trigger re-analysis where needed
* **[HARDENED]** Classify feedback: correction, enhancement, gap report, false positive
* **[HARDENED]** Confidence decay for unvalidated knowledge
* **[HARDENED]** A/B testing for analysis strategies
* **[HARDENED]** Metrics dashboard (accuracy over time, coverage gaps)
* **[HARDENED]** Human-in-loop escalation triggers

### **Inputs**

* Developer feedback
* LLM outputs
* Code changes

### **Outputs**

* Updated semantic model
* Confidence adjustments
* **[HARDENED]** Trend analytics

---

## **12\. Cross-Cutting Concerns [NEW]**

### **Security Model**

* Code access permissions (who can query what repositories/modules)
* Sensitive code detection (credentials, PII handlers, crypto keys)
* Audit logging for all queries and modifications
* Sandboxed LLM prompts to prevent injection attacks
* Data residency compliance (EU data stays in EU infrastructure)

### **Scalability Architecture**

* Maximum codebase size specification (lines of code, file count)
* Horizontal scaling strategy for large monorepos (>10M LOC)
* Partial analysis for PR-scoped understanding
* Background reprocessing vs. on-demand analysis trade-offs
* Sharding strategy for distributed graph storage

### **Observability**

* System metrics: latency (p50, p95, p99), throughput, queue depth
* Analysis coverage tracking per repository
* Knowledge freshness monitoring and alerting
* User engagement analytics (queries per day, satisfaction scores)
* Error rate dashboards with drill-down capability

### **Configuration Management**

* Language-specific parser configuration
* Analysis depth toggles (quick scan vs. deep analysis)
* Output format preferences per user/team
* Model selection preferences and cost limits
* Feature flags for experimental capabilities

---

# **High-Level Interaction Flow (Simplified)**

`Source Artifacts`  
   `↓`  
`Parsing & IR`  
   `↓`  
`Semantic Graph`  
   `↓`  
`Behavior + Intent + Runtime`  
   `↓`  
`Knowledge Store`  
   `↓`  
`Documentation & Local Q&A`  
   `↓`  
`Task-Aware Context Synthesis`  
   `↓`  
`Frontier LLM (minimal tokens)`

---

## **Enhanced Layer Interaction Diagram**

```mermaid
flowchart TB
    subgraph Ingestion
        L1[Layer 1: Source Ingestion]
    end
    
    subgraph Analysis
        L2[Layer 2: Structural Parsing]
        L3[Layer 3: Semantic Graph]
        L4[Layer 4: Behavioral Analysis]
        L5[Layer 5: Runtime Signals]
        L6[Layer 6: Intent Extraction]
    end
    
    subgraph Intelligence
        L7[Layer 7: Doc Synthesis]
        L8[Layer 8: Knowledge Store]
        L9[Layer 9: Context Synthesis]
    end
    
    subgraph Interface
        L10[Layer 10: LLM Interface]
        DEV[Developer]
    end
    
    subgraph Evolution
        L11[Layer 11: Feedback Loop]
    end
    
    subgraph Cross-Cutting
        SEC[Security]
        OBS[Observability]
        CFG[Configuration]
    end
    
    L1 --> L2
    L2 --> L3
    L3 --> L4
    L3 --> L6
    L4 --> L8
    L5 -.-> L8
    L6 --> L8
    L8 --> L7
    L8 --> L9
    L9 --> L10
    L10 --> DEV
    DEV --> L11
    L11 --> L8
    L11 --> L3
    
    SEC -.-> L1 & L8 & L10
    OBS -.-> L1 & L3 & L8 & L10
    CFG -.-> L2 & L4 & L9
```

---

## **Why this architecture fits your stated priorities**

* **Accuracy** → grounded in structure, behavior, and execution; confidence-scored
* **Relevance** → task-aware synthesis, not blind retrieval; token-budgeted
* **Completeness** → intent + runtime + static analysis; lifecycle-tracked
* **Performance trade-offs accepted** → deeper analysis allowed; tiered depth
* **Production-Ready** → scalability, security, observability built-in
* **Trust** → provenance tracking, human-in-loop validation, audit logging

You've essentially defined a **code intelligence system**, not a chatbot with embeddings.

---

## **Implementation Status & Roadmap**

### **Phase 1: Foundation (COMPLETED)**
1. **[x] Unified Semantic Graph (Layer 3)**: Multi-language support (Python, JS, Java, MD, YAML).
2. **[x] Token-Budgeted Synthesis (Layer 9)**: Priority-ranked context generation.
3. **[x] Local Knowledge Store (Layer 8)**: JSON persistence with graph-like querying.
4. **[x] Service Layer (Architecture Substrate)**: Unified business logic for CLI and API.

### **Phase 2: Intelligence Server (COMPLETED)**
5. **[x] FastAPI Server (Layer 10)**: REST API for local IDE agent integration.
6. **[x] Hot Reload (Layer 8)**: Dynamic refresh of knowledge store without downtime.
7. **[x] Granular API (Layer 8)**: Programmatic access to raw entities and relationships.

### **Phase 3: Deep Analysis (NEXT)**
8. **[ ] Static Behavioral Analysis (Layer 4)**: Data flow and state transition tracking.
9. **[ ] Intent Extraction (Layer 6)**: Linking commit messages and ADRs to semantic nodes.
10. **[ ] Confidence Scoring (Layer 3)**: Weighted edges based on analysis source quality.

### **Phase 4: Enterprise (FUTURE)**
11. **[ ] Security & RBAC**: Who can query what modules.
12. **[ ] Scalability**: Supporting monorepos > 1M LOC.
13. **[ ] Team Sharing**: Remote knowledge store synchronization.