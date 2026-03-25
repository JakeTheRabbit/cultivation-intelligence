# ADR-0004: Advisory-First Architecture with Gated Automation

**Status:** Accepted
**Date:** 2025-01-18
**Deciders:** Engineering Lead, Head Grower, Facility Manager
**Supersedes:** N/A
**Superseded by:** N/A (trigger for revisitation: Phase 4 prerequisites met)

---

## Context and Problem Statement

The cultivation intelligence system will eventually be technically capable of sending control commands to grow environment hardware — adjusting nutrient dosing pumps, dehumidifier setpoints, CO2 injection regulators, and similar actuators — through the Home Assistant automation layer.

The question this ADR addresses is not *whether the system can* send control commands, but *whether and when it should*, and under what governance structure.

This is a consequential decision with several distinct dimensions:

**Technical dimension:** Model predictions at Phase 1 are based on 5–20 historical batches. Confidence intervals are wide. A model that is correct 80% of the time is wrong 20% of the time — one in five automated actions could be a mistake. On a 10-week batch with 100 plants in flower, a single wrong automated EC correction at the wrong time can cost thousands of dollars and delay the supply of licensed medicinal product.

**Regulatory dimension:** NZ Medicinal Cannabis Scheme licensees are required to maintain complete batch records including all interventions. If an automated system makes adjustments, those adjustments must be attributable to a responsible human operator, traceable, and defensible in a compliance audit. The regulatory framework was written with human growers in mind; automated control introduces questions of accountability that have not been tested in the NZ regulatory context.

**Trust dimension:** The Head Grower and operators are domain experts with years of cultivation experience. The system is new. If the system takes actions that operators disagree with — even correct ones — it undermines their authority, creates resentment, and destroys the collaborative relationship that makes the system valuable. Trust must be earned through demonstrated accuracy and transparency, not assumed through capability.

**Safety dimension:** Cannabis plants respond to environmental stress in ways that compound over time. A bad automated action taken at 02:00 that would be reversed at the morning walkthrough can cause 8 hours of suboptimal conditions. A bad automated action during a sensitive growth stage (stretch period in early flower, flush week) can affect the entire batch's outcome.

---

## Decision Drivers

1. **Crop loss risk:** A single failed flower-room batch represents a substantial financial loss and disruption to the licensed supply programme.
2. **Regulatory accountability:** Under NZ medicinal cannabis regulations, a responsible person must be accountable for cultivation decisions. The accountability of automated actions must be established before automation is deployed.
3. **Model uncertainty at Phase 1:** With 5–20 training batches, prediction confidence intervals are wide. The system does not yet know what it does not know.
4. **Operator trust precedence over efficiency:** The system's long-term value depends on operators engaging with it honestly. Forcing automation on operators who do not trust the system produces an adversarial dynamic that is worse than no automation.
5. **Feedback loop integrity:** A system that acts on its own recommendations and then trains on the results of those actions can develop a self-reinforcing bias. The training data becomes correlated with previous model outputs rather than with independent operator judgement.
6. **Narrow action scope for Phase 4:** When automation is eventually appropriate, it should cover only the smallest possible set of actions — those that are genuinely tedious, low-risk, and well-understood.
7. **Reversibility:** Any automated action must be easily and immediately reversible by an operator.
8. **Explicitness of scope:** Operators must know exactly which parameters can be automatically adjusted and under what conditions, at all times.

---

## Considered Options

### Option 1: Full Advisory Mode (Phases 0–2), No Automation

The system is purely observational and advisory throughout its operational life. It generates recommendations but never executes any control actions. All adjustments are made by human operators.

**Advantages:** Zero risk of automated crop damage. Complete regulatory clarity. Operators retain full authority.
**Disadvantages:** Misses the efficiency and consistency benefits of automation for genuinely low-risk, repetitive adjustments. Over time, if the system is highly accurate, failing to act on correct recommendations has its own cost.

### Option 2: Advisory Mode with Immediate Automation for Confident Predictions

When the model's prediction confidence exceeds a threshold (e.g., > 85% confidence that an EC correction is needed), the system automatically executes the action without operator approval.

**Advantages:** Maximises efficiency; exploits model confidence signals.
**Disadvantages:** Fundamentally flawed for several reasons:
- Confidence scores from LightGBM are not calibrated probabilities — a "90% confidence" prediction can be wrong 50% of the time in practice without calibration
- Operators cannot audit what the system is doing in real time; they discover actions after the fact
- Destroys operator trust immediately if a high-confidence automated action turns out to be wrong
- Creates the feedback loop problem (model trains on its own actions)

This option is rejected outright.

### Option 3: Advisory-First Architecture with Gated Automation (Chosen)

The system is advisory-only for Phases 0–2 (approximately 18 months of operation). No control integration exists in the codebase during these phases — it is not a configuration switch; it is an absent capability.

Phase 4 introduces automation for a strictly bounded set of low-risk actions, only after a formal approval process including: Phase 2 sustained exit criteria, 60-day shadow automation, legal review, Head Grower written sign-off, and Facility Manager approval. The automation is bounded by hard constraints that cannot be exceeded.

### Option 4: Parallel Human-Machine Operation (Collaborative Automation)

A model where every automated action requires a human to confirm it within a time window. If the operator confirms, the action proceeds. If the operator does not respond, the action is either cancelled or escalated.

**Advantages:** Human always in the loop.
**Disadvantages:** Creates alert fatigue if confirmation requests are frequent. If operators are confident the system is correct, they confirm everything without reading, which provides no safety benefit. The approval workflow imposes friction that may cause operators to reject confirmations to avoid interruptions.

This option is not chosen for Phase 4 but elements of it (confirmation windows, rollback capability) are incorporated into the Phase 4 design.

---

## Decision

**Option 3 — Advisory-First Architecture with Gated Automation — is chosen.**

The system is **advisory-only** for all of Phase 0, 1, and 2.

Control integration code does not exist in the codebase during Phases 0–2. This is not enforced by a feature flag or configuration setting — there is literally no code path that can send a command to any actuator. This is an explicit architectural choice: the absence of capability is the strongest form of constraint.

Phase 4 may introduce bounded automation for a narrow set of low-risk actions, after all prerequisites are met. This requires an explicit engineering change (adding the control integration layer) and a new ADR that supersedes this one's Phase 4 provisions.

---

## Advisory Architecture Design

### What "Advisory" Means in Practice

The system produces **recommendations** — structured, human-readable suggestions that an operator should take a specific action. Recommendations are:
- Created by the recommendation engine when environmental conditions deviate from targets
- Stored in the database with their full context (sensor readings, feature values, SHAP attributions)
- Presented in the operator dashboard with priority ordering
- **Always waiting for a human decision** — accept, reject, or defer

No recommendation is ever automatically acted upon. The word "automated" in the context of Phases 0–2 refers only to the automated generation of recommendations, not their execution.

### Recommendation Workflow

```
Environmental condition detected by risk scorer
            │
            ▼
Recommendation created (status: pending)
            │
            ▼
Operator views recommendation in dashboard
            │
       ┌────┴────┐
       │         │
    Accept    Reject/Defer
       │         │
    Action    Reason logged
    logged    in audit table
       │
Intervention logged in
batch record
```

### What Is Always Manual (Permanently)

The following actions are permanently excluded from automation under any circumstances, regardless of Phase:

1. **Lighting schedule changes:** Photoperiod manipulation is the primary driver of phase transitions (veg to flower). An automated error could trigger premature flowering, a hermaphroditic response, or re-vegetative stress. The financial and regulatory consequences are severe.

2. **Major nutrient formula changes:** Switching from a veg to a bloom nutrient ratio, changing a base nutrient line, or adjusting a full reservoir. These changes have long-lasting effects and require experienced judgement about plant stage, health, and upcoming phase transitions.

3. **pH adjustment beyond ±0.2 units:** Large pH swings cause nutrient lockout. Automated pH control is a well-known source of oscillation (pH bounce) in automated fertigation systems. This requires hardware-specific pH dosing controller integration that is out of scope.

4. **Harvest initiation:** Harvest timing determines cannabinoid profile, yield, and compliance with batch record requirements. This must always be a human decision with documented trichome assessment.

5. **Any action during active pest or disease treatment:** The interaction between automated adjustments and ongoing treatment protocols is unpredictable. When a treatment is active, automation is suspended for all actuators in that room.

6. **Transplanting, pruning, defoliation:** Physical interventions. These are manual by definition.

7. **Reservoir flush or drain:** Large changes to nutrient solution composition. High risk of severe plant stress if mistimed.

### Phase 4 Automation Scope (When Prerequisites Are Met)

The following and only the following are candidates for Phase 4 automation:

| Action | Condition for Automation | Hard Bounds |
|---|---|---|
| Minor EC correction | EC deviates > 0.2 mS/cm from setpoint during mid-veg or early-flower stable growth phase | Max ±0.1 mS/cm per correction; max 1 correction per 4 hours; only between 07:00 and 20:00 NZST; only if at least one operator has logged in within the past 4 hours |

This scope is intentionally minimal. The principle is to automate what is genuinely tedious, demonstrably low-risk, and well within the model's accuracy. There is no pressure to expand this scope; any new action type requires the full prerequisite and approval cycle.

### Hard Constraint Enforcement

Phase 4 automation is built on a constraint enforcement layer that is architecturally separate from the model inference layer:

```
Model inference layer
        │
        ▼ (proposed action)
Constraint enforcement layer
        │
    ┌───┴────────────────────┐
    │ Hard bounds check       │
    │ Rate limiting check     │
    │ Operating hours check   │
    │ Stale sensor check      │
    │ Blackout period check   │
    └───────────┬────────────┘
                │
         PASS           FAIL
           │                │
    Execute action     Reject action
    Log to audit       Log rejection reason
    table              Generate alert
```

The constraint enforcement layer:
- Is independently unit-tested with adversarial inputs (attempts to exceed bounds)
- Is reviewed by an external reviewer before Phase 4 activation
- Cannot be bypassed by model output — it is not configurable at runtime

---

## Rationale for Advisory-First

### The Feedback Loop Problem

One of the most insidious risks of automated control in a learning system is the feedback loop between model actions and training data.

If the model recommends increasing CO2 from 900 ppm to 1000 ppm, and the system automatically executes this action, the subsequent sensor readings reflect the model's action, not the grow environment's natural state. When this batch is used for model training, the model learns from an environment that was shaped by its own previous outputs.

Over time, this creates a distribution shift in the training data that is entirely endogenous — the model is learning about its own control policy, not about the underlying relationship between environment and crop outcome. This leads to policy collapse: the model converges to a narrow range of actions that it has observed previously, reducing its ability to respond to novel conditions.

Advisory-only operation avoids this entirely: operators make independent decisions, and the training labels (batch outcomes, intervention logs) reflect human judgement, not model output.

### Crop Value and Irreversibility

A flowering cannabis batch at Legacy Ag Limited represents a significant investment: plant inputs, weeks of labour, lighting and climate control costs, and the regulatory overhead of a licensed batch. A batch that fails due to a bad automated action cannot be recovered. The crop value is lost, the batch record must be documented for the Ministry of Health, and the facility's production schedule is disrupted.

The expected loss from a bad automated action (probability of error × batch value) must be compared against the efficiency gains from automation. At Phase 1 model accuracy levels, this calculation does not favour automation. At Phase 3 model accuracy levels with 30+ training batches, the calculation may change — but the Phase 4 prerequisites exist precisely to establish confidence in the model's accuracy before trusting it to act autonomously.

### Regulatory Accountability Under NZ Medicinal Cannabis Scheme

The Misuse of Drugs (Medicinal Cannabis) Regulations 2019 requires licensed cultivators to maintain cultivation records attributing interventions to responsible persons. The scheme was designed with human growers in mind.

If an automated system makes a nutrient adjustment, who is the "responsible person"? The engineer who wrote the code? The operator who last reviewed the recommendations? The Head Grower who configured the target profiles?

This question does not have a clear answer under current NZ medicinal cannabis regulations, and it has not been tested. Legal review is a Phase 4 prerequisite precisely to address this question before any automation is activated.

Until legal clarity is obtained, automated actions create an attribution gap in the batch record that is a regulatory liability.

### Operator Trust Cannot Be Assumed

The Head Grower and cultivation operators have decades of combined growing experience. They have developed environmental intuitions that may not be captured in the model's feature set. When the model generates a recommendation that conflicts with the Head Grower's assessment of a batch, it is not obvious which is more likely to be correct.

In Phase 1 and 2, the correct response to this conflict is to present the recommendation transparently, allow the operator to reject it with a reason, and use that rejection as a feedback signal to improve the model. If the operator is consistently rejecting a specific recommendation type, the model is likely miscalibrated, not the operator.

Automation before this calibration is complete inverts the power relationship: the system acts on its own judgement, and the operator must intervene to override it. This is the wrong dynamic for building a collaborative human-AI cultivation system. Advisory mode preserves operator authority while allowing the system to prove its value incrementally.

---

## Monitoring Criteria for Phase 4 Activation

Phase 4 automation is not activated by meeting the technical prerequisites alone. All of the following must be satisfied and documented:

1. **Phase 2 sustained exit criteria:** Recommendation acceptance rate > 50%, sustained for 60+ days (not just at the Phase 2 exit point). Acceptance rate must be stable or improving.

2. **60-day shadow automation:** The Phase 4 automation engine runs in shadow mode for 60 days — it logs what actions it would have taken without executing them. Head Grower reviews proposed actions weekly. Acceptance rate for shadow actions (operator agrees with what the system would have done) must exceed 70%.

3. **Zero harmful proposals in shadow period:** During the 60-day shadow period, none of the proposed automated actions would have caused harm if executed (validated by Head Grower review against batch notes and outcomes).

4. **Legal review completed:** Written confirmation from legal counsel that automated control actions can be appropriately attributed and recorded in the batch record for NZ Medicinal Cannabis Scheme compliance.

5. **Safety review sign-off:** Formal safety review document completed, reviewed by Head Grower and Facility Manager, with specific attention to: constraint bounds, rate limiting, operating hours restrictions, blackout periods, and rollback procedure.

6. **Adversarial constraint testing:** The constraint enforcement layer has been tested with adversarial inputs attempting to exceed bounds. All tests pass.

7. **Rollback tested:** The rollback mechanism has been demonstrated to work within 5 minutes of an executed action.

---

## Consequences

### Positive

1. **Zero automated crop damage risk in Phases 0–2:** The absence of control integration code means there is no code path through which the system can harm a batch, regardless of bugs, misconfigurations, or adversarial inputs.

2. **Regulatory clarity in Phases 0–2:** All interventions are made by human operators and are attributable. The batch record is unambiguous.

3. **Operator trust built through advisory accuracy:** When operators see that the system's recommendations are consistently useful and well-explained, they trust it more. This trust is the prerequisite for Phase 4 cooperation.

4. **Training data integrity:** Batch outcomes and operator interventions in the training data reflect independent human judgement, not the model's previous actions.

5. **Phase 4 is achievable:** The advisory architecture preserves the ability to add automation later. Adding control integration in Phase 4 is an additive change (new module, new configuration, new test suite), not a refactor of the existing system.

### Negative

1. **Advisory-only is less efficient than automation for repetitive, low-risk adjustments:** An EC correction that the system recommends and the operator must manually execute takes 5–10 minutes of operator time. An automated correction would take 0 seconds. Over a 10-week batch, this adds up. This cost is real and accepted as the price of safety and trust-building.

2. **Operators can ignore correct recommendations:** If a high-priority recommendation is dismissed and a batch outcome suffers, the system will have been right but unable to act. This is the fundamental tradeoff of advisory architecture. The mitigation is Phase 2's recommendation engagement and acceptance rate tracking.

3. **Phase 4 is complex to implement safely:** Adding bounded automation with constraint enforcement, shadow mode, rollback, and audit trails is a significant engineering effort. The decision to defer this investment until Phase 3 prerequisites are met is deliberate but means Phase 4 automation will not be available until well into the second year of operation.

4. **System appears passive to business stakeholders:** Stakeholders who expected "AI controlling the grow room" may be disappointed by the advisory architecture. Managing this expectation is an ongoing communication responsibility.

---

## Phase 4 Architecture Preview (When Implemented)

When Phase 4 prerequisites are met, the automation layer will be added as a new module (`cultivation/automation/`) with the following structure:

```
cultivation/automation/
├── __init__.py
├── engine.py           # Reads pending automation proposals from queue
├── constraints.py      # Hard constraint enforcement (independently tested)
├── executor.py         # HA service call wrapper (sends actual commands)
├── shadow.py           # Shadow mode: log without executing
├── audit.py            # Immutable audit log writer
└── rollback.py         # Reversal of recent automated actions
```

The automation module is **separate from the recommendation module**. Recommendations are for human operators; automation proposals are a distinct flow that goes through the constraint layer before any execution. A recommendation for the dashboard and an automation proposal for a control actuator are different objects with different lifecycles.

---

*This ADR was written with reference to: Amodei et al. (2016) "Concrete Problems in AI Safety"; Amershi et al. (2019) "Software Engineering for Machine Learning: A Case Study" (Microsoft Research); and the NZ Misuse of Drugs (Medicinal Cannabis) Regulations 2019.*
