import os
import time
import yaml
from abc import ABC
from http import HTTPStatus
from typing import Dict, Optional, TextIO
from jinja2 import Template

from kubernetes import client, config
from kubernetes.utils import create_from_dict
from kubernetes.client import Configuration
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

from application_hub_context.models import (
    ConfigMapEnvVarReference,
    Role,
    RoleBinding,
    Subject,
    Verb,
)
from application_hub_context.parser import ConfigParser


class ApplicationHubContext(ABC):
    def __init__(
        self,
        namespace,
        spawner,
        config_path: str,
        kubeconfig_file: TextIO = None,
        **kwargs,
    ):
        # k8s
        self.kubeconfig_file = kubeconfig_file
        self.api_client = self._get_api_client(self.kubeconfig_file)
        self.core_v1_api = self._get_core_v1_api()
        self.batch_v1_api = self._get_batch_v1_api()
        self.apps_v1_api = self._get_apps_v1_api()
        self.rbac_authorization_v1_api = self._get_rbac_authorization_v1_api()
        self.custom_objects_api = self._get_custom_objects_api()
        self.namespace = namespace
        self.spawner = spawner
        # get the groups the user belongs to
        self.user_groups = [group.name for group in self.spawner.user.groups]
        # try to get the profile slug (e.g. during a hook call)
        self.profile_slug = self.spawner.user_options.get("profile", None)
        # pod env vars
        self.env_vars = {}

        # loads config
        self.config_parser = ConfigParser.read_file(
            config_path=config_path, user_groups=self.user_groups, spawner=self.spawner, namespace=self.namespace
        )

        # update class dict with kwargs
        self.__dict__.update(kwargs)

    def set_pod_env_vars(self, **kwargs):
        extended_vars = {**self.env_vars, **kwargs}

        for key, value in extended_vars.items():
            if isinstance(value, str):
                # If value is a simple string
                self.spawner.environment[key] = value
            elif isinstance(value, ConfigMapEnvVarReference):
                # If value must be retrieved from an existing configmap
                config_map_name = value.from_config_map.name
                config_map_key = value.from_config_map.key
                try:
                    api_response = self.core_v1_api.read_namespaced_config_map(
                        name=config_map_name, namespace=self.namespace
                    )
                    if config_map_key not in api_response.data:
                        raise KeyError(
                            f"Key '{config_map_key}' not found "
                            f"in config map '{config_map_name}'"
                        )
                    self.spawner.environment[key] = api_response.data[config_map_key]
                except ApiException as e:
                    print("Exception in read_namespaced_config_map: %s\n" % e)
                    raise
                except KeyError as e:
                    print("Configuration error: %s\n" % e)
                    raise

    @staticmethod
    def _get_api_client(kubeconfig_file: TextIO = None):
        try:
            config.load_incluster_config()  # raises if not in cluster
            api_client = client.ApiClient()
            return api_client
        except ConfigException as exception:
            print(exception)

        proxy_url = os.getenv("HTTP_PROXY", None)
        kubeconfig = os.getenv("KUBECONFIG", None)

        if proxy_url:
            api_config = Configuration(host=proxy_url)
            api_config.proxy = proxy_url
            api_client = client.ApiClient(api_config)

        elif kubeconfig:
            # this is needed because kubernetes-python does not consider
            # the KUBECONFIG env variable
            config.load_kube_config(config_file=kubeconfig)
            api_client = client.ApiClient()
        elif kubeconfig_file:
            config.load_kube_config(config_file=kubeconfig_file)
            api_client = client.ApiClient()
        else:
            # if nothing is specified, kubernetes-python will use the file
            # in ~/.kube/config
            config.load_kube_config()
            api_client = client.ApiClient()

        return api_client

    def _get_core_v1_api(self) -> client.CoreV1Api:
        return client.CoreV1Api(api_client=self.api_client)

    def _get_batch_v1_api(self) -> client.BatchV1Api:
        return client.BatchV1Api(api_client=self.api_client)

    def _get_rbac_authorization_v1_api(self) -> client.RbacAuthorizationApi:
        return client.RbacAuthorizationV1Api(self.api_client)

    def _get_apps_v1_api(self) -> client.AppsV1Api:
        return client.AppsV1Api(self.api_client)
    
    def _get_custom_objects_api(self) -> client.CustomObjectsApi:    
        return client.CustomObjectsApi(self.api_client)

    def is_object_created(self, read_method, **kwargs):
        read_methods = {}

        read_methods["read_namespace"] = self.core_v1_api.read_namespace
        read_methods[
            "read_namespaced_role"
        ] = self.rbac_authorization_v1_api.read_namespaced_role  # noqa: E501
        read_methods[
            "read_namespaced_role_binding"
        ] = self.rbac_authorization_v1_api.read_namespaced_role_binding  # noqa: E501

        read_methods[
            "read_namespaced_config_map"
        ] = self.core_v1_api.read_namespaced_config_map  # noqa: E501

        read_methods[
            "read_namespaced_persistent_volume_claim"
        ] = self.core_v1_api.read_namespaced_persistent_volume_claim  # noqa: E501

        read_methods[
            "read_namespaced_secret"
        ] = self.core_v1_api.read_namespaced_secret  # noqa: E501
        try:
            if read_method in [
                "read_namespaced_config_map",
                "read_namespaced_role",
                "read_namespaced_role_binding",
                "read_namespaced_persistent_volume_claim",
                "read_namespaced_secret",
            ]:
                read_methods[read_method](namespace=self.namespace, **kwargs)
            else:
                read_methods[read_method](self.namespace)
        except ApiException as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                return None
            else:
                raise exc
        return read_methods

    def is_namespace_created(self, **kwargs):
        return self.is_object_created("read_namespace", **kwargs)

    def is_namespace_deleted(self, **kwargs):
        """Helper function for retry in dispose"""
        return not self.is_namespace_created()

    def is_role_binding_created(self, **kwargs):
        return self.is_object_created("read_namespaced_role_binding", **kwargs)

    def is_role_created(self, **kwargs):
        return self.is_object_created("read_namespaced_role", **kwargs)

    def is_config_map_created(self, **kwargs):
        return self.is_object_created("read_namespaced_config_map", **kwargs)

    def is_pvc_created(self, **kwargs):
        return self.is_object_created(
            "read_namespaced_persistent_volume_claim", **kwargs
        )  # noqa: E501

    def is_image_pull_secret_created(self, **kwargs):
        return self.is_object_created("read_namespaced_secret", **kwargs)

    def get_profile_list(self):
        pass

    def initialise(self):
        pass

    def dispose(self):
        pass

    def create_namespace(
        self, labels: dict = None, annotations: dict = None
    ) -> client.V1Namespace:

        if labels is None:
            labels = self.spawner.user_namespace_labels
        else:
            labels = {**labels, **self.spawner.user_namespace_labels}

        if self.is_namespace_created():
            self.spawner.log.info(
                f"namespace {self.namespace} exists, skipping creation"
            )
            return self.core_v1_api.read_namespace(name=self.namespace)

        self.spawner.log.info(f"creating namespace {self.namespace}")
        try:
            body = client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=self.namespace, labels=labels, annotations=annotations
                )  # noqa: E501
            )
            response = self.core_v1_api.create_namespace(
                body=body, async_req=False
            )  # noqa: E501

            if not self.retry(self.is_namespace_created):
                raise ApiException(http_resp=response)
            self.spawner.log.info(f"namespace {self.namespace} created")
            return response
        except ApiException as e:
            self.spawner.log.error(f"namespace {self.namespace} creation failed, {e}\n")
            raise e

    @staticmethod
    def retry(fun, max_tries=10, interval=1, **kwargs):
        for i in range(max_tries):
            try:
                time.sleep(interval)
                return fun(**kwargs)
            except ApiException as exc:
                if exc.status.value < 500 and exc.status.value != 429:
                    # Useless to retry against a 4xx/not-429
                    raise exc
            except Exception:
                continue
        if i == max_tries:
            raise ApiException()

    def create_configmap(
        self,
        name,
        key,
        content,
        annotations: Dict = {},
        labels: Dict = {},
    ):
        metadata = client.V1ObjectMeta(
            annotations=annotations,
            deletion_grace_period_seconds=30,
            labels=labels,
            name=name,
            namespace=self.namespace,
        )

        data = {}
        data[key] = content

        config_map = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            data=data,
            metadata=metadata,
        )

        try:
            response = self.core_v1_api.create_namespaced_config_map(
                namespace=self.namespace,
                body=config_map,
                pretty=True,
            )

            if not self.retry(self.is_config_map_created, name=name, interval=2):
                raise ApiException(http_resp=response)
            self.spawner.log.info(f"config map {name} created")
            return response

        except ApiException as e:
            self.spawner.log.info(
                f"config map {name} not created in the time interval assigned"
            )
            raise e

    def create_pvc(
        self,
        name,
        access_modes,
        size,
        storage_class,
        annotations: Optional[Dict[str, str]] = None
    ):
        if self.is_pvc_created(name=name):
            return self.core_v1_api.read_namespaced_persistent_volume_claim(
                name=name, namespace=self.namespace
            )

        metadata = client.V1ObjectMeta(name=name, namespace=self.namespace, annotations=annotations)

        spec = client.V1PersistentVolumeClaimSpec(
            access_modes=access_modes,
            resources=client.V1ResourceRequirements(
                requests={"storage": size}
            ),  # noqa: E501
        )

        spec.storage_class_name = storage_class

        body = client.V1PersistentVolumeClaim(metadata=metadata, spec=spec)

        try:
            response = self.core_v1_api.create_namespaced_persistent_volume_claim(  # noqa: E501
                self.namespace, body, pretty=True
            )

            if not self.retry(self.is_pvc_created, name=name):
                raise ApiException(http_resp=response)
            self.spawner.log.info(f"pvc {name} created")
            return response
        except ApiException as e:
            self.spawner.log.error(
                f"pvc {name} not created in the time interval assigned:"
                f" Exception when calling get status: {e}\n"
            )
            raise e

    def delete_pvc(self, name):
        try:
            response = self.core_v1_api.delete_namespaced_persistent_volume_claim(
                name=name, namespace=self.namespace
            )
            return response
        except ApiException as e:
            self.spawner.log.error(f"Exception deleting pvc {name}: {e}\n")

    def delete_config_map(self, name):
        try:
            response = self.core_v1_api.delete_namespaced_config_map(
                name=name, namespace=self.namespace
            )
            return response
        except ApiException as e:
            self.spawner.log.error(f"Exception deleting config map {name}: {e}\n")

    def delete_role_binding(self, role_binding: RoleBinding):
        try:
            self.rbac_authorization_v1_api.delete_namespaced_role_binding(
                name=role_binding.name, namespace=self.namespace
            )
        except ApiException as e:
            self.spawner.log.error(
                f"Exception deleting role binding {role_binding.name}: {e}\n"
            )

        try:
            self.rbac_authorization_v1_api.delete_namespaced_role(
                name=role_binding.role.name, namespace=self.namespace
            )
        except ApiException as e:
            self.spawner.log.error(
                f"Exception deleting role {role_binding.role.name}: {e}\n"
            )

    def create_role_binding(self, name: str, subjects: list[Subject], role: Role):
        if self.is_role_binding_created(name=name):
            return self.rbac_authorization_v1_api.read_namespaced_role_binding(
                name=name, namespace=self.namespace
            )

        metadata = client.V1ObjectMeta(name=name, namespace=self.namespace)

        role_ref = client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=role.name)

        subject_list = []
        for subject in subjects:
            subject = {
                "kind": subject.kind.value,
                "name": subject.name,
                "namespace": self.namespace,
            }
            subject_list.append(subject)

        body = client.V1RoleBinding(
            metadata=metadata, role_ref=role_ref, subjects=subject_list
        )  # noqa: E501

        try:
            response = self.rbac_authorization_v1_api.create_namespaced_role_binding(  # noqa: E501
                self.namespace, body, pretty=True
            )

            if not self.retry(self.is_role_binding_created, name=name):
                raise ApiException(http_resp=response)
            self.spawner.log.info(f"role binding {name} created")
            return response
        except ApiException as e:
            self.spawner.log.error(
                f"role binding {name} not created in the time interval assigned:"
                f" Exception when calling get status: {e}\n"
            )
            raise e

    def delete_image_pull_secret(self, name):
        try:
            response = self.core_v1_api.delete_namespaced_secret(
                name=name, namespace=self.namespace
            )
            return response
        except ApiException as e:
            self.spawner.log.error(
                f"Exception deleting image pull secret {name}: {e}\n"
            )

    def create_role(
        self,
        name: str,
        verbs: list[Verb],
        resources: list[str] = [""],
        api_groups: list[str] = ["*"],
    ):
        if self.is_role_created(name=name):
            return self.rbac_authorization_v1_api.read_namespaced_role(
                name=name, namespace=self.namespace
            )

        metadata = client.V1ObjectMeta(name=name, namespace=self.namespace)

        rule = client.V1PolicyRule(
            api_groups=api_groups, resources=resources, verbs=verbs
        )

        body = client.V1Role(metadata=metadata, rules=[rule])

        try:
            response = (
                self.rbac_authorization_v1_api.create_namespaced_role(  # noqa: E501
                    self.namespace, body, pretty=True
                )
            )

            if not self.retry(self.is_role_created, name=name):
                raise ApiException(http_resp=response)
            self.spawner.log.info(f"role {name} created")
            return response

        except ApiException as e:
            self.spawner.log.error(
                f"role {name} not created in the time interval assigned: "
                f"Exception when calling get status: {e}\n"
            )
            raise e

    def create_image_pull_secret(self, name: str, data):
        if self.is_image_pull_secret_created(name=name):
            return self.core_v1_api.read_namespaced_secret(
                namespace=self.namespace, name=name
            )  # noqa: E501

        metadata = {"name": name, "namespace": self.namespace}

        secret_data = {".dockerconfigjson": data}

        secret = client.V1Secret(
            api_version="v1",
            data=secret_data,
            kind="Secret",
            metadata=metadata,
            type="kubernetes.io/dockerconfigjson",
        )

        try:
            response = self.core_v1_api.create_namespaced_secret(
                namespace=self.namespace,
                body=secret,
                pretty=True,
            )

            if not self.retry(self.is_image_pull_secret_created, name=name, interval=1):
                raise ApiException(http_resp=response)
            self.spawner.log.info(f"image pull secret {name} created")
            return response

        except ApiException as e:
            self.spawner.log.error(
                f"image pull secret {name} not created " "in the time interval assigned"
            )
            raise e

    def patch_service_account(self, secret_name: str):
        # adds a secret to the namespace default service account

        service_account_body = self.core_v1_api.read_namespaced_service_account(
            name="default", namespace=self.namespace
        )

        self.spawner.log.info(
            "service_account_body.image_pull_secrets "
            f"{service_account_body.image_pull_secrets}"
        )
        self.spawner.log.info(
            f"service_account_body.secrets {service_account_body.secrets}"
        )

        if service_account_body.secrets is None:
            service_account_body.secrets = []

        if service_account_body.image_pull_secrets is None:
            service_account_body.image_pull_secrets = []

        for elem in service_account_body.image_pull_secrets:
            if elem.name == secret_name:
                return

        self.spawner.log.info(f"patching service account with secret {secret_name}")
        service_account_body.secrets.append({"name": secret_name})
        service_account_body.image_pull_secrets.append({"name": secret_name})

        try:
            self.core_v1_api.patch_namespaced_service_account(
                name="default",
                namespace=self.namespace,
                body=service_account_body,
                pretty=True,
            )
        except ApiException as e:
            raise e

    # new function to apply a set of manifests like kubectl apply -f
    # def apply_manifests(self, manifest_file):
    def apply_manifest(self, manifest):

        template = Template(yaml.dump(manifest))
        rendered_manifest = template.render(spawner=self.spawner,
                                            namespace=self.namespace)
        
        self.spawner.log.info(f"Applying manifest name: {yaml.safe_load(rendered_manifest).get('metadata').get('name')}")

        if yaml.safe_load(rendered_manifest)["kind"] in ["Release"]:
            # see https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CustomObjectsApi.md#create_namespaced_custom_object
            # Define the Crossplane HelmRelease details
            group = "helm.crossplane.io" 
            version = "v1beta1"          
            plural = "releases"          

            self.spawner.log.info(f"Creating Crossplane HelmRelease {yaml.safe_load(rendered_manifest).get('metadata').get('name')}")
            
            # Create the Crossplane HelmRelease
            self.custom_objects_api.create_cluster_custom_object(
                group=group,
                version=version,
                plural=plural,
                body=yaml.safe_load(rendered_manifest)
            )
        if yaml.safe_load(rendered_manifest)["kind"] in ["Object"]:
            # see https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CustomObjectsApi.md#create_namespaced_custom_object
            # Define the Crossplane Kubernetes Object details
            group = "kubernetes.crossplane.io" 
            version = "v1alpha2"          
            plural = "objects"          

            self.spawner.log.info(f"Creating Crossplane Kubernetes Object {yaml.safe_load(rendered_manifest).get('metadata').get('name')}")
            
            # Create the Crossplane Kubernetes Object
            self.custom_objects_api.create_cluster_custom_object(
                group=group,
                version=version,
                plural=plural,
                body=yaml.safe_load(rendered_manifest)
            )
        elif yaml.safe_load(rendered_manifest)["kind"] in ["ExternalSecret"]:
            self.spawner.log.info(f"Creating ExternalSecret {yaml.safe_load(rendered_manifest).get('metadata').get('name')} in namespace {self.namespace}")
            # Define the ExternalSecret details
            group = "external-secrets.io"  
            version = "v1beta1"          
            namespace = self.namespace       
            plural = "externalsecrets"

            self.custom_objects_api.create_namespaced_custom_object(
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                body=yaml.safe_load(rendered_manifest)
            )

        elif yaml.safe_load(rendered_manifest)["kind"] in ["Secret"]:

            self.spawner.log.info(f"Creating Secret {yaml.safe_load(rendered_manifest).get('metadata').get('name')} in namespace {self.namespace}")
            create_from_dict(
                k8s_client=self.api_client,
                data=yaml.safe_load(rendered_manifest),
                verbose=True,
                namespace=self.namespace,
            )
        else:
            self.spawner.log.info(f"Creating object of kind {yaml.safe_load(rendered_manifest).get('kind')} name {yaml.safe_load(rendered_manifest).get('metadata').get('name')} in namespace {self.namespace}")
            create_from_dict(
                k8s_client=self.api_client,
                data=yaml.safe_load(rendered_manifest),
                verbose=True,
                namespace=self.namespace,
            )

    def unapply_manifests(self, manifest_content):

        for k8_object in manifest_content:
            kind = k8_object.get("kind")
            self.spawner.log.info(
                f"Deleting {kind} {k8_object.get('metadata', {}).get('name')}"
            )
            metadata = k8_object.get("metadata", {})
            namespace = metadata.get("namespace", self.namespace)
            name = metadata.get("name")

            if not kind or not name:
                continue

            try:
                if kind == "Deployment":
                    self.apps_v1_api.delete_namespaced_deployment(name, namespace)
                elif kind == "Service":
                    self.core_v1_api.delete_namespaced_service(name, namespace)
                elif kind == "Job":
                    self.batch_v1_api.delete_namespaced_job(name, namespace)
                elif kind == "Pod":
                    self.core_v1_api.delete_namespaced_pod(name, namespace)
                elif kind == "Role":
                    self.rbac_authorization_v1_api.delete_namespaced_role(name, namespace)
                elif kind == "RoleBinding":
                    self.rbac_authorization_v1_api.delete_namespaced_role_binding(name, namespace)
                elif kind == "ServiceAccount":
                    self.core_v1_api.delete_namespaced_service_account(name, namespace)
                elif kind == "ConfigMap":
                    self.core_v1_api.delete_namespaced_config_map(name, namespace)    
                elif kind == "Secret":
                    self.core_v1_api.delete_namespaced_secret(name, namespace)
                elif kind == "Release":
                    self.spawner.log.info(f"Deleting Crossplane HelmRelease {name}")
                    response = self.custom_objects_api.delete_cluster_custom_object(
                        group="helm.crossplane.io",
                        version="v1beta1",
                        plural="releases",
                        name=name,
                    )
                    self.spawner.log.info(f"Response: {response}")
                elif kind == "Object":
                    self.spawner.log.info(f"Deleting Crossplane Kubernetes Object {name}")
                    response = self.custom_objects_api.delete_cluster_custom_object(
                        group="kubernetes.crossplane.io",
                        version="v1alpha2",
                        plural="objects",
                        name=name,
                    )
                    self.spawner.log.info(f"Response: {response}")
                elif kind == "ExternalSecret":
                    self.spawner.log.info(f"Deleting ExternalSecret {name} from namespace {namespace}")
                    self.custom_objects_api.delete_namespaced_custom_object(
                        group="external-secrets.io",
                        version="v1beta1",
                        namespace=namespace,
                        plural="externalsecrets",
                        name=name,
                    )
                # Add other kinds as needed
                else:
                    self.spawner.log.error(f"Unsupported kind: {kind}")
            except client.exceptions.ApiException as e:
                self.spawner.log.error(f"An error occurred: {e}")


class DefaultApplicationHubContext(ApplicationHubContext):
    
    def __init__(self, namespace, spawner, config_path: str, kubeconfig_file: TextIO = None, skip_namespace_check=False, **kwargs):
        
        # skip_namespace_check is a flag to skip the namespace check and creation
        self.skip_namespace_check = skip_namespace_check
        
        # add kwargs as members of the class
        self.__dict__.update(kwargs)

        super().__init__(namespace, spawner, config_path, kubeconfig_file, **kwargs)
    
    def get_profile_list(self):
        return self.config_parser.get_profiles()

    # def render(self, manifest):
    #     # render the manifest using the spawner object
    #     template = Template(yaml.dump(manifest))
    #     return yaml.safe_load(template.render(spawner=self.spawner))

    def initialise(self):
        # set the spawner timeout to 10 minutes
        self.spawner.http_timeout = 600

        # get the profile id from the profile definition slug
        profile_id = self.config_parser.get_profile_by_slug(slug=self.profile_slug).id

        self.spawner.log.info(
            f"Initialising {self.profile_slug} (profile id {profile_id})"
        )

        self.spawner.image = self.config_parser.get_profile_by_slug(
            slug=self.profile_slug
        ).definition.kubespawner_override.image

        # pod limits
        self.spawner.cpu_limit = self.config_parser.get_profile_by_slug(
            slug=self.profile_slug
        ).definition.kubespawner_override.cpu_limit
        self.spawner.mem_limit = self.config_parser.get_profile_by_slug(
            slug=self.profile_slug
        ).definition.kubespawner_override.mem_limit
        if (
            self.config_parser.get_profile_by_slug(
                slug=self.profile_slug
            ).definition.kubespawner_override.extra_resource_limits
            is not None
        ):
            self.spawner.extra_resource_limits = self.config_parser.get_profile_by_slug(
                slug=self.profile_slug
            ).definition.kubespawner_override.extra_resource_limits

        # pod guarantees
        cpu_guarantee = self.config_parser.get_profile_by_slug(
            slug=self.profile_slug
        ).definition.kubespawner_override.cpu_guarantee
        if cpu_guarantee is not None:
            self.spawner.cpu_guarantee = cpu_guarantee

        mem_guarantee = self.config_parser.get_profile_by_slug(
            slug=self.profile_slug
        ).definition.kubespawner_override.mem_guarantee
        if cpu_guarantee is not None:
            self.spawner.mem_guarantee = mem_guarantee

        extra_resource_guarantee = self.config_parser.get_profile_by_slug(
            slug=self.profile_slug
        ).definition.kubespawner_override.extra_resource_limits

        if extra_resource_guarantee is not None:
            self.spawner.extra_resource_guarantees = extra_resource_guarantee

        # node selector
        self.spawner.node_selector = self.config_parser.get_profile_by_slug(
            slug=self.profile_slug
        ).node_selector
        self.spawner.log.info(
            f"Initialising pod with image {self.spawner.image} on node pool"
            f"{self.spawner.node_selector}, cpu_limit: "
            f"{self.spawner.cpu_limit}, mem_limit: {self.spawner.mem_limit}"
        )

        # set the pod env vars
        config_env_vars = self.config_parser.get_profile_pod_env_vars(
            profile_id=profile_id
        )
        self.set_pod_env_vars(**(config_env_vars or {}))
        
        if not self.skip_namespace_check:
            self.spawner.log.info(f"Checking namespace {self.namespace}")
            # check the namespace
            if not self.is_namespace_created():
                self.spawner.log.info(f"Creating namespace {self.namespace}")
                self.create_namespace()
        else:    
            self.spawner.log.info(f"Skipping namespace check")

        # process the config maps
        config_maps = self.config_parser.get_profile_config_maps(profile_id=profile_id)

        # process the manifests
        manifests = self.config_parser.get_profile_manifests(profile_id=profile_id)

        if manifests:
            for manifest in manifests:
                self.spawner.log.info(f"Apply manifest {manifest.name}")

                
                for k8_object in manifest.content:
                    try:

                        # Check and log the 'kind' of the Kubernetes object
                        if 'kind' in k8_object:
                            self.spawner.log.info(f"Applying manifest of kind: {k8_object['kind']}")
                            self.apply_manifest(k8_object)  # Apply the manifest
                        else:
                            self.spawner.log.warning(f"Manifest does not contain a 'kind': {k8_object}")

                    except Exception as err:
                        self.spawner.log.error(f"Unexpected {err}, {type(err)}")
                        self.spawner.log.error(
                            f"Skipping creation of manifest {manifest.name}"
                        )


        if config_maps:
            for config_map in config_maps:
                try:
                    if not self.is_config_map_created(name=config_map.name):
                        self.spawner.log.info(f"Creating configmap {config_map.name}")
                        self.create_configmap(
                            name=config_map.name,
                            key=config_map.key,
                            content=config_map.content,
                            annotations=None,
                            labels=None,
                        )
                    if config_map.mount_path is not None:

                        self.spawner.log.info(f"Mounting configmap {config_map.name}")
                        self.spawner.volume_mounts.extend(
                            [
                                {
                                    "name": config_map.name,
                                    "mountPath": config_map.mount_path,
                                    "subPath": config_map.key,
                                },
                            ]
                        )
                        self.spawner.volumes.extend(
                            [
                                {
                                    "name": config_map.name,
                                    "configMap": {
                                        "name": config_map.name,
                                        "defaultMode": int(config_map.default_mode, 8)
                                        if config_map.default_mode
                                        else 0o644,  # noqa: E501
                                    },
                                }
                            ]
                        )
                        self.spawner.log.info(
                            f"Mounted configmap {config_map.name} (key {config_map.key})"  # noqa: E501
                        )
                except Exception as err:
                    self.spawner.log.error(f"Unexpected {err=}, {type(err)=}")
                    self.spawner.log.error(
                        f"Skipping creation of configmap {config_map.name}"
                    )

        #  process the volumes
        volumes = self.config_parser.get_profile_volumes(profile_id=profile_id)

        if volumes:
            for volume in volumes:
                self.spawner.log.info(f"Mounting volume {volume.name}")
                try:
                    if not self.is_pvc_created(name=volume.claim_name):
                        self.spawner.log.info(
                            f"Creating volume claim {volume.claim_name})"
                        )
                        self.create_pvc(
                            name=volume.claim_name,
                            access_modes=volume.access_modes,
                            size=volume.size,
                            storage_class=volume.storage_class,
                            annotations=volume.annotations
                        )
                    self.spawner.log.info(
                        f"Mounting volume {volume.name} (claim {volume.claim_name}))"
                    )
                    self.spawner.volume_mounts.extend(
                        [
                            {
                                "name": volume.volume_mount.name,
                                "mountPath": volume.volume_mount.mount_path,
                            },
                        ]
                    )

                    self.spawner.volumes.extend(
                        [
                            {
                                "name": volume.name,
                                "persistentVolumeClaim": {
                                    "claimName": volume.claim_name
                                },
                            }
                        ]
                    )

                except Exception as err:
                    self.spawner.log.error(f"Unexpected {err}, {type(err)}")
                    self.spawner.log.error(f"Skipping creation of volume {volume.name}")

            # process the role bindings
            role_bindings = self.config_parser.get_profile_role_bindings(
                profile_id=profile_id
            )

            if role_bindings:
                for role_binding in role_bindings:
                    self.spawner.log.info(f"Creating role binding {role_binding.name}")
                    try:
                        # checking if role binding is already created
                        if not self.is_role_binding_created(name=role_binding.name):
                            # checking if role is already created
                            if not self.is_role_created(name=role_binding.role.name):
                                self.spawner.log.info(
                                    f"Creating role {role_binding.role.name}"
                                )
                                self.create_role(
                                    name=role_binding.role.name,
                                    verbs=role_binding.role.verbs,
                                    resources=role_binding.role.resources,
                                    api_groups=role_binding.role.api_groups,
                                )

                            # creating role binding
                            self.create_role_binding(
                                name=role_binding.name,
                                role=role_binding.role,
                                subjects=role_binding.subjects,
                            )

                    except Exception as err:
                        self.spawner.log.error(f"Unexpected {err}, {type(err)}")
                        self.spawner.log.error(
                            f"Skipping creation of role binding {role_binding.name}"
                        )
            # process the role bindings
            image_pull_secrets = self.config_parser.get_profile_image_pull_secrets(
                profile_id=profile_id
            )

            if image_pull_secrets:
                for image_pull_secret in image_pull_secrets:
                    self.spawner.log.info(
                        f"Create image pull secret {image_pull_secret.name}"
                    )
                    try:
                        if not self.is_image_pull_secret_created(
                            name=image_pull_secret.name
                        ):
                            self.spawner.log.info(
                                f"Creating image pull secret {image_pull_secret.name})"
                            )
                            self.create_image_pull_secret(
                                name=image_pull_secret.name,
                                data=image_pull_secret.data,
                            )
                    except Exception as err:
                        self.spawner.log.error(f"Unexpected {err}, {type(err)}")
                        self.spawner.log.error(
                            "Skipping creation of image pull secret"
                            f" {image_pull_secret.name}"
                        )
                    self.spawner.log.info(
                        f"Patch service account with image pull secret {image_pull_secret.name}"
                    )
                    self.patch_service_account(secret_name=image_pull_secret.name)

            # process the init containers
            init_containers = self.config_parser.get_profile_init_containers(
                profile_id=profile_id
            )

            if init_containers:
                for init_container in init_containers:
                    self.spawner.log.info(
                        f"Create init container {init_container.name}"
                    )
                    try:
                        init_container_spec = {
                                    "name": init_container.name,
                                    "image": init_container.image,
                                    "command": init_container.command,
                                    "volumeMounts": [
                                        volume_mount.model_dump(
                                            by_alias=True, exclude_none=True
                                        )
                                        for volume_mount in init_container.volume_mounts
                                    ],
                                }
                        # Inherit the spawner UID so the init container can
                        # write to the workspace volume (owned by that UID).
                        if self.spawner.uid is not None:
                            init_container_spec["securityContext"] = {
                                "runAsUser": self.spawner.uid
                            }
                        self.spawner.init_containers.extend([init_container_spec])
                    except Exception as err:
                        self.spawner.log.error(f"Unexpected {err}, {type(err)}")
                        self.spawner.log.error(
                            f"Skipping creation of init container {init_container.name}"
                        )

            
            # process the pod env vars from config maps
            env_from_config_maps = self.config_parser.get_profile_env_from_config_maps(
                profile_id=profile_id
            )
            self.spawner.log.info(f"env_from_config_maps {env_from_config_maps}")
            if env_from_config_maps:
                self.spawner.extra_container_config["env_from"] = []
            
                for env_from_config_map in env_from_config_maps:
                    self.spawner.log.info(f"env_from_config_map {env_from_config_map}")
                    self.spawner.extra_container_config["env_from"].append(
                            {
                                "configMapRef": {
                                    "name": env_from_config_map,
                                }})
                self.spawner.log.info(f"extra_container_config {self.spawner.extra_container_config}")

            # process the pod env vars from secrets
            env_from_secrets = self.config_parser.get_profile_env_from_secrets(
                profile_id=profile_id
            )
            self.spawner.log.info(f"env_from_secrets {env_from_secrets}")
            if env_from_secrets:
                if self.spawner.extra_container_config.get("env_from") is None:
                    self.spawner.extra_container_config["env_from"] = []

                for env_from_secret in env_from_secrets:
                    self.spawner.log.info(f"env_from_secret {env_from_secret}")
                    self.spawner.extra_container_config["env_from"].append(
                            {
                                "secretRef": {
                                    "name": env_from_secret,
                                }})
                self.spawner.log.info(f"extra_container_config {self.spawner.extra_container_config}")


            secret_mounts = self.config_parser.get_profile_secret_mounts(
                profile_id=profile_id
            )

            if secret_mounts:
                for secret_mount in secret_mounts:
                    self.spawner.log.info(f"Mounting secret {secret_mount.name}")
                    self.spawner.volume_mounts.extend(
                        [
                            {
                                "name": secret_mount.name,
                                "mountPath": secret_mount.mount_path,
                                "subPath": secret_mount.sub_path,
                            },
                        ]
                    )

                    self.spawner.volumes.extend(
                        [
                            {
                                "name": secret_mount.name,
                                "secret": {
                                    "secretName": secret_mount.name,
                                },
                            }
                        ]
                    )

                self.spawner.log.info(f"Mounted secret {secret_mount.name}")

    def dispose(self):
        profile_id = self.config_parser.get_profile_by_slug(slug=self.profile_slug).id

        self.spawner.log.info(
            f"Disposing {self.profile_slug} instance (profile id {profile_id})"
        )

        # process the config maps
        config_maps = self.config_parser.get_profile_config_maps(profile_id=profile_id)
        if config_maps:
            for config_map in config_maps:
                if not config_map.persist:
                    self.spawner.log.info(f"Dispose config map {config_map.name}")

                    self.delete_config_map(name=config_map.name)

        # deal with the volumes
        volumes = self.config_parser.get_profile_volumes(profile_id=profile_id)

        if volumes:
            for volume in volumes:
                if not volume.persist:
                    self.spawner.log.info(
                        f"Dispose volume {volume.name}, claim {volume.claim_name}"
                    )

                    self.delete_pvc(name=volume.claim_name)

        # deal with the role bindings
        role_bindings = self.config_parser.get_profile_role_bindings(
            profile_id=profile_id
        )

        if role_bindings:
            for role_binding in role_bindings:
                if not role_binding.persist:
                    self.spawner.log.info(f"Dispose role binding {role_binding.name}")
                    self.delete_role_binding(role_binding=role_binding)

        # process the manifests
        manifests = self.config_parser.get_profile_manifests(profile_id=profile_id)

        if manifests:
            for manifest in manifests:
                if manifest.persist:
                    self.spawner.log.info(f"Persist manifest {manifest.name}")
                if not manifest.persist:
                    self.spawner.log.info(f"Un-apply manifest {manifest.name}")
                    self.unapply_manifests(manifest_content=manifest.content)

        # deal with the image pull secrets
        image_pull_secrets = self.config_parser.get_profile_image_pull_secrets(
            profile_id=profile_id
        )

        if image_pull_secrets:
            for image_pull_secret in image_pull_secrets:
                if not image_pull_secret.persist:
                    self.spawner.log.info(
                        f"Dispose image pull secret {image_pull_secret.name}"
                    )
                    self.delete_image_pull_secret(name=image_pull_secret.name)

                    service_account_body = (
                        self.core_v1_api.read_namespaced_service_account(
                            name="default", namespace=self.namespace
                        )
                    )
                    for elem in service_account_body.image_pull_secrets:
                        if elem.name == image_pull_secret.name:
                            service_account_body.image_pull_secrets.remove(
                                {"name": elem.name}
                            )

                            self.spawner.log.info(
                                f"Remove image pull secret {image_pull_secret.name}"
                                " from default service account"
                            )

                            try:
                                self.core_v1_api.patch_namespaced_service_account(
                                    name="default",
                                    namespace=self.namespace,
                                    body=service_account_body,
                                    pretty=True,
                                )
                            except ApiException as e:
                                raise e

                            break
