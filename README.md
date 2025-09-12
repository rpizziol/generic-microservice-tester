# Generic Microservice Tester
A generic and highly configurable microservice, designed to simulate complex application topologies on Kubernetes for testing and performance analysis purposes.

## Project Goal
This tool allows you to create microservice architectures (e.g., call chains, fan-out) without writing code. The behavior of each instance, such as CPU load and outbound calls (synchronous, asynchronous, or probabilistic), is entirely controlled by environment variables defined in the Kubernetes deployment files. It is ideal for experimenting with service meshes like Istio, auto-scalers like HPA, and monitoring platforms like Prometheus in a fast and repeatable way.

## Author
Roberto Pizziol

## License
This project is released under the MIT License.
