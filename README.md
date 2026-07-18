# Jetstream DevOps Assignment

Production-grade, zero single-point-of-failure stack: FastAPI + MongoDB replica set, deployed via Terraform-automated Kubernetes, packaged with Helm, delivered through ArgoCD GitOps, observed end-to-end with OpenTelemetry (traces, metrics, logs).

## 1. Architecture & Component Interaction

```
                     [ User / Browser ]
                             │
                             ▼ (kubectl port-forward / Ingress)
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
  headless svc: mongodb-headless                       │
        │                                              ▼ (OTLP/gRPC :4317)
        ▼                                    [ otel-collector Deployment ]
[ PersistentVolumeClaims ]                    (ConfigMap-driven pipeline)
  (local-path StorageClass,                             │
   1 per replica)                                       ├──► traces ──► [ Jaeger ]
                                                          └──► metrics/logs ──► debug exporter

[ ArgoCD Application ] ──watches── [ GitHub: master branch, path k8s/ ]
     automated sync + selfHeal + prune → cluster always mirrors git
```

### Data flow

1. **API traffic**: Client hits the `articles-api` ClusterIP Service, load-balanced across 3 FastAPI pods, each on a different worker node. Interactive OpenAPI docs at `/docs` (Swagger, built into FastAPI — no extra setup needed).
2. **Database**: FastAPI uses `motor` (async Mongo driver) against a 3-member MongoDB replica set (`rs0`) via stable per-pod DNS (`mongodb-N.mongodb-headless`). Reads/writes go through the primary; the replica set survives any single mongod pod loss.
3. **Observability**: `instrumentation.py` wires `TracerProvider`, `MeterProvider`, and `LoggerProvider` into the FastAPI app at startup, auto-instrumenting HTTP (FastAPI) and DB calls (PyMongo/motor). All three signals ship over OTLP/gRPC to a central `otel-collector`, which fans traces out to Jaeger (metrics/logs currently on a debug exporter — swappable for Prometheus/Loki without touching app code, since the Collector decouples the app from the backend).
4. **GitOps**: An ArgoCD `Application` continuously tracks this repo's `master` branch (`k8s/` path). Any new commit is auto-detected and auto-synced to the cluster; manual `kubectl edit` drift is auto-reverted (`selfHeal: true`) — proven live by pushing a commit and watching the change land in the cluster with zero manual `kubectl apply`.

## 2. Repository Layout

| Path | Contents |
|---|---|
| `app/` | FastAPI service — `main.py` (CRUD + probes), `models.py` (Pydantic schemas), `instrumentation.py` (OTel setup), `Dockerfile` |
| `infra/` | Terraform: automates the local Kubernetes cluster |
| `k8s/` | Plain Kubernetes manifests (Deployment, PDB, Service, StatefulSet, Jobs) — what ArgoCD tracks |
| `charts/backend/`, `charts/mongodb/` | Helm charts templatizing the above, for parameterized/repeatable deploys |
| `observability/` | Jaeger + OTel Collector manifests (ConfigMap-driven pipeline) |
| `gitops/` | ArgoCD `Application` definition |

## 3. Application

FastAPI CRUD API for `articles`: `POST/GET/PUT/DELETE /articles`, `GET /articles/{id}`. Pydantic models carry example values so `/docs` renders a fully-populated Swagger UI out of the box.

**Probes**: `/healthz` (liveness — process alive) and `/ready` (readiness — active `db.command("ping")` against Mongo). See [Design Decisions](#5-design-decisions) for why these are split.

## 4. Infrastructure & Deployment

- **Containerization**: Multi-stage `Dockerfile` (`python:3.11-slim` builder → slim runner), non-root `appuser:999`.
- **IaC**: Terraform automates a local multi-node Kubernetes cluster (control-plane + 3 workers). See [Design Decisions](#5-design-decisions) for why this uses `kind` instead of the originally-planned K3d.
- **HA resources**: `articles-api` Deployment (3 replicas, hard pod anti-affinity, PDB `minAvailable: 2`); `mongodb` StatefulSet (3 members, per-pod PVCs, headless service for stable DNS) plus a one-shot Job that initiates the replica set.
- **Helm**: Both the backend and MongoDB manifests are templatized (`charts/backend`, `charts/mongodb`), externalizing replica counts, image refs, Mongo URI, and OTel endpoint into `values.yaml`.
- **GitOps**: `gitops/argo-application.yaml` — ArgoCD watches this repo's `master`/`k8s`, auto-syncs, auto-heals, auto-prunes.
- **Observability**: OpenTelemetry Collector (`observability/otel-collector.yaml`, config in a ConfigMap) receiving OTLP from the app and exporting traces to Jaeger (`observability/jaeger.yaml`).

## 5. Design Decisions

1. **StatefulSet vs. Deployment for the database.** MongoDB needs predictable, stable identities and dedicated persistent volumes per member. A StatefulSet (not a Deployment) avoids split-brain scenarios and data loss during scaling — each `mongodb-N` pod keeps its own PVC across restarts and reschedules.

2. **Hard pod anti-affinity for the API.** `requiredDuringSchedulingIgnoredDuringExecution` on `kubernetes.io/hostname` physically forces the 3 API replicas onto 3 different worker nodes. If one node dies, the other two replicas absorb traffic immediately — verified live by checking pod-to-node placement after every deploy.

3. **Liveness/readiness probe separation.** `/healthz` always returns 200 if the process is up; `/ready` actually pings Mongo. If Mongo goes down, only readiness fails — traffic stops routing to that pod, but Kubernetes doesn't restart-loop a perfectly healthy process. Confirmed live: pods stayed `Running` with `0/1 Ready` (zero restarts) while MongoDB wasn't yet deployed.

4. **OpenTelemetry Collector as a decoupling layer.** The app only ever talks OTLP to `otel-collector:4317`. Swapping Jaeger for Tempo, or wiring up Prometheus/Loki for metrics and logs, is a Collector config change — zero application code changes.

5. **Rolling update strategy tuned for a fixed-size node pool.** With hard anti-affinity on exactly 3 worker nodes, the *default* rolling update strategy (`maxSurge: 25%`) tries to schedule a 4th pod during a rollout and deadlocks (`FailedScheduling: didn't match pod anti-affinity rules`) — hit and fixed live. `maxSurge: 0, maxUnavailable: 1` terminates one old pod before starting its replacement, keeping the update possible on a constrained node pool while still respecting the PDB's `minAvailable: 2`.

6. **`kind` over K3d for Terraform-automated clusters.** The original plan targeted K3d, but the only available community Terraform provider for it (`moio/k3d`) vendors a stale `k3d` library that crash-loops the server node against a modern Docker/kernel combination. `kind` (Kubernetes SIG project) has an actively-maintained Terraform provider (`tehcyx/kind`) and was substituted — same "1 control-plane + N workers" topology, fully declarative, verified live via `kubectl get nodes` and `k9s`.

## 6. Running Locally

```bash
# 1. Provision the cluster
cd infra && terraform init && terraform apply

# 2. Deploy the app + database
kubectl apply -f ../k8s/

# 3. Deploy observability
kubectl apply -f ../observability/

# 4. (Optional) install ArgoCD and apply the Application for GitOps-driven sync
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl apply -f ../gitops/argo-application.yaml

# 5. Access services
kubectl port-forward svc/articles-api 8090:8000   # Swagger: http://localhost:8090/docs
kubectl port-forward svc/jaeger 16687:16686        # Jaeger:  http://localhost:16687
kubectl port-forward svc/argocd-server -n argocd 8443:443   # ArgoCD: https://localhost:8443
```

Alternatively, deploy via Helm instead of step 2:

```bash
helm install articles-api charts/backend
helm install mongodb charts/mongodb
```
