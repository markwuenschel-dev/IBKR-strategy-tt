---
title: "Programming Principles"
category: topic
status: current
summary: "Engineering principles for abstract, type-guided, declarative, combinatoric, pipeline-oriented, parallel, and adaptive code: factories/registries/schemas as the evolution surface, typed boundaries and lineage as the safety surface, gates at promotion."
related:
  - docs/programming-guidelines.md
  - docs/05-quant-engine.md
  - docs/conformance/determinism-tiers.md
  - docs/promotion-gates.md
  - docs/01-architecture-anchor.md
---

# Programming Principles

*Engineering principles for abstract, type-guided, declarative, extensible, combinatoric, pipeline-oriented, parallel, and adaptive systems.*

This is the design-philosophy companion to [programming-guidelines.md](programming-guidelines.md): the guidelines say what the live code concretely does (layout, chokepoints, idioms, testing); this document says how code is *shaped* — the principles reviews apply when the question is "is this the right design?", not "does this follow the house style?". Descends from Programming Guidelines v3.2.

> **Change in v3.2 — read this.** Section 6 is reworked. The old "nothing is done until it is fully gated" rule applied the *promotion* bar to *every capability from its first line*, forcing each small task to carry a full evidence-emission tax. Gates now apply at **promotion**, not as the definition of "done." Cheap correctness invariants stay always-on; the heavy validation battery moves to the promotion boundary and is built **once** in the harness. §7.2 is scoped accordingly. Everything else is the same principles, compressed.

## 1. Purpose

How Eilixa code is written for maximum abstraction, typed composition, declarative construction, combinatoric exploration, and parallel efficiency under non-stationarity. Factories, registries, schemas, and explicit execution plans are the **evolution surface**; typed boundaries, invariants, and artifact lineage are the **safety surface**.

## 2. North-Star Constraints

The system stays point-in-time correct, reproducible within its declared determinism tier, auditable via content-addressed artifacts, and observable in production.

**Correctness beats speed.** If an optimization undermines PIT integrity, determinism, or schema validity, it is rejected. Speed work lives inside the functional core, behind invariant tests and instrumentation.

## 3. Core Engineering Model

**3.1 Abstract & type-guided.** Design around contracts (Protocol, typed boundary models), not concrete classes. Implementations swap without touching callers. Prefer narrow interfaces, typed value objects, and fixed-shape IRs with masking over deep inheritance and ad-hoc polymorphism. Runtime flexibility only behind stable typed contracts.

**3.2 Registry-driven.** Signals, ops, strategies, allocators, planners, gates, and cost/execution models register into explicit, version-aware registries — no static wiring unless it is a measured hot path. Each subsystem lowers into a single canonical IR/planner/executor path; parallel authoring surfaces for the same semantic layer are transitional only.

**3.3 Declarative, schema-first.** Express variation as manifests, schemas, typed configs, and IR dimensions before imperative branching. Validate boundary objects at construction; invalid states fail early with typed errors. If a capability can be a manifest dimension, registry entry, or planner input, do that before adding bespoke control flow.

**3.4 Factory-first.** Factories are the multiplication layer: they generate graphs, plans, and combinatoric variants from small declarative specs. A factory focuses on (1) canonical IR construction, (2) boundary validation, (3) lowering to efficient plans, (4) stable hashes/keys for reuse, (5) parallel-friendly partitioning (symbols/time/folds/tasks), (6) reproducibility metadata. Combinatoric search (signals × params × costs × policies × regimes) is factory output, not manual glue.

**3.5 Combinatoric by design.** Small primitives recombined by factories. A new variant is a new schema dimension, planner rule, or registry entry — not a one-off pipeline. Manual enumeration does not scale.

**3.6 Pipeline-oriented.** Explicit stages with typed handoffs: spec → IR → validated IR → plan → execution → artifacts. Stage boundaries are visible in code and inspectable in artifacts. Do not collapse planning, execution, and evaluation into opaque control flow.

**3.7 Batch, vectorized, parallel.** Assume large universes and many folds. Prefer columnar/vectorized compute and batch ops; design functions embarrassingly parallel over symbols, folds, and regime episodes. Python orchestrates efficient kernels; it is not the default site of per-element compute.

**3.8 Local opacity, global clarity.** *(reframed)* Dense, non-obvious code is acceptable **only inside an explicitly labeled hot kernel** that is: isolated behind a stable typed interface, gated by a conformance test against a readable reference implementation, instrumented, and justified by benchmark evidence. You pay illegibility in a sealed box, never across the codebase. Accidental opacity is not an optimization strategy.

## 4. Architecture Rules

**4.1 Functional core / imperative shell.** Computation (transforms, features, signals, allocators, planners) is pure and deterministic. Side effects (I/O, networking, broker calls, clocks, persistence) stay in the shell. The shell coordinates; the core computes.

**4.2 Fixed-dimensional interfaces with masking.** Prefer fixed-shape tensors/vectors with masks over dynamic resizing — for replay compatibility, caching, planning simplicity, and fast CPU/GPU kernels. Use dynamic structure only where structurally necessary and outside hot surfaces.

**4.3 Point-in-time discipline.** All market, fundamental, and macro access flows through the PIT front door (`DataView.as_of(T)`, see [05-quant-engine.md](05-quant-engine.md)) enforcing `valid_time ≤ T` and `knowledge_time ≤ T`. No direct raw-dataset access in strategy, planner, feature, or evaluator code. This is an architecturally enforced boundary, not a convention — make a non-PIT access a *type* error wherever the type system allows it. It is the single defense against lookahead.

**4.4 Determinism tiers.** Every pipeline declares its tier ([determinism-tiers.md](conformance/determinism-tiers.md) is authoritative for the D0–D3 definitions) in metadata. Use hierarchical seed derivation, deterministic ordering, explicit partitioning, and stable aggregation. Not deterministic within the required tier ⇒ not promotable.

**4.5 Value semantics.** Manifests, IR nodes, plans, descriptors, and artifact metadata are immutable. Mutation is explicit, local, and isolated from the core. No hidden mutation across planning or execution boundaries.

## 5. Performance Engineering

**5.1 Hot paths are explicit.** Performance-critical sections are isolated, labeled, and benchmark-covered. Keep them small and stable; keep experimentation in factories, manifests, and planners.

**5.2 Memory & copy discipline.** Minimize materializations and conversions; prefer lazy/streaming where feasible. Cache at the right layer (spec/IR → plan → materialized features → artifacts). Cache keys must include registry versions, schema versions, and PIT boundaries — an output you cannot name precisely you cannot trust.

**5.3 Parallel execution.** Write compute that maps to multiprocessing, thread pools, vectorized kernels, GPU batches, or distributed schedulers. Partition along stable axes (symbol/time/fold/task); keep cross-partition coordination explicit, coarse-grained, and rare.

## 6. Testing and Gates *(reworked)*

**6.1 Two boundaries, not one.** Separate *done* from *promotable*:

- **Done (development).** The code is typed, correct, and passes its **cheap always-on invariants** (§6.2). That is the whole bar. A capability is complete without emitting any promotion evidence. Build, explore, and iterate here.
- **Promotable (production).** The heavyweight evidence battery — statistical validity, cost realism, meta-validity, execution integrity — is required **only when promoting a strategy or model toward production** (see [promotion-gates.md](promotion-gates.md)).

The retired rule ("if it cannot be gated, it is not done") applied the promotion bar to every capability from its first line. That is an upfront tax that strangles exploration: you build evidence harnesses instead of shipping capabilities. Gate at the promotion boundary; keep development fast.

**6.2 Cheap invariants, always on.** Property-based tests for what is catastrophic *and* cheap to check: leakage, PIT boundaries, determinism, schema/IR round-trips, hash stability, planner/executor idempotence, monotonicity, and stability bounds. These run continuously, cost little, and are the real safety net during development. Invariants are authoritative; examples support them. (This is the principle behind the delivery rule "the gate ships with the feature" in [programming-guidelines.md](programming-guidelines.md): the tests and CI enforcement that ship with a PR are these cheap invariants, not the promotion battery.)

**6.3 Build the gate once.** Promotion evidence is emitted by the **shared promotion harness** (planner/evaluator level), not re-implemented per capability. Adding a signal or transform must never mean wiring up six kinds of evidence emission. If promotion needs a new kind of evidence, it goes in the harness, once.

## 7. Operational Visibility and Failure Discipline

No `print`. Structured logging, tracing, and explicit execution metadata. Every plan has an ID/hash; every promoted execution emits enough to reproduce and debug; every artifact ties back to its inputs, registry state, and determinism tier. Observability is part of correctness, not post-hoc tooling.

**7.1 Exception handling.** Precise, typed, domain-aware. Bare `except:` is forbidden. `except Exception:` is allowed only for structured-log-then-immediate-re-raise, translation to a typed domain error, or controlled boundary handling with explicit policy. Silent failure paths are forbidden. *(CI lint: forbid bare except and silent except Exception blocks.)*

**7.2 Reproducibility metadata.** A **promoted / production** result must carry the metadata to reconstruct it: spec hash, registry versions, planner version, seed lineage, PIT boundary, determinism tier, partition identity, and artifact lineage. Exploratory runs need not (§6.1) — though spec/seed hashing comes free from the content-addressing layer, so keep that. A production result you cannot reconstruct is not production-grade.

## 8. Evolution and Adaptation

Expect signal, model, and regime drift. Design for replacement, not permanence. Adaptation changes components **without dissolving contracts**, and it lives in models, parameters, and state — online learning, meta-learning, regime-conditional weights — **not in self-modifying source**. Factories, schemas, registries, and planners are the evolution surface; gates, typed boundaries, and artifact lineage are the safety surface. Extensibility is mandatory; structural discipline is non-negotiable.
