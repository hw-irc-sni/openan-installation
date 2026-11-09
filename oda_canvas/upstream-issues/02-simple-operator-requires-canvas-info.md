<!--
Draft for https://github.com/tmforum-oda/oda-canvas/issues/new?template=fix.md (label: bug-fix).
Posted as https://github.com/tmforum-oda/oda-canvas/issues/664. Line references are permalinks to upstream main 6218a78.
-->

# dependentapi-simple-operator: a resolved DependentAPI never becomes ready when canvas-info-service is unavailable

## Description

When `dependentapi-simple-operator` finds a matching ExposedAPI for a DependentAPI,
`setDependentAPIStatus` first writes the result to the canvas-info service (TMF638 Service
Inventory, via `updateServiceInventory`). Only after that succeeds does it patch the DependentAPI
with the URL and `implementation.ready: true`
([dependentApiSimpleOperator.py L326-L384](https://github.com/tmforum-oda/oda-canvas/blob/6218a78420c731a36456b62b0f6de5c07d4bafd4/source/operators/TMFOP005-Dependency-Management/simple-dependency-management/docker/src/dependentApiSimpleOperator.py#L326-L384)):

```python
depapi["status"]["depapiStatus"]["url"] = url
depapi["status"]["implementation"] = {"ready": True}
try:
    ...
    svc_id = updateServiceInventory(logw, component_name, da_name, specification, url)
    depapi["status"]["depapiStatus"]["svcInvID"] = svc_id
except Exception as e:
    raise kopf.TemporaryError(
        f"setDependentAPIStatus: Exception in updateServiceInventory: {str(e)}")
# patch_namespaced_custom_object(...) is only reached if the above succeeded
```

If canvas-info can't be reached, the `TemporaryError` is raised **before** the patch. kopf
retries, but the DependentAPI never gets its URL or ready flag. That is true even though
discovery succeeded and the URL is already known. The Component then stays in
`In-Progress-DepApi`.

canvas-info is a soft dependency in the chart (`canvas-info-service.enabled` can be set to
`false` in `charts/canvas-oda/values.yaml`). In the operator, though, it is a hard runtime
dependency for every resolved dependency. Other ways it can be unavailable:
- It is deployed but unhealthy. #597 reports a MongoDB connection failure when it is deployed
  as a subchart.
- It is still starting up during a fresh install.
- `CANVAS_INFO_ENDPOINT` points at the wrong address.

### Steps to reproduce

1. Install the Canvas with `canvas-info-service.enabled: false` (other values default).
2. Deploy two components, where B declares a dependent API whose `specification.url` equals an
   exposed API of A.
3. Look at B's DependentAPI and the operator log.

**Expected:** B's DependentAPI gets `depapiStatus.url` and `implementation.ready: true`. The
service-inventory registration failure is logged as a warning, and B reaches `Complete`.

**Actual:** the operator logs `setDependentAPIStatus: Exception in updateServiceInventory: ...`
and retries forever. The DependentAPI stays not ready, and B stays in `In-Progress-DepApi`.

## Proposed fix

Make the Service Inventory registration best-effort and decouple it from DependentAPI readiness.
The DependentAPI CR status is the source of truth, and canvas-info is a convenience projection of
it for components that look up their dependencies through TMF638.

1. Patch the DependentAPI (`url`, `implementation.ready`) first, independently of canvas-info.
2. Then call `updateServiceInventory`. On failure, log a warning and record it in the status
   (e.g. `depapiStatus.svcInvSync: failed` with a message). Retry it, but without unsetting
   readiness, e.g. with a kopf timer or a dedicated handler keyed on a missing `svcInvID`.
3. Optional: an operator setting (e.g. `CANVAS_INFO_ENDPOINT=""` or
   `serviceInventory.enabled: false`) that skips registration entirely when the chart disables
   canvas-info. That avoids pointless retries and log noise. The chart could set it
   automatically from `canvas-info-service.enabled`.
4. Apply the same ordering in `dependentApiDelete`. Removing the Service Inventory entry
   (`removeServiceInventory`) should not block deletion when canvas-info is down; it already
   uses `ignore_not_found=True`, but connection errors still propagate.
5. BDD: "a DependentAPI resolves and becomes ready when canvas-info-service is disabled", and
   "the Service Inventory entry is created once canvas-info becomes available".

Components that rely on canvas-info to look up dependency URLs (e.g. the reference components'
`downstreamAPI.js`) are not affected: whenever canvas-info is available, the entry is still
written.
