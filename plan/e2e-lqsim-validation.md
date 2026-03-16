# Piano: Validazione E2E CPU + confronto predizioni lqsim

<!-- cross-reviewed -->

> Test E2E che valida: (1) il busy-time rispetta i service time attesi,
> (2) le metriche misurate matchano le predizioni lqsim del modello LQN.

## Step 1: Creare il modello LQN validation-model.lqn

**File nuovo:** `test/lqn-groundtruth/validation-model.lqn`

Modello con 3 task: TClient (reference/load generator), TServer (activity diagram
con tutti i costrutti), TLeaf (leaf semplice per sync call target).

```
G
"validation-model"
0.0001
500
1
0.5
-1

P 0
p PClient f i
p PServer f m 1
p PLeaf f m 1
-1

T 0
t TClient r client -1 PClient z 2.0 m 1
t TServer n process -1 PServer m 1
t TLeaf n compute -1 PLeaf m 1
-1

E 0
s client 0.0 -1
y client process 1.0 -1
A process start
s compute 0.01 -1
-1

A TServer
s start 0.01
s fast 0.02
s slow 0.03
s work_a 0.02
s work_b 0.01
s finish 0.005
y finish compute 1.0
:
start -> (0.7)fast + (0.3)slow;
fast[process];
slow -> work_a & work_b;
work_a & work_b -> finish;
finish[process]
-1
```

Costrutti coperti: sequenza, OR-fork (70/30), AND-fork/join, sync call, reply.

CPU demand atteso TServer per richiesta:
- 70% fast path: start(0.01) + fast(0.02) = 0.03s
- 30% slow path: start(0.01) + slow(0.03) + work_a(0.02) + work_b(0.01) + finish(0.005) = 0.075s
- E[S_cpu] = 0.7 * 0.03 + 0.3 * 0.075 = 0.0435s

**Verifica:** `lqsim -p -C 5,10,1000000 test/lqn-groundtruth/validation-model.lqn` deve completare senza errori.

## Step 2: Wrapper Python per lqsim

**File nuovo:** `tools/lqsim_runner.py`

Wrapper minimale che:
1. Trova lqsim via env var `LQSIM_PATH` o `shutil.which("lqsim")`
2. Esegue `lqsim -p -C 5,10,1000000 model.lqn` via `subprocess.run`
3. Cerca il file `.p` nella stessa directory del modello
4. Parsa il formato `.p`:
   - Linee `B task_name throughput` → throughput
   - Linee `R task_name response_time` → response time
   - Linee `U task_name utilization` → utilization (processor)
5. Ritorna `dict[str, dict]` con metriche per task
6. CLI standalone: `python tools/lqsim_runner.py model.lqn` stampa risultati

Formato output `.p` di lqsim:
```
B TServer              : process         0.487321
R TServer              : process         0.089234
U PServer              : TServer         0.021188
```

**Verifica:** `python tools/lqsim_runner.py test/lqn-groundtruth/validation-model.lqn` deve stampare le predizioni.

## Step 3: Test basso carico — Utilization Law

**File nuovo:** `tests/e2e/test_utilization_law.py`

Prerequisiti: Docker con GMT image buildata (`gmt-test:latest`).

Flow del test:
1. Compilare `validation-model.lqn` con `build_task_config` → config JSON per TServer e TLeaf
2. Avviare 2 container Docker:
   - TServer: `GUNICORN_WORKERS=1, LQN_TASK_CONFIG=<json_tserver>`
   - TLeaf: `GUNICORN_WORKERS=1, LQN_TASK_CONFIG=<json_tleaf>`
   - Connessi via Docker network per service discovery
3. Inviare 200 richieste a `GET /process` con rate basso (~2 req/s) usando un loop Python con `time.sleep(0.5)` tra le richieste
4. Per ogni risposta raccogliere wall-clock response time
5. Calcolare:
   - X = 200 / T_totale (throughput effettivo)
   - RT_mean = media dei response time
6. Verificare a basso carico (queuing trascurabile):
   - RT medio del fast path (70% delle richieste) ~ 0.03s ± 30%
   - RT medio complessivo coerente con il modello
   - X stabile attorno a 2 req/s

Mark test con `@pytest.mark.e2e` e skip se Docker non disponibile.

**Verifica:** `pytest tests/e2e/test_utilization_law.py -v -s`

## Step 4: Test carico moderato — confronto predizioni lqsim

**File nuovo:** `tests/e2e/test_lqsim_predictions.py`

Prerequisiti: Docker + lqsim.

Flow del test:
1. Eseguire lqsim sul modello via `lqsim_runner.py` → predizioni (throughput, RT per task)
2. Stessa infrastruttura Docker di Step 3 (TServer + TLeaf)
3. Inviare carico piu' alto: 500 richieste con rate ~10 req/s per 50 secondi
4. Misurare throughput effettivo X e response time medio RT
5. Calcolare MAPE:
   - MAPE_RT = |RT_pred - RT_meas| / RT_meas
6. Verificare MAPE_RT < 25%

Skip se lqsim non disponibile.

**Verifica:** `pytest tests/e2e/test_lqsim_predictions.py -v -s`

## Ordine: Step 1 → Step 2 → Step 3 → Step 4 (tutti sequenziali)

## File coinvolti

| File | Azione | Step |
|---|---|---|
| `test/lqn-groundtruth/validation-model.lqn` | NUOVO | 1 |
| `tools/lqsim_runner.py` | NUOVO | 2 |
| `tests/e2e/test_utilization_law.py` | NUOVO | 3 |
| `tests/e2e/test_lqsim_predictions.py` | NUOVO | 4 |

## Comandi di verifica

```bash
# Solve con lqsim
lqsim -p -C 5,10,1000000 test/lqn-groundtruth/validation-model.lqn

# Build Docker image
docker build -f docker/Dockerfile -t gmt-test:latest .

# Test E2E
pytest tests/e2e/test_utilization_law.py tests/e2e/test_lqsim_predictions.py -v -s

# Lint
ruff check tools/ tests/
```
