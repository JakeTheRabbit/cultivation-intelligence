# ADR-0001: Monolithic FastAPI Service vs Microservices

**Status:** Accepted
**Date:** 2025-01-15
**Deciders:** Engineering Lead, Head Grower, Facility Manager
**Supersedes:** N/A
**Superseded by:** N/A (trigger for revisitation: > 3 developers, or services require independent scaling)

---

## Context and Problem Statement

Legacy Ag Limited requires a cultivation intelligence platform that ingests sensor telemetry from Home Assistant, computes environmental risk scores, generates agronomic recommendations, serves predictions from ML models, and presents all of this through an operator-facing dashboard.

The fundamental architectural question is: should this system be built as a single deployable unit (a monolith), or as a collection of independently deployable services (microservices)?

This decision must be made at the start of the project because it governs the fundamental shape of the codebase, the deployment infrastructure, the development workflow, and the operational burden. It is not trivially reversible — a microservices architecture begun incorrectly is expensive to consolidate, and a monolith built without internal boundaries is expensive to break up later.

### Constraints and Context

The following constraints significantly narrow the decision space:

1. **Team size:** The system is being built and maintained by one primary developer, potentially two. This is not a large engineering team.
2. **Stage:** This is a greenfield system at a single facility with no established user base. The requirements will change as operators interact with the system and the ML models produce their first outputs.
3. **Infrastructure:** The system runs on a single facility server. There is no Kubernetes cluster, no cloud infrastructure, and no platform engineering team.
4. **Primary goal:** The primary goal for the first 18 months is establishing operator trust, not maximising throughput or demonstrating architectural sophistication.
5. **Operational knowledge:** The operators who will maintain this system are cultivation professionals, not DevOps engineers. Deployment complexity must be minimised.

---

## Decision Drivers

1. **Development velocity:** The system must demonstrate value quickly. Architectural overhead that slows feature development is directly harmful to the project's success.
2. **Operational simplicity:** The system must be operable by a small team without dedicated platform engineering. `docker compose up` is the target deployment model.
3. **Debugging and observability:** With one developer, debugging a distributed system (tracing requests across service boundaries, correlating logs) is disproportionately expensive.
4. **Refactoring flexibility:** Requirements will change. The architecture must support change without requiring cross-service contract negotiations.
5. **Preserved upgrade path:** The architecture should not permanently foreclose microservice extraction if the team grows or scaling requirements change.
6. **Data consistency:** The ingest pipeline, feature computation, risk scoring, and recommendation generation share data. Distributing these requires distributed coordination or eventual consistency, adding complexity.
7. **Testing simplicity:** Integration tests for a monolith are straightforward. Integration tests for microservices require service orchestration in CI.
8. **Cost:** Running multiple containerised services is marginally more expensive than one, but the real cost is the cognitive overhead of managing multiple codebases, CI pipelines, and deployment units.
9. **Latency requirements:** The internal calls between ingest → feature computation → risk scoring → recommendation are not latency-sensitive (15-minute batch cycle). There is no argument for network separation based on latency requirements.
10. **Module boundary discipline:** The risk of a monolith is that module boundaries erode over time. Mitigation requires discipline in code organisation, enforced by linting rules and code review.

---

## Considered Options

### Option 1: Monolithic FastAPI Service (Chosen)

A single FastAPI application that includes all functionality: HTTP ingest endpoints, the feature engineering pipeline (run via APScheduler), the risk scoring engine, the recommendation engine, the model inference API, and the admin/management API. Internal code is organised into modules (`cultivation/ingest/`, `cultivation/features/`, `cultivation/risk/`, `cultivation/recommendations/`, `cultivation/models/`, `cultivation/api/`) with well-defined interfaces between them.

Deployment: one Docker container for the API/scheduler, one for the database (TimescaleDB), one for the cache (Redis), and one for the frontend.

### Option 2: Microservices Architecture

Separate the system into independently deployable services:
- **Ingest Service:** Receives webhook events from HA and writes raw sensor readings to the database
- **Feature Service:** Reads raw data, computes features, writes to Redis
- **Inference Service:** Loads ML models, serves predictions via HTTP
- **Recommendation Service:** Reads features and predictions, generates recommendations
- **API Gateway:** Routes external API requests to the appropriate internal service
- **Frontend:** React UI, served separately

Each service has its own CI pipeline, its own deployment unit, its own health check, and potentially its own language/runtime choice.

### Option 3: Serverless Functions

Deploy each functional unit as a cloud function (AWS Lambda, Google Cloud Functions, or self-hosted via Knative or OpenFaaS). The feature pipeline becomes a triggered function; the prediction API becomes a function invoked on request; the recommendation engine becomes a function triggered by a database event.

---

## Decision

**Option 1 — Monolithic FastAPI service with internal module boundaries — is chosen.**

The system will be structured as a single FastAPI application with a clearly defined internal module hierarchy. Module interfaces are enforced by convention and code review, not by network boundaries. The internal structure is designed to preserve the option of extracting services later without requiring a full rewrite.

---

## Rationale

### Why Not Microservices?

The case for microservices rests on three genuine benefits: independent scalability, team autonomy, and technology heterogeneity. None of these apply here.

**Independent scalability** is irrelevant when there is one server. Even if the feature pipeline were CPU-intensive enough to justify its own container, the server-level resource constraint is the binding limit, not service granularity. Network-separated services would not improve throughput on a single machine; they would add the overhead of serialisation, network calls, and service discovery without any benefit.

**Team autonomy** — the ability for multiple teams to own and deploy their services independently — is meaningless with one or two developers. Microservice architecture is designed to solve organisational coordination problems by enforcing API contracts as the coordination mechanism. When there is one developer, this is pure overhead.

**Technology heterogeneity** — the ability to write each service in the most appropriate language — is not a requirement here. Python is appropriate for all components of this system. There is no component that would benefit from being written in Go or Rust.

Against these non-applicable benefits, microservices impose real costs on a small team:
- **Service discovery and networking:** Even in Docker Compose, services must address each other by container hostname. An ingest service calling the feature service requires an HTTP client, error handling for network failures, retry logic, and timeout handling. All of this code is replaced by a simple Python function call in a monolith.
- **Distributed tracing:** Debugging a request that spans five services requires distributed tracing infrastructure (Jaeger, Zipkin, or similar). Without it, correlating logs across services to diagnose a bug is extremely slow. A monolith has a single, coherent log stream.
- **Schema versioning:** When the ingest service changes the structure of the data it writes, all downstream services must be updated in a coordinated deployment. In a monolith, this is a refactor. In microservices, it is a versioned API contract migration.
- **Testing complexity:** Testing an individual service in isolation requires mocking all other services. Integration testing requires orchestrating all services together. In a monolith, integration tests run against the full in-process stack.
- **Deployment complexity:** Each service has its own CI pipeline, container image, and health check. Deploying a feature that touches four services requires four coordinated deployments. In a monolith, it is one deployment.

The seminal articulation of this problem is Martin Fowler's "Microservices Premium" — the productivity overhead of microservices is only recouped at team sizes and complexity levels that do not apply here.

### Why Not Serverless?

Serverless functions have significant cold-start latency that is incompatible with the APScheduler-based feature pipeline. The pipeline runs every 15 minutes and must complete within 60 seconds. Cold starts can add 1–5 seconds of overhead on stateless function runtimes; for a pipeline that opens database connections and loads feature computation state, this is unacceptable.

Additionally, the system is explicitly on-premises. Cloud serverless offerings (Lambda, Cloud Functions) require cloud infrastructure. Self-hosted serverless (Knative, OpenFaaS) adds significant operational complexity for no benefit.

### Why a Monolith Is Correct for This Stage

The monolith is not chosen because microservices are bad. It is chosen because the microservices premium is real, the team size does not justify the overhead, and the benefits do not apply to this context.

A well-structured monolith is preferable to a poorly-structured set of microservices. The key is the "well-structured" part: the monolith must have real internal boundaries, not an undifferentiated ball of mud.

The internal module structure enforces the same separation of concerns that microservices would, without the network overhead:

```
cultivation/
├── ingest/           # HA webhook handling, entity validation, raw write
│   ├── __init__.py
│   ├── handlers.py   # FastAPI route handlers
│   ├── validator.py  # Entity validation and unit normalisation
│   └── writer.py     # Database write logic
├── features/         # Feature engineering pipeline
│   ├── __init__.py
│   ├── pipeline.py   # Orchestration, APScheduler job
│   ├── rolling.py    # Rolling statistics computation
│   ├── derived.py    # VPD, temperature delta, phase-aware features
│   └── cache.py      # Redis read/write
├── risk/             # Risk scoring engine
│   ├── __init__.py
│   ├── scorer.py     # Composite risk score computation
│   └── profiles.py   # Target profile loading and management
├── recommendations/  # Recommendation generation
│   ├── __init__.py
│   ├── engine.py     # Core recommendation logic
│   ├── templates.py  # Recommendation text templates
│   └── dedup.py      # Deduplication logic
├── models/           # ML model inference
│   ├── __init__.py
│   ├── registry.py   # Model loading and versioning
│   └── inference.py  # Prediction generation
└── api/              # FastAPI routes (thin layer — delegates to above modules)
    ├── __init__.py
    ├── batches.py
    ├── sensors.py
    ├── risk.py
    └── recommendations.py
```

Each module has a defined public interface (the `__init__.py` exports). Cross-module calls go through these interfaces. Direct import of internal implementation details from other modules is prohibited and enforced by a linting rule.

This structure means that if the team grows and microservice extraction becomes appropriate, each module is already a service boundary. The extraction involves:
1. Moving the module to a new repository
2. Adding an HTTP client wrapper around the module's public interface in the original codebase
3. Deploying the extracted service

This is straightforward. It is not possible without this structure.

---

## Consequences

### Positive Consequences

1. **Single deployment unit:** `docker compose up` starts the entire application. Operators and maintainers do not need to understand service orchestration.
2. **Unified log stream:** All events from ingest to recommendation are in one log stream, searchable with a single `docker compose logs` command.
3. **Simple debugging:** A bug that spans ingest → feature computation → risk scoring is debugged by reading a single stack trace and a single log file.
4. **Fast refactoring:** Changing the interface between two modules is a Python refactor with type-checker support. No API contract migration required.
5. **Simple testing:** Integration tests run against the full in-process application. No service mocking required.
6. **No network latency on internal calls:** The feature pipeline calling the risk scorer is a function call, not an HTTP round trip.
7. **Single CI pipeline:** One GitHub Actions workflow builds, tests, and lints the entire codebase.
8. **Lower cognitive overhead:** One developer can hold the entire system's structure in their head. With microservices, each service boundary adds cognitive overhead.

### Negative Consequences

1. **Deployment coupling:** A change to the ingest module requires deploying the entire application, even if no other module changed. In practice, with a small team, this is acceptable — deployments are infrequent and controlled.
2. **Resource coupling:** A CPU-intensive feature pipeline run competes with API request handling for the same process resources. Mitigation: the API runs in an async event loop (FastAPI + uvicorn), and the feature pipeline runs in a background thread via APScheduler. They share the GIL but in practice the pipeline is I/O bound (database reads, Redis writes) and does not contend significantly with the API.
3. **Technology homogeneity:** All components must be Python. If a future component would benefit from a different runtime (e.g., a Rust-based high-performance ingest handler), it cannot be added without architectural change.
4. **Boundary erosion risk:** Without enforcement, internal module boundaries can erode over time as developers take shortcuts. Mitigation: linting rules (import-linter or similar) enforce that `cultivation/api/` cannot import directly from `cultivation/features/` internal implementation files.

---

## Migration Path

This decision should be revisited when any of the following triggers occur:

1. **Team grows beyond 3 developers:** At this scale, team autonomy becomes a real concern. Module ownership can be formalised, and extraction of independent services becomes justified.
2. **Services require fundamentally different scaling:** If the ingest service needs to handle 10x the current write volume and the API does not, independent horizontal scaling becomes valuable.
3. **Technology heterogeneity requirement:** If a specific component genuinely requires a different runtime (e.g., real-time streaming with Kafka consumers), that component can be extracted.
4. **Build/test time exceeds 10 minutes:** At this point, independent CI pipelines per module become worthwhile.

The extraction sequence, when justified, should follow the strangler fig pattern:
1. Extract the component with the most stable interface first (likely the ingest service)
2. Deploy it as a separate container communicating via HTTP
3. The original monolith continues to work, using an HTTP client where it previously called the module directly
4. Validate the extracted service in production for 30 days before extracting the next

---

*This ADR was written after reviewing: Fowler (2015) "Microservices Premium", Richardson (2018) "Microservices Patterns", and Sam Newman (2019) "Monolith to Microservices". The decision reflects this system's specific context and constraints, not a general preference.*
