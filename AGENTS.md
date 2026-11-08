# AGENTS.md

This file provides guidance to coding agent when working with code in this repository.

## What this repo is

Installation tooling (no application source) for OpenAN: Registry Center, Orchestration Center (OC) and a web frontend (openan-portal, or the workflow-designer served by OC's image). Service images are built from separate repos (`project-openan/registry-center`, `project-openan/orchestration-center`) and the portal repo; this repo only builds/packages/deploys them.

## Deployment lines (each is largely self-contained)

- `containerized/online-install/` — maintained Helm chart (`openan-chart/`) + `install.sh`/`configure.py` convenience installer. The chart is the source of truth; the installer never bypasses it. Cluster/Helm/Ingress infra is managed independently.
- `containerized/offline-install/` — offline Kubernetes line. Has its **own copy** of chart templates (`chart/`), plus `scripts/` (install, push-images, check-env); its cert generator is the shared `tool/gen_certs_offline.py` at repo root (online line: `tool/gen_certs.py`). Includes MySQL and PostgreSQL statefulsets, frontend nginx with mTLS to backends (BFF proxy), PV/PVC per storage class.
- `containerized/offline-install-compose/` — offline single-host Docker Compose (`scripts/install.sh`, `scripts/configure.py`, `config.env.example`, `templates/`). One instance per app, not HA.
- `containerized/build/` — shared image builder (`build.sh [registry|orchestration|portal]`, `portal/` for the portal image + nginx `default.conf.template`).
- `binary/` — historical VM/bare-metal installers; **not validated against the current auth/config contract**. Don't treat as supported.
- `docs/en/*.en.md` + `docs/zh/*.zh.md` — bilingual installation guides and design docs per line; `docs/en/TRANSPORT_DEPLOYMENT.md` (+ `docs/zh/TRANSPORT_DEPLOYMENT.zh.md`) is the HTTP/HTTPS, auth, and database **deployment contract** — read before changing chart values, auth, or TLS behavior. Keep the zh version in sync.
- `oda_canvas/` — currently empty (branch `oda-integration`).

Because online and offline-k8s charts are duplicated, a fix in one template often needs mirroring in the other (check both `online-install/openan-chart/templates/` and `offline-install/chart/templates/`).

## Commands

```bash
pip install pytest pyyaml cryptography     # CI deps; tests also need helm v3.19 on PATH
python -m pytest tests/ -q                 # full contract suite (what CI runs)
python -m pytest tests/test_offline_chart_contract.py -q            # one file
python -m pytest tests/test_deployment_contract.py -q -k <name>     # one test

# Shell syntax checks CI runs
bash -n containerized/online-install/install.sh
bash -n containerized/build/build.sh
bash -n containerized/build/portal/build-portal-image.sh
find containerized/offline-install-compose -name '*.sh' -exec bash -n {} \;

# Install (Helm)
cd containerized/online-install
bash install.sh --skip-build --skip-infra --values /path/to/deployment.yaml
helm upgrade --install openan ./openan-chart -n openan --create-namespace -f deployment.yaml
```

Tests are "contract" tests: they render charts with `helm template` and assert on manifests/docs/scripts (including mermaid diagrams in docs via `test_docs_mermaid_contract.py`). Changing templates, values, scripts or docs will usually require updating the matching test. `tests/offline_compose_container_smoke.py` is a real Docker/Compose smoke test run only in the CI `offline-compose-container` job (needs sudo + a built bundle).

## Key contract points

- Image tags have no usable chart default: enabled `registry/orchestration/frontend.image.tag` must be set explicitly. Tags and source revisions are independent.
- CI pins accepted org source revisions of registry-center and orchestration-center in `.github/workflows/deployment-contract.yml`; update those pins when adopting newer service revisions (not personal forks).
- OC runs single-replica until shared sessions exist. `namespace` value must equal Helm `--namespace`; resource names are fixed → one stack per namespace.
- Auth: OC uses `ORCH_EXTERNAL_AUTH_MODE=user` (admin seeded from `OC_ADMIN_INITIAL_PASSWORD` Secret only when the users table is empty); auth is independent of TLS. Secrets, certs (`*.pem`, `*.key`), `.env`, `*.local.yaml` and compose `config.env`/`pki/`/`runtime/` are gitignored — never commit them.
- Models config is a complete `models.yaml` (ConfigMap/mount); the `llm` shortcut covers OpenAI-compatible profiles only.
