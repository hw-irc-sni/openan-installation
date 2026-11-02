# Proposal 2: Multi-agent workflow definition and execution

**Status:** design sketch for review. Not a TM Forum specification; no real `TMFCxxx`/`TMFxxx` id is assigned.
**Grounding:** this gap is **new**. It is not in the cached `gaps-backlog.md`; the nearest entries are the order-domain "Fallout / exception-handling (order & process orchestration)" item (TAC-280) and the TMFS030 AI-native taxonomy note, neither of which concerns agent workflows. It has **not been cross-corroborated** across use cases (none of the 24 cached use cases mention agents or AI).

## 1. Problem

Agents packaged as components (Canvas AI-native design) are composed into multi-agent processes. There is no ODA-standard way to **define** such a process (which task is performed by which agent, with which inputs and outputs, with which human checkpoints), **run** it, **observe** its state, or **record** its execution. TMF701 ProcessFlow Management already models process and task flows, but not agent-specific tasks, failure, or execution contracts.

## 2. Evidence base

| Source | What it shows |
|---|---|
| Canvas `AI-Native-Canvas-design.md` | Multi-agent architectures, evaluation framework (pre- and post-production), observability, audit |
| One open-source implementation of an agent orchestrator (OpenAN Orchestration Center, API reference) | Needed in practice: stored workflow definitions with steps bound to registered agents; creation from a procedure document or from a natural-language intent; lookup of existing definitions by intent; execution with a streamed event feed; stored execution records; human-reviewable generated plans |
| Cached TMF701 (v4.1.0), TMFC012 | An existing flow model and a component that already exposes it (optional) |

The implementation informs requirements only; the proposal is expressed in TM Forum and A2A/MCP terms.

## 3. Preferred shape A: agent-task profile and small extensions of TMF701

### 3.1 What exists today (unchanged)

- `ProcessFlowSpecification`: `id, href, description, lastUpdate, lifecycleStatus, name, version, attachment, channel, processFlowSpecificationCharacteristic (array of CharacteristicSpecification), processFlowSpecificationRelationship, relatedEntitySpecification, relatedParty, taskFlowSpecification, ...`
- `TaskFlowSpecification`: `id, href, description, lastUpdate, lifecycleStatus, name, taskFlowSpecificationType, version, attachment, channel, relatedEntitySpecification (array of RelatedEntitySpecification), relatedParty, relatedProcessFlowSpecification, taskFlowSpecification`. `taskFlowSpecificationType` is a **free string** ("send task, receive task, user task, manual task, script task, business rule task"). **It has no characteristic list.**
- `ProcessFlow`: `id, href, processFlowDate, processFlowSpecification, channel, characteristic, processFlowSpecificationRef, relatedEntity, relatedParty, state, taskFlow`. `ProcessFlowStateType` = active | cancelled | hold | completed.
- `TaskFlow`: `id, href, completionDate, completionMethod, isMandatory, priority, requestedCompletionDate, requestedStartDate, startDate, taskFlowSpecification, channel, characteristic, relatedEntity, relatedParty, state, taskFlowRelationship, taskFlowSpecificationRef`. `TaskFlowStateType` = new | active | hold | cancelled | completed.
- `RelatedEntity`: `id, href, name, role, @baseType, @schemaLocation, @type, @referredType`. `CharacteristicSpecification`: `id, configurable, description, extensible, isUnique, maxCardinality, minCardinality, name, regex, valueType, charSpecRelationship, characteristicValueSpecification, validFor, ...`.
- TMF701 has a hub and events: `processFlow*Event`, `processFlowSpecification*Event`, `taskFlow*Event`, `taskFlowSpecification*Event` (create, attribute value change, state change, delete as defined).
- TMFC012 already exposes TMF701 (optional) and publishes process-flow events through its hub.

### 3.2 Profile conventions (no schema change)

| Need | Profile mapping |
|---|---|
| Agent task | `taskFlowSpecificationType: "agent task"` (new value of an existing free string). Tool call: `"tool task"`. Human checkpoint: the existing `"user task"`. |
| Which agent performs a task (design time) | `TaskFlowSpecification.relatedEntitySpecification` with `role: "performer"`, `@referredType` = the agent `ResourceSpecification` (see Proposal 1), or a selection constraint described in `description` until Q1 is decided |
| Which agent performed it (run time) | `TaskFlow.relatedEntity` with `role: "performer"`, `@referredType: "Resource"`, `id` = the agent resource id |
| Inputs and outputs | `TaskFlow.characteristic` entries named `input.<name>` / `output.<name>` with `valueType` and `value` |
| Process-level parameters | `processFlowSpecificationCharacteristic` / `ProcessFlow.characteristic` |
| Generated plans | `ProcessFlowSpecification.lifecycleStatus` (for example `draft` until reviewed) and a characteristic `generatedBy` (human \| model identifier) |
| Execution record | the `ProcessFlow` instance and its `taskFlow` array are the record; a characteristic `trace` may reference an external trace |
| Progress feed | TMF701 hub notifications for `taskFlowStateChangeEvent` and `processFlowStateChangeEvent` |

### 3.3 Proposed additions that **do** change the TMF701 schema

Marked as additions to the existing schema; everything not listed is unchanged.

```yaml
definitions:
  TaskFlowStateType:                  # EXISTING: new | active | hold | cancelled | completed
    enum: [new, active, hold, cancelled, completed, failed]      # ADDITION: failed
  ProcessFlowStateType:               # EXISTING: active | cancelled | hold | completed
    enum: [active, cancelled, hold, completed, failed]           # ADDITION: failed
  TaskFlowSpecification:              # EXISTING (no characteristic list today)
    properties:
      taskFlowSpecCharacteristic:     # ADDITION, mirrors processFlowSpecificationCharacteristic
        type: array
        items: { $ref: '#/definitions/CharacteristicSpecification' }   # EXISTING definition
        description: 'Declares the input and output characteristics a task of this specification accepts and produces'
```

Rationale: an agent task can fail on its own terms and today can only be modelled as `cancelled`; and without a characteristic list at task-specification level the input/output contract of a task cannot be declared, only observed on the `TaskFlow`. The naming follows the existing `processFlowSpecificationCharacteristic` pattern; the final name is for the API owners.

### 3.4 State mapping (open question Q2)

A2A task states (submitted, working, input-required, completed, canceled, failed, and others in later versions; **to verify against the current A2A specification**) map as: submitted = `new`; working = `active`; input-required = `hold`; completed = `completed`; canceled = `cancelled`; failed = `failed` (addition). Whether `hold` should carry a reason characteristic (input needed, approval pending) is open.

### 3.5 Component side

No new component for shape A: any component exposing TMF701 can host agent workflows, and TMFC012 already shows the pattern. A deployment needs only the profile document and the two small schema additions.

## 4. Fallback shape B: new component skeleton (only if A cannot carry execution semantics)

```yaml
# Draft in the real IG1242 component.yaml shape. Every API id below exists in apis.json.
spec:
  componentMetadata:
    id: TMFCxxx                       # placeholder, never a real id
    name: AgentWorkflowOrchestration  # working name
    version: 0.1.0
    status: "proposed"
    description: Defines, runs and records multi-agent workflows over registered agents and tools.
    functionalBlock: TBD              # open question Q3
    eTOMs: []                         # not proposed (Q4)
    functionalFrameworkFunctions: []
    SIDs: []
  coreFunction:
    exposedAPIs:
      - { id: TMF701, apiType: openapi, apiSDO: tmForum, name: process-flow-management-api, required: true,
          resources: [ { processFlowSpecification: [GET, GET /id, POST, PATCH, DELETE] },
                       { taskFlowSpecification: [GET, GET /id, POST, PATCH, DELETE] },
                       { processFlow: [POST, GET, GET /id, DELETE] }, { taskFlow: [PATCH, GET, GET /id] } ] }
    dependentAPIs:
      - { id: TMF639, apiType: openapi, apiSDO: tmForum, name: resource-inventory-management-api, required: true,
          resources: [ { resource: [GET, GET /id] } ] }          # agents and tools (Proposal 1)
      - { id: TMF632, apiType: openapi, apiSDO: tmForum, name: party-management-api, required: false,
          resources: [ { organization: [GET, GET /id] } ] }
  managementFunction:
    exposedAPIs: [ { name: metrics, apiType: prometheus } ]
  securityFunction:
    exposedAPIs:
      - { id: TMF669, apiType: openapi, name: party-role-management-api, required: true }
  eventNotification:
    publishedEvents:                  # via the TMF701 hub
      - processFlowCreateEvent
      - processFlowStateChangeEvent
      - taskFlowStateChangeEvent
      - processFlowSpecificationCreateEvent
    subscribedEvents:
      - resourceStateChangeEvent      # TMF639: an assigned agent becoming unavailable
```

## 5. What this proposal does NOT resolve

- **Q1** How a task specification expresses *selection* of a performer ("any agent with skill X") rather than a fixed one; `relatedEntitySpecification` has no query semantics.
- **Q2** Final state machine, including a reason for `hold` and whether `failed` should be added or `cancelled` plus a characteristic is preferred.
- **Q3** Functional block for shape B; not assumed.
- **Q4** eTOM, functional-framework and SID mapping; not proposed.
- **Q5** **Streaming execution.** TMF701 notifications (hub callbacks) cover state changes, not token- or message-level event streams (SSE); whether that belongs in this API, in TMF688 (cached as "Event Streaming API"), or out of scope is open.
- **Q6** **LLM-planned workflows.** Generated plans, review gates and guardrail thresholds (the Canvas design mentions guardrails only in prose) have no schema here beyond `lifecycleStatus` and `generatedBy`.
- **Q7** **Evaluation.** The Canvas design describes an evaluation framework; how evaluation results attach to a `ProcessFlow` is not proposed.
- **Q8** **Non-deterministic re-execution and idempotence** of agent tasks are execution-engine concerns, deliberately not standardised here.
- **Q9** TMF701 is a *Beta* API in the TM Forum table (the TMFC012 specification URLs for it sit under `Beta`); the maturity of the base API must be considered before proposing extensions (to verify).
