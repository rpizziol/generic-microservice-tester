# Piano: Generazione locustfile + deploy.sh dal modello LQN

<!-- cross-reviewed -->

> Estende il compilatore LQN con la generazione di: (1) un locustfile.py derivato
> dal reference task, (2) un manifest K8s per il Locust Job in-cluster,
> (3) uno script deploy.sh per orchestrare build/deploy/loadtest/teardown.

## Contesto

Il reference task LQN (es. TClient) definisce il workload: think time `z`, multiplicity
`m` (numero utenti), e le chiamate `y`/`z` verso entry dei task non-reference.
Attualmente il compilatore lo skippa. Questa feature lo usa per generare un load
generator Locust che gira come K8s Job nel cluster, con DNS nativo.

Semantica LQN del ciclo client:
1. Esegui TUTTE le chiamate dell'entry (in ordine di definizione)
2. Per call rate frazionario (es. 1.2): floor(n) garantite + prob extra
3. Dormi think time z (distribuzione esponenziale)
4. Ripeti

## Step 1: Generatore locustfile

**File nuovo:** `tools/locustfile_gen.py`

Funzione `generate_locustfile(model: LqnModel) -> str` che:

1. Trova il reference task (`task.is_reference == True`)
2. Estrae `think_time` (z) e `multiplicity` (m)
3. Per ogni entry del reference task, estrae le chiamate sync (`phase_sync_calls`)
   e async (`phase_async_calls`)
4. Risolve ogni target entry in URL K8s via `resolve_call_target()`:
   es. entry "process" su TServer → `http://tserver-svc/process`
5. Genera un locustfile Python con:
   - `LqnClient(HttpUser)` con `wait_time` esponenziale
   - Un singolo metodo `@task def cycle(self)` che esegue tutte le chiamate
   - Chiamate frazionarie: `floor(n)` garantite + `random() < frac` per l'extra
   - Port 80 negli URL (K8s Service standard)

Template del locustfile generato:
```python
"""Auto-generated locustfile from LQN model: {model_name}
Reference task: {task_name} (m={multiplicity}, z={think_time})
"""
import random
from locust import HttpUser, task

THINK_TIME = {think_time}

class LqnClient(HttpUser):
    host = "http://{first_target_svc}"

    def wait_time(self):
        return random.expovariate(1.0 / THINK_TIME) if THINK_TIME > 0 else 0

    @task
    def cycle(self):
        {call_block}
```

Il `call_block` per ogni chiamata (sync e async):
```python
        # {mean_calls} calls to {entry_name} on {task_name} (sync)
        for _ in range({floor_n}):
            self.client.get("http://{svc}/{entry}")
        if random.random() < {frac}:  # solo se frac > 0
            self.client.get("http://{svc}/{entry}")
```

CLI: `python tools/locustfile_gen.py model.lqn` stampa il locustfile su stdout.
Flag `-o locustfile.py` per scrivere su file.

Entry point pip: `lqn-locustfile = "gmt.tools.locustfile_gen:main"` in pyproject.toml.

**Verifica:** `python tools/locustfile_gen.py test/lqn-groundtruth/validation-model.lqn`
deve produrre un locustfile valido che chiama `http://tserver-svc/process`.

## Step 2: Generatore deploy.sh

**File nuovo:** `tools/deploy_gen.py`

Funzione `generate_deploy_script(model: LqnModel, image: str, namespace: str) -> str`
che genera uno script bash con comandi `up`, `down`, `test`:

- `deploy.sh up` → crea namespace, applica manifesti K8s, attende pod ready
- `deploy.sh down` → elimina namespace
- `deploy.sh test [--users N] [--duration Ts]` → crea ConfigMap col locustfile,
  lancia Locust Job, attende completamento, mostra risultati

Il Locust Job manifest (generato inline nello script o come YAML separato):
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: locust-loadtest
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: locust
        image: locustio/locust:2.32.6
        command: ["locust"]
        args:
        - "--headless"
        - "--users={multiplicity}"
        - "--spawn-rate={spawn_rate}"
        - "--run-time={duration}"
        - "--host=http://{entry_svc}"
        - "-f"
        - "/mnt/locustfile.py"
        volumeMounts:
        - name: locustfile
          mountPath: /mnt
      volumes:
      - name: locustfile
        configMap:
          name: gmt-locustfile
```

CLI: `python tools/deploy_gen.py model.lqn --image bistrulli/generic-microservice-tester:latest`
stampa deploy.sh su stdout. Flag `-o deploy.sh` per scrivere su file.

Entry point pip: `lqn-deploy = "gmt.tools.deploy_gen:main"` in pyproject.toml.

Lo script generato ha questa struttura (ispirato al deploy.sh di TLG):
```bash
#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="gmt-{model_name}"
IMAGE="{image}"

cmd_up() {
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    cat <<'MANIFEST' | kubectl apply -n "$NAMESPACE" -f -
    {k8s_manifests}
MANIFEST
    kubectl wait --for=condition=Ready pod --all -n "$NAMESPACE" --timeout=300s
}

cmd_down() {
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --timeout=60s
}

cmd_test() {
    local users="${1:-{multiplicity}}"
    local duration="${2:-60s}"
    kubectl create configmap gmt-locustfile -n "$NAMESPACE" \
        --from-literal=locustfile.py='{locustfile_content}' \
        --dry-run=client -o yaml | kubectl apply -f -
    cat <<'JOBMANIFEST' | kubectl apply -n "$NAMESPACE" -f -
    {locust_job_yaml}
JOBMANIFEST
    kubectl wait --for=condition=complete job/locust-loadtest \
        -n "$NAMESPACE" --timeout=300s
    kubectl logs job/locust-loadtest -n "$NAMESPACE"
}

case "${1:-}" in
    up)   cmd_up ;;
    down) cmd_down ;;
    test) shift; cmd_test "$@" ;;
    *)    echo "Usage: $0 up|down|test [users] [duration]"; exit 1 ;;
esac
```

**Verifica:** `python tools/deploy_gen.py test/lqn-groundtruth/validation-model.lqn --image bistrulli/generic-microservice-tester:latest -o /tmp/deploy.sh && bash /tmp/deploy.sh up`

## Step 3: Unit test per locustfile_gen

**File nuovo:** `tests/unit/test_locustfile_gen.py`

Test con modelli groundtruth:

1. `test_generates_valid_python` — il locustfile generato passa `compile(source, '<test>', 'exec')`
2. `test_single_entry_model` — validation-model: una sola chiamata sync a `tserver-svc/process`
3. `test_multi_entry_model` — template_annotated: 5 chiamate (visit×3, save×1, notify×1 async, read×1, buy×1.2)
4. `test_fractional_calls` — verifica che buy×1.2 generi il blocco `floor(1) + random < 0.2`
5. `test_async_calls_included` — le chiamate `z` (async) sono incluse nel ciclo
6. `test_think_time` — verifica che z=2.0 → `THINK_TIME = 2.0`
7. `test_no_reference_task_raises` — modello senza reference task → errore chiaro
8. `test_resolve_urls` — gli URL contengono i nomi DNS K8s corretti

**Verifica:** `pytest tests/unit/test_locustfile_gen.py -v`

## Step 4: Unit test per deploy_gen

**File nuovo:** `tests/unit/test_deploy_gen.py`

1. `test_generates_valid_bash` — lo script generato contiene `#!/usr/bin/env bash` e `set -euo pipefail`
2. `test_contains_namespace` — il namespace e' derivato dal nome del modello
3. `test_contains_manifests` — lo script contiene i deployment YAML
4. `test_locust_job_manifest` — contiene il Job con immagine `locustio/locust`, ConfigMap mount
5. `test_users_from_multiplicity` — il numero di utenti Locust corrisponde alla multiplicity del ref task

**Verifica:** `pytest tests/unit/test_deploy_gen.py -v`

## Step 5: Aggiornamento pyproject.toml

**File modificato:** `pyproject.toml`

Aggiungere entry point:
```toml
[project.scripts]
lqn-compile = "gmt.tools.lqn_compiler:main"
lqsim-run = "gmt.tools.lqsim_runner:main"
lqn-locustfile = "gmt.tools.locustfile_gen:main"
lqn-deploy = "gmt.tools.deploy_gen:main"
```

**Verifica:** `pip install -e . && lqn-locustfile test/lqn-groundtruth/validation-model.lqn`

## Ordine: Step 1 → Step 2 → Step 3 → Step 4 → Step 5 (tutti sequenziali)

## File coinvolti

| File | Azione | Step |
|---|---|---|
| `tools/locustfile_gen.py` | NUOVO | 1 |
| `tools/deploy_gen.py` | NUOVO | 2 |
| `tests/unit/test_locustfile_gen.py` | NUOVO | 3 |
| `tests/unit/test_deploy_gen.py` | NUOVO | 4 |
| `pyproject.toml` | MODIFICA | 5 |

## Comandi di verifica

```bash
# Genera locustfile
python tools/locustfile_gen.py test/lqn-groundtruth/validation-model.lqn

# Genera deploy.sh
python tools/deploy_gen.py test/lqn-groundtruth/validation-model.lqn \
    --image bistrulli/generic-microservice-tester:latest

# Test
pytest tests/unit/test_locustfile_gen.py tests/unit/test_deploy_gen.py -v

# Lint
ruff check tools/locustfile_gen.py tools/deploy_gen.py tests/unit/test_locustfile_gen.py tests/unit/test_deploy_gen.py

# Full suite
pytest tests/unit/ -q
```
