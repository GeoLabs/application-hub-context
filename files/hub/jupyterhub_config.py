import os
import sys

from tornado.httpclient import AsyncHTTPClient


from application_hub_context.app_hub_context import DefaultApplicationHubContext

configuration_directory = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, configuration_directory)

from z2jh import (
    get_config,
    get_name,
    get_name_env,
)

config_path = "/usr/local/etc/applicationhub/config.yml"

namespace_prefix = "jupyter"


def custom_options_form(spawner):

    spawner.log.info("Configure profile list")

    namespace = f"{namespace_prefix}-{spawner.user.name}"

    workspace = DefaultApplicationHubContext(
        namespace=namespace,
        spawner=spawner,
        config_path=config_path,
    )

    spawner.profile_list = workspace.get_profile_list()

    return spawner._options_form_default()


def pre_spawn_hook(spawner):

    profile_slug = spawner.user_options.get("profile", None)

    env = os.environ["JUPYTERHUB_ENV"].lower()

    spawner.environment["CALRISSIAN_POD_NAME"] = f"jupyter-{spawner.user.name}-{env}"

    spawner.log.info(f"Using profile slug {profile_slug}")

    namespace = f"{namespace_prefix}-{spawner.user.name}"

    workspace = DefaultApplicationHubContext(
        namespace=namespace, spawner=spawner, config_path=config_path, skip_namespace_check=False, 
    )

    workspace.initialise()

    profile_id = workspace.config_parser.get_profile_by_slug(slug=profile_slug).id

    default_url = workspace.config_parser.get_profile_default_url(profile_id=profile_id)

    if default_url:
        spawner.log.info(f"Setting default url to {default_url}")
        spawner.default_url = default_url


def post_stop_hook(spawner):

    namespace = f"jupyter-{spawner.user.name}"

    workspace = DefaultApplicationHubContext(
        namespace=namespace, spawner=spawner, config_path=config_path
    )
    spawner.log.info("Dispose in post stop hook")
    workspace.dispose()


c.JupyterHub.default_url = "spawn"


# Configure JupyterHub to use the curl backend for making HTTP requests,
# rather than the pure-python implementations. The default one starts
# being too slow to make a large number of requests to the proxy API
# at the rate required.
AsyncHTTPClient.configure("tornado.curl_httpclient.CurlAsyncHTTPClient")

c.ConfigurableHTTPProxy.api_url = (
    f'http://{get_name("proxy-api")}:{get_name_env("proxy-api", "_SERVICE_PORT")}'
)
# c.ConfigurableHTTPProxy.should_start = False

# Don't wait at all before redirecting a spawning user to the progress page
c.JupyterHub.tornado_settings = {
    "slow_spawn_timeout": 0,
}

jupyterhub_env = os.environ["JUPYTERHUB_ENV"].upper()
jupyterhub_hub_host = "application-hub-hub.jupyter"
jupyterhub_single_user_image = os.environ["JUPYTERHUB_SINGLE_USER_IMAGE_NOTEBOOKS"]

# Authentication
c.LocalAuthenticator.create_system_users = True
c.Authenticator.admin_users = {"jovyan"}
# Deprecated
c.Authenticator.allowed_users = {"jovyan", "alice", "bob"}
c.JupyterHub.authenticator_class = "dummy"

# HTTP Proxy auth token
c.ConfigurableHTTPProxy.auth_token = get_config("proxy.secretToken")
c.JupyterHub.cookie_secret_file = "/srv/jupyterhub/cookie_secret"
# Proxy config
c.JupyterHub.cleanup_servers = False
# Network
c.JupyterHub.allow_named_servers = True
c.JupyterHub.ip = "0.0.0.0"
c.JupyterHub.hub_ip = "0.0.0.0"
c.JupyterHub.hub_connect_ip = jupyterhub_hub_host
# Misc
c.JupyterHub.cleanup_servers = False

# Culling
c.JupyterHub.services = [
    {
        "name": "idle-culler",
        "admin": True,
        "command": [sys.executable, "-m", "jupyterhub_idle_culler", "--timeout=3600"],
    }
]

# Logs
c.JupyterHub.log_level = "DEBUG"

# Spawner
c.JupyterHub.spawner_class = "kubespawner.KubeSpawner"
c.KubeSpawner.environment = {
    "JUPYTER_ENABLE_LAB": "true",
}

c.KubeSpawner.uid = 1001
c.KubeSpawner.fs_gid = 100
c.KubeSpawner.hub_connect_ip = jupyterhub_hub_host

# SecurityContext
c.KubeSpawner.privileged = True
c.KubeSpawner.allow_privilege_escalation = True

# ServiceAccount
c.KubeSpawner.service_account = "default"
c.KubeSpawner.start_timeout = 60 * 15
c.KubeSpawner.image = jupyterhub_single_user_image
c.KubernetesSpawner.verify_ssl = True
c.KubeSpawner.pod_name_template = (
    "jupyter-{username}-{servername}-" + os.environ["JUPYTERHUB_ENV"].lower()
)

# Namespace
c.KubeSpawner.namespace = "jupyter"

# User namespace
c.KubeSpawner.enable_user_namespaces = True
c.KubeSpawner.user_namespace_labels = {"eso": "enabled"}

# Volumes
# volumes are managed by the pre_spawn_hook/post_stop_hook

# TODO - move this value to the values.yaml file
c.KubeSpawner.image_pull_secrets = ["cr-config"]

# custom options form
c.KubeSpawner.options_form = custom_options_form

# hooks
c.KubeSpawner.pre_spawn_hook = pre_spawn_hook
c.KubeSpawner.post_stop_hook = post_stop_hook


async def modify_pod_hook(spawner, pod):
    """Ensure init containers run with the same UID as the main container.

    KubeSpawner applies `uid` only to the main container's securityContext.
    Init containers created by app_hub_context inherit none, so they run as
    the image's default user (UID 1000).  When the workspace volume already
    contains files written by UID 1001 (the spawner UID), those init
    containers cannot chmod/write them — triggering a back-off restart.
    """
    uid = getattr(spawner, "uid", None)
    if uid is not None and pod.spec.init_containers:
        from kubernetes_asyncio.client.models import V1SecurityContext
        for container in pod.spec.init_containers:
            if container.security_context is None:
                container.security_context = V1SecurityContext(run_as_user=uid)
    return pod


c.KubeSpawner.modify_pod_hook = modify_pod_hook

c.JupyterHub.template_paths = [
    "/opt/jupyterhub/template",
    "/usr/local/share/jupyterhub/templates",
]
