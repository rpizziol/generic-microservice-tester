# Generic Microservice Tester

A highly configurable, single-image microservice designed to simulate complex application topologies on Kubernetes for testing and performance analysis. This project was created to provide a flexible "test harness" for experimenting with service meshes, auto-scalers, and monitoring tools without writing custom application code.

## Core Concept

Instead of writing multiple applications to test a distributed system, you can deploy this single Docker image multiple times with different runtime configurations. The behavior of each microservice instance is controlled entirely by environment variables defined in your Kubernetes deployment manifests.

This allows you to rapidly prototype and benchmark sophisticated service chains, fan-out architectures, or any other topology, making it the perfect tool for experimenting with:

- Service meshes like Istio or Linkerd.
- The Kubernetes Horizontal Pod Autoscaler (HPA).
- Monitoring and observability platforms like Prometheus and Grafana.

## Repository Structure

The project is organized into three main directories to keep the source code, container configuration, and Kubernetes manifests separate and clean.

```
/
├── src/
│   ├── app.py              # Flask app (activity engine, tracing, legacy mode)
│   ├── lqn_parser.py       # LQN V5 text format parser
│   ├── busy_wait.c         # C extension for GIL-releasing CPU busy-wait
│   └── requirements.txt    # Python dependencies
├── tools/
│   ├── lqn_compiler.py     # LQN-to-K8s manifest compiler
│   ├── lqsim_runner.py     # lqsim wrapper: run simulations, parse .p output
│   └── lqn_model_utils.py  # Parametric LQN model generation (e.g., change multiplicity)
├── docker/
│   ├── Dockerfile          # Multi-stage build (gcc → python:3.12-slim)
│   └── entrypoint.sh       # Gunicorn launcher
├── kubernetes/
│   ├── base/               # Generic deployment + service templates
│   └── examples/           # Ready-to-use topologies (2-tier, chain, choice)
├── tests/                  # pytest test suite (171 unit + E2E)
│   ├── unit/               # Parser, compiler, engine, trace validation
│   ├── e2e/                # Docker E2E (utilization, lqsim, closed-loop) + K8s topology
│   └── helpers/            # Trace validator utility
└── test/
    └── lqn-groundtruth/    # Reference LQN models (template_annotated, validation-model)
```

## How to Use It: Creating Your Own Topology

The real power of this project is defining your own service architecture directly in YAML. Let's create a 3-service chain (`entry -> middle -> backend`) as an example.

### Step 1: Create a New Manifest File

Create a new file, for instance `kubernetes/examples/chain-app.yaml`.

### Step 2: Define the backend Service

This service is the last in the chain, so it doesn't call any other services.

```yaml
# In chain-app.yaml

apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend-deployment
spec:
  # ... boilerplate ...
  template:
    # ... boilerplate ...
    spec:
      containers:
      - name: app
        image: rpizziol/generic-microservice-tester:latest # Your image
        env:
        - name: SERVICE_NAME
          value: "backend"
        - name: SERVICE_TIME_SECONDS
          value: "0.2" # Simulate 200ms of work
        - name: OUTBOUND_CALLS
          value: "" # <-- KEY: This service calls no others
---
apiVersion: v1
kind: Service
metadata:
  name: backend-svc
# ... boilerplate ...
```

### Step 3: Define the middle Service

This service will be configured to make a single, synchronous call to our `backend-svc`.

```yaml
# In chain-app.yaml, after the backend service

apiVersion: apps/v1
kind: Deployment
metadata:
  name: middle-deployment
spec:
  # ... boilerplate ...
  template:
    # ... boilerplate ...
    spec:
      containers:
      - name: app
        image: rpizziol/generic-microservice-tester:latest
        env:
        - name: SERVICE_NAME
          value: "middle"
        - name: SERVICE_TIME_SECONDS
          value: "0.1" # Simulate 100ms of work
        - name: OUTBOUND_CALLS
          value: "SYNC:backend-svc:1.0" # <-- KEY: Always calls backend-svc
---
apiVersion: v1
kind: Service
metadata:
  name: middle-svc
# ... boilerplate ...
```

### Step 4: Define the entry Service

Finally, the entrypoint service will call the `middle-svc`. This is the service you will point your Istio Gateway to.

```yaml
# In chain-app.yaml, after the middle service

apiVersion: apps/v1
kind: Deployment
metadata:
  name: entry-deployment
spec:
  # ... boilerplate ...
  template:
    # ... boilerplate ...
    spec:
      containers:
      - name: app
        image: rpizziol/generic-microservice-tester:latest
        env:
        - name: SERVICE_NAME
          value: "entry"
        - name: SERVICE_TIME_SECONDS
          value: "0.05" # Simulate 50ms of work
        - name: OUTBOUND_CALLS
          value: "SYNC:middle-svc:1.0" # <-- KEY: Always calls middle-svc
---
apiVersion: v1
kind: Service
metadata:
  name: entry-svc
# ... boilerplate ...
```

By applying this single file, you have just created a 3-tier application without writing a single line of application code!

## Configuration Details (Environment Variables)

| Variable             | Description                                                             | Format / Example                                                   |
|-----------------------|------------------------------------------------------------------------|----------------------------------------------------------------------|
| `SERVICE_NAME`        | A friendly name for the service instance, returned in the JSON response. | `frontend-service`                                                  |
| `SERVICE_TIME_SECONDS`| **Mean** of exponential distribution for CPU work simulation. Each request samples a random service time from an exponential distribution with this mean. Provides realistic stochastic behavior. | `0.2` (mean 200ms, actual times vary) |
| `GUNICORN_WORKERS`    | The number of Gunicorn worker processes to spawn. A good starting point is (2 * number of cores) + 1. | `2` |
| `GUNICORN_THREADS`    | The number of threads per worker. Useful for I/O-bound tasks. | `4` |
| `OUTBOUND_CALLS`      | A comma-separated list defining the outbound HTTP calls to make upon receiving a request. | `TYPE:service_name:probability` |

## OUTBOUND_CALLS Format Explained

Each call is a colon-separated string: `TYPE:SERVICE_NAME:PROBABILITY`.

- **TYPE**: Can be `SYNC` (blocking call) or `ASYNC` (non-blocking call).
- **SERVICE_NAME**: The Kubernetes service name to call (e.g., `backend-svc`).
- **PROBABILITY**:
  - `1.0`: The call is always made. Multiple calls with 1.0 will all be executed.
  - `< 1.0`: The call is probabilistic. If multiple calls have a probability less than 1.0, only one will be chosen randomly based on their relative weights.

**Example:**

```yaml
value: "SYNC:backend-a:0.6,SYNC:backend-b:0.4,ASYNC:logger-svc:1.0"
```

This configuration will:

- Always send an asynchronous, non-blocking call to `logger-svc`.
- Then, make a single synchronous, blocking call to either `backend-a` (60% chance) or `backend-b` (40% chance).

## Stochastic Service Time Modeling

This microservice implements **realistic stochastic service times** using exponential distribution to better simulate real-world microservice behavior.

### Service Time Behavior

- **Exponential Distribution**: Each request samples CPU work time from an exponential distribution
- **Mean Parameter**: `SERVICE_TIME_SECONDS` specifies the mean (λ⁻¹) of the exponential distribution
- **Realistic Variability**: Service times vary naturally - some requests complete quickly, others take longer
- **Mathematical Accuracy**: Follows exponential distribution properties (memoryless, heavy tail)

### Example Behavior

```yaml
env:
- name: SERVICE_TIME_SECONDS
  value: "0.2"  # Mean service time of 200ms
```

**Sample outputs:**
- Request 1: 0.05s (fast completion)
- Request 2: 0.18s (near mean)
- Request 3: 0.73s (long tail event)
- Request 4: 0.12s (typical)

This provides more realistic load patterns for testing autoscalers, monitoring systems, and performance analysis.

## LQN-Semantic Compliance for Asynchronous Calls

This microservice implements **LQN (Layered Queueing Network) semantic compliance** for asynchronous calls to support accurate performance modeling and prediction.

### Key Features

- **Worker-Isolated Thread Pool**: Asynchronous calls (`ASYNC` type) are executed via a dedicated `ThreadPoolExecutor` per Gunicorn worker
- **Dedicated HTTP Session**: Async calls use a separate `requests.Session` with independent connection pooling
- **True "Send-No-Reply"**: Implements genuine fire-and-forget semantics as defined in LQN theory
- **Accurate Metrics**: CPU timing and throughput measurements exclude async call overhead

### Technical Implementation

- **Main Thread**: Handles synchronous calls and core service logic using shared HTTP session
- **Async Thread Pool**: Each Gunicorn worker has its own `ThreadPoolExecutor` (10 threads) for async delegation
- **No Blocking**: The main thread continues immediately after submitting async calls
- **Resource Separation**: Async threads use a dedicated HTTP session (separate connection pool, no retries)

### Response Status Codes

- **Synchronous calls**: Return actual HTTP status codes (e.g., `200`, `404`, `500`)
- **Asynchronous calls**: Return `"async_pooled"` to indicate successful submission to the worker thread pool

This architecture ensures that performance measurements and LQN model predictions align accurately with real-world behavior.

## LQN Model Compilation

GMT is designed as a **compilation target** for LQN (Layered Queueing Network) models. You write the performance model in standard `.lqn` format, and GMT compiles it into a set of microservices on Kubernetes that faithfully reproduce the modeled behavior.

### How it works

Each **Task** in the LQN model becomes a Kubernetes microservice (Deployment + Service). The task's logic — its entries, activity diagrams, service times, and inter-task calls — is encoded in a single `LQN_TASK_CONFIG` JSON environment variable that the microservice interprets at runtime.

```
┌──────────────┐     ┌─────────────┐     ┌─────────────────────────┐
│  model.lqn   │ ──> │  Compiler   │ ──> │  K8s Manifests (YAML)   │
│  (LQN model) │     │  lqn2kube   │     │  1 Deployment + Service │
│              │     │             │     │  per non-ref Task       │
└──────────────┘     └─────────────┘     └─────────────────────────┘
```

### End-to-end example

Consider this LQN model with a server task (`TServer`) that has 4 entries, an activity diagram with AND-fork/join and OR-fork, and two backing services (`TFileServer`, `TBackup`):

```
# model.lqn (simplified)
t TServer n visit buy notify save -1 PServer m 2

# Entry "visit" uses an activity diagram:
A visit cache
#   cache -> (0.95)internal + (0.05)external   [OR-fork]
#   internal[visit], external[visit]            [reply]

# Entry "buy" uses an activity diagram:
A buy prepare
#   prepare -> pack & ship                      [AND-fork]
#   pack & ship -> display                      [AND-join]
#   display[buy]                                [reply]

# Entries "notify" and "save" are phase-based:
s notify 0.08 -1       # 80ms CPU work, no calls
s save 0.02 -1         # 20ms CPU, then sync call to write
y save write 1.0 -1
```

**Step 1: Compile the model to K8s manifests**

```bash
python tools/lqn_compiler.py model.lqn
```

This generates Deployment + Service for each non-reference task: `tserver-svc`, `tfileserver-svc`, `tbackup-svc`. Reference tasks (like `TClient`) are skipped — they represent the external workload generator.

The compiler:
- Resolves call targets to K8s DNS names (e.g., `y save write 1.0` → `tfileserver-svc/write`)
- Serializes the activity graph (AND-fork/join, OR-fork, sequences, reply semantics) as JSON
- Sets `GUNICORN_WORKERS` from task multiplicity, CPU limits from processor multiplicity

**Step 2: Deploy**

```bash
python tools/lqn_compiler.py model.lqn | kubectl apply -f -
```

**Step 3: Send requests**

Each entry is accessible as an HTTP endpoint on the task's service:

```bash
# Hit the "visit" entry (OR-fork: 95% internal, 5% external)
curl http://tserver-svc/visit

# Hit the "buy" entry (AND-fork: pack & ship in parallel, then display)
curl http://tserver-svc/buy

# Hit "notify" (80ms CPU work, no downstream calls)
curl http://tserver-svc/notify

# Hit "save" (20ms CPU, then sync call to tfileserver-svc/write)
curl http://tserver-svc/save
```

### What happens inside the microservice

When a request arrives at `GET /buy`, the activity engine:

1. Executes activity `prepare` (0.01s CPU busy-wait, exponentially distributed)
2. **AND-fork**: executes `pack` (0.03s) and `ship` (0.01s) **in parallel** using a C extension that releases the GIL
3. **AND-join**: waits for both to complete (wall-clock ≈ max(0.03, 0.01) = 0.03s)
4. Executes activity `display` (0.001s)
5. **Reply**: sends HTTP response back to caller

When a request arrives at `GET /visit`:

1. Executes activity `cache` (0.001s)
2. **OR-fork**: chooses `internal` (95%) or `external` (5%) probabilistically
3. If `internal`: executes it (0.001s), replies
4. If `external`: executes it (0.003s), makes sync call to `tfileserver-svc/read`, replies

### How the task logic is specified

The task's entire behavior is encoded in the `LQN_TASK_CONFIG` environment variable as JSON. The compiler generates this automatically, but you can also write it manually:

```json
{
  "task_name": "TServer",
  "entries": {
    "visit": {"start_activity": "cache"},
    "buy": {"start_activity": "prepare"},
    "notify": {"service_time": 0.08},
    "save": {"service_time": 0.02, "sync_calls": {"tfileserver-svc/write": 1.0}}
  },
  "activities": {
    "prepare": {"service_time": 0.01},
    "pack": {"service_time": 0.03},
    "ship": {"service_time": 0.01},
    "display": {"service_time": 0.001},
    "cache": {"service_time": 0.001},
    "internal": {"service_time": 0.001},
    "external": {"service_time": 0.003, "sync_calls": {"tfileserver-svc/read": 1.0}}
  },
  "graph": {
    "and_forks": [{"from": "prepare", "branches": ["pack", "ship"]}],
    "and_joins": [{"branches": ["pack", "ship"], "to": "display"}],
    "or_forks": [{"from": "cache", "branches": [
      {"prob": 0.95, "to": "internal"}, {"prob": 0.05, "to": "external"}
    ]}],
    "replies": {"internal": "visit", "external": "visit", "display": "buy"},
    "sequences": []
  }
}
```

### Compiler CLI

```bash
# Generate YAML to stdout
python tools/lqn_compiler.py model.lqn

# Deploy directly to K8s
python tools/lqn_compiler.py model.lqn | kubectl apply -f -

# Custom Docker image
python tools/lqn_compiler.py --image myregistry/gmt:v1.0 model.lqn

# Save to file
python tools/lqn_compiler.py model.lqn -o kubernetes/generated/model.yaml

# Custom namespace
python tools/lqn_compiler.py --namespace production model.lqn

# Dry-run (show what would be generated)
python tools/lqn_compiler.py --dry-run model.lqn
```

### Debugging and tracing

```bash
# Enable execution tracing (trace included in JSON response)
LQN_TRACE=1 curl http://tserver-svc/buy

# Dry-run mode (no CPU work, no HTTP calls — instant response with trace)
# Set LQN_DRY_RUN=1 in the Deployment env vars
```

### LQN-Specific Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LQN_TASK_CONFIG` | `""` | JSON-encoded task fragment. When set, activates LQN interpreter mode |
| `LQN_DRY_RUN` | `0` | Set to `1` to skip CPU work and HTTP calls (for testing) |
| `LQN_TRACE` | `0` | Set to `1` to include structured execution trace in responses |

## Author
Roberto Pizziol

## License
This project is released under the MIT License.
