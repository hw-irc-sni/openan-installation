# openan-registry-center

Helm chart that packages the OpenAN Registry Center (registry and semantic search of signed A2A Agent Cards) as an
**ODA Component** (`oda.tmforum.org/v1`). It is normally installed by the OpenAN operator
(`operator/openan-operator`), which creates the referenced Secrets and passes these values from the OpenAN CR
(`spec.registryCenter.values`). It can also be installed by hand into a namespace watched by the ODA Canvas.

## Install

```bash
helm upgrade --install openan-registry-center ./charts/openan-registry-center -n components \
  --set image.tag=<tag> --set runtimeSecret=<secret> --set credentialSecret=<secret>
```

Required values (the render fails without them):

| Value | Purpose |
|---|---|
| `image.tag` | No usable default; see `docs/en/TRANSPORT_DEPLOYMENT.md` |
| `runtimeSecret` | Secret with `INTEGRATION_TOKEN_HMAC_KEY` / `INTEGRATION_FINGERPRINT_KEY` (env) |
| `credentialSecret` | Secret with `integration_credentials.conf` (static bearer tokens) |
| `credentialsChecksum` | Set by the OpenAN operator to the hash of the credentials file; adds the pod annotation `checksum/credentials`, so the pod restarts when the file changes (it is mounted by `subPath`). Empty: no annotation |

The Component is named `<release>-<component.name>` (default `<release>-registrycenter`). Keep `replicas: 1`: events
and schedulers are process-local.

## What it renders

- The `Component`, with the native API `agentregistry` (`/<component>/rest/v1/registry-center`, port 5000). Its OpenAPI
  document is served by a small `spec` sidecar on `specPort` (8081).
- A Deployment with a PVC for `data/` and `log/`, and a Service.
- A ConfigMap of generated `REGISTRY_*` variables. Generated keys always win over `registry.extraEnv`, and the operator
  keeps the same list in `GENERATED_ENV` (`operator/openan-operator/src/stack.py`).

**Gateway path prefix.** The ODA Canvas component gateway forwards `/<componentName>/...` to the Service unchanged. The
chart therefore always sets `REGISTRY_PATH_PREFIX=/<componentName>`, and the Registry Center (`path.prefix`) serves every
route both with and without that prefix. Images without `path.prefix` (including `v1.1.2`) ignore it, so their
ExposedAPI URLs answer 404 through the gateway. In-cluster clients that call the Service directly are unaffected.

**Callback CA bundle.** Callback certificates are verified against the image's default CAs. For listeners with private
certificates (for example in-cluster), set `broadcast.callbackCaConfigMap` to a ConfigMap in the release namespace whose
key `broadcast.callbackCaKey` holds the CA bundle (PEM). The chart mounts it and sets
`REGISTRY_BROADCAST_CALLBACK_CA_FILE`; the Registry Center (`broadcast.callback.ca.file`) trusts it in addition to the
default CAs, for callback deliveries only. Verification is never disabled. A missing ConfigMap keeps the pod from
starting, and an unloadable bundle stops the Registry Center at startup. The bundle is read at startup, so restart the pod
after changing it. Images without `broadcast.callback.ca.file` ignore it.

## Values

| Value | Default | Purpose |
|---|---|---|
| `component.id` / `component.functionalBlock` | empty | `OPENAN-RC` / `AIAgentRegistry`; `TMFC012` / `Production` with `tmf639.enabled` |
| `component.name`, `component.version`, `component.apipolicy.*` | see `values.yaml` | Component name suffix, metadata, Canvas gateway policy for every exposed API |
| `registry.identityMode`, `registry.authMode` | `token`, `static_bearer` | Main-port identity (the operator controls them) |
| `registry.publicBaseUrl` | empty | External base URL including the gateway prefix; used for TMF639 `href`s and `Location` headers |
| `registry.signatureValidation` | `true` | Verify Agent Card signatures |
| `registry.extraEnv` | `{}` | Extra non-secret `REGISTRY_*` settings (generated keys cannot be overridden) |
| `registry.modelsYaml` | empty | Complete `models.yaml`; empty means no models |
| `trust.organization`, `trust.configMap` | empty | Local JWK verification keys, mounted under `etc/sign_verify/jwks/<organization>` |
| `persistence.*`, `podSecurityContext`, `resources` | see `values.yaml` | Storage and pod user (source builds run as 10001) |
| `tmf639.enabled`, `tmf639.specificationUrl` | `false`, empty | TMFC012 alignment, below |
| `broadcast.enabled`, `broadcast.callbackAllowlist`, `broadcast.allowHttpCallbacks` | `false`, empty, `false` | Change broadcast for native webhooks and the TMF639 `/hub`; the allowlist is required when enabled |
| `broadcast.callbackCaConfigMap`, `broadcast.callbackCaKey` | empty, `ca.crt` | ConfigMap and key of a PEM bundle of extra CAs for callback TLS, below |
| `catalog.enabled`, `catalog.specificationUrl` | `false`, canonical TMF634 v5 URL | Optional TMF634 DependentAPI, below |

## TMFC012 Resource Inventory (optional)

With `tmf639.enabled=true` the Component declares itself as TMFC012 Resource Inventory of A2A agents:

- It exposes `TMF639` v5 (`required: true`) at `/<component>/tmf-api/resourceInventoryManagement/v5`, next to the native
  API, which becomes `required: false`.
- `componentMetadata` gets `eTom: [1.5.4, 1.5.4.5]`, `SID: [Resource_ABE]` and a description listing the deviations
  below.
- `REGISTRY_TMF639_ENABLED=true` is set. The image must contain `agent_registry/tmf639`.

`tmf639.specificationUrl` is required. It must be an `https` URL of the Registry Center profile
`tmf639-a2a-agent-v5.yaml`, for example
`https://raw.githubusercontent.com/project-openan/registry-center/<tag>/docs/openapi/tmf639-a2a-agent-v5.yaml`. The
Canvas matches dependencies by exact specification URL, so declaring the canonical TMF OAS URL would make this
component the TMF639 provider for every other component in the cluster. The render refuses any other file. The spec
sidecar also serves a copy (`openapi/tmf639-a2a-agent-v5.yaml`) for the gateway and developer UI, but the Component
never declares that release-specific in-cluster URL.

With `broadcast.enabled=true` as well, `eventNotification.publishedEvents` lists `ResourceCreateEvent`,
`ResourceAttributeValueChangeEvent`, `ResourceStateChangeEvent` and `ResourceDeleteEvent`. These are delivered to
`/hub` subscribers as TMF630 callbacks, not through a Canvas event topic.

`catalog.enabled=true` declares the `TMF634` DependentAPI with `required: false`. It is only a declaration: the
Registry Center does not check cards against a catalog yet. The Canvas ignores `required` when computing status, so the
Component stays `InProgress` until a catalog that declares exactly `catalog.specificationUrl` is deployed.

### Deviations from TMFC012

| TMFC012 says | This component |
|---|---|
| TMF634 dependency required; inventory checked against the Resource Catalog | TMF634 optional; cards are validated against the A2A AgentCard schema and their signature |
| TMF639 `POST`/`PATCH` on arbitrary resources | Only signed `agentCard` payloads; `PATCH` replaces the whole card |
| Create events on every inventory create | Events only for published agents (a card pending review produces none until approved) |
| Canonical TMF639 OAS URL | Release-independent Registry Center profile URL |
| TMF685, TMF716, TMF701 | Not exposed |

The resource mapping, visibility rules and event semantics are in the `registry-center` API Reference ("TMF639
Resource Inventory facade"). The design and open items are in
[`oda_canvas/TMF639-FACADE-PLAN.md`](../../oda_canvas/TMF639-FACADE-PLAN.md).

## Through the OpenAN operator

The operator restricts what a CR may set:

- **Pinned by the operator:** Secret references, the trust store, `registry.authMode`, `registry.identityMode` and
  `registry.signatureValidation`.
- **Image:** `image.repository` must start with a prefix in `ALLOWED_IMAGE_PREFIXES`.
- **Profile URL:** `tmf639.specificationUrl` must start with a prefix in `ALLOWED_SPEC_URL_PREFIXES`. The default
  prefix is `https://raw.githubusercontent.com/project-openan/registry-center/`.
- **Callback hosts:** every host in `broadcast.callbackAllowlist` must be in `ALLOWED_CALLBACK_HOSTS`, which is empty
  by default.
- **Callback CA:** `broadcast.callbackCaConfigMap` must be in `ALLOWED_CALLBACK_CA_CONFIGMAPS`, which is empty by
  default.
- **Not settable from a CR:** `broadcast.allowHttpCallbacks` and `catalog.specificationUrl`.

See `operator/openan-operator/README.md`. The kind demo (`oda_canvas/`) exposes these settings as `make` variables.
