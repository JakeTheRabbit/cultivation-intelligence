# Pull Request

## Summary

<!-- What does this PR do? Provide a concise description of the changes and their purpose. -->
<!-- Link to the related issue if applicable: Closes #000 -->

**Related Issue:** <!-- e.g., Closes #42, Fixes #7, Ref #100 -->

---

## Type of Change

<!-- Check all that apply. -->

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update (no code changes)
- [ ] Refactor (code restructuring, no behavioral change)
- [ ] Performance improvement
- [ ] Tests (adding or improving test coverage)
- [ ] Infrastructure / CI/CD / DevOps
- [ ] Dependency update

---

## Changes Made

### Core Changes

<!-- List the primary code changes made in this PR. Be specific about files or modules changed. -->

-
-
-

### API Changes

<!-- Describe any changes to API endpoints, request/response schemas, or client-facing behavior. -->
<!-- Note: Is this change backward compatible? If not, mark as Breaking Change above. -->

- [ ] No API changes
- [ ] Backward-compatible API change (describe below)
- [ ] Breaking API change (requires client updates or versioning strategy)

> Details:

### Data Model Changes

<!-- Describe any changes to database models, TimescaleDB hypertables, or data schemas. -->

- [ ] No data model changes
- [ ] Migration included (`alembic revision` generated and tested)
- [ ] Hypertable structure change (requires careful rollout)
- [ ] TimescaleDB continuous aggregate updated

> Migration file(s):

### Configuration Changes

<!-- List any new or modified environment variables, settings, or configuration files. -->

- [ ] No configuration changes
- [ ] New environment variables added (documented in `.env.example`)
- [ ] Existing environment variables modified or removed
- [ ] Docker Compose / deployment configuration changed

> Variable(s) changed:

---

## Domain Impact

<!-- Help reviewers understand the blast radius of this change within the cultivation intelligence system. -->

- [ ] Affects sensor ingestion pipeline (MQTT, Home Assistant, AquaPro)
- [ ] Affects feature engineering or time-series aggregation
- [ ] Affects model training pipeline or training data
- [ ] Affects inference / real-time predictions
- [ ] Affects the recommendations engine
- [ ] Affects control systems or safety constraints (**requires extra review — tag a safety reviewer**)
- [ ] Affects Home Assistant integration (entities, automations, webhooks)
- [ ] Affects AquaPro / nutrient dosing integration
- [ ] Affects external API contracts or webhooks
- [ ] Affects background tasks or scheduled jobs (Celery / APScheduler)
- [ ] Changes any inter-service data contracts or Pydantic schemas

**Does this change any data contracts?**
<!-- If yes, confirm downstream consumers (dashboards, HA, external clients) have been updated or are backward compatible. -->

---

## Testing

### How Was This Tested?

<!-- Describe the testing strategy used for this PR. -->

- [ ] Unit tests (pytest, mocked dependencies)
- [ ] Integration tests (against real TimescaleDB + Redis services)
- [ ] Manual testing (describe scenario below)
- [ ] End-to-end test with Home Assistant
- [ ] Load / performance testing

**Manual test scenario (if applicable):**

> Steps taken:

### Test Coverage

- [ ] Test coverage maintained or improved (CI coverage report checked)
- [ ] New code paths are covered by tests
- [ ] Edge cases considered and tested (null sensors, missing batches, stale data)

### Safety-Related Changes

<!-- If this PR touches control logic, dosing, environment thresholds, or any safety constraint: -->

- [ ] Not applicable — no safety-system changes
- [ ] Safety constraint unit tests passing
- [ ] Fail-safe behavior verified (system reverts to safe state on error)
- [ ] Manual review of control loop logic completed
- [ ] Threshold / limit values reviewed and documented

---

## Documentation

- [ ] Updated relevant docs in `docs/` directory
- [ ] Added or updated an Architecture Decision Record (ADR) if an architectural decision was made
- [ ] Updated `CHANGELOG.md` with a user-facing description of changes
- [ ] API docs auto-generated from updated Pydantic schemas / FastAPI routes
- [ ] Inline code comments added for non-obvious logic
- [ ] No documentation changes needed

---

## Deployment Notes

### Database Migrations

- [ ] No database migrations required
- [ ] Migration required — has been tested against a copy of production schema
- [ ] Migration is backward compatible (old app version can run against new schema)
- [ ] Migration is NOT backward compatible (requires coordinated deploy)

**Migration notes:**

### Environment Variables

<!-- List any new or changed environment variables that need to be set in production. -->

| Variable | Required | Default | Description |
|---|---|---|---|
| | | | |

### Breaking API Changes

<!-- If this is a breaking API change, describe the migration path for API consumers. -->

- [ ] Not applicable
- [ ] API version bumped
- [ ] Deprecation notice added to old endpoint(s)
- [ ] Migration guide written

### Model Retraining

- [ ] No model changes
- [ ] Model schema changed — retraining required before deploy
- [ ] Model weights updated and stored in artifact registry
- [ ] Feature pipeline changes are backward compatible with current model version

### Rollback Plan

<!-- How do we roll back if this deploy causes issues? -->

---

## Checklist

<!-- Confirm each item before requesting review. -->

### Code Quality

- [ ] Code follows project style (ruff lint and format passing locally)
- [ ] mypy type checking passes with no new errors
- [ ] No commented-out debug code left in
- [ ] No `print()` statements — logging used instead
- [ ] No hardcoded secrets, tokens, or passwords
- [ ] No hardcoded environment-specific values (URLs, IPs, ports)
- [ ] Pydantic models used for all API input/output validation

### Tests

- [ ] All existing tests still pass (`pytest` green locally)
- [ ] New tests written for new functionality
- [ ] Tests use fixtures and factories rather than raw DB inserts where possible
- [ ] Async tests use `pytest-asyncio` correctly

### Documentation & Review

- [ ] PR title follows conventional commits format (e.g., `feat:`, `fix:`, `chore:`)
- [ ] PR description is complete and accurate
- [ ] Requested review from appropriate domain expert(s)
- [ ] Linked relevant issue(s) or roadmap item(s)

### Security

- [ ] No new dependencies with known vulnerabilities added
- [ ] Any new third-party packages reviewed for license compatibility
- [ ] Input validation is in place for any new user-facing endpoints
- [ ] Authentication / authorization checked for new routes

---

## Screenshots / Logs

<!-- If this PR changes UI behavior, API responses, or produces notable logs, include evidence here. -->
<!-- Dashboard screenshots, curl responses, log snippets, metric graphs — anything that helps reviewers verify the change. -->

<details>
<summary>Click to expand</summary>

```
# Paste relevant output here
```

</details>

---

## Reviewer Notes

<!-- Anything specific you want reviewers to focus on or be aware of. -->
<!-- e.g., "Please pay close attention to the safety threshold logic in src/control/safety.py" -->
<!-- e.g., "The SQL in migration 0024 is complex — please review carefully before approving" -->
