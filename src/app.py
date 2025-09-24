import os
import random
import requests
import threading
import time
from flask import Flask, jsonify

app = Flask(__name__)

# --- Configuration Section ---
# The behavior of this microservice is controlled by environment variables.
#
# SERVICE_NAME: A friendly name for this instance (e.g., "frontend", "backend-a").
#
# SERVICE_TIME_SECONDS: Simulates CPU work by busy-waiting for a specified duration.
#                       Example: "0.1" for 100ms of CPU load.
#
# OUTBOUND_CALLS: Defines downstream HTTP calls.
#                 Format: "TYPE:service_name:probability,TYPE:service_name:probability,..."
#                 Example: "SYNC:backend-a:0.6,SYNC:backend-b:0.4,ASYNC:logger-svc:1.0"

def do_work():
    """Simulates CPU-intensive work using a busy-wait loop."""
    try:
        service_time_str = os.environ.get('SERVICE_TIME_SECONDS', '0')
        service_time = float(service_time_str)
        if service_time > 0:
            start_time = time.thread_time()
            while time.thread_time() - start_time < service_time:
                # This loop actively consumes CPU cycles
                pass
            print(f"Completed busy-wait for {service_time} seconds.")
    except (ValueError, TypeError):
        print(f"Invalid SERVICE_TIME_SECONDS value: {service_time_str}. Skipping work simulation.")


def parse_outbound_calls():
    """Reads and parses the OUTBOUND_CALLS environment variable."""
    config_str = os.environ.get('OUTBOUND_CALLS', '')
    if not config_str:
        return [], []

    targets = []
    call_defs = config_str.split(',')
    for call_def in call_defs:
        try:
            call_type, service_name, prob_str = call_def.strip().split(':')
            probability = float(prob_str)
            targets.append({
                "type": call_type.upper(),
                "service": service_name,
                "probability": probability
            })
        except ValueError:
            print(f"Skipping malformed call definition: {call_def}")

    # Separate probabilistic calls from fixed calls (which are always executed)
    probabilistic_targets = [t for t in targets if t['probability'] < 1.0]
    fixed_targets = [t for t in targets if t['probability'] >= 1.0]

    return probabilistic_targets, fixed_targets

def make_call(target):
    """Executes a single HTTP GET request to a downstream service."""
    service_name = target['service']
    # Kubernetes DNS resolves the service name to its internal IP address
    url = f"http://{service_name}"
    try:
        response = requests.get(url, timeout=5)
        print(f"Called {url}, status: {response.status_code}")
        return {"service": service_name, "status": response.status_code}
    except requests.exceptions.RequestException as e:
        print(f"Failed to call {url}: {e}")
        return {"service": service_name, "status": "error", "reason": str(e)}

@app.route('/')
def handle_request():
    """Main endpoint to handle incoming requests."""
    my_name = os.environ.get('SERVICE_NAME', 'generic-service')

    # 1. Simulate the service's own workload first
    do_work()

    # 2. Parse the downstream call configuration
    probabilistic_targets, fixed_targets = parse_outbound_calls()
    results = []

    # 3. Execute all fixed calls (probability >= 1.0)
    for target in fixed_targets:
        if target['type'] == 'SYNC':
            results.append(make_call(target))
        elif target['type'] == 'ASYNC':
            # Run the call in a separate thread and don't wait for the result
            thread = threading.Thread(target=make_call, args=(target,))
            thread.start()
            results.append({"service": target['service'], "status": "async_sent"})

    # 4. Choose and execute one of the probabilistic calls based on their weights
    if probabilistic_targets:
        services = [t['service'] for t in probabilistic_targets]
        weights = [t['probability'] for t in probabilistic_targets]

        # Randomly choose one service based on the defined probabilities
        chosen_service_name = random.choices(services, weights=weights, k=1)[0]

        # Find the full definition for the chosen target
        chosen_target = next(t for t in probabilistic_targets if t['service'] == chosen_service_name)

        # Execute the call (must be synchronous to make the choice meaningful)
        results.append(make_call(chosen_target))

    return jsonify({"message": f"Response from {my_name}", "outbound_results": results})

