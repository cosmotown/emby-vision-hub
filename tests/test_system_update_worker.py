import copy
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import docker

import config_manager
import constants
from services import self_update
from tasks import system_update_worker
from tests.test_transactional_self_update import container_attrs, portainer_attrs


class FakeImage:
    def __init__(self, image_id, platform="linux/amd64"):
        self.id = image_id
        os_name, architecture = platform.split("/")[:2]
        self.attrs = {"Id": image_id, "Os": os_name, "Architecture": architecture, "Config": {"Healthcheck": {"Test": ["CMD", "health"]}}}

    def reload(self):
        return None


class FakeContainer:
    def __init__(self, owner, name, container_id, attrs, status="running"):
        self.owner = owner
        self.name = name
        self.id = container_id
        self.attrs = attrs
        self.status = status
        self.removed = False

    def reload(self):
        return None

    def exec_run(self, command):
        image_id = self.attrs["Image"]
        version = self.owner.versions[image_id]
        output = json.dumps({
            "app_version": version,
            "schema_contract": "evh-7.2-additive-v1",
            "rollback_safe": True,
        }).encode()
        return types.SimpleNamespace(exit_code=0, output=output)

    def stop(self, timeout=30):
        self.status = "exited"
        self.attrs["State"] = {"Status": "exited", "Health": {"Status": "unhealthy"}}

    def rename(self, name):
        self.owner.by_name.pop(self.name, None)
        self.name = name
        self.attrs["Name"] = f"/{name}"
        self.owner.by_name[name] = self

    def start(self):
        self.status = "running"
        self.attrs["State"] = {"Status": "running", "Health": {"Status": "healthy"}}

    def remove(self, force=False):
        self.removed = True
        self.owner.by_name.pop(self.name, None)
        self.owner.by_id.pop(self.id, None)


class FakeContainers:
    def __init__(self, client):
        self.client = client

    def get(self, identity):
        value = self.client.by_name.get(identity) or self.client.by_id.get(identity)
        if value is None:
            raise docker.errors.NotFound("missing")
        return value

    def run(self, **kwargs):
        image = self.client.images.get(kwargs["image"])
        return json.dumps({
            "app_version": self.client.versions[image.id],
            "schema_contract": "evh-7.2-additive-v1",
            "rollback_safe": True,
        }).encode()

    def list(self, all=False, filters=None):
        values = list(self.client.by_id.values())
        labels = (filters or {}).get("label") or []
        for expression in labels:
            key, _, expected = expression.partition("=")
            values = [container for container in values if str(((container.attrs.get("Config") or {}).get("Labels") or {}).get(key)) == expected]
        return values


class FakeImages:
    def __init__(self, client):
        self.client = client

    def get(self, identity):
        image = self.client.image_by_ref.get(identity) or self.client.image_by_id.get(identity)
        if image is None:
            raise docker.errors.ImageNotFound("missing")
        return image


class FakeAPI:
    def __init__(self, client):
        self.client = client
        self.create_count = 0

    def pull(self, image, stream, decode, platform):
        self.client.pull_platform = platform
        yield {"status": "Status: Image is up to date for " + image}

    def create_container_from_config(self, config, name, platform):
        self.create_count += 1
        container_id = f"new-{self.create_count}"
        attrs = {
            "Id": container_id,
            "Image": self.client.images.get(config["Image"]).id,
            "Name": f"/{name}",
            "Config": {key: copy.deepcopy(value) for key, value in config.items() if key not in {"HostConfig", "NetworkingConfig"}},
            "HostConfig": copy.deepcopy(config["HostConfig"]),
            "Mounts": copy.deepcopy(self.client.source_mounts),
            "NetworkSettings": {"Networks": {}},
            "State": {"Status": "created", "Health": {"Status": "starting"}},
        }
        for network_name, endpoint in (config.get("NetworkingConfig") or {}).get("EndpointsConfig", {}).items():
            attrs["NetworkSettings"]["Networks"][network_name] = copy.deepcopy(endpoint)
        container = FakeContainer(self.client, name, container_id, attrs, status="created")
        self.client.add(container)
        return {"Id": container_id}


class FakeClient:
    def __init__(self, source_image_id="sha256:old", target_image_id="sha256:new"):
        self.by_name = {}
        self.by_id = {}
        self.source_image = FakeImage(source_image_id)
        self.target_image = FakeImage(target_image_id)
        self.image_by_id = {source_image_id: self.source_image, target_image_id: self.target_image}
        self.image_by_ref = {"tzyzero186/emby-vision-hub:7.2.32": self.target_image}
        self.versions = {source_image_id: "7.2.31", target_image_id: "7.2.32"}
        self.source_mounts = copy.deepcopy(container_attrs()["Mounts"])
        attrs = container_attrs(source_image_id)
        attrs["State"] = {"Status": "running", "Health": {"Status": "healthy"}}
        self.source = FakeContainer(self, "emby-toolkit", "source-container", attrs)
        self.add(self.source)
        self.containers = FakeContainers(self)
        self.images = FakeImages(self)
        self.api = FakeAPI(self)
        self.pull_platform = None
        self.closed = False

    def add(self, container):
        self.by_name[container.name] = container
        self.by_id[container.id] = container

    def close(self):
        self.closed = True

    def make_portainer_source(self):
        attrs = portainer_attrs(self.source_image.id)
        attrs["Id"] = self.source.id
        attrs["Name"] = f"/{self.source.name}"
        attrs["State"] = {"Status": "running", "Health": {"Status": "healthy"}}
        attrs["Mounts"] = copy.deepcopy(self.source_mounts)
        self.source.attrs = attrs
        return self.source


class SystemUpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"APP_DATA_DIR": self.temporary.name}, clear=False)
        self.environment.start()
        self.config = mock.patch.dict(
            config_manager.APP_CONFIG,
            {constants.CONFIG_OPTION_DOCKER_IMAGE_NAME: "tzyzero186/emby-vision-hub:latest"},
            clear=False,
        )
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def transaction(self, client):
        return self_update.create_transaction(
            source_container_id=client.source.id,
            source_container_name=client.source.name,
            source_image_id=client.source_image.id,
            source_version="7.2.31",
            source_schema_contract="evh-7.2-additive-v1",
            target_version="7.2.32",
            target_image="tzyzero186/emby-vision-hub:7.2.32",
            deployment_type="standalone_docker",
        )

    def portainer_transaction(self, client):
        client.make_portainer_source()
        contract = self_update.validate_deployment_scope(
            client, client.source, client.source.attrs, "portainer_compose"
        )
        return self_update.create_transaction(
            source_container_id=client.source.id,
            source_container_name=client.source.name,
            source_image_id=client.source_image.id,
            source_version="7.2.31",
            source_schema_contract="evh-7.2-additive-v1",
            target_version="7.2.32",
            target_image="tzyzero186/emby-vision-hub:7.2.32",
            deployment_type="portainer_compose",
            deployment_image_reference=contract["image_reference"],
            deployment_identity_fingerprint=contract["identity_fingerprint"],
        )

    def test_portainer_stack_update_preserves_identity_and_pins_compose_image(self):
        client = FakeClient()
        transaction = self.portainer_transaction(client)
        source_labels = copy.deepcopy(client.source.attrs["Config"]["Labels"])
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "SUCCESS")
        candidate = client.by_name["emby-toolkit"]
        labels = candidate.attrs["Config"]["Labels"]
        for key in self_update.COMPOSE_IDENTITY_LABELS:
            self.assertEqual(labels[key], source_labels[key])
        self.assertEqual(labels[self_update.COMPOSE_IMAGE_LABEL], "sha256:new")
        self.assertEqual(
            labels[self_update.MANAGED_IMAGE_REFERENCE_LABEL],
            "tzyzero186/emby-vision-hub:latest",
        )
        self.assertEqual(candidate.attrs["Config"]["Image"], "sha256:new")
        self_update.validate_source_repository(candidate.attrs)
        self.assertEqual(
            self_update.detect_deployment_type(candidate.attrs)[:2],
            ("portainer_compose", True),
        )

    def test_portainer_stack_multiple_instances_fails_before_stop(self):
        client = FakeClient()
        transaction = self.portainer_transaction(client)
        duplicate_attrs = copy.deepcopy(client.source.attrs)
        duplicate_attrs["Id"] = "duplicate"
        duplicate_attrs["Name"] = "/duplicate"
        duplicate = FakeContainer(client, "duplicate", "duplicate", duplicate_attrs)
        client.add(duplicate)
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(client.source.status, "running")
        self.assertEqual(client.api.create_count, 0)

    def test_pull_up_to_date_still_recreates_old_running_image(self):
        client = FakeClient()
        transaction = self.transaction(client)
        api = client.api
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "SUCCESS")
        self.assertEqual(client.api.create_count, 1)
        self.assertEqual(client.pull_platform, "linux/amd64")
        self.assertEqual(client.by_name["emby-toolkit"].attrs["Image"], "sha256:new")
        self.assertTrue(client.source.removed)

    def test_same_image_and_version_does_not_recreate(self):
        client = FakeClient(source_image_id="sha256:same", target_image_id="sha256:same")
        client.versions["sha256:same"] = "7.2.32"
        transaction = self_update.create_transaction(
            source_container_id=client.source.id,
            source_container_name=client.source.name,
            source_image_id="sha256:same",
            source_version="7.2.32",
            source_schema_contract="evh-7.2-additive-v1",
            target_version="7.2.32",
            target_image="tzyzero186/emby-vision-hub:7.2.32",
            deployment_type="standalone_docker",
        )
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "ALREADY_CURRENT")
        self.assertEqual(client.api.create_count, 0)

    def test_target_version_mismatch_fails_before_container_mutation(self):
        client = FakeClient()
        client.versions["sha256:new"] = "7.2.99"
        transaction = self.transaction(client)
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(client.api.create_count, 0)
        self.assertEqual(client.source.status, "running")

    def test_pull_error_on_any_stream_line_fails_before_mutation(self):
        client = FakeClient()
        transaction = self.transaction(client)
        api = client.api

        def failing_pull(*args, **kwargs):
            yield {"errorDetail": {"message": "synthetic pull failure"}}
            yield {"status": "later line must not hide the error"}

        client.api.pull = failing_pull
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["last_error"], "evh_update_pull_failed")
        self.assertEqual(api.create_count, 0)
        self.assertEqual(client.source.status, "running")

    def test_schema_contract_mismatch_fails_before_mutation(self):
        client = FakeClient()
        transaction = self.transaction(client)
        original_inspector = system_update_worker.inspect_image_metadata

        def incompatible(docker_client, image, platform, transaction_id):
            result = original_inspector(docker_client, image, platform, transaction_id)
            if image == "sha256:new":
                result["schema_contract"] = "incompatible-v2"
            return result

        with mock.patch.object(system_update_worker, "inspect_image_metadata", side_effect=incompatible):
            result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(client.api.create_count, 0)
        self.assertEqual(client.source.status, "running")

    def test_health_failure_rolls_back_old_container(self):
        client = FakeClient()
        transaction = self.transaction(client)

        def health(container, timeout=180):
            if container.attrs["Image"] == "sha256:new":
                raise self_update.SelfUpdateError("synthetic health timeout")

        with mock.patch.object(system_update_worker, "wait_for_healthy", side_effect=health):
            result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "ROLLED_BACK")
        self.assertEqual(client.by_name["emby-toolkit"].id, "source-container")
        self.assertEqual(client.by_name["emby-toolkit"].status, "running")
        self.assertNotIn("new-1", client.by_id)

    def test_recreate_failure_rolls_back_old_container(self):
        client = FakeClient()
        transaction = self.transaction(client)
        with mock.patch.object(
            client.api,
            "create_container_from_config",
            side_effect=docker.errors.APIError("synthetic create failure"),
        ):
            result = system_update_worker.run_transaction(
                transaction["transaction_id"], client=client
            )
        self.assertEqual(result["state"], "ROLLED_BACK")
        self.assertEqual(client.by_name["emby-toolkit"].id, "source-container")
        self.assertEqual(client.by_name["emby-toolkit"].status, "running")

    def test_interrupted_after_backup_rename_resumes_without_duplicate_source(self):
        client = FakeClient()
        transaction = self.transaction(client)
        fingerprint = self_update.runtime_config_fingerprint(client.source.attrs)
        backup = self_update.backup_name("emby-toolkit", transaction["transaction_id"])
        self_update.append_transaction_event(
            transaction["transaction_id"],
            "RECREATING",
            "interrupted",
            config_fingerprint_before=fingerprint,
            backup_container_name=backup,
            target_image_id="sha256:new",
            target_platform="linux/amd64",
            target_schema_contract="evh-7.2-additive-v1",
            replace_intent=True,
        )
        client.source.stop()
        client.source.rename(backup)
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "SUCCESS")
        self.assertEqual(client.api.create_count, 1)
        self.assertEqual(client.by_name["emby-toolkit"].attrs["Image"], "sha256:new")

    def test_missing_source_before_commit_is_ambiguous_not_success(self):
        client = FakeClient()
        transaction = self.transaction(client)
        fingerprint = self_update.runtime_config_fingerprint(client.source.attrs)
        backup_name = self_update.backup_name("emby-toolkit", transaction["transaction_id"])
        create_config = self_update.safe_container_config(
            client.source.attrs, "tzyzero186/emby-vision-hub:7.2.32"
        )
        client.source.stop()
        client.source.rename(backup_name)
        response = client.api.create_container_from_config(
            create_config, name="emby-toolkit", platform="linux/amd64"
        )
        target = client.containers.get(response["Id"])
        target.start()
        client.source.remove(force=True)
        self_update.append_transaction_event(
            transaction["transaction_id"],
            "VERIFYING",
            "interrupted after verification",
            config_fingerprint_before=fingerprint,
            backup_container_name=backup_name,
            target_image_id="sha256:new",
            target_schema_contract="evh-7.2-additive-v1",
        )
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "AMBIGUOUS")
        self.assertEqual(client.by_name["emby-toolkit"].id, target.id)
        self.assertEqual(client.api.create_count, 1)

    def test_illegal_target_image_is_rejected_without_mutation(self):
        client = FakeClient()
        transaction = self.transaction(client)
        with self.assertRaises(self_update.SelfUpdateError):
            self_update.update_transaction(transaction["transaction_id"], target_image="attacker/image:latest")
        # Corrupted durable input must also fail closed, independently of the
        # normal writer's immutable-field check.
        transaction["target_image"] = "attacker/image:latest"
        self_update._atomic_write_json(self_update.transaction_path(transaction["transaction_id"]), transaction)
        result = system_update_worker.run_transaction(transaction["transaction_id"], client=client)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(client.api.create_count, 0)
        self.assertFalse(client.source.removed)


if __name__ == "__main__":
    unittest.main()
