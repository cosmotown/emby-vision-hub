"""Opt-in real Docker coverage for self-updater inspect/create normalization."""

import copy
import os
import shutil
import tempfile
import unittest
import uuid

import docker

from services import self_update


@unittest.skipUnless(
    os.environ.get("EVH_RUN_DOCKER_INTEGRATION") == "1",
    "set EVH_RUN_DOCKER_INTEGRATION=1 to use a real Docker daemon",
)
class SelfUpdateDockerIntegrationTests(unittest.TestCase):
    def test_compose_style_source_and_stopped_candidate_are_semantically_equal(self):
        client = docker.from_env()
        run_id = uuid.uuid4().hex[:12]
        source_name = f"evh-updater-source-{run_id}"
        candidate_name = f"evh-updater-candidate-{run_id}"
        network_name = f"evh-updater-network-{run_id}"
        volume_name = f"evh_updater_volume_{run_id}"
        shared_tmp_root = os.environ.get("EVH_DOCKER_HOST_TMP_ROOT")
        host_root = tempfile.mkdtemp(
            prefix=f"evh-updater-{run_id}-",
            dir=shared_tmp_root or None,
        )
        os.makedirs(os.path.join(host_root, "config"), exist_ok=True)
        os.makedirs(os.path.join(host_root, "readonly"), exist_ok=True)
        source = candidate = network = volume = None
        try:
            image_name = os.environ.get("EVH_DOCKER_TEST_IMAGE", "alpine:3.20")
            image = client.images.get(image_name)
            image.reload()
            network = client.networks.create(
                network_name, driver="bridge", labels={"evh.integration": run_id}
            )
            volume = client.volumes.create(
                volume_name, labels={"evh.integration": run_id}
            )
            host_config = client.api.create_host_config(
                binds={
                    os.path.join(host_root, "config"): {"bind": "/config", "mode": "rw"},
                    os.path.join(host_root, "readonly"): {
                        "bind": "/diagnostic-ro",
                        "mode": "ro",
                    },
                    volume_name: {"bind": "/diagnostic-volume", "mode": "rw"},
                },
                port_bindings={"5257/tcp": ("127.0.0.1", "0")},
                restart_policy={"Name": "unless-stopped", "MaximumRetryCount": 0},
                network_mode=network_name,
                init=True,
            )
            networking_config = {
                "EndpointsConfig": {
                    network_name: {
                        "Aliases": ["emby-toolkit", "evh-service"],
                        "IPAMConfig": {},
                    }
                }
            }
            labels = {
                "com.docker.compose.project": "evh-integration",
                "com.docker.compose.service": "emby-toolkit",
                "com.docker.compose.container-number": "1",
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.config-hash": "integration-config-hash",
                "com.docker.compose.project.working_dir": "/opt/evh",
                "com.docker.compose.project.config_files": "/opt/evh/compose.yml",
                "com.docker.compose.image": image.id,
                "io.portainer.accesscontrol.public": "true",
                "io.portainer.stack.name": "evh-integration",
            }
            created = client.api.create_container(
                image=image.id,
                name=source_name,
                command=["sleep", "3600"],
                environment={"APP_VERSION": "integration"},
                labels=labels,
                host_config=host_config,
                networking_config=networking_config,
                ports=["5257/tcp"],
                healthcheck={
                    "test": ["CMD-SHELL", "exit 0"],
                    "interval": 30_000_000_000,
                    "timeout": 5_000_000_000,
                    "retries": 3,
                },
                platform=self_update.image_platform(image.attrs),
            )
            source = client.containers.get(created["Id"])
            source.reload()

            clone = self_update.safe_container_config(
                copy.deepcopy(source.attrs), image.id
            )
            candidate_response = client.api.create_container_from_config(
                clone,
                name=candidate_name,
                platform=self_update.image_platform(image.attrs),
            )
            candidate = client.containers.get(candidate_response["Id"])
            candidate.reload()

            self.assertEqual(source.status, "created")
            self.assertEqual(candidate.status, "created")
            self.assertEqual(
                self_update.runtime_config_projection(source.attrs),
                self_update.runtime_config_projection(candidate.attrs),
            )
            self.assertEqual(
                self_update.runtime_config_fingerprint(source.attrs),
                self_update.runtime_config_fingerprint(candidate.attrs),
            )
        finally:
            for container in (candidate, source):
                if container is not None:
                    try:
                        container.remove(force=True, v=False)
                    except docker.errors.NotFound:
                        pass
            if network is not None:
                try:
                    network.remove()
                except docker.errors.NotFound:
                    pass
            if volume is not None:
                try:
                    volume.remove(force=True)
                except docker.errors.NotFound:
                    pass
            shutil.rmtree(host_root, ignore_errors=True)
            client.close()


if __name__ == "__main__":
    unittest.main()
