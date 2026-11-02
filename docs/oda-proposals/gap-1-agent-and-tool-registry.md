# Proposal 1: Agent and tool registry with discovery (A2A Agent Cards, MCP tools)

**Status:** design sketch for review. Not a TM Forum specification; no real `TMFCxxx`/`TMFxxx` id is assigned.
**Grounding:** this gap is **new**. It is not in the cached `gaps-backlog.md` (the only related entry is the TMFS030 note about an "AI-powered / AI-native" taxonomy), none of the 24 cached use cases mention agents or AI, and it has **not been cross-corroborated** the way backlog entries are. Treat the evidence below as two independent observations, not a validated need.

## 1. Problem

Components can already declare `a2a` and `mcp` exposed APIs (Canvas component model). What is not standardised is how a Canvas or an operator **lists, discovers, trusts and tracks the lifecycle of** the agents and tools those APIs represent. `AI-Native-Canvas-design.md` calls for "a registry of components, agents, tools, and use cases" built on **TMF639** and a **Use Case Registry** tagging agents, tools and high-risk use cases, but defines neither the resource representation nor the discovery and trust behaviour.

## 2. Evidence base

| Source | What it shows |
|---|---|
| Canvas `AI-Native-Canvas-design.md` | Resource Inventory (TMF639) as the registry of components, agents, tools, use cases; Use Case Registry tagging agents/tools/high-risk use cases (EU AI Act) |
| One open-source implementation of an agent registry (OpenAN Registry Center, API reference) | Requirements actually needed in practice: identity = (provider organization, name); a signed card document; skills and interfaces; approval lifecycle (registered, published, rejected, deprecated); health/heartbeat; ranked discovery by task description; change notifications (registered, updated, deregistered, health changed); per-owner write isolation |
| Cached TMF639/TMF634/TMFC012 | Existing generic inventory, catalog and component the profile can reuse |

The implementation informs the requirement list only; the proposal below is expressed in A2A/MCP and TM Forum terms.

## 3. Preferred shape A: profile of TMF639 + TMF634 (no new component)

### 3.1 What exists today (unchanged)

- **TMF639 Resource Inventory (v4.0.0)**, `Resource` properties: `id, href, category, description, endOperatingDate, name, resourceVersion, startOperatingDate, activationFeature, administrativeState, attachment, note, operationalState, place, relatedParty, resourceCharacteristic, resourceRelationship, resourceSpecification, resourceStatus, usageState, @baseType, @schemaLocation, @type`. Required: `href`, `id`.
- State enums: `resourceStatus` = standby | alarm | available | reserved | unknown | suspended; `usageState` = idle | active | busy; `administrativeState` = locked | unlocked | shutdown; `operationalState` = enable | disable.
- `Characteristic`: `id, name, valueType, characteristicRelationship, value, @baseType, @schemaLocation, @type`. `ResourceSpecificationRef`: `id, href, name, version, ...`.
- TMF639 already exposes a **hub** and events: `resourceCreateEvent`, `resourceAttributeValueChangeEvent`, `resourceStateChangeEvent`, `resourceDeleteEvent`.
- **TMF634 Resource Catalog (v4.1.0)**, `ResourceSpecification`: `id, href, category, description, isBundle, lastUpdate, lifecycleStatus, name, version, attachment, featureSpecification, relatedParty, resourceSpecCharacteristic, resourceSpecRelationship, targetResourceSchema, validFor, ...`.
- **TMFC012 ResourceInventory (v2.2.0, functionalBlock Production)** already exposes TMF639 (required) and depends on TMF634 (required), TMF669, TMF632.

### 3.2 Proposed additions (profile conventions; **no schema change** needed for 3.2.1 to 3.2.4)

**3.2.1 Catalog (TMF634).** One `ResourceSpecification` per agent or tool *type*: `category: "ai-agent" | "ai-tool"`; `resourceSpecCharacteristic` entries declare the characteristics in 3.2.2; `targetResourceSchema` references the card/tool JSON schema (A2A AgentCard, MCP tool definition).

**3.2.2 Instance (TMF639).** One `Resource` per registered agent or tool endpoint:

| Concept | Profile mapping |
|---|---|
| name, version | `name`, `resourceVersion` |
| provider organization | `relatedParty` with `role: "provider"` referring to a TMF632 Organization |
| protocol | `resourceCharacteristic` `protocol` = `a2a` \| `mcp`, plus `protocolVersion` |
| endpoints | `resourceCharacteristic` `interfaces` (array of `{url, binding, version}`) |
| skills / tools | `resourceCharacteristic` `skills` (array of `{id, name, description, tags}`) |
| modes, capabilities | `resourceCharacteristic` `inputModes`, `outputModes`, `capabilities` |
| full source document | `resourceCharacteristic` `card` (valueType object) or `attachment` |
| signature result | `resourceCharacteristic` `signatureStatus` = verified \| unverified \| failed, and `signerKeyId` |
| identity uniqueness | (provider organization, name) unique within `category` (a profile constraint) |

**3.2.3 Lifecycle and health (open question Q1).** Candidate mapping of the registry lifecycle onto existing enums: registered/awaiting approval = `administrativeState: locked`; published = `unlocked`; deprecated = `shutdown`; rejected = `resourceStatus: suspended`; health = `resourceStatus` available | alarm | unknown. The alternative is a dedicated `lifecycleStatus` characteristic, leaving the enums to operational meaning.

**3.2.4 Notifications.** Use the existing TMF639 hub and events: registration = `resourceCreateEvent`, change = `resourceAttributeValueChangeEvent`, deregistration = `resourceDeleteEvent`, health/lifecycle = `resourceStateChangeEvent`. No other API is needed for notifications, so TMF688 is not required for this profile.

**3.2.5 Discovery filter (existing mechanism, open question Q2).** List by `category`, provider and skill id using TMF639's attribute filtering on `resourceCharacteristic`; the exact filter syntax for characteristic values must be confirmed against the TM Forum REST guidelines.

### 3.3 Proposed addition that **does** change the API surface

A ranked discovery operation, following the task-resource pattern TMF716 uses for actions (`/cancelResourceReservation`). **New, proposed:**

```yaml
# ADDITION to TMF639 (or to a profile document); not part of the existing schema
paths:
  /resourceDiscovery:            # proposed
    post:
      summary: Rank registered resources against a natural-language task description
      requestBody:
        content:
          application/json:
            schema: { $ref: '#/definitions/ResourceDiscovery_Create' }
      responses:
        '201': { description: Created, schema: { $ref: '#/definitions/ResourceDiscovery' } }
definitions:
  ResourceDiscovery_Create:      # proposed
    properties:
      task:         { type: string, description: 'Natural-language description of the work to be done' }
      category:     { type: string, description: 'ai-agent | ai-tool' }
      topN:         { type: integer }
      requiredSkill:{ type: string }
  ResourceDiscovery:             # proposed
    properties:
      id: { type: string }
      href: { type: string }
      match:
        type: array
        items:
          properties:
            resource: { $ref: '#/definitions/ResourceRef' }   # existing definition
            score:    { type: number }
            reason:   { type: string }
```

### 3.4 Component side

TMFC012 needs **no structural change** for shape A; the profile would be referenced from its description and conformance notes. eTOM and SID mapping for agent resources is **not proposed** (open question Q4).

## 4. Fallback shape B: new component skeleton (only if A cannot express discovery or trust)

```yaml
# Draft in the real IG1242 component.yaml shape. Illustrative; every API id below exists in apis.json.
spec:
  componentMetadata:
    id: TMFCxxx                      # placeholder, never a real id
    name: AgentRegistryManagement    # working name
    version: 0.1.0
    status: "proposed"
    description: Registers, verifies, discovers and tracks the lifecycle of AI agents and tools (A2A, MCP).
    functionalBlock: TBD             # open question Q3; TMFC012 sits in "Production"
    eTOMs: []                        # not proposed (Q4)
    functionalFrameworkFunctions: []
    SIDs: []
  coreFunction:
    exposedAPIs:
      - { id: TMF639, apiType: openapi, apiSDO: tmForum, name: resource-inventory-management-api, required: true,
          resources: [ { resource: [GET, GET /id, POST, PATCH, DELETE] } ] }
      - { id: TBD-ResourceDiscovery, apiType: openapi, apiSDO: tmForum, name: resource-discovery-api,   # NEW, proposed API (section 3.3)
          required: false, resources: [ { resourceDiscovery: [POST, GET /id] } ] }
    dependentAPIs:
      - { id: TMF634, apiType: openapi, apiSDO: tmForum, name: resource-catalog-management-api, required: true,
          resources: [ { resourceSpecification: [GET, GET /id, POST, PATCH] } ] }
      - { id: TMF632, apiType: openapi, apiSDO: tmForum, name: party-management-api, required: false,
          resources: [ { organization: [GET, GET /id] } ] }
  managementFunction:
    exposedAPIs: [ { name: metrics, apiType: prometheus } ]
  securityFunction:
    exposedAPIs:
      - { id: TMF669, apiType: openapi, name: party-role-management-api, required: true }
  eventNotification:
    publishedEvents:                 # via the TMF639 hub
      - resourceCreateEvent
      - resourceAttributeValueChangeEvent
      - resourceStateChangeEvent
      - resourceDeleteEvent
    subscribedEvents: []
```

## 5. What this proposal does NOT resolve

- **Q1** Overload the TMF639 state enums or add a `lifecycleStatus` characteristic? Needs a decision from the TMF639 owners.
- **Q2** Standard filter syntax for `resourceCharacteristic` values; whether skill-based filtering is portable across TMF639 implementations.
- **Q3** Functional block for shape B (Production, or a new AI block); not assumed.
- **Q4** eTOM, functional-framework and SID mapping for agent and tool resources; not proposed.
- **Q5** **Trust model.** Who verifies signatures and where verifier keys are registered (a key-registration operation, an `https` key-set URL, or both) is deliberately left open; it must align with the A2A signature specification current at submission time (to verify).
- **Q6** Whether the Use Case Registry (high-risk tagging) is a characteristic of the agent resource, a separate resource, or an extension of `dependentModels`; the Canvas design describes it only in prose.
- **Q7** How the Canvas resource-inventory service models agents today; **not verified**, and it determines whether A is a documentation task or a code change.
- **Q8** Scale limits (the implementation studied limits registrations by default) are an implementation matter, not proposed as a standard constraint.
