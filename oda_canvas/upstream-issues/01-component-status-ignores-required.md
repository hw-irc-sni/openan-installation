<!--
Draft for https://github.com/tmforum-oda/oda-canvas/issues/new?template=fix.md (label: bug-fix).
Posted as https://github.com/tmforum-oda/oda-canvas/issues/663. Line references are permalinks to upstream main 6218a78.
-->

# componentOperator: optional dependent APIs (`required: false`) block the Component from reaching `Complete`

## Description

A Component can mark a dependent API as optional with `required: false`. The Component CRD
documents the field ("Required is a flag that indicates whether the API is required or not"),
and the Component Operator copies it onto the generated DependentAPI resource
([componentOperator.py L1542-L1543](https://github.com/tmforum-oda/oda-canvas/blob/6218a78420c731a36456b62b0f6de5c07d4bafd4/source/operators/TMFOP001-Component-Management/component-management/componentOperator.py#L1542-L1543)).

However, the flag is never used when the Component's deployment status is computed. The status
summary counts **every** declared DependentAPI as desired, and moves the Component from
`In-Progress-DepApi` to `Complete` only when all of them are ready
([componentOperator.py L2238-L2345](https://github.com/tmforum-oda/oda-canvas/blob/6218a78420c731a36456b62b0f6de5c07d4bafd4/source/operators/TMFOP001-Component-Management/component-management/componentOperator.py#L2238-L2345)):

```python
countOfDesiredDependentAPIs = countOfDesiredDependentAPIs + len(status["coreDependentAPIs"])
...
status_summary["deployment_status"] = "In-Progress-DepApi"
if countOfCompleteDependentAPIs == countOfDesiredDependentAPIs:
    status_summary["deployment_status"] = "Complete"
```

Neither dependency-management operator acts on the flag either:
- `dependentapi-simple-operator` never reads it.
- `dependency-management-keycloak-authz` only logs it
  ([L709](https://github.com/tmforum-oda/oda-canvas/blob/6218a78420c731a36456b62b0f6de5c07d4bafd4/source/operators/TMFOP005-Dependency-Management/dependency-management-using-keycloak-authorization/docker/src/dependencyManagementKeycloakAuthzOperator.py#L709)).

As a result, an optional dependency that has no provider in the Canvas keeps the Component in
`In-Progress-DepApi` **indefinitely**. Optional and required dependencies behave the same.

### Why it matters

- Many TM Forum component specifications list optional dependent APIs. For example, TMFC012
  Resource Inventory has TMF669, TMF632, TMF673/674/675 as optional. A conformant component that
  declares them can only reach `Complete` if *every* optional provider is also installed.
- Component authors work around this by leaving optional dependencies out of the Component
  envelope. The envelope then no longer describes the component's real contract, and the
  operator has nothing to resolve once a provider does appear later.
- Tooling and CI that wait for `deployment_status == Complete` (smoke tests, BDD setup) hang or
  fail.

### Steps to reproduce

1. Install the Canvas with defaults.
2. Deploy any component whose `coreFunction.dependentAPIs` contains an entry with
   `required: false` and a `specification.url` that no ExposedAPI in the cluster declares.
3. `kubectl get components -n components -o jsonpath='{.items[*].status.summary/status.deployment_status}'`

**Expected:** `Complete` once all exposed APIs, identity config, secrets management and the
**required** dependent APIs are ready. The optional DependentAPI is reported as not ready, but
doesn't block.

**Actual:** `In-Progress-DepApi`, forever.

## Proposed fix

In the status summary, count a DependentAPI towards `countOfDesiredDependentAPIs` only when it is
required. Optional ones should still appear in the `*DependentAPIsummary` strings and in the
per-API status.

- The flag is optional in the CRD. Default it to `true` when absent, which preserves today's
  behaviour for components that don't set it.
- The child status entries (`coreDependentAPIs`, `managementDependentAPIs`,
  `securityDependentAPIs`) need to carry `required`, so the summary can read it without
  re-fetching each DependentAPI. Today they carry `name`, `uid`, `url`, `ready`.
- Optional, nice to have: a separate count (e.g. `optionalDependentAPIsPending`) in
  `summary/status`, so operators can see that optional dependencies are unresolved without the
  Component looking stuck.
- BDD: add a scenario to the Component Management features: "a Component with an unresolvable
  optional dependent API reaches Complete", plus the inverse for a required one.

Related: #614 extends dependent-API discovery to MCP/A2A. Those dependencies are more likely to
be optional, which makes this more visible.
