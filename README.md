# Jetstream DevOps Assignment

Production-grade, zero single-point-of-failure stack: FastAPI + MongoDB replica set, deployed via Terraform-automated Kubernetes, packaged with Helm, delivered through ArgoCD GitOps, exposed via Ingress, observed end-to-end with OpenTelemetry (traces, metrics, logs).

## Prerequisites

| Tool | Used for | Version verified against |
|---|---|---|
| [Docker](https://docs.docker.com/engine/install/) | Runs the local Kubernetes nodes, builds/runs the app image | 29.x |
| [Terraform](https://developer.hashicorp.com/terraform/install) | Provisions the cluster | 1.15.x |
| [kind](https://kind.sigs.k8s.io/) | Local multi-node Kubernetes (control-plane + 3 workers) | 5.9.x — invoked by Terraform, no manual `kind` commands needed |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Cluster interaction | 1.36.x |
| [Helm](https://helm.sh/docs/intro/install/) | Chart linting/templating (ArgoCD renders charts itself at runtime) | 4.x |
| A Docker Hub (or other registry) account | Hosting the app image | — |

No cloud account is required — this uses **Option B (local cluster)**, `kind`, provisioned entirely through Terraform.

## 1. Architecture & Component Interaction

```
                     [ User / Browser ]
                             │
                             ▼ (http://localhost:8080 — Ingress)
                  [ ingress-nginx-controller ]
                    (hostPort 80, pinned to
                     the control-plane node)
                             │
              [ articles-api Service (ClusterIP) ]
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                     ▼
  [ Pod: worker  ]     [ Pod: worker2 ]      [ Pod: worker3 ]     ◄── 3 replicas,
  articles-api          articles-api          articles-api            hard anti-affinity:
  (FastAPI)             (FastAPI)             (FastAPI)               1 pod per node
        │                    │                     │
        ├────────────────────┼─────────────────────┤
        ▼                                           ▼
[ mongodb-0/1/2 StatefulSet ]           [ OpenTelemetry SDK, in-process ]
  3-member replica set (rs0)                  traces + metrics + logs
  hard anti-affinity + PDB                              │
  headless svc: mongodb-headless                        ▼ (OTLP/gRPC :4317)
        │                                    [ otel-collector Deployment ]
        ▼                                     (ConfigMap-driven pipeline)
[ PersistentVolumeClaims ]                              │
  (local-path StorageClass,                             ├──► traces ──► [ Jaeger ]
   1 per replica)                                        └──► metrics/logs ──► debug exporter

[ ArgoCD Application ] ──watches── [ GitHub: master branch, charts/backend + charts/mongodb ]
     multi-source Helm rendering, automated sync + selfHeal + prune → cluster always mirrors git
```

### Data flow

1. **API traffic**: External requests hit `http://localhost:8080`, which `kind`'s Terraform-configured port mapping forwards to the control-plane node's port 80, where the `ingress-nginx-controller` pod is pinned (`hostPort: 80`). The Ingress resource routes `/` to the `articles-api` ClusterIP Service, load-balanced across 3 FastAPI pods on 3 different worker nodes. Interactive OpenAPI docs at `/docs` (Swagger, built into FastAPI — no extra setup needed).
2. **Database**: FastAPI uses `motor` (async Mongo driver) against a 3-member MongoDB replica set (`rs0`) via stable per-pod DNS (`mongodb-N.mongodb-headless`). Reads/writes go through the primary; the replica set survives any single mongod pod loss.
3. **Observability**: `instrumentation.py` wires `TracerProvider`, `MeterProvider`, and `LoggerProvider` into the FastAPI app at startup, auto-instrumenting HTTP (FastAPI) and DB calls (PyMongo/motor). All three signals ship over OTLP/gRPC to a central `otel-collector`, which fans traces out to Jaeger (metrics/logs currently on a debug exporter — swappable for Prometheus/Loki without touching app code, since the Collector decouples the app from the backend).
4. **GitOps**: An ArgoCD `Application` continuously tracks this repo's `master` branch via **two Helm sources** (`charts/backend`, `charts/mongodb`) — ArgoCD renders the charts internally (no separate `helm install` needed). Any new commit is auto-detected and auto-synced; manual `kubectl edit`/`kubectl delete` drift is auto-reverted (`selfHeal: true`) — proven live twice: once via a label edit, once by deleting the entire Deployment/StatefulSet/Services and watching ArgoCD recreate them within seconds.

## 2. Repository Layout

| Path | Contents |
|---|---|
| `app/` | FastAPI service — `main.py` (CRUD + probes), `models.py` (Pydantic schemas), `instrumentation.py` (OTel setup), `Dockerfile` |
| `infra/` | Terraform: automates the local Kubernetes cluster |
| `k8s/` | Plain Kubernetes manifests (Deployment, PDB, Service, Ingress, StatefulSet, Jobs) — a working non-Helm deploy path, kept in sync with the charts |
| `charts/backend/`, `charts/mongodb/` | Helm charts templatizing the above — **what ArgoCD actually deploys** (multi-source `Application`) |
| `observability/` | Jaeger + OTel Collector manifests (ConfigMap-driven pipeline) |
| `gitops/` | ArgoCD `Application` definition |

## 3. Application

FastAPI CRUD API for `articles`: `POST/GET/PUT/DELETE /articles`, `GET /articles/{id}`. Pydantic models carry example values so `/docs` renders a fully-populated Swagger UI out of the box.

**Probes**: `/healthz` (liveness — process alive) and `/ready` (readiness — active `db.command("ping")` against Mongo). See [Design Decisions](#5-design-decisions) for why these are split.

## 4. Infrastructure & Deployment

- **Containerization**: Multi-stage `Dockerfile` (`python:3.11-slim` builder → slim runner), non-root `appuser:999`. MongoDB runs from the official `mongo:7` image.
- **IaC**: Terraform automates a local multi-node Kubernetes cluster (control-plane + 3 workers), including the `ingress-ready=true` label on the control-plane node and the `8080→80` host port mapping used by Ingress. See [Design Decisions](#5-design-decisions) for why this uses `kind` instead of the originally-planned K3d.
- **HA resources**:
  - `articles-api` Deployment — 3 replicas, hard pod anti-affinity, PDB `minAvailable: 2`, tuned rolling-update strategy.
  - `mongodb` StatefulSet — 3 members, hard pod anti-affinity, PDB `minAvailable: 2`, per-pod PVCs, headless service for stable DNS, plus a `PostSync` ArgoCD hook Job that initiates the replica set.
- **Ingress**: `ingress-nginx`, pinned to the control-plane node via `nodeSelector: ingress-ready=true` (see [Design Decisions](#5-design-decisions) — this isn't automatic), routes `/` to `articles-api:8000`.
- **Helm**: Both the backend and MongoDB manifests are templatized (`charts/backend`, `charts/mongodb`), externalizing replica counts, image refs, Mongo URI, OTel endpoint, and PDB/ingress settings into `values.yaml`.
- **GitOps**: `gitops/argo-application.yaml` — a multi-source ArgoCD `Application` watching `charts/backend` and `charts/mongodb` on `master`, auto-syncs, auto-heals, auto-prunes.
- **Observability**: OpenTelemetry Collector (`observability/otel-collector.yaml`, config in a ConfigMap) receiving OTLP from the app and exporting traces to Jaeger (`observability/jaeger.yaml`).

## 5. Design Decisions

1. **StatefulSet vs. Deployment for the database.** MongoDB needs predictable, stable identities and dedicated persistent volumes per member. A StatefulSet (not a Deployment) avoids split-brain scenarios and data loss during scaling — each `mongodb-N` pod keeps its own PVC across restarts and reschedules.

2. **Hard pod anti-affinity for both tiers.** `requiredDuringSchedulingIgnoredDuringExecution` on `kubernetes.io/hostname` physically forces the 3 API replicas *and* the 3 MongoDB members onto 3 different worker nodes each. If one node dies, the other replicas of each tier absorb the load immediately — verified live by checking pod-to-node placement after every deploy.

3. **Liveness/readiness probe separation.** `/healthz` always returns 200 if the process is up; `/ready` actually pings Mongo. If Mongo goes down, only readiness fails — traffic stops routing to that pod, but Kubernetes doesn't restart-loop a perfectly healthy process. Confirmed live: pods stayed `Running` with `0/1 Ready` (zero restarts) while MongoDB wasn't yet deployed.

4. **OpenTelemetry Collector as a decoupling layer.** The app only ever talks OTLP to `otel-collector:4317`. Swapping Jaeger for Tempo, or wiring up Prometheus/Loki for metrics and logs, is a Collector config change — zero application code changes.

5. **Rolling update strategy tuned for a fixed-size node pool.** With hard anti-affinity on exactly 3 worker nodes, the *default* rolling update strategy (`maxSurge: 25%`) tries to schedule a 4th pod during a rollout and deadlocks (`FailedScheduling: didn't match pod anti-affinity rules`) — hit and fixed live. `maxSurge: 0, maxUnavailable: 1` terminates one old pod before starting its replacement, keeping the update possible on a constrained node pool while still respecting the PDB's `minAvailable: 2`. StatefulSets don't have this problem — they replace pods strictly one at a time by default.

6. **`kind` over K3d for Terraform-automated clusters.** The only available community Terraform provider for K3d (`moio/k3d`) vendors a stale `k3d` library that crash-loops the server node against a modern Docker/kernel combination. `kind` (Kubernetes SIG project) has an actively-maintained Terraform provider (`tehcyx/kind`) and was substituted — same "1 control-plane + N workers" topology, fully declarative, verified live via `kubectl get nodes` and `k9s`.

7. **ArgoCD deploys via Helm sources, not raw manifests — with two gotchas fixed live.** The `Application` uses `sources: [charts/backend, charts/mongodb]` so ArgoCD renders the charts internally. Switching from raw-manifest tracking to Helm-source tracking surfaced two real issues: (a) a `batch/v1` Job's `spec.template` is **immutable** — ArgoCD's normal apply-based sync fails and retries forever against it, fixed by marking the replica-set-init Job an ArgoCD `PostSync` hook (`hook-delete-policy: BeforeHookCreation`), which deletes-and-recreates instead of patching; (b) chart templates had silently drifted from manifest state that live testing had added directly (a label), which `selfHeal` would have reverted — caught by diffing rendered chart output against live cluster state before cutting over, not after.

8. **`ingress-nginx`'s node placement is not automatic on a multi-node kind cluster.** The upstream kind-flavored install manifest tolerates the control-plane's taint but sets **no `nodeSelector`** — on a single-node kind cluster it lands on the only node available (the control-plane) by coincidence; on our 4-node cluster it scheduled onto an arbitrary worker, silently breaking the host-port mapping (which is bound only to the control-plane container). Fixed by patching `nodeSelector: {ingress-ready: "true"}`, targeting the exact label Terraform already applies to the control-plane node for this purpose.

9. **A full StatefulSet delete (not a rolling restart) can genuinely deadlock a MongoDB replica set.** Tested live by deleting the Deployment/StatefulSet/Services outright (to verify ArgoCD `selfHeal` recreates them — it did, in ~5 seconds). But because all 3 MongoDB pods restarted simultaneously, each one's boot-time self-identification check (`Locally stored replica set configuration does not have a valid entry for the current node`) failed before any peer was reachable to vouch for it, and all 3 landed in `REMOVED` state — a real deadlock requiring a manual `rs.reconfig(..., {force: true})` to recover. Lesson: normal rolling operations (`kubectl rollout restart`, single-pod deletes) never hit this, because StatefulSets always replace pods one at a time, keeping a quorum-eligible peer alive throughout. Full-resource deletion is a good chaos test, but isn't representative of any real operational path.

## 6. Cluster Setup (Terraform)

```bash
cd infra
terraform init
terraform apply
```

This provisions a 4-node `kind` cluster (1 control-plane + 3 workers) with the `ingress-ready=true` label and the `8080→80` host port mapping already wired in (`infra/main.tf`, `infra/variables.tf`). `terraform apply` also switches your default `kubectl` context to the new cluster.

## 7. Application Deployment

### Via ArgoCD (GitOps — recommended, mirrors what's actually running)

```bash
# Install ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl wait --for=condition=Ready pods --all -n argocd --timeout=180s

# Point it at this repo (multi-source Helm: charts/backend + charts/mongodb)
kubectl apply -f gitops/argo-application.yaml
```

ArgoCD takes it from there — clones the repo, renders both Helm charts, applies everything, and keeps syncing on every new commit to `master`.

### Via plain kubectl (no ArgoCD/Helm)

```bash
kubectl apply -f k8s/
```

### Via Helm directly (no ArgoCD)

```bash
helm install articles-api charts/backend
helm install mongodb charts/mongodb
```

### Ingress controller (required for the steps below)

`kind` doesn't ship an Ingress controller — install `ingress-nginx` and pin it to the control-plane node (**this pinning step is required on a multi-node kind cluster** — see [Design Decisions](#5-design-decisions) #8):

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
kubectl wait --namespace ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=180s

# Required: pin the controller to the labeled control-plane node so hostPort 80
# actually lines up with the 8080→80 mapping Terraform configured.
kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=json \
  -p '[{"op": "add", "path": "/spec/template/spec/nodeSelector/ingress-ready", "value": "true"}]'
```

### Observability stack

```bash
kubectl apply -f observability/
```

## 8. Accessing the API

Once the Ingress controller is up, the API is reachable directly at **`http://localhost:8080`** — no port-forwarding needed.

```bash
# Health/readiness
curl http://localhost:8080/healthz
curl http://localhost:8080/ready

# Create
curl -X POST http://localhost:8080/articles \
  -H "Content-Type: application/json" \
  -d '{"title":"My Article","content":"Hello world","author":"You"}'

# List
curl http://localhost:8080/articles

# Read one
curl http://localhost:8080/articles/<id>

# Update
curl -X PUT http://localhost:8080/articles/<id> \
  -H "Content-Type: application/json" \
  -d '{"title":"Updated title"}'

# Delete
curl -X DELETE http://localhost:8080/articles/<id>
```

Swagger UI: **http://localhost:8080/docs**

Other UIs, via port-forward (internal-only tools, intentionally not exposed through the Ingress):

```bash
kubectl port-forward svc/jaeger 16687:16686                  # Jaeger:  http://localhost:16687
kubectl port-forward svc/argocd-server -n argocd 8443:443    # ArgoCD:  https://localhost:8443
```
