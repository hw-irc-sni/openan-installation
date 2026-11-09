# Plan: TMF639 facade for the Registry Center (TMFC012 alignment)

Status (2026-11-09):

- §1–§4 (facade, hub and events) are implemented in `registry-center` on branch `feat/tmf639-facade` of the fork `hw-irc-sni/registry-center`, merged into the fork's `oda-integration` (e60f0f9). Not yet proposed upstream.
- Deployed in the kind demo and verified end to end through the Canvas gateway, including the hub (§4.1).
- §5 (chart) is implemented in this repo.
- §8 (catalog) is still a proposal.

## Goal

Package the OpenAN Registry Center (RC) as an ODA Component aligned with **TMFC012 Resource
Inventory v2.5.0**, which requires exposing **TMF639 Resource Inventory Management**
(`resource`: `GET`, `GET /id`, `POST`, `PATCH`, `DELETE`).

Today RC exposes only its proprietary API under `/rest/v1/registry-center`. Declaring `TMF639`
in the chart before a TMF639 endpoint exists would be a false conformance claim. That is why the
facade comes first and the chart change second.

## Decisions (agreed 2026-11-09)

| # | Decision |
|---|---|
| 1 | Resource `id` is a deterministic encoding of organisation + name (§2). |
| 2 | TMF639 **v5 only**. |
| 3 | The facade is implemented **inside `registry-center`** (§1). |
| 4 | TMF634 is declared `required: false`, **but only rendered when `catalog.enabled=true`** (default false), because of the §6 findings. The deviation from TMFC012 is documented (§7). A Resource Catalog compatible solution is planned in §8. Conditional rendering confirmed 2026-11-09 (option a). |
| 6 | The Canvas behaviours found in §6 are raised upstream. Posted as [#663](https://github.com/tmforum-oda/oda-canvas/issues/663) (optional dependent APIs block `Complete`) and [#664](https://github.com/tmforum-oda/oda-canvas/issues/664) (resolved DependentAPI needs canvas-info); drafts in `oda_canvas/upstream-issues/`. |
| 5 | The Canvas dependent-API resolution check has been done; results are in §6. |

## 1. Where the facade lives

A new `agent_registry/tmf639/` FastAPI router in the `registry-center` repo. It is mounted with
`app.include_router(...)` the same way `knowledge_graph_router` is mounted
(`agent_registry/server.py:2059`), and gated by `REGISTRY_TMF639_ENABLED` (default off).

It reuses, in-process:
- RC authentication (token/static_bearer, mTLS) and owner isolation
- signature validation and content-safety checks
- approval visibility rules
- the event bus

Base path: `/tmf-api/resourceInventoryManagement/v5`.

This repo is install-only (see AGENTS.md), so it gets only the chart changes (§5) and a new CI pin
in `.github/workflows/deployment-contract.yml` for the `registry-center` revision that contains
the facade.

## 2. Resource mapping (AgentCard → TMF639 `Resource`)

The mapping mirrors the Canvas's own TMF639 component registry
(`oda-canvas/source/tmf-services/TMF639_Resource_Inventory/services/KubernetesResourceService.js`:
`LogicalResource`, a `category`, a `resourceSpecification` ref, `resourceCharacteristic`).

Field shapes still need checking against the TMF639 v5 OAS listed in the TMFC012 YAML. For
example, in v5 `relatedParty` is a `RelatedPartyRefOrPartyRoleRef` wrapper (`role` plus
`partyOrPartyRole`), not a flat ref.

| TMF639 field | Source in RC |
|---|---|
| `id` | `b64url(organization) + "." + b64url(name)`: UTF-8, base64url without padding. `.` is not in the base64url alphabet, so the split is unambiguous and the id is safe in one path segment. Decoding gives the `(organization, name)` key RC already uses. An id that doesn't decode gives 404, not 400, so ids can't be probed. |
| `href` | `{publicBaseUrl}/<component>/tmf-api/resourceInventoryManagement/v5/resource/{id}`. It must include the Canvas gateway's `/<component>` prefix, because clients follow `href`. |
| `@type` / `@baseType` | `LogicalResource` / `Resource` |
| `name`, `description`, `version` | `card.name`, `card.description`, `card.version` |
| `category` | `A2AAgent` (fixed; lets consumers filter agents apart from Canvas `ODAComponent` entries) |
| `resourceSpecification` | Without a catalog: a static ref `{name: "A2A AgentCard", version: card.protocolVersion}`. With a catalog: the catalog entry's `id`/`href` (§8). |
| `relatedParty` | `provider.organization` with role `provider`; owner identity with role `owner` (owner/admin only) |
| `administrativeState` / `lifecycleState` | `published`→`unlocked`/`installed`, `registered`/`rejected`→`locked`/`planned`, `deprecated`→`shuttingDown`/`pendingRemoval` (v5 enums) |
| `operationalState` / `availabilityStatus` | health `healthy`→`enabled`/`online`, `suspect`→`enabled`/`degraded`, `offline`→`disabled`/`offline`; `unknown` or heartbeat off → both omitted, `unknownStatus: true`. v5 has no `resourceStatus`. |
| `resourceVersion`, `creationDate` | `card.version`; registration time |
| `resourceCharacteristic` | `agentCard` (the stored **signed** card, camelCase), `url` and `protocolVersion` (from the first `supportedInterfaces` entry; v1 cards have no top-level url), `skills`, `tags`, `layer`, `reviewStatus` (exact RC status), `healthStatus`, `lastHeartbeat` |

Visibility (as implemented): callers see discoverable cards (published, not hidden by heartbeat
filtering) plus the cards they own (verified owner); admins (token mode, `nms_oss`) see every state.
Any other unpublished card is 404 on every method, and a 403 on someone else's visible card does not
name the owner. Milvus-only mode answers 503 where RC does.

Implemented in `registry-center` branch `feat/tmf639-facade` (`agent_registry/tmf639/`), §1–§3; the
API Reference section "TMF639 Resource Inventory facade" there is the authoritative contract.

## 3. Operations

| TMF639 | Maps to | Notes |
|---|---|---|
| `GET /resource` | query, or the manage list for admins | Filters: `name`, `category`, `administrativeState`, `operationalState`, `relatedParty.name` (organization). `fields=`, `offset`/`limit`, `X-Total-Count`/`X-Result-Count` headers. RC has no server-side state filters, so the facade filters in memory. Pagination and counts must be computed **after** visibility filtering, or the counts leak unpublished cards. |
| `GET /resource/{id}` | get card + review status + health | 404 when the card is not visible to the caller (no existence leak) |
| `POST /resource` | register | The body **must** carry the signed card in `resourceCharacteristic[name=agentCard]`. Other fields are derived, so they are ignored or rejected with 400 if they conflict. Signature checks and content-safety checks run unchanged. |
| `PATCH /resource/{id}` | full replace (PUT) | JSON merge patch only. The only patchable field is `resourceCharacteristic` carrying a new signed `agentCard`; anything else returns 400. Cards are JWS-signed, so a field-level patch cannot be applied without breaking the signature. |
| `DELETE /resource/{id}` | deregister | 204 |

Out of scope for TMF639; these stay on the native API: semantic search, publish/reject/deprecate,
heartbeat, health history/stream, keys, knowledge graph.

## 4. Events (TMF639 hub)

- `POST /hub`, `DELETE /hub/{id}` map onto RC subscriptions (same allowlist, same HMAC option).
  Each subscription gets a `format: tmf639` flag so the dispatcher wraps payloads in the TMF event
  envelope (`eventId`, `eventTime`, `eventType`, `event.resource`).
- **RC events track discoverability, not storage** (`core.py` `_publish_discoverability_event` and
  the review transitions around `core.py:990-1020`):
  - `AGENT_REGISTERED` fires when a card **becomes published**: either it was registered as
    published (no approval), or a pending card was approved.
  - Registering a pending (`registered`) card emits **nothing**, so subscribers never learn about
    unpublished cards.
  - `AGENT_DEREGISTERED` fires when a published card leaves discovery: deleted, or moved out of
    `published` by any transition other than reject or deprecate.
  - `AGENT_REVIEW_REJECTED` and `AGENT_REVIEW_DEPRECATED` carry only the identity and the reason,
    never the card.

  The TMF639 hub therefore means "**published-agent inventory changes**". A `POST /resource` that
  lands as `registered` produces no event until it is approved. This must be documented, because a
  TMF consumer would otherwise expect a create event on every POST.
- Event mapping. TMF639 v5 names, confirmed against the v5.0.0 OAS: `/hub`, `GET`/`DELETE /hub/{id}`, and listeners for the
  four events below. TMFC012's
  published-events table lists the v4 names: `resourceCreateEvent`, `resourceChangeEvent`,
  `resourceDeleteEvent`, `resourceAttributeValueChangeEvent`.

  | RC event | TMF639 v5 event | Payload |
  |---|---|---|
  | `AGENT_REGISTERED` | `ResourceCreateEvent` | full mapped resource |
  | `AGENT_UPDATED` | `ResourceAttributeValueChangeEvent` | full mapped resource |
  | `AGENT_DEREGISTERED` | `ResourceDeleteEvent` | `id`, `href`, `name` only |
  | `AGENT_REVIEW_DEPRECATED` | `ResourceStateChangeEvent` (`administrativeState: shuttingDown`) | `id`, `href`, `name`, state only; no card. Forwarded only when `from_status` was `published`: the native event is not discoverability-gated. |
  | `AGENT_REVIEW_REJECTED` | not forwarded | a rejected card was never public |
  | `AGENT_HEALTH_CHANGED` | `ResourceStateChangeEvent` (`operationalState`, `availabilityStatus`) | only for published cards |
  | `SYNC_REQUIRED` | not forwarded | Facade consumers re-list with `GET /resource`. TMF639 has no change-feed cursor; RC's `/changes?since=` stays on the native API. |
- As implemented:
  - Each event is POSTed on its own to `callback` exactly as given, like the Canvas reference TMF639
    service. The hub `query` supports only `eventType=...`.
  - Delivery is at-least-once: retries happen in process, then through the durable retry sweep, up to
    `broadcast.delivery.max.attempts`.
  - Order is not guaranteed across retries.
  - Over the rate limit, events stay pending instead of degrading to `SYNC_REQUIRED`.
  - The API Reference in `registry-center` is the contract.

### 4.1 Demo verification (2026-11-09)

Kind demo, Registry Center built from `oda-integration` (`OPENAN_RC_BUILD=true OPENAN_RC_TAG=src`), with
`OPENAN_TMF639_ENABLED=true`, the profile URL on the fork's `oda-integration` branch, and
`OPENAN_BROADCAST_ENABLED=true OPENAN_BROADCAST_CALLBACK_HOSTS=hub-listener.components.svc`.

- **Setup.** A throwaway HTTPS listener (`hub-listener` in `components`) logged every callback. Its certificate came from a
  temporary CA, which the Registry Center was made to trust by patching its Deployment out of band (`SSL_CERT_FILE` plus a
  mounted CA ConfigMap). The listener and the patch were removed afterwards, and broadcast was turned off again.
- **Component.** `Complete` as TMFC012. With broadcast on, `eventNotification.publishedEvents` lists `resourceinventory`
  with the profile URL; with broadcast off, the list is empty.
- **Hub API through the gateway** (`https://127.0.0.1/openan-registry-center-registrycenter/tmf-api/resourceInventoryManagement/v5`),
  using the operator's token (`vendor_agent` role, not admin): `POST /hub` returned 201 with `Location`, `GET /hub/{id}`
  returned 200, and `DELETE /hub/{id}` returned 204. Without a token, `POST /hub` returned a TMF630 401.
- **Events.** The hub `query` was `eventType=` with all four events.

  | Trigger | Callback received | Latency |
  |---|---|---|
  | `DELETE /resource/{id}` of `Sample-A2A-T-Agent` | `ResourceDeleteEvent` | about 0.3 s |
  | Operator restart, so card sync registers the agent again | `ResourceCreateEvent` | delivered with the registration |

  - Both callbacks went to the `callback` path as given (`/tmf639/listener`), one event per POST.
  - Each callback carried the envelope (`@type`, `@baseType: Event`, `eventId`, `eventTime`, `eventType`, `title`,
    `domain: ResourceInventory`).
  - The Create event carried the full `LogicalResource` (`administrativeState: unlocked`, `lifecycleState`, the
    characteristics, `resourceSpecification`, `relatedParty`). The Delete event carried the reference only.
  - `href` used `registry.publicBaseUrl`, so it pointed at the gateway URL.
  - The hub had no `secret`, so the callbacks carried no `X-Registry-Signature`, as documented.
- **Not exercised:** `ResourceAttributeValueChangeEvent` and `ResourceStateChangeEvent`, signed callbacks, and
  retry and degraded delivery. The unit tests in `registry-center` (`tests/test_tmf639_events.py`,
  `tests/test_tmf639_api.py`) cover them.

## 5. Chart changes (this repo, after the facade ships)

Implemented in chart 0.2.0 with these differences from the list below:

- **Hub wiring.** `broadcast.enabled`, `broadcast.callbackAllowlist` and `broadcast.allowHttpCallbacks`
  are first-class values. They are generated env, because the operator rejects `ALLOWLIST` in `extraEnv`.
  `publishedEvents` is rendered only when both `tmf639.enabled` and `broadcast.enabled` are set.
- **Profile URL.** `tmf639.specificationUrl` has no default and is required when TMF639 is enabled.
  The canonical TMF OAS URL is refused. The profile lives at `registry-center/docs/openapi/tmf639-a2a-agent-v5.yaml`;
  use a pinned upstream tag once it is merged.
- **Catalog.** `catalog.*` only declares the DependentAPI. No `REGISTRY_CATALOG_*` env is generated until §8 exists in RC.
- **CI pin.** The `deployment-contract.yml` pin is unchanged. It only feeds the Compose smoke test, which
  doesn't use TMF639, and AGENTS.md forbids pinning fork commits.
- **Operator.** `GENERATED_ENV` lists the four new keys.

`charts/openan-registry-center`:

1. `templates/component.yaml`
   - `componentMetadata`: `id: TMFC012`, `functionalBlock: Production`,
     `eTom: [1.5.4, 1.5.4.5]`, `SID: [Resource_ABE]`, and a description stating that it is an
     A2A-agent inventory implementation, with the deviations listed in §7.
   - `coreFunction.exposedAPIs`:
     - `tmf639`:
       - `id: TMF639`, `apiSDO: tmForum`, `required: true`, `resources: [resource]`
       - `specification: [{url: <stable TMF639 v5 profile URL>, version: "5"}]`. This must be a
         **release-independent** URL: the profile published in the `registry-center` repo at a
         pinned tag, e.g. `https://raw.githubusercontent.com/project-openan/registry-center/<tag>/docs/openapi/tmf639-a2a-agent-v5.yaml`.
         It is exposed as the value `tmf639.specificationUrl`. Do **not** use the canonical TMF
         OAS URL (§6), and do not use the in-cluster sidecar URL either, because it contains the
         release name and namespace. The sidecar copy stays only as a convenience for the gateway
         and developer UI.
       - `path: /<component>/tmf-api/resourceInventoryManagement/v5`, `port: 5000`
     - keep `agentregistry` (native API) with `required: false`
   - `coreFunction.dependentAPIs`: `TMF634` with `required: false`, rendered **only when
     `catalog.enabled=true`** (default false). See §6 for why it can't be declared
     unconditionally.
   - `eventNotification.publishedEvents`: `ResourceCreateEvent`,
     `ResourceAttributeValueChangeEvent`, `ResourceStateChangeEvent`, `ResourceDeleteEvent`
     (TMF639 v5; see §4).
   - Do not add TMF688 or TMF672 (the Canvas operators provide them, per IG1242 v17).
2. `values.yaml`
   - `tmf639.enabled` (default false; when false, render the current component unchanged),
     passed to the container as `REGISTRY_TMF639_ENABLED`.
   - `catalog.*`, see §8.
   - Updated `component.*` defaults.
3. `openapi/`: keep `registry-center.yaml`, and add `tmf639-a2a-agent-v5.yaml`, the TMF639 v5
   profile restricted to what §3 supports (signed-card POST/PATCH bodies, the supported filters).
4. `Chart.yaml`: bump to `0.2.0` with a changelog line.
5. Tests: update `tests/test_oda_component_charts_contract.py` and
   `tests/test_chart_value_quoting.py` for each combination of `tmf639.enabled` and
   `catalog.enabled`.
6. Docs: `oda_canvas/README.md`; mirror in the zh docs if `docs/` pages change.

## 6. Check: Canvas dependent-API resolution (done 2026-11-09)

Sources:
- `oda-canvas/source/operators/TMFOP005-Dependency-Management/simple-dependency-management/docker/src/dependentApiSimpleOperator.py`
- the Keycloak variant `dependencyManagementKeycloakAuthzOperator.py`
- `TMFOP001-Component-Management/component-management/componentOperator.py`
- the running kind cluster

### How a provider is chosen

- The demo runs **`dependentapi-simple-operator`** (canvas-oda default) with
  `componentRegistryUrl: ""`. It therefore uses `customresource_service_discovery`.
- That function lists **every ExposedAPI in the cluster** (all namespaces) and puts the
  requester's own APIs last. It returns the **first** one with `apiType == openapi`, ready, and
  **`spec.specification.url` exactly equal** to the DependentAPI's specification URL.
- Nothing else is consulted: not the TMF `id` (`TMF639`), names, labels, namespaces, or
  `category`. There is also no scoping or tie-breaking beyond list order.
- With the Component Registry enabled, it uses the same exact-URL filter
  (`component_reg_client.py`) and the first match wins. If that first match is not `available`,
  it raises a TemporaryError instead of trying the next provider.
- The Keycloak variant matches on the same URL, but only within an explicit
  `target-component`, so it can't be hijacked.

### Consequences

- **Risk confirmed, but only for a shared URL.** If RC declared TMF639 with the canonical TMF OAS
  URL, any component depending on that literal URL (for example, a TMFC011 or TMFC038 that needs
  network resources) could be bound to the agent registry. In this demo RC would be the *only*
  TMF639 provider, so it would always be chosen.
- **Mitigation: a distinct, stable specification URL.** Declare a release-independent TMF639
  profile URL (§5). Only components that ask for that exact URL will resolve to RC, and generic
  TMF639 dependents never match. A consumer that legitimately wants agents declares the same
  stable URL, without needing to know RC's release name or namespace. An in-cluster sidecar URL
  would require that, because matching is exact-string. The cluster shows that providers already declare author-chosen URLs: raw GitHub URLs for
  the product-catalog reference component, in-cluster sidecar URLs for RC and OC. So this is
  normal practice, not a workaround. `id: TMF639` stays declared for directory and conformance
  purposes; discovery doesn't use it.

### Two further findings that affect decision 4

- **`required: false` doesn't affect Component status.** In `componentOperator.py`, the summary
  around line 2238 counts *every* declared DependentAPI. A Component reaches `Complete` only when
  all of them are ready; otherwise it stays `In-Progress-DepApi` indefinitely. In this demo,
  `make component-sample` requires every component to be `Complete`, so it would fail.
- **A resolved dependency also needs canvas-info.** When the simple operator finds a match, it
  writes the result to the canvas-info service (TMF638, `updateServiceInventory`) *before* marking
  the DependentAPI ready, and raises a TemporaryError if that fails. The demo has
  `canvas-info-service.enabled: false`, so even a resolvable TMF634 dependency would never become
  ready here.

**Result:** the TMF634 dependency is declared `required: false` as agreed, but rendered only
when `catalog.enabled=true`. Enabling it is supported only when the Canvas has a TMFC010 provider
**and** `canvas-info-service` enabled (§8). With the default (`false`), the deviation is
documented (§7) and the Component still reaches `Complete`.

## 7. Documented deviations from TMFC012

To be stated in the component `description`, the chart README, and `oda_canvas/README.md`.

| TMFC012 says | This component | Why |
|---|---|---|
| TMF634 dependency **required**; inventory items checked for consistency with the Resource Catalog | TMF634 **optional**. Without a catalog, cards are checked against the A2A AgentCard schema and their signature. With `catalog.enabled`, they are checked against a catalog spec (§8). | RC entries are signed A2A cards, whose schema RC already validates. A catalog isn't available in every install. |
| TMF639 `POST`/`PATCH` on arbitrary resources | Only signed `agentCard` payloads; `PATCH` replaces the whole card. | JWS integrity; a field patch would break the signature. |
| Create events on every inventory create | Events only for **published** agents (§4). | RC never announces unapproved cards. |
| Canonical TMF639 OAS URL | Stable, release-independent TMF639 v5 profile URL. | Prevents Canvas dependency hijacking (§6). |
| TMF685, TMF716, TMF701 (optional APIs) | Not exposed. | Pools, reservations and process flows don't apply to agents. |

## 8. Plan: Resource Catalog (TMF634) compatible solution

Goal: meet TMFC012's catalog-consistency requirement for real. Every agent resource references a
`ResourceSpecification` in a TMFC010 Resource Catalog, and registration is checked against it.
RC stays a **consumer** of TMF634, not a provider: the catalog side belongs to TMFC010, and
exposing TMF634 from RC would bring back the §6 hijacking risk.

### 8.1 Catalog content

- One `ResourceSpecification` per supported A2A protocol version: name `A2A AgentCard`, `version`
  set to the A2A `protocolVersion`, `lifecycleStatus: Launched`, `isBundle: false`.
  - `resourceSpecCharacteristic` mirrors the card contract: `url`, `protocolVersion`, `skills`,
    `capabilities`, `provider`, `securitySchemes`, `layer`, `tags`, with value types and
    mandatory flags taken from RC's AgentCard validation.
  - A `targetResourceSchema` points to the AgentCard JSON schema served by the spec sidecar.
- One `ResourceCategory`, `A2A Agents`, optionally with child categories per RC layer. The
  specifications are linked to it.
- Optional, later: a narrower spec per agent family (for example "RAN optimisation agent") if
  operators want to govern which kinds of agents can be onboarded. Out of scope for the first
  iteration.

### 8.2 Registry Center behaviour (code in `registry-center`)

New module `agent_registry/catalog/`, enabled by `REGISTRY_CATALOG_ENABLED`:

1. **Endpoint resolution.** Use `REGISTRY_CATALOG_URL` if it is set. Otherwise look up the
   resolved DependentAPI URL in canvas-info (`GET .../service`, filtered by the `componentName`
   and `dependencyName` characteristics), the same way the reference components do it
   (`reference-example-components/.../utils/downstreamAPI.js`).
2. **Seeding.** At startup, and only if `REGISTRY_CATALOG_SEED=true`, create the spec(s) and the
   category if they don't exist. Look them up idempotently by name + version, and never
   overwrite an existing spec. Disable seeding when catalog governance belongs to another team.
3. **Cache.** Keep an in-memory cache of the relevant specs, keyed by `protocolVersion`. Refresh
   it from a TMF634 hub subscription (`resourceSpecificationCreateEvent` / `ChangeEvent` /
   `DeleteEvent`, which TMFC012 lists as subscribed events), plus a periodic re-list as a
   fallback. The listener endpoint is RC's own, behind the gateway.
4. **Consistency check on register and replace**, controlled by `REGISTRY_CATALOG_ENFORCEMENT`
   (`enforce` | `warn` | `off`):
   - `enforce`: reject with 422 when no `Launched`/`Active` spec matches the card's
     `protocolVersion`, or when a mandatory characteristic is missing.
   - `warn`: register anyway, but log and audit the mismatch.
   - When the catalog can't be reached, `enforce` fails closed (503); `warn` registers without
     the ref and marks `catalogCheck: unverified`.
5. **Linking.** The TMF639 `resourceSpecification` ref uses the catalog `id`/`href`/`version`.
   Native API responses get the same ref as a non-breaking extra field.
6. **Retirement.** When a spec moves to `Retired`/`Obsolete`, new registrations against it are
   rejected. Existing agents keep running but carry `specStatus: retired`, and RC emits a
   `ResourceAttributeValueChangeEvent` for each affected published agent.

### 8.3 Chart (this repo)

`values.yaml`:

```yaml
catalog:
  enabled: false        # renders the TMF634 DependentAPI (required: false)
  specificationUrl: "https://tmf-open-api-table-documents.s3.eu-west-1.amazonaws.com/OpenApiTable/TMF634_Resource_Catalog/5.0.0/swagger/TMF634-Resource_Catalog_Management-v5.0.0.oas.yaml"
  url: ""               # optional explicit endpoint; bypasses canvas-info lookup
  seed: true
  enforcement: warn     # enforce | warn | off
```

- `specificationUrl` must equal **literally** the URL the catalog provider declares. The default
  is the canonical TMF634 v5 URL that the TMFC010 reference implementation declares
  (`TMFC010-v1.6.0/ComponentRI/templates`).
- `component.yaml` renders the DependentAPI only when `catalog.enabled`. The values map to
  `REGISTRY_CATALOG_*` environment variables in the ConfigMap.
- Contract tests cover rendered and not-rendered cases, and check that `enabled` without a URL
  source still renders (resolution happens at runtime).

### 8.4 Demo (`oda_canvas/`)

- A new opt-in target, e.g. `make resource-catalog`. It installs the TMFC010 reference
  implementation (`TMFC010-v1.6.0/ComponentRI`, image `akumartmf/resourcecatalogv5api:0.2`,
  TMF634 v5 on port 8634) into `components`.
- When the catalog is enabled, it also sets `canvas-info-service.enabled: true` in
  `canvas-values.yaml.tpl`, because the simple operator needs it (§6).
- Then it installs RC with `catalog.enabled=true`.
- Acceptance:
  - RC's TMF634 DependentAPI becomes ready and the Component reaches `Complete`.
  - The `A2A AgentCard` spec is visible in the catalog.
  - `GET /resource/{id}` returns a `resourceSpecification.href` that resolves in the catalog.
  - With `enforcement: enforce`, a card with an unsupported `protocolVersion` gets 422.

### 8.5 Open items to verify before implementing

- Authentication between RC and the catalog. The TMFC010 RI declares a static Canvas role.
  Check whether TMFOP004 Credentials Management can issue RC a client credential, or whether the
  demo catalog accepts unauthenticated in-cluster calls.
- Whether the TMFC010 RI supports hub subscriptions for `resourceSpecification*` events. If not,
  rely on the periodic re-list only.
- Whether TMF634 v5 `resourceSpecCharacteristic` can express the nested AgentCard structures
  (`skills[]`), or whether `targetResourceSchema` alone carries them.

## 9. Known gaps and risks

- **Overlap with the Canvas registry.** The Canvas already exposes components and APIs through its
  own TMF639 registry (`component-registry-service-tmf639`). Decide whether RC agents should also
  appear there (federation), or stay only in RC with `category: A2AAgent`.
- **Conformance testing.** The local `oda-component-ctk` has no TMF639 API CTK. Use the TM Forum
  TMF639 v5 CTK and add a BDD feature in the style of
  `TMFC012-v2.4.0/BDD/TMFC012-DependentAPIInteraction.feature`.
- **Partial conformance by design.** POST and PATCH accept only a signed AgentCard, so a generic
  CTK that creates or patches plain resources will get 400. Expect full conformance on `GET`,
  `GET /id` and `DELETE`. POST and PATCH pass only if the CTK can be fed a pre-signed card payload.
  Otherwise they are documented partial conformance (§7).
- **Upstream Canvas behaviour.** Both the "`required` is ignored for Component status" and the
  "canvas-info is mandatory for a resolved dependency" behaviours are worth raising as
  `oda-canvas` issues. If fixed upstream, the TMF634 DependentAPI could be declared
  unconditionally.

- **Callback TLS trust.** The Registry Center verifies callback certificates against its default CA bundle, and
  neither the chart nor the OpenAN CR can add a CA. A CR also cannot enable plain-HTTP callbacks. So in-cluster
  listeners need a certificate from a public CA. Otherwise they need an operator-approved way to supply a CA bundle,
  which does not exist yet. The §4.1 test used an out-of-band Deployment patch.

## 10. Suggested sequence

1. Implement the TMF639 router (§1–§3) plus unit tests in `registry-center`, behind
   `REGISTRY_TMF639_ENABLED`.
2. Implement the hub/event formatter (§4).
3. Run the TMF639 v5 CTK against a local RC. Expect `GET`/`DELETE` to pass and `POST`/`PATCH`
   to pass only with pre-signed card fixtures (§9), and record the result as the conformance
   statement.
4. Make the chart changes (§5) behind `tmf639.enabled`, plus the profile OAS, the contract
   tests and the CI pin.
5. Deploy in the kind demo (`make bootstrap-openan`). Verify gateway routing, check that no
   other component's TMF639 dependency resolves to RC, and check that the Component reaches
   `Complete`. Done 2026-11-09, including an end-to-end hub test (§4.1).
6. Catalog phase (§8): the `agent_registry/catalog/` module, `catalog.*` values,
   `make resource-catalog`, and the acceptance checks in §8.4.
