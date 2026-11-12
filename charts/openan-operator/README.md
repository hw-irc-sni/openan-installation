# openan-operator

Helm chart that deploys the OpenAN operator onto a running ODA Canvas.
Scaffolded from the ODA Canvas operator charts (`component-operator`, `pdb-management-operator`).

> The image, version, entrypoint and RBAC rules in `values.yaml` / `templates/rbac.yaml` are placeholders
> (marked `TODO`). Set them to match the real operator before installing.

## Install

From a local checkout:

```bash
helm upgrade --install openan-operator ./charts/openan-operator -n canvas
```

From the GitHub repository (clone, then install from the folder):

```bash
git clone <openan-installation repo URL> && cd openan-installation
helm upgrade --install openan-operator ./charts/openan-operator -n canvas
```

To install straight from the online repository, publish the chart as a Helm repo (for example with
`helm package` + GitHub Pages, as the ODA Canvas charts do) and then:

```bash
helm repo add openan <repo URL>
helm install openan-operator openan/openan-operator -n canvas
```

## Key values

| Value | Default | Description |
|---|---|---|
| `deployment.image` / `version` / `prereleaseSuffix` | placeholder | Operator image. A suffix forces `imagePullPolicy: Always` |
| `deployment.monitoredNamespaces` | `components,odacompns-*` | Namespaces watched (kopf `-n` flags) |
| `deployment.command` / `args` | kopf run | Container entrypoint |
| `tmf.operatorId` / `operatorName` | empty | Adds `oda.tmforum.org/operatorId` / `operatorName` labels (badge in canvas-portal-v2) |
| `configmap.extra` | `{}` | Extra environment variables, e.g. the CR policy lists `ALLOWED_IMAGE_PREFIXES`, `ALLOWED_SPEC_URL_PREFIXES`, `ALLOWED_CALLBACK_HOSTS` and `ALLOWED_CALLBACK_CA_CONFIGMAPS`, and the agent sync settings `AGENT_PROXY_IMAGE`, `AGENT_UPSTREAM_ALLOWED_HOSTS`, `AGENT_SYNC_RESYNC_SECONDS` (see `operator/openan-operator/README.md`) |
| `global.image.registry` | empty | Private registry prefix (with trailing `/`) |
| `global.imagePullSecrets` | `[]` | Image pull secrets |

The release namespace is always `{{ .Release.Namespace }}`; nothing is hardcoded.

## Showing up in canvas-portal-v2 (Operators page)

The portal lists Deployments whose container or Deployment name matches an entry in `OPERATOR_SIGNATURES`
(`canvas-portal-v2/backend/k8s/resources/operators.py` in the ODA Canvas repo). Add:

```python
"openan-operator": ("openan-operator",),
```

and rebuild the portal backend image. The container name in this chart equals `deployment.operatorName`, so it matches.
