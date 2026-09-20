import os
import tempfile
import unittest
from unittest import mock

import config_manager
import constants
import docker
from handler import github
from services import self_update


class SimpleContainer:
    def __init__(self, attrs):
        self.attrs = attrs
        self.id = attrs["Id"]
        self.name = attrs["Name"].lstrip("/")
        self.status = "running"

    def reload(self):
        return None


class SimpleContainers:
    def __init__(self, container):
        self.container = container

    def get(self, identity):
        if identity in {self.container.name, self.container.id, self.container.id[:12]}:
            return self.container
        raise docker.errors.NotFound("missing")

    def list(self, all=False, filters=None):
        return [self.container]


class SimpleClient:
    def __init__(self, attrs):
        self.container = SimpleContainer(attrs)
        self.containers = SimpleContainers(self.container)


def container_attrs(image_id="sha256:old", container_id="a" * 64):
    return {
        "Id": container_id,
        "Image": image_id,
        "Name": "/emby-toolkit",
        "Config": {
            "Image": "tzyzero186/emby-vision-hub:latest",
            "Hostname": container_id[:12],
            "Env": ["A=1", "B=two"],
            "Cmd": ["serve"],
            "Entrypoint": ["/entrypoint.sh"],
            "WorkingDir": "/app",
            "User": "1000:1000",
            "Labels": {"user.label": "preserve"},
            "Healthcheck": {"Test": ["CMD", "curl", "-f", "http://localhost:5257/api/health"]},
            "ExposedPorts": {"5257/tcp": {}},
        },
        "HostConfig": {
            "Binds": ["/host/config:/config:rw", "data:/data:ro", "/var/run/docker.sock:/var/run/docker.sock:rw"],
            "PortBindings": {"5257/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5257"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "evh-net",
            "CapAdd": ["NET_ADMIN"],
            "CapDrop": ["MKNOD"],
            "Devices": [{"PathOnHost": "/dev/null", "PathInContainer": "/dev/null", "CgroupPermissions": "rwm"}],
            "SecurityOpt": ["no-new-privileges"],
            "Dns": ["1.1.1.1"],
            "DnsOptions": ["timeout:2"],
            "DnsSearch": ["example.test"],
            "ExtraHosts": ["example:127.0.0.1"],
            "Privileged": False,
            "ReadonlyRootfs": False,
            "Runtime": "runc",
            "IpcMode": "private",
            "PidMode": "",
            "UTSMode": "",
            "UsernsMode": "",
        },
        "Mounts": [
            {"Type": "bind", "Source": "/host/config", "Destination": "/config", "RW": True, "Mode": "rw", "Propagation": "rprivate"},
            {"Type": "volume", "Name": "data", "Source": "/var/lib/docker/volumes/data/_data", "Destination": "/data", "RW": False, "Mode": "ro", "Propagation": ""},
            {"Type": "volume", "Name": "anonymous", "Source": "/var/lib/docker/volumes/anonymous/_data", "Destination": "/cache", "RW": True, "Mode": "", "Propagation": ""},
            {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True, "Mode": "rw", "Propagation": "rprivate"},
        ],
        "NetworkSettings": {
            "Networks": {
                "evh-net": {
                    "Aliases": [container_id, container_id[:12], "emby-toolkit", "evh-alias"],
                    "Links": None,
                    "IPAMConfig": None,
                    "DriverOpts": None,
                    "GwPriority": 0,
                }
            }
        },
    }


def portainer_attrs(image_id="sha256:old", container_id="a" * 64, image_reference="tzyzero186/emby-vision-hub:latest"):
    attrs = container_attrs(image_id=image_id, container_id=container_id)
    attrs["Config"]["Image"] = image_reference
    attrs["Config"]["Labels"].update({
        "com.docker.compose.project": "evh",
        "com.docker.compose.service": "evh",
        "com.docker.compose.container-number": "1",
        "com.docker.compose.oneoff": "False",
        "com.docker.compose.config-hash": "1" * 64,
        "com.docker.compose.project.working_dir": "/data/compose/42",
        "com.docker.compose.project.config_files": "/data/compose/42/docker-compose.yml",
        "com.docker.compose.image": image_id,
    })
    return attrs


class TransactionalSelfUpdateTests(unittest.TestCase):
    def test_semantic_version_is_strict_and_ordered(self):
        self.assertEqual(self_update.parse_stable_version("v7.2.31"), (7, 2, 31))
        self.assertGreater(self_update.compare_stable_versions("7.10.0", "7.9.99"), 0)
        for value in ("v7.2.31-rc1", "latest", "7.2", "07.2.31"):
            with self.assertRaises(ValueError):
                self_update.parse_stable_version(value)

    def test_latest_release_excludes_draft_prerelease_and_tags(self):
        releases = [
            {"version": "v7.2.32", "draft": True, "prerelease": False, "source": "release"},
            {"version": "v7.2.33", "draft": False, "prerelease": True, "source": "release"},
            {"version": "v7.2.34", "draft": False, "prerelease": False, "source": "tag"},
            {"version": "v7.2.31", "draft": False, "prerelease": False, "source": "release", "published_at": "2026-09-18T00:00:00Z"},
        ]
        self.assertEqual(github.get_latest_stable_release(releases)["version"], "v7.2.31")

    def test_environment_image_and_container_name_have_priority(self):
        with mock.patch.dict(config_manager.APP_CONFIG, {constants.CONFIG_OPTION_DOCKER_IMAGE_NAME: "saved/repo:latest", "container_name": "saved"}, clear=True):
            with mock.patch.dict(os.environ, {constants.ENV_VAR_DOCKER_IMAGE_NAME: "tzyzero186/emby-vision-hub:latest", constants.ENV_VAR_CONTAINER_NAME: "runtime"}):
                self.assertEqual(config_manager.get_docker_image_name(), "tzyzero186/emby-vision-hub:latest")
                self.assertEqual(config_manager.get_container_name(), "runtime")

    def test_deployment_preflight_is_explicit_and_fail_closed(self):
        standalone = container_attrs()
        self.assertEqual(self_update.detect_deployment_type(standalone)[:2], ("standalone_docker", True))
        compose = container_attrs()
        compose["Config"]["Labels"].update({
            "com.docker.compose.project": "evh",
            "com.docker.compose.service": "evh",
            "com.docker.compose.oneoff": "False",
        })
        self.assertEqual(self_update.detect_deployment_type(compose)[:2], ("docker_compose", False))
        compose["Config"]["Image"] = "tzyzero186/emby-vision-hub:7.2.31"
        self.assertEqual(self_update.detect_deployment_type(compose)[:2], ("docker_compose", False))
        for labels, expected in (
            ({"io.portainer.stack.name": "evh"}, "portainer_compose"),
            ({"com.docker.compose.project.working_dir": "/data/compose/42"}, "portainer_compose"),
            ({"com.docker.compose.project.working_dir": "/opt/1panel/apps/evh"}, "1panel"),
            ({"com.docker.swarm.service.name": "evh"}, "swarm"),
            ({"io.kubernetes.container.name": "evh"}, "kubernetes"),
        ):
            attrs = container_attrs()
            attrs["Config"]["Labels"] = labels
            kind, supported, _ = self_update.detect_deployment_type(attrs)
            self.assertEqual(kind, expected)
            self.assertFalse(supported)

        portainer = portainer_attrs()
        self.assertEqual(self_update.detect_deployment_type(portainer)[:2], ("portainer_compose", True))
        portainer["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/srv/lab"
        portainer["Config"]["Labels"]["com.docker.compose.project.config_files"] = "/srv/lab/compose.yml"
        portainer["Config"]["Labels"]["io.portainer.stack.name"] = "evh"
        self.assertEqual(self_update.detect_deployment_type(portainer)[:2], ("portainer_compose", True))
        portainer["Config"]["Image"] = "tzyzero186/emby-vision-hub:7.2.33"
        self.assertEqual(self_update.detect_deployment_type(portainer)[:2], ("portainer_compose", False))

    def test_config_clone_preserves_runtime_contract(self):
        attrs = container_attrs()
        payload = self_update.safe_container_config(attrs, "tzyzero186/emby-vision-hub:7.2.32")
        self.assertEqual(payload["Image"], "tzyzero186/emby-vision-hub:7.2.32")
        self.assertEqual(payload["Env"], attrs["Config"]["Env"])
        self.assertEqual(payload["HostConfig"]["PortBindings"], attrs["HostConfig"]["PortBindings"])
        self.assertEqual(payload["HostConfig"]["RestartPolicy"], attrs["HostConfig"]["RestartPolicy"])
        self.assertIn("anonymous:/cache:rw", payload["HostConfig"]["Binds"])
        self.assertEqual(payload["NetworkingConfig"]["EndpointsConfig"]["evh-net"]["Aliases"], ["evh-alias"])

    def test_runtime_fingerprint_ignores_runtime_identity_only(self):
        before = container_attrs(container_id="a" * 64)
        after = container_attrs(image_id="sha256:new", container_id="b" * 64)
        after["Name"] = "/emby-toolkit"
        after["Config"]["Image"] = "tzyzero186/emby-vision-hub:7.2.32"
        after["Config"]["Hostname"] = "b" * 12
        after["NetworkSettings"]["Networks"]["evh-net"]["Aliases"] = ["b" * 64, "b" * 12, "emby-toolkit", "evh-alias"]
        self.assertEqual(self_update.runtime_config_fingerprint(before), self_update.runtime_config_fingerprint(after))
        after["HostConfig"]["PortBindings"]["5257/tcp"][0]["HostPort"] = "9999"
        self.assertNotEqual(self_update.runtime_config_fingerprint(before), self_update.runtime_config_fingerprint(after))

    def test_config_mount_must_be_one_persistent_rw_mount(self):
        attrs = container_attrs()
        self.assertEqual(self_update.find_config_mount(attrs)["Destination"], "/config")
        attrs["Mounts"][0]["RW"] = False
        with self.assertRaises(self_update.SelfUpdateError):
            self_update.find_config_mount(attrs)

    def test_docker_socket_must_be_an_explicit_rw_bind(self):
        attrs = container_attrs()
        self.assertEqual(self_update.find_docker_socket_mount(attrs)["Source"], "/var/run/docker.sock")
        attrs["Mounts"][-1]["RW"] = False
        with self.assertRaises(self_update.SelfUpdateError):
            self_update.find_docker_socket_mount(attrs)

    def test_update_lock_is_atomic_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"APP_DATA_DIR": directory}):
            first = self_update.create_transaction(
                source_container_id="source",
                source_container_name="emby-toolkit",
                source_image_id="sha256:source",
                source_version="7.2.31",
                source_schema_contract="evh-7.2-additive-v1",
                target_version="7.2.32",
                target_image="tzyzero186/emby-vision-hub:7.2.32",
                deployment_type="standalone_docker",
            )
            with self.assertRaises(self_update.SelfUpdateError):
                self_update.create_transaction(
                    source_container_id="other",
                    source_container_name="emby-toolkit",
                    source_image_id="sha256:other",
                    source_version="7.2.31",
                    source_schema_contract="evh-7.2-additive-v1",
                    target_version="7.2.32",
                    target_image="tzyzero186/emby-vision-hub:7.2.32",
                    deployment_type="standalone_docker",
                )
            self.assertEqual(self_update.get_active_transaction()["transaction_id"], first["transaction_id"])
            self_update.release_active_transaction(first["transaction_id"])

    def test_transaction_status_survives_a_new_reader(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"APP_DATA_DIR": directory}
        ):
            transaction = self_update.create_transaction(
                source_container_id="source",
                source_container_name="emby-toolkit",
                source_image_id="sha256:source",
                source_version="7.2.31",
                source_schema_contract="evh-7.2-additive-v1",
                target_version="7.2.32",
                target_image="tzyzero186/emby-vision-hub:7.2.32",
                deployment_type="standalone_docker",
            )
            self_update.append_transaction_event(
                transaction["transaction_id"], "PULLING", "persisted"
            )
            reloaded = self_update.load_transaction(transaction["transaction_id"])
            self.assertEqual(reloaded["state"], "PULLING")
            self.assertEqual(
                self_update.get_active_transaction()["transaction_id"],
                transaction["transaction_id"],
            )

    def test_missing_worker_is_restarted_from_persisted_transaction(self):
        attrs = container_attrs()
        attrs["Id"] = "source-container"
        client = SimpleClient(attrs)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"APP_DATA_DIR": directory}
        ), mock.patch.object(
            self_update, "worker_container_is_active", return_value=False
        ), mock.patch.object(self_update, "start_worker") as start_worker:
            transaction = self_update.create_transaction(
                source_container_id="source-container",
                source_container_name="emby-toolkit",
                source_image_id="sha256:old",
                source_version="7.2.31",
                source_schema_contract="evh-7.2-additive-v1",
                target_version="7.2.32",
                target_image="tzyzero186/emby-vision-hub:7.2.32",
                deployment_type="standalone_docker",
            )
            recovered = self_update.recover_update_transaction(client)
        self.assertEqual(recovered["transaction_id"], transaction["transaction_id"])
        start_worker.assert_called_once()

    def test_start_derives_exact_official_target_and_accepts_no_client_target(self):
        attrs = container_attrs()
        attrs["Id"] = "source-container"
        attrs["Name"] = "/emby-toolkit"
        client = SimpleClient(attrs)
        release = {
            "version": "v7.2.35",
            "draft": False,
            "prerelease": False,
            "source": "release",
            "published_at": "2026-09-18T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                os.environ,
                {
                    "APP_DATA_DIR": directory,
                    constants.ENV_VAR_CONTAINER_NAME: "emby-toolkit",
                    constants.ENV_VAR_DOCKER_IMAGE_NAME: "tzyzero186/emby-vision-hub:latest",
                },
            ), mock.patch.object(self_update, "assert_self_identity"), mock.patch.object(self_update, "start_worker") as start_worker:
                transaction = self_update.start_update_transaction(client, release)
        self.assertEqual(transaction["target_version"], "7.2.35")
        self.assertEqual(transaction["target_image"], "tzyzero186/emby-vision-hub:7.2.35")
        start_worker.assert_called_once()

    def test_start_accepts_only_strict_single_instance_portainer_latest_stack(self):
        attrs = portainer_attrs(container_id="source-container")
        attrs["Name"] = "/emby-toolkit"
        client = SimpleClient(attrs)
        release = {
            "version": "v7.2.34",
            "draft": False,
            "prerelease": False,
            "source": "release",
            "published_at": "2026-09-20T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "APP_DATA_DIR": directory,
                constants.ENV_VAR_CONTAINER_NAME: "emby-toolkit",
                constants.ENV_VAR_DOCKER_IMAGE_NAME: "tzyzero186/emby-vision-hub:latest",
            },
        ), mock.patch.object(self_update, "assert_self_identity"), mock.patch.object(
            self_update, "start_worker"
        ) as start_worker:
            transaction = self_update.start_update_transaction(client, release)
        self.assertEqual(transaction["deployment_type"], "portainer_compose")
        self.assertEqual(
            transaction["deployment_image_reference"],
            "tzyzero186/emby-vision-hub:latest",
        )
        self.assertRegex(transaction["deployment_identity_fingerprint"], r"^[0-9a-f]{64}$")
        start_worker.assert_called_once()

    def test_portainer_fixed_tag_is_rejected_with_specific_code(self):
        attrs = portainer_attrs(
            container_id="source-container",
            image_reference="tzyzero186/emby-vision-hub:7.2.33",
        )
        client = SimpleClient(attrs)
        release = {
            "version": "v7.2.34",
            "draft": False,
            "prerelease": False,
            "source": "release",
            "published_at": "2026-09-20T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "APP_DATA_DIR": directory,
                constants.ENV_VAR_CONTAINER_NAME: "emby-toolkit",
                constants.ENV_VAR_DOCKER_IMAGE_NAME: "tzyzero186/emby-vision-hub:latest",
            },
        ), mock.patch.object(self_update, "assert_self_identity"), mock.patch.object(
            self_update, "start_worker"
        ) as start_worker:
            with self.assertRaisesRegex(self_update.SelfUpdateError, "evh_update_portainer_image_must_be_latest"):
                self_update.start_update_transaction(client, release)
        start_worker.assert_not_called()

    def test_draft_prerelease_and_downgrade_never_start_worker(self):
        attrs = container_attrs()
        attrs["Id"] = "source-container"
        client = SimpleClient(attrs)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "APP_DATA_DIR": directory,
                constants.ENV_VAR_CONTAINER_NAME: "emby-toolkit",
                constants.ENV_VAR_DOCKER_IMAGE_NAME: "tzyzero186/emby-vision-hub:latest",
            },
        ), mock.patch.object(self_update, "start_worker") as start_worker:
            for release in (
                {"version": "v7.2.32", "draft": True, "prerelease": False, "source": "release"},
                {"version": "v7.2.32-rc1", "draft": False, "prerelease": True, "source": "release"},
                {"version": "v7.2.30", "draft": False, "prerelease": False, "source": "release"},
            ):
                with self.assertRaises((ValueError, self_update.SelfUpdateError)):
                    self_update.start_update_transaction(client, release)
            start_worker.assert_not_called()

    def test_explicit_missing_container_name_does_not_fall_back(self):
        attrs = container_attrs()
        client = SimpleClient(attrs)
        with mock.patch.dict(os.environ, {constants.ENV_VAR_CONTAINER_NAME: "wrong-name"}):
            with self.assertRaises(self_update.SelfUpdateError):
                self_update.resolve_self_container(client)


if __name__ == "__main__":
    unittest.main()
