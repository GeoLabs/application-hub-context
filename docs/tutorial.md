# ApplicationHub — Getting Started Tutorial

This tutorial walks you through the full ApplicationHub lifecycle on a local Kubernetes cluster:

1. [Prerequisites](#1-prerequisites)
2. [Generate the Hub configuration](#2-generate-the-hub-configuration)
3. [Deploy the ApplicationHub](#3-deploy-the-applicationhub)
4. [Log in and use the sample application](#4-log-in-and-use-the-sample-application)
5. [Build your own application](#5-build-your-own-application)
6. [Add your application to the Hub](#6-add-your-application-to-the-hub)
7. [Next steps](#7-next-steps)

---

## 1. Prerequisites

### Local Kubernetes cluster

You need a running Kubernetes cluster reachable from your machine. Any of the following work out of the box:

#### kind

```bash
kind create cluster --name apphub
kubectl config use-context kind-apphub
```

#### minikube

```bash
minikube start --driver=docker
kubectl config use-context minikube
```

#### Docker Desktop

Enable Kubernetes in **Settings → Kubernetes → Enable Kubernetes**.

### Tools

| Tool      | Minimum version | Install                          |
| --------- | --------------- | -------------------------------- |
| `kubectl` | 1.28            | <https://kubernetes.io/docs/tasks/tools/> |
| `skaffold` | 2.17           | <https://skaffold.dev/docs/install/> |
| `helm`    | 3.14            | <https://helm.sh/docs/intro/install/> |
| `docker`  | 24              | <https://docs.docker.com/get-docker/> |

### Clone the repository

```bash
git clone https://github.com/EOEPCA/application-hub-context.git
cd application-hub-context
```

---

## 2. Generate the Hub configuration

`tutorial-config.yml` is not committed to the repository — it must be generated locally before deploying.

### Install the prerequisites

```bash
pip install app-hub-configurator   # provides the dump-config command
```

You also need [`iga-streamlit-demo`](https://github.com/EOEPCA/iga-streamlit-demo) cloned **alongside** this repository — `dump-config` reads the profile definition from its `profile/` directory:

```
parent/
├── application-hub-context/   ← this repo
└── iga-streamlit-demo/        ← must be here
```

```bash
git clone https://github.com/EOEPCA/iga-streamlit-demo ../iga-streamlit-demo
```

> [!TIP]
> If you cloned the repository, a `pyproject.toml` is available.  Run `pip install -e .` inside a virtual environment to install all dev dependencies at once.

### Generate `tutorial-config.yml`

```bash
task generate_tutorial_config
```

This runs two commands in sequence:

1. **`dump-config`** — generates all profiles (including `iga-streamlit-demo`) using the profile definition from `../iga-streamlit-demo/profile/`.

The result is a `tutorial-config.yml` ready to be loaded by Skaffold.

> [!WARNING]
> Any manual change to `tutorial-config.yml` will be overwritten on the next `task generate_tutorial_config`.  To modify the `iga-streamlit-demo` profile, edit `profile/iga_profiles.py` in the [`iga-streamlit-demo`](https://github.com/EOEPCA/iga-streamlit-demo) repository.

---

## 3. Deploy the ApplicationHub

The `tutorial` Skaffold profile deploys the hub using the pre-built stable image together with the [sample application](https://github.com/EOEPCA/iga-streamlit-demo) profile, so no local Docker build is required.

```bash
skaffold run -p tutorial
```

Skaffold will:

1. Add the `eoepca` Helm repository (`https://eoepca.github.io/helm-charts-dev/`) automatically.
2. Deploy the `application-hub` Helm chart (v4.0.2) into the `jupyter` namespace.
3. Apply the cluster role binding and initialisation job.
4. Forward port `8000` on your machine to the Hub proxy.

Wait until the hub pod is ready:

```bash
kubectl get pods -n jupyter -w
```

You should see a `hub-*` pod in `Running` state and a `proxy-*` pod in `Running` state.

### Verify

```bash
kubectl get pods -n jupyter
kubectl get configmap -n jupyter
```

---

## 4. Log in and use the sample application

Open <http://localhost:8000> in your browser.

Log in with the default test credentials:

| Field    | Value    |
| -------- | -------- |
| Username | `jovyan` |
| Password | `12345`  |

On the profile selection page you will see **Sample Streamlit App**. Click **Start** to spawn it.

The app displays runtime information injected by the Hub (user name, service prefix, …) and links to the next steps of this tutorial.

> [!TIP]
> The initialisation job (`hub-content-init`) automatically creates `group-a`, `group-b`, and `group-c` and adds `jovyan`, `alice`, and `bob` to each. You can log in as any of these users.

---

## 5. Build your own application

### 5.1 Clone the sample app

```bash
git clone https://github.com/EOEPCA/iga-streamlit-demo.git
cd iga-streamlit-demo
```

The repository contains:

```
Dockerfile      # Python 3.12 + Streamlit + jhsingle-native-proxy
app.py          # Streamlit application — edit this
entrypoint.sh   # Startup script (port forwarding via jhsingle-native-proxy)
```

### 5.2 Customise `app.py`

Replace the content of `app.py` with your own Streamlit application. For example:

```python
import streamlit as st

st.title("My EO Dashboard")
st.write("Hello from my custom ApplicationHub app!")
```

### 5.3 Add Python dependencies

Add any extra packages to the `Dockerfile`:

```dockerfile
RUN pip install --no-cache-dir \
    "jhsingle-native-proxy>=0.0.9" \
    streamlit \
    folium \          # example: interactive maps
    xarray            # example: NetCDF / EO data
```

### 5.4 Build and test locally

```bash
docker build -t my-app:local .
docker run --rm -p 8888:8888 my-app:local
```

Open <http://localhost:8888> to verify your app.

### 5.5 Push to a registry

```bash
docker tag my-app:local ghcr.io/<your-org>/my-app:latest
docker push ghcr.io/<your-org>/my-app:latest
```

---

## 6. Add your application to the Hub

### 6.1 Write a profile

Copy `tutorial-config.yml` to `custom-config.yml` and add a new profile:

```yaml
profiles:

- id: sample_app            # keep the existing sample profile
  # … (unchanged)

- id: my_app                # your new profile
  groups:
  - group-a
  definition:
    display_name: My EO Dashboard
    description: My custom Earth Observation dashboard.
    slug: my_app_slug
    default: false
    kubespawner_override:
      image: ghcr.io/<your-org>/my-app:latest
      cpu_limit: 1
      cpu_guarantee: null
      mem_limit: 2G
      mem_guarantee: null
      extra_resource_limits: {}
      extra_resource_guarantees: {}
  volumes:
  - name: workspace-volume
    claim_name: workspace-claim
    size: 5Gi
    storage_class: standard
    access_modes:
    - ReadWriteOnce
    persist: false
    volume_mount:
      name: workspace-volume
      mount_path: /home/jovyan
  pod_env_vars:
    HOME: /home/jovyan
```

For a full reference of all profile fields, see the [Configuration](configuration.md) page.

### 6.2 Validate the config (optional)

```bash
task check_schema CONFIG=custom-config.yml
```

### 6.3 Deploy with the custom profile

```bash
skaffold run -p custom
```

Skaffold patches the Hub's config map with your `custom-config.yml`. After the Hub pod restarts, your new profile will appear on the login page.


