# CLAUDE.md — Generic Microservice Tester

> Istruzioni operative per lo sviluppo assistito da Claude Code.
> Questo file viene letto automaticamente all'apertura del progetto.

---

## Panoramica

**Generic Microservice Tester** e' un microservizio configurabile a immagine singola, progettato per simulare topologie applicative complesse su Kubernetes. Ogni istanza del container viene configurata interamente tramite variabili d'ambiente, eliminando la necessita' di scrivere codice applicativo custom per ogni servizio.

**Visione**: il progetto e' pensato come *compilation target* per modelli LQN (Layered Queueing Network). Ogni Task LQN viene mappato su un Deployment K8s con variabili d'ambiente che ne definiscono il comportamento (tempo di servizio, chiamate uscenti, concorrenza). Basato sul paper muP (Garbi, Incerto, Tribastone — IEEE CLOUD 2023).

**Stack tecnologico**: Python 3.12, Flask, Gunicorn, psutil, numpy, requests, Docker, Kubernetes.

---

## Python Environment

```bash
# Creare il virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Installare le dipendenze
pip install -r src/requirements.txt

# Linting e formatting
ruff check src/
ruff format src/
```

> **Nota**: i hook di Claude Code eseguono `ruff format` e `ruff check --fix` automaticamente su ogni file `.py` modificato.

---

## Struttura Progetto

```
generic-microservice-tester/
├── src/
│   ├── app.py              # App Flask (activity engine LQN + modalita' legacy)
│   ├── lqn_parser.py       # Parser formato LQN V5 testuale
│   ├── busy_wait.c         # C extension per busy-wait GIL-releasing (AND-fork)
│   └── requirements.txt     # Dipendenze Python
├── tools/
│   ├── lqn_compiler.py     # Compilatore LQN → manifesti K8s
│   ├── lqsim_runner.py     # Wrapper lqsim: esegue simulazioni e parsa output .p
│   └── lqn_model_utils.py  # Utility per modelli LQN parametrici (es. cambio multiplicity)
├── docker/
│   ├── Dockerfile          # Multi-stage build (gcc builder + python:3.12-slim runtime)
│   └── entrypoint.sh       # Launcher Gunicorn
├── kubernetes/
│   ├── base/
│   │   ├── deployment.yaml # Template generico di deployment
│   │   └── service.yaml    # Template generico di service
│   └── examples/
│       ├── 2-tier-app.yaml  # Topologia a 2 livelli
│       ├── 2-tier-hpa.yaml  # 2 livelli con HPA
│       ├── chain-app.yaml   # Catena di servizi
│       └── choice-app.yaml  # Routing probabilistico
├── tests/
│   ├── unit/               # Test unitari (parser, compiler, engine, trace)
│   ├── e2e/                # Test E2E (Docker: utilization law, lqsim predictions, closed-loop; K8s: topology)
│   └── helpers/            # Utility di validazione trace
├── test/
│   └── lqn-groundtruth/    # Modelli LQN di riferimento (template_annotated, validation-model)
├── plan/                    # Piani di implementazione (per /orchestrate)
├── .claude/
│   ├── settings.json        # Permessi e hook
│   ├── settings.local.json  # Override locali
│   ├── commands/            # Slash commands
│   └── skills/              # Knowledge base per lo sviluppo
├── CLAUDE.md                # Questo file
└── README.md                # Documentazione pubblica
```

---

## Componenti Chiave

| Componente | File | Descrizione |
|---|---|---|
| **LQN Parser** | `src/lqn_parser.py` | Parser completo del formato LQN V5 testuale. Produce dataclass `LqnModel` con processori, task, entry, activity e activity graph. |
| **LQN Compiler** | `tools/lqn_compiler.py` | Compila file `.lqn` in manifesti K8s (Deployment + Service per task non-reference). Risolve target chiamate in nomi DNS K8s. |
| **Activity Engine** | `src/app.py` → `execute_activity_graph()` | Interprete di activity diagram LQN: sequenze, AND-fork/join (parallelo), OR-fork (probabilistico), reply semantics. Usa `LQN_TASK_CONFIG` JSON. |
| **C extension busy-wait** | `src/busy_wait.c` | Busy-wait CPU che rilascia il GIL tramite ctypes. Usa `CLOCK_THREAD_CPUTIME_ID` per timing per-thread. Abilita vero parallelismo nei branch AND-fork. |
| **Execution tracing** | `src/app.py` → trace params | Ogni funzione del motore produce eventi strutturati (activity, and_fork, and_join, or_fork, reply, sync_call, async_call). Attivabile con `LQN_TRACE=1`. |
| **Dry-run mode** | `src/app.py` → `LQN_DRY_RUN=1` | Esecuzione istantanea senza CPU e HTTP. Per testing e verifica formale. |
| **Tempo di servizio stocastico** | `src/app.py` → `do_work()` | Distribuzione esponenziale con media configurabile via `SERVICE_TIME_SECONDS`. Modalita' legacy. |
| **CPU delta tracking (psutil)** | `src/app.py` → `do_work()` | Tracciamento del tempo CPU user-space per processo con delta tracking. Modalita' legacy. |
| **Chiamate sincrone (SYNC)** | `src/app.py` → `make_call()` | Chiamate HTTP bloccanti via `requests.Session` condivisa con connection pooling (100 connessioni). |
| **Chiamate asincrone (ASYNC)** | `src/app.py` → `make_async_call_pooled()` | Fire-and-forget tramite `ThreadPoolExecutor` isolato per worker. Sessione HTTP dedicata, semantica LQN "send-no-reply". |
| **Routing probabilistico** | `src/app.py` → `handle_legacy_request()` | Chiamate con probabilita' < 1.0 scelte con `random.choices` (weighted). Modalita' legacy. |
| **Gunicorn launcher** | `docker/entrypoint.sh` | Configura workers, threads e worker-class sync per timing CPU accurato. |
| **lqsim Runner** | `tools/lqsim_runner.py` | Wrapper per lqsim: esegue simulazioni, parsa output tabellare `.p` (throughput, service time, utilization per task). |
| **LQN Model Utils** | `tools/lqn_model_utils.py` | Utility per generare modelli LQN parametrici (es. modifica multiplicity di un task reference per test a diversi livelli di carico). |

---

## LQN <-> K8s Mapping

Mappatura critica tra entita' LQN e costrutti Kubernetes/microservizio:

| Entita' LQN | Mapping K8s/Microservizio | Variabile d'Ambiente |
|---|---|---|
| **Processor** | Pod (resource limits `cpu`/`memory`) | — |
| **Task** | Deployment (`replicas` = livello di concorrenza) | `GUNICORN_WORKERS` |
| **Entry** | Endpoint HTTP (`/<entry_name>`) | `LQN_TASK_CONFIG` (entries) |
| **Activity** | CPU busy-wait (distribuzione esponenziale) via C extension | `LQN_TASK_CONFIG` (activities) |
| **Activity Diagram** | Sequenze, AND-fork/join, OR-fork, reply | `LQN_TASK_CONFIG` (graph) |
| **Sync Call (y)** | Chiamata SYNC uscente (bloccante) | `LQN_TASK_CONFIG` o `OUTBOUND_CALLS` |
| **Async Call (z)** | Chiamata ASYNC uscente (fire-and-forget) | `LQN_TASK_CONFIG` o `OUTBOUND_CALLS` |
| **Open Workload** | Generatore di carico esterno (e.g., k6, hey) | — |

### Esempio di traduzione LQN -> YAML

Un Task LQN con service time medio 0.2s, 3 worker, che chiama sincronamente un altro Task:

```yaml
env:
- name: SERVICE_NAME
  value: "task-a"
- name: SERVICE_TIME_SECONDS
  value: "0.2"
- name: GUNICORN_WORKERS
  value: "3"
- name: OUTBOUND_CALLS
  value: "SYNC:task-b-svc:1.0"
```

---

## Convenzioni Codice

### Python
- **Naming**: `snake_case` per funzioni e variabili, `PascalCase` per classi, `UPPER_CASE` per costanti
- **Type hints**: sempre sulle firme delle funzioni
- **Stringhe**: f-strings per formattazione, mai `%` o `.format()`
- **Import**: stdlib prima, third-party dopo, locali per ultimi. Import assoluti
- **Eccezioni**: catturare eccezioni specifiche, mai `except:` generico
- **Docstring**: ogni funzione pubblica deve avere una docstring che spiega il *perche'*, non solo il *cosa*
- **Variabili globali worker-level**: prefisso `_` per stato interno al worker (e.g., `_last_user_time`)

### YAML (Kubernetes)
- Indentazione a 2 spazi
- Sempre specificare `resources.requests` e `resources.limits`
- Label standard: `app.kubernetes.io/name`, `app.kubernetes.io/component`
- Mai hardcodare credenziali nei manifest

### Docker
- Immagine base: `python:3.12-slim`
- `PYTHONUNBUFFERED=1` sempre attivo
- Usare `exec` nell'entrypoint per signal forwarding corretto

---

## Comandi Rapidi

```bash
# Build Docker image
docker build -f docker/Dockerfile -t generic-microservice-tester:latest .

# Run locale (senza K8s)
docker run -e SERVICE_NAME=test -e SERVICE_TIME_SECONDS=0.1 -p 8080:8080 generic-microservice-tester:latest

# Deploy su Kubernetes
kubectl apply -f kubernetes/examples/2-tier-app.yaml

# Eliminare un deployment
kubectl delete -f kubernetes/examples/2-tier-app.yaml

# Test locale con curl
curl http://localhost:8080/

# Compilare modello LQN in manifesti K8s
python tools/lqn_compiler.py test/lqn-groundtruth/template_annotated.lqn

# Deploy da modello LQN
python tools/lqn_compiler.py model.lqn | kubectl apply -f -

# Eseguire test suite
pytest tests/unit/ -v --tb=short

# Linting
ruff check src/ tools/ tests/
ruff format --check src/

# Installare dipendenze
pip install -r src/requirements.txt
```

---

## Slash Commands

| Comando | Descrizione |
|---|---|
| `/orchestrate` | Analizza una richiesta complessa, crea piano dettagliato con task tracciabili |
| `/plan` | Genera un piano di implementazione strutturato senza eseguire codice |
| `/implement` | Esegue l'implementazione seguendo un piano esistente, un task alla volta |
| `/auto` | Modalita' autonoma: pianifica, implementa e verifica in sequenza |
| `/cross-review` | Review incrociata del codice con prospettive multiple (sicurezza, performance, manutenibilita') |
| `/lqn-expert` | Consulenza specialistica su modelli LQN e mapping verso K8s |
| `/challenge-perf` | Analisi critica delle performance: identifica bottleneck e propone ottimizzazioni |
| `/audit` | Audit completo del progetto: struttura, sicurezza, best practice |
| `/detect-bs` | Verifica affermazioni tecniche nel codice o nella documentazione |
| `/review` | Code review standard con focus su correttezza e stile |
| `/test` | Genera o esegue test per il componente specificato |
| `/gen-plan` | Genera un piano di implementazione e lo salva in `plan/` |
| `/refine` | Raffina una singola componente con miglioramenti incrementali |
| `/refine-full` | Raffinamento completo del progetto con analisi approfondita |
| `/refine-model` | Raffina il mapping LQN-K8s e la fedelta' del modello |
| `/benchmark` | Crea o esegue benchmark di performance per il microservizio |
| `/ci-check` | Verifica che il progetto passi tutti i controlli CI (lint, test, build) |
| `/prepare` | Prepara il progetto per un rilascio: changelog, versioning, check finali |
| `/sync-docs` | Sincronizza documentazione con lo stato attuale del codice |
| `/cleanup-plans` | Rimuove piani completati o obsoleti dalla directory `plan/` |
| `/cleanup-branches` | Pulisce branch Git locali e remoti non piu' necessari |
| `/verify-plan` | Verifica lo stato di completamento di un piano esistente |

---

## Configuration (Variabili d'Ambiente)

| Variabile | Default | Descrizione | Esempio |
|---|---|---|---|
| `SERVICE_NAME` | `generic-service` | Nome identificativo dell'istanza del servizio | `frontend` |
| `SERVICE_TIME_SECONDS` | `0` | Media della distribuzione esponenziale per il tempo di servizio CPU (secondi) | `0.2` |
| `OUTBOUND_CALLS` | `""` | Chiamate HTTP uscenti. Formato: `TYPE:service:prob,...` | `SYNC:backend:0.6,ASYNC:logger:1.0` |
| `GUNICORN_WORKERS` | `2` | Numero di worker processes Gunicorn | `4` |
| `GUNICORN_THREADS` | `1` | Numero di thread per worker (1 = process-based per timing CPU accurato) | `1` |
| `LQN_TASK_CONFIG` | `""` | JSON con configurazione task LQN (entries, activities, graph). Se presente, attiva modalita' LQN | `{"task_name":"T1",...}` |
| `LQN_DRY_RUN` | `0` | Modalita' dry-run: `1` = skip CPU e HTTP, esecuzione istantanea | `1` |
| `LQN_TRACE` | `0` | Abilita tracing strutturato nella response JSON | `1` |

### Formato OUTBOUND_CALLS

```
TYPE:SERVICE_NAME:PROBABILITY[,TYPE:SERVICE_NAME:PROBABILITY,...]
```

- **TYPE**: `SYNC` (bloccante, attende risposta) o `ASYNC` (fire-and-forget)
- **SERVICE_NAME**: nome del Service K8s da chiamare
- **PROBABILITY**: `1.0` = sempre eseguita; `< 1.0` = scelta probabilistica (solo una tra le probabilistiche viene eseguita per richiesta, scelta con weighted random)

---

## Note per lo Sviluppo

- **Due modalita' di funzionamento**: LQN (con `LQN_TASK_CONFIG`) e legacy (con `SERVICE_TIME_SECONDS` + `OUTBOUND_CALLS`)
- **C extension**: `busy_wait.c` rilascia il GIL per vero parallelismo nei branch AND-fork. Compilato nel Docker multi-stage build
- **Activity engine**: `execute_activity_graph()` cammina il grafo con cycle detection, validazione nomi, tracing strutturato
- **AND-fork parallelismo**: `FORK_EXECUTOR` (ThreadPoolExecutor, 8 worker) + C extension per esecuzione parallela vera
- Il busy-wait legacy usa `time.process_time()` per misurare tempo CPU effettivo, non wall-clock time
- Ogni worker Gunicorn ha stato isolato (`_last_user_time`, `SESSION`, `ASYNC_SESSION`, `ASYNC_EXECUTOR`, `FORK_EXECUTOR`)
- Il delta tracking gestisce automaticamente il restart dei worker
- **Compilatore LQN**: `tools/lqn_compiler.py` traduce file `.lqn` in manifesti K8s con `LQN_TASK_CONFIG` serializzato
- **Test suite**: 171 unit test (parser, compiler, engine, trace, validazione) + E2E Docker (utilization law, lqsim predictions, closed-loop 50% utilization) + E2E K8s (topology). Verifica formale via trace matching
- I manifest K8s in `kubernetes/examples/` sono pronti all'uso e autocontenuti (deployment + service)
