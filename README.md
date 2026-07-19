# Jetstream DevOps Assignment

Production-grade, zero single-point-of-failure stack: FastAPI + MongoDB replica set, deployed on a Terraform-provisioned Kubernetes cluster, packaged with Helm, delivered through ArgoCD GitOps, exposed via Ingress, autoscaled with an HPA, observed end-to-end with OpenTelemetry (traces, metrics, logs).

## Architecture Overview

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
  [ Pod: worker  ]     [ Pod: worker2 ]      [ Pod: worker3 ]     ◄── HPA: 3-6 replicas,
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

[ ArgoCD: jetstream-devops-assignment ] ──watches── [ GitHub master: charts/backend + charts/mongodb ]
[ ArgoCD: ingress-nginx ]                ──watches── [ official ingress-nginx Helm chart ]
     both: automated sync + selfHeal + prune → cluster always mirrors declared state
```

### Components

| Component | What it is | Where |
|---|---|---|
| `articles-api` | FastAPI CRUD service (`POST/GET/PUT/DELETE /articles`) | `app/` |
| `mongodb` | 3-member MongoDB replica set (StatefulSet) | `charts/mongodb`, `k8s/mongodb-*.yaml` |
| `ingress-nginx` | Ingress controller, only public entry point | ArgoCD-managed, official upstream chart |
| `otel-collector` + `jaeger` | Observability pipeline | `observability/` |
| ArgoCD (2 Applications) | GitOps — reconciles cluster state to this repo | `gitops/` |
| HPA (`articles-api-hpa`) | CPU-based autoscaler for the API tier | `charts/backend/templates/hpa.yaml` |

### Data flow

1. **API traffic**: External requests hit `http://localhost:8080`, which `kind`'s Terraform-configured port mapping forwards to the control-plane node's port 80, where the `ingress-nginx-controller` pod is pinned (`hostPort: 80`). The Ingress resource routes `/` to the `articles-api` ClusterIP Service, load-balanced across FastAPI pods on distinct worker nodes. Interactive OpenAPI docs at `/docs` (Swagger, built into FastAPI — no extra setup needed).
2. **Database**: FastAPI uses `motor` (async Mongo driver) against a 3-member MongoDB replica set (`rs0`) via stable per-pod DNS (`mongodb-N.mongodb-headless`). Reads/writes go through the primary; the replica set survives any single mongod pod loss.
3. **Autoscaling**: `metrics-server` feeds CPU utilization to `articles-api-hpa`, which scales the Deployment between 3 and 6 replicas on a 70% CPU target.
4. **Observability**: `instrumentation.py` wires `TracerProvider`, `MeterProvider`, and `LoggerProvider` into the FastAPI app at startup, auto-instrumenting HTTP (FastAPI) and DB calls (PyMongo/motor). All three signals ship over OTLP/gRPC to a central `otel-collector`, which fans traces out to Jaeger (metrics/logs currently on a debug exporter — swappable for Prometheus/Loki without touching app code, since the Collector decouples the app from the backend).
5. **GitOps**: Two ArgoCD `Application`s reconcile the cluster: `jetstream-devops-assignment` (multi-source Helm: `charts/backend` + `charts/mongodb`) and `ingress-nginx` (official upstream chart, values-overridden for `kind`). Any new commit is auto-detected and auto-synced; manual `kubectl edit`/`kubectl delete` drift is auto-reverted (`selfHeal: true`) — proven live: a label edit, a full Deployment/StatefulSet/Service deletion (recreated in ~5s), and normal commit-triggered redeploys.

### Repository Layout

| Path | Contents |
|---|---|
| `app/` | FastAPI service — `main.py` (CRUD + probes), `models.py` (Pydantic schemas), `instrumentation.py` (OTel setup), `Dockerfile` |
| `infra/` | Terraform: automates the local Kubernetes cluster |
| `k8s/` | Plain Kubernetes manifests — a working non-Helm deploy path, kept in sync with the charts |
| `charts/backend/`, `charts/mongodb/` | Helm charts — **what ArgoCD actually deploys** |
| `observability/` | Jaeger + OTel Collector manifests |
| `gitops/` | ArgoCD `Application` definitions (app+db, and ingress-nginx) |

## Prerequisites

| Tool | Used for | Version verified against |
|---|---|---|
| [Docker](https://docs.docker.com/engine/install/) | Runs the local Kubernetes nodes, builds/runs the app image | 29.x |
| [Terraform](https://developer.hashicorp.com/terraform/install) | Provisions the cluster | 1.15.x |
| [kind](https://kind.sigs.k8s.io/) | Local multi-node Kubernetes (control-plane + 3 workers) | 5.9.x — invoked by Terraform, no manual `kind` commands needed |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Cluster interaction | 1.36.x |
| [Helm](https://helm.sh/docs/intro/install/) | Chart linting/templating (ArgoCD renders charts itself at runtime) | 4.x |
| A Docker Hub (or other registry) account | Hosting the app image | — |

**No cloud account is required.** This uses **Option B (local cluster)** — `kind` — provisioned entirely through Terraform, matching the assignment's accepted-tools list.

## Cluster Setup

```bash
cd infra
terraform init
terraform apply
```

This provisions a 4-node `kind` cluster (1 control-plane + 3 workers) — see `infra/main.tf` (`kind_cluster` resource, dynamic `worker` node blocks) and `infra/variables.tf` (`agent_count`, `http_host_port`, etc.). Two things are pre-wired here specifically for later steps:

- the control-plane node is labeled `ingress-ready=true` (required for the Ingress controller placement fix — see [Design Decisions](#design-decisions) #8)
- a host port mapping (`8080 → 80` on the control-plane node) is what makes `http://localhost:8080` reach the cluster at all

`terraform apply` also switches your default `kubectl` context to the new cluster (`kind-jetstream-cluster`). Verify with:

```bash
kubectl get nodes
```

## Application Deployment

### 1. ArgoCD (GitOps control plane)

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl wait --for=condition=Ready pods --all -n argocd --timeout=180s
```

### 2. Application + database, via Helm charts (ArgoCD-managed)

```bash
kubectl apply -f gitops/argo-application.yaml
```

This is a **multi-source** Application — ArgoCD clones the repo, renders `charts/backend` and `charts/mongodb` internally (no separate `helm install` needed), and applies both. It also runs a `PostSync` hook Job that initiates the MongoDB replica set once all 3 members are up.

*(Alternative, non-GitOps paths: `kubectl apply -f k8s/` for plain manifests, or `helm install articles-api charts/backend && helm install mongodb charts/mongodb` for direct Helm without ArgoCD.)*

### 3. Ingress controller (also ArgoCD-managed)

`kind` doesn't ship an Ingress controller. This repo manages `ingress-nginx` as a second ArgoCD `Application`, sourced from the **official upstream Helm chart**, with values overridden to pin it to the control-plane node and use `ClusterIP` (see [Design Decisions](#design-decisions) #8, #10):

```bash
kubectl apply -f gitops/argo-application-ingress-nginx.yaml
```

### 4. Autoscaling (HPA prerequisite)

The HPA needs real CPU metrics, which `kind` doesn't provide out of the box:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

(The `--kubelet-insecure-tls` flag is needed because `kind` node kubelets use self-signed certs that `metrics-server` doesn't trust by default — a well-known local-cluster-only requirement, not needed on managed cloud Kubernetes.)

`articles-api-hpa` (bundled in the backend chart) then scales `articles-api` between 3 and 6 replicas on 70% CPU utilization:

```bash
kubectl get hpa articles-api-hpa
```

### 5. Observability stack

```bash
kubectl apply -f observability/
```

## Accessing the API

Once the Ingress controller is `Healthy` (`kubectl get application ingress-nginx -n argocd`), the API is reachable directly at **`http://localhost:8080`** — no port-forwarding needed.

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

Swagger UI (interactive, same 5 operations): **http://localhost:8080/docs**

Other UIs, via port-forward (internal-only tools, intentionally not exposed through the Ingress):

```bash
kubectl port-forward svc/jaeger 16687:16686                  # Jaeger:  http://localhost:16687
kubectl port-forward svc/argocd-server -n argocd 8443:443    # ArgoCD:  https://localhost:8443
```

## Design Decisions

1. **StatefulSet vs. Deployment for the database.** MongoDB needs predictable, stable identities and dedicated persistent volumes per member. A StatefulSet (not a Deployment) avoids split-brain scenarios and data loss during scaling — each `mongodb-N` pod keeps its own PVC across restarts and reschedules.

2. **Hard pod anti-affinity for both tiers.** `requiredDuringSchedulingIgnoredDuringExecution` on `kubernetes.io/hostname` physically forces the API replicas *and* the 3 MongoDB members onto distinct worker nodes. If one node dies, the other replicas of each tier absorb the load immediately — verified live by checking pod-to-node placement after every deploy.

3. **Liveness/readiness probe separation.** `/healthz` always returns 200 if the process is up; `/ready` actually pings Mongo. If Mongo goes down, only readiness fails — traffic stops routing to that pod, but Kubernetes doesn't restart-loop a perfectly healthy process. Confirmed live: pods stayed `Running` with `0/1 Ready` (zero restarts) while MongoDB wasn't yet deployed.

4. **OpenTelemetry Collector as a decoupling layer.** The app only ever talks OTLP to `otel-collector:4317`. Swapping Jaeger for Tempo, or wiring up Prometheus/Loki for metrics and logs, is a Collector config change — zero application code changes.

5. **Rolling update strategy tuned for a fixed-size node pool.** With hard anti-affinity on exactly 3 worker nodes, the *default* rolling update strategy (`maxSurge: 25%`) tries to schedule a 4th pod during a rollout and deadlocks (`FailedScheduling: didn't match pod anti-affinity rules`) — hit and fixed live. `maxSurge: 0, maxUnavailable: 1` terminates one old pod before starting its replacement, keeping the update possible on a constrained node pool while still respecting the PDB's `minAvailable: 2`. StatefulSets don't have this problem — they replace pods strictly one at a time by default.

6. **`kind` over K3d for Terraform-automated clusters.** The only available community Terraform provider for K3d (`moio/k3d`) vendors a stale `k3d` library that crash-loops the server node against a modern Docker/kernel combination. `kind` (Kubernetes SIG project) has an actively-maintained Terraform provider (`tehcyx/kind`) and was substituted — same "1 control-plane + N workers" topology, fully declarative, verified live via `kubectl get nodes` and `k9s`.

7. **ArgoCD deploys via Helm sources, not raw manifests — with two gotchas fixed live.** The `Application` uses `sources: [charts/backend, charts/mongodb]` so ArgoCD renders the charts internally. Switching from raw-manifest tracking to Helm-source tracking surfaced two real issues: (a) a `batch/v1` Job's `spec.template` is **immutable** — ArgoCD's normal apply-based sync fails and retries forever against it, fixed by marking the replica-set-init Job an ArgoCD `PostSync` hook (`hook-delete-policy: BeforeHookCreation`), which deletes-and-recreates instead of patching; (b) chart templates had silently drifted from manifest state that live testing had added directly (a label), which `selfHeal` would have reverted — caught by diffing rendered chart output against live cluster state before cutting over, not after.

8. **`ingress-nginx`'s node placement is not automatic on a multi-node kind cluster.** The upstream kind-flavored install manifest (and the official Helm chart's defaults) tolerate the control-plane's taint but set **no `nodeSelector`** — on a single-node kind cluster it lands on the only node available (the control-plane) by coincidence; on our 4-node cluster it scheduled onto an arbitrary worker, silently breaking the host-port mapping (bound only to the control-plane container). Fixed by an explicit `nodeSelector: {ingress-ready: "true"}`, targeting the exact label Terraform already applies to the control-plane node for this purpose.

9. **A full StatefulSet delete (not a rolling restart) can genuinely deadlock a MongoDB replica set.** Tested live by deleting the Deployment/StatefulSet/Services outright (to verify ArgoCD `selfHeal` recreates them — it did, in ~5 seconds). But because all 3 MongoDB pods restarted simultaneously, each one's boot-time self-identification check (`Locally stored replica set configuration does not have a valid entry for the current node`) failed before any peer was reachable to vouch for it, and all 3 landed in `REMOVED` state — a real deadlock requiring a manual `rs.reconfig(..., {force: true})` to recover. Lesson: normal rolling operations (`kubectl rollout restart`, single-pod deletes) never hit this, because StatefulSets always replace pods one at a time, keeping a quorum-eligible peer alive throughout. Full-resource deletion is a good chaos test, but isn't representative of any real operational path.

10. **HPA coexists with hard anti-affinity, with a local-cluster caveat.** `articles-api-hpa` scales from 3 to 6 replicas on 70% CPU (verified live via `metrics-server`: `cpu: 8%/70%`, correctly holding at `minReplicas` under normal load). But hard anti-affinity limits real placement to 1 pod per node — with exactly 3 worker nodes, any replica beyond 3 would schedule-fail (`Pending`) until a 4th node exists. This is intentional, not an oversight: on a cloud cluster this is exactly the trigger point for a node autoscaler (Cluster Autoscaler / Karpenter) to add capacity so HPA's decision and the node pool grow together — the local cluster proves the CPU-based scaling *decision* is wired correctly, while node-count headroom is a separate, environment-specific concern. Also required: an ArgoCD `ignoreDifferences` entry on the Deployment's `spec.replicas` — without it, `selfHeal` would fight the autoscaler by forcing replicas back to the chart's static value on every sync.
