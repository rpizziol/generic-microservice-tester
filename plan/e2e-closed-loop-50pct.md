# Piano: Test E2E closed-loop a ~50% utilization

<!-- cross-reviewed -->

> Test E2E che valida il microservizio sotto carico moderato (~50% utilization)
> con workload closed-loop (N clienti + think time), confrontando RT misurato
> contro predizioni lqsim.

## Step 1: Utility per modello LQN parametrico

**File nuovo:** `tools/lqn_model_utils.py`

Funzione minimale che legge un file `.lqn`, modifica la multiplicity di un task
reference (TClient), e scrive il risultato in un file temporaneo.

```python
def set_client_multiplicity(lqn_path: str, task_name: str, new_m: int) -> Path:
    """Read .lqn file, replace multiplicity on task_name line, write to tempfile."""
```

Approccio: regex replacement sulla riga `t <task_name> ... m <N>`.
Ritorna il path al file temporaneo (il chiamante e' responsabile della pulizia).

**Verifica:** `python -c "from tools.lqn_model_utils import set_client_multiplicity; ..."`

## Step 2: Test closed-loop a ~50% utilization

**File nuovo:** `tests/e2e/test_closed_loop_utilization.py`

Prerequisiti: Docker con GMT image buildata (`gmt-test:latest`), lqsim disponibile.

### Parametri del test

Dal modello `validation-model.lqn`:
- TServer service time S ≈ 0.047s (da lqsim)
- Think time z = 2.0s (da TClient `z 2.0`)
- Target utilization U = 0.5
- N clienti = ceil(U × (z + R) / S) ≈ 22 (con R ≈ S/(1-U) per M/M/1)
- GUNICORN_WORKERS = 1 (single server, coerente con TServer `m 1`)

### Flow del test

1. Generare modello parametrico: `set_client_multiplicity("validation-model.lqn", "TClient", 22)` → tempfile
2. Eseguire lqsim sul modello parametrico via `lqsim_runner.run_and_parse()` → predizioni (RT, throughput, utilization)
3. Avviare 2 container Docker (stessa infrastruttura degli altri test E2E):
   - TServer: `GUNICORN_WORKERS=1, LQN_TASK_CONFIG=<json_tserver>`
   - TLeaf: `GUNICORN_WORKERS=1, LQN_TASK_CONFIG=<json_tleaf>`
   - Container names: `tserver-closed`, `tleaf-closed`
   - Ports: 18084 (TServer), 18085 (TLeaf)
   - Network: `gmt-e2e-closed`
4. Warm-up: 10s di richieste (scartate)
5. Fase di misura: lanciare 22 thread via `ThreadPoolExecutor`, ciascuno esegue il loop:
   ```
   while not stop_event:
       start = monotonic()
       GET /process
       elapsed = monotonic() - start
       results.append(elapsed)
       sleep(random.expovariate(1/2.0))  # think time esponenziale come lqsim
   ```
   Durata misura: 60 secondi
6. Raccogliere metriche:
   - RT_mean = media dei response time
   - X = num_requests / durata_misura (throughput)
   - U_measured = X × S_pred (utilization law)
7. Confrontare con predizioni lqsim:
   - MAPE_RT = |RT_pred - RT_mean| / RT_mean < 25%
   - Verificare che U_measured sia nell'intorno di 0.5 ± 0.15

### Think time esponenziale

lqsim tratta `z` come media di una distribuzione esponenziale. Per coerenza,
il generatore di carico usa `random.expovariate(1/z)` anziche' un delay fisso.

Mark test con `@pytest.mark.e2e`. Skip se Docker o lqsim non disponibili.

**Verifica:** `pytest tests/e2e/test_closed_loop_utilization.py -v -s`

## Ordine: Step 1 → Step 2 (sequenziali)

## File coinvolti

| File | Azione | Step |
|---|---|---|
| `tools/lqn_model_utils.py` | NUOVO | 1 |
| `tests/e2e/test_closed_loop_utilization.py` | NUOVO | 2 |

## Comandi di verifica

```bash
# Build Docker image (se non gia' buildata)
docker build -f docker/Dockerfile -t gmt-test:latest .

# Test E2E
pytest tests/e2e/test_closed_loop_utilization.py -v -s

# Lint
ruff check tools/lqn_model_utils.py tests/e2e/test_closed_loop_utilization.py

# Full test suite (regressioni)
pytest tests/unit/ -q
```
