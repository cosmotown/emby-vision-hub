"""Permanent regression for the 15 final-audit counterexamples.

Fake Docker deliberately has no daemon access. Compose's old positive support
expectation is replaced by the explicitly approved external-manager boundary.
"""
import copy
import json
import multiprocessing as mp
import os
import unittest
from contextlib import ExitStack
from unittest import mock

from handler import github
from services import self_update as su
from tasks import system_update_worker as worker
from tests.test_system_update_worker import FakeClient, FakeContainer
from tests import test_system_update_worker as worker_fixtures
from tests.test_transactional_self_update import container_attrs


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = worker_fixtures.SystemUpdateWorkerTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def tx(self, client):
        return self.fixture.transaction(client)

    def test_01_rollback_preserves_same_name_outsider(self):
        client = FakeClient()
        tx = self.tx(client)
        outsider = FakeContainer(client, "emby-toolkit", "outsider", container_attrs(), "exited")
        def collision(*args, **kwargs):
            client.add(outsider)
            raise RuntimeError("create response unknown")
        client.api.create_container_from_config = collision
        result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "AMBIGUOUS")
        self.assertFalse(outsider.removed)
        self.assertFalse(client.source.removed)
        with self.assertRaises(su.SelfUpdateError):
            self.tx(client)

    def test_02_worker_name_collision_does_not_delete(self):
        client = FakeClient()
        tx = self.tx(client)
        outsider = FakeContainer(client, su.worker_name(client.source.name, tx["transaction_id"]), "outsider", container_attrs(), "exited")
        client.add(outsider)
        with self.assertRaises(su.SelfUpdateError):
            su.start_worker(client, tx, client.source)
        self.assertFalse(outsider.removed)

    def test_03_metadata_requires_boolean_true_before_stop(self):
        original = worker.inspect_image_metadata
        for value in ("false", "False", "0", False, "true", 1, None):
            with self.subTest(value=value):
                client = FakeClient()
                tx = self.tx(client)
                def metadata(*args, **kwargs):
                    result = original(*args, **kwargs)
                    result["rollback_safe"] = value
                    return result
                with mock.patch.object(worker, "inspect_image_metadata", side_effect=metadata), mock.patch.object(client.source, "stop") as stop:
                    result = worker.run_transaction(tx["transaction_id"], client)
                self.assertEqual(result["state"], "FAILED")
                self.assertEqual(client.api.create_count, 0)
                stop.assert_not_called()

    def test_04_unhealthy_during_verification_never_success(self):
        client = FakeClient()
        tx = self.tx(client)
        original = worker.inspect_container_metadata
        def metadata(container):
            result = original(container)
            if container.id != client.source.id:
                container.attrs["State"]["Health"]["Status"] = "unhealthy"
            return result
        with mock.patch.object(worker, "inspect_container_metadata", side_effect=metadata):
            result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "ROLLED_BACK")
        self.assertEqual(client.source.status, "running")

    def test_05_recovery_uses_pinned_image_despite_repointed_tag(self):
        client = FakeClient()
        tx = self.tx(client)
        su.update_transaction(tx["transaction_id"], target_image_id="sha256:new", target_platform="linux/amd64", target_schema_contract="evh-7.2-additive-v1", config_fingerprint_before=su.runtime_config_fingerprint(client.source.attrs))
        client.image_by_ref[tx["target_image"]] = client.source_image
        with mock.patch.object(client.api, "pull", side_effect=AssertionError("must not re-pull")):
            result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "SUCCESS")
        self.assertEqual(client.by_name["emby-toolkit"].attrs["Image"], "sha256:new")

    def test_06_create_and_metadata_use_pinned_id(self):
        client = FakeClient()
        tx = self.tx(client)
        with mock.patch.object(client.api, "create_container_from_config", wraps=client.api.create_container_from_config) as create, mock.patch.object(worker, "inspect_image_metadata", wraps=worker.inspect_image_metadata) as inspect:
            result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "SUCCESS")
        self.assertEqual(create.call_args.args[0]["Image"], "sha256:new")
        self.assertEqual([call.args[1] for call in inspect.call_args_list], ["sha256:old", "sha256:new"])

    def test_07_raw_sdk_secrets_never_persisted_or_exposed(self):
        client = FakeClient()
        tx = self.tx(client)
        marker = "synthetic_credential_7b632"
        def failed(*args, **kwargs):
            yield {"error": "X-Emby-Token=" + marker + " api_key=" + marker + " authorization=Bearer " + marker + " cookie=" + marker + " password=" + marker}
        client.api.pull = failed
        result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "FAILED")
        self.assertNotIn(marker, json.dumps(result))
        self.assertNotIn(marker, json.dumps(su.redact_transaction(result)))
        self.assertNotIn(marker, su.transaction_path(tx["transaction_id"]).read_text())
        self.assertNotIn(marker, su.safe_error(RuntimeError(marker)))

    def test_08_compose_incomplete_and_container_network_rejected(self):
        attrs = container_attrs()
        attrs["Config"]["Labels"] = {"com.docker.compose.project": "audit", "com.docker.compose.service": "evh", "com.docker.compose.oneoff": "False"}
        attrs["HostConfig"]["NetworkMode"] = "container:other"
        self.assertFalse(su.detect_deployment_type(attrs)[1])

    def test_09_compose_never_recreated_even_with_complete_labels(self):
        client = FakeClient()
        client.source.attrs["Config"]["Labels"].update({"com.docker.compose.project": "audit", "com.docker.compose.service": "evh", "com.docker.compose.oneoff": "False", "com.docker.compose.project.working_dir": "/lab", "com.docker.compose.project.config_files": "/lab/compose.yml"})
        tx = self.tx(client)
        with mock.patch.object(client.source, "stop") as stop:
            result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "FAILED")
        stop.assert_not_called()
        self.assertEqual(client.api.create_count, 0)

    def test_10_capability_order_does_not_change_fingerprint(self):
        before = container_attrs()
        before["HostConfig"]["CapAdd"] = ["NET_ADMIN", "SYS_NICE"]
        after = copy.deepcopy(before)
        after["HostConfig"]["CapAdd"].reverse()
        self.assertEqual(su.runtime_config_fingerprint(before), su.runtime_config_fingerprint(after))
        after["HostConfig"]["CapAdd"].append("SYS_ADMIN")
        self.assertNotEqual(su.runtime_config_fingerprint(before), su.runtime_config_fingerprint(after))

    def test_11_mounts_volume_not_duplicated_in_binds(self):
        attrs = container_attrs()
        attrs["HostConfig"]["Binds"] = [v for v in attrs["HostConfig"]["Binds"] if not v.startswith("data:")]
        attrs["HostConfig"]["Mounts"] = [{"Type": "volume", "Source": "data", "Target": "/data", "ReadOnly": True}]
        cloned = su.safe_container_config(attrs, "sha256:new")
        self.assertFalse(any(v.startswith("data:/data:") for v in cloned["HostConfig"]["Binds"]))
        self.assertTrue(cloned["HostConfig"]["Mounts"][0]["ReadOnly"])

    def test_12_release_requires_publication_and_explicit_flags(self):
        self.assertIsNone(github.get_latest_stable_release([{"source": "release", "version": "v7.2.32", "published_at": None}]))

    def test_13_wrong_backup_identity_never_adopted(self):
        client = FakeClient()
        tx = self.tx(client)
        backup_name = su.backup_name(client.source.name, tx["transaction_id"])
        outsider = FakeContainer(client, backup_name, "outsider", container_attrs())
        client.add(outsider)
        result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "AMBIGUOUS")
        self.assertFalse(outsider.removed)
        self.assertEqual(client.source.name, "emby-toolkit")

    def test_14_duplicate_workers_only_one_enters_docker(self):
        tx = self.tx(FakeClient())
        ctx = mp.get_context("fork")
        entered, release, queue = ctx.Event(), ctx.Event(), ctx.Queue()
        def first():
            client = FakeClient()
            original = worker.pull_target_image
            def pull(*args):
                entered.set()
                if not release.wait(10):
                    raise AssertionError("release timeout")
                return original(*args)
            with mock.patch.object(worker, "pull_target_image", side_effect=pull):
                queue.put(worker.run_transaction(tx["transaction_id"], client)["state"])
        process = ctx.Process(target=first)
        process.start()
        try:
            self.assertTrue(entered.wait(10))
            client = FakeClient()
            with mock.patch.object(client.containers, "get", side_effect=AssertionError("loser Docker operation")):
                self.assertEqual(worker.run_transaction(tx["transaction_id"], client)["state"], "BUSY")
        finally:
            release.set()
            process.join(15)
            if process.is_alive():
                process.terminate()
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(queue.get(timeout=2), "SUCCESS")

    def test_15_two_processes_terminal_active_lock_only_one_winner(self):
        tx = self.tx(FakeClient())
        su.append_transaction_event(tx["transaction_id"], "FAILED", "old terminal")
        ctx = mp.get_context("fork")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        def child():
            barrier.wait(10)
            try:
                self.tx(FakeClient())
                queue.put("winner")
            except su.SelfUpdateError:
                queue.put("loser")
        processes = [ctx.Process(target=child) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.terminate()
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(queue.get(timeout=2) for _ in processes), ["loser", "winner"])

    def test_strict_environment_boolean(self):
        for value in ("false", "False", "0", False, 0):
            self.assertIs(su.strict_bool(value), False)
        for value in ("true", "1", True, 1):
            self.assertIs(su.strict_bool(value), True)
        for value in ([], {}, None, "yes", 2):
            with self.assertRaises(su.SelfUpdateError):
                su.strict_bool(value)

    def test_missing_healthcheck_fails_before_stop(self):
        client = FakeClient()
        tx = self.tx(client)
        client.target_image.attrs["Config"] = {}
        result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(client.source.status, "running")

    def test_health_unhealthy_does_not_wait_for_timeout(self):
        client = FakeClient()
        client.source.attrs["State"]["Health"]["Status"] = "unhealthy"
        with mock.patch.object(worker.time, "sleep", side_effect=AssertionError("unhealthy must not wait")):
            with self.assertRaises(su.SelfUpdateError):
                worker.wait_for_healthy(client.source)

    def test_unknown_create_response_preserves_object(self):
        client = FakeClient()
        tx = self.tx(client)
        original = client.api.create_container_from_config
        def create(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("response lost")
        client.api.create_container_from_config = create
        result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "AMBIGUOUS")
        self.assertFalse(client.by_name["emby-toolkit"].removed)
        self.assertFalse(client.source.removed)

    def test_pinned_target_fields_and_terminal_states_immutable(self):
        tx = self.tx(FakeClient())
        su.update_transaction(tx["transaction_id"], target_image_id="sha256:new")
        with self.assertRaises(su.SelfUpdateError):
            su.update_transaction(tx["transaction_id"], target_image_id="sha256:other")
        su.append_transaction_event(tx["transaction_id"], "FAILED", "done")
        with self.assertRaises(su.SelfUpdateError):
            su.append_transaction_event(tx["transaction_id"], "PULLING", "late owner")

    def test_resource_change_detected(self):
        attrs = container_attrs()
        before = su.runtime_config_fingerprint(attrs)
        attrs["HostConfig"]["Memory"] = 1024 * 1024 * 512
        self.assertNotEqual(before, su.runtime_config_fingerprint(attrs))

    def test_unsupported_mount_options_rejected(self):
        attrs = container_attrs()
        attrs["HostConfig"]["Mounts"] = [{"Type": "volume", "Target": "/other", "Source": "v", "VolumeOptions": {"Subpath": "x"}}]
        with self.assertRaises(su.SelfUpdateError):
            su.safe_container_config(attrs, "sha256:new")

    def test_crash_recovery_all_seven_boundaries(self):
        for phase in ("after_pull", "before_stop", "after_stop", "after_create", "after_start", "before_health", "before_commit"):
            with self.subTest(phase=phase):
                # Each crash owns an independent persistent config fixture;
                # AMBIGUOUS intentionally prevents any subsequent transaction.
                fixture = worker_fixtures.SystemUpdateWorkerTests()
                fixture.setUp()
                try:
                    client = FakeClient()
                    tx = fixture.transaction(client)
                    def after_call(original):
                        def action(*args, **kwargs):
                            original(*args, **kwargs)
                            raise SystemExit("synthetic process crash")
                        return action
                    with ExitStack() as stack:
                        if phase == "after_pull":
                            stack.enter_context(mock.patch.object(worker, "pull_target_image", side_effect=after_call(worker.pull_target_image)))
                        elif phase == "before_stop":
                            stack.enter_context(mock.patch.object(client.source, "stop", side_effect=SystemExit("crash")))
                        elif phase == "after_stop":
                            stack.enter_context(mock.patch.object(client.source, "stop", side_effect=after_call(client.source.stop)))
                        elif phase == "after_create":
                            stack.enter_context(mock.patch.object(client.api, "create_container_from_config", side_effect=after_call(client.api.create_container_from_config)))
                        elif phase == "after_start":
                            original_start = FakeContainer.start
                            def start(container):
                                original_start(container)
                                if container.id != client.source.id:
                                    raise SystemExit("crash")
                            stack.enter_context(mock.patch.object(FakeContainer, "start", start))
                        elif phase == "before_health":
                            stack.enter_context(mock.patch.object(worker, "wait_for_healthy", side_effect=SystemExit("crash")))
                        else:
                            event = su.append_transaction_event
                            def before_commit(tid, state, *args, **kwargs):
                                if state == "SUCCESS":
                                    raise SystemExit("crash")
                                return event(tid, state, *args, **kwargs)
                            stack.enter_context(mock.patch.object(su, "append_transaction_event", side_effect=before_commit))
                        with self.assertRaises(SystemExit):
                            worker.run_transaction(tx["transaction_id"], client)
                    with mock.patch.object(client.api, "pull", side_effect=AssertionError("recovery cannot resolve mutable tag")):
                        result = worker.run_transaction(tx["transaction_id"], client)
                    expected = "FAILED" if phase == "after_pull" else "AMBIGUOUS" if phase == "after_create" else "SUCCESS"
                    self.assertEqual(result["state"], expected)
                    if expected != "SUCCESS":
                        self.assertFalse(client.source.removed)
                    else:
                        self.assertEqual(result["candidate_container_id"], client.by_name["emby-toolkit"].id)
                finally:
                    fixture.tearDown()

    def test_target_not_rollback_safe_prevents_any_health_or_stop(self):
        client = FakeClient()
        tx = self.tx(client)
        original = worker.inspect_image_metadata
        def metadata(c, image, *args):
            value = original(c, image, *args)
            if image == "sha256:new":
                value["rollback_safe"] = False
            return value
        with mock.patch.object(worker, "inspect_image_metadata", side_effect=metadata), mock.patch.object(worker, "wait_for_healthy", side_effect=AssertionError("must reject before health")), mock.patch.object(client.source, "stop") as stop:
            result = worker.run_transaction(tx["transaction_id"], client)
        self.assertEqual(result["state"], "FAILED")
        stop.assert_not_called()

    def test_clone_readonly_mode_with_propagation(self):
        attrs = container_attrs()
        attrs["HostConfig"]["Binds"][1] = "data:/data:ro,z"
        cloned = su.safe_container_config(attrs, "sha256:new")
        self.assertEqual(sum(":/data:" in value for value in cloned["HostConfig"]["Binds"]), 1)
        self.assertIn("data:/data:ro,z", cloned["HostConfig"]["Binds"])

    def test_release_missing_or_invalid_flags_not_coerced(self):
        for value in (None, "false", 0, [], {}):
            release = {"version": "7.2.32", "source": "release", "draft": value, "prerelease": False, "published_at": "2026-09-18T00:00:00Z"}
            self.assertIsNone(github.get_latest_stable_release([release]))

    def test_claim_is_observable_and_old_owner_cannot_change_terminal(self):
        client = FakeClient()
        tx = self.tx(client)
        result = worker.run_transaction(tx["transaction_id"], client)
        self.assertTrue(result["worker_id"])
        self.assertTrue(result["claimed_at"])
        with self.assertRaises(su.SelfUpdateError):
            su.update_transaction(tx["transaction_id"], worker_id="late-owner")

    def test_running_source_self_identity_must_match_process_namespaces(self):
        client = FakeClient()
        with self.assertRaises(su.SelfUpdateError):
            su.assert_self_identity(client.source)

    def test_api_projection_scrubs_legacy_raw_errors(self):
        result = su.redact_transaction({"last_error": "Authorization=Bearer synthetic", "message": "password=synthetic", "events": [{"message": "api_key=synthetic"}]})
        self.assertNotIn("synthetic", json.dumps(result))


class UpdaterRouteSafetyTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from routes import system
        self.routes = system
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "isolated-updater-test"
        self.app.register_blueprint(system.system_bp)
        self.client = self.app.test_client()
        self.auth = mock.patch.dict(su.config_manager.APP_CONFIG, {"auth_enabled": False})
        self.auth.start()

    def tearDown(self):
        self.auth.stop()

    def test_external_target_is_not_forwarded(self):
        with mock.patch.object(self.routes, "start_system_update", return_value={"state": "PREPARING", "transaction_id": "a" * 32}) as start:
            response = self.client.post("/api/system/update/start", json={"container": "unrelated", "image": "untrusted/repo:latest", "command": "never-run"})
        self.assertEqual(response.status_code, 202)
        start.assert_called_once_with()

    def test_sdk_error_api_and_logs_do_not_echo_credentials(self):
        import docker
        marker = "synthetic_registry_password_api_key_token"
        for error in (docker.errors.APIError(marker), RuntimeError(marker), su.SelfUpdateError(marker)):
            with mock.patch.object(self.routes, "start_system_update", side_effect=error), mock.patch.object(self.routes.logger, "error") as log:
                response = self.client.post("/api/system/update/start")
            self.assertIn(response.status_code, (400, 500, 503))
            self.assertNotIn(marker, response.get_data(as_text=True))
            self.assertNotIn(marker, str(log.call_args_list))

    def test_retired_get_never_starts_update(self):
        with mock.patch.object(self.routes, "start_system_update") as start:
            self.assertEqual(self.client.get("/api/system/update/stream").status_code, 410)
            self.assertEqual(self.client.get("/api/system/update/start").status_code, 405)
        start.assert_not_called()

    def test_authenticated_deployment_rejects_anonymous_update_and_status(self):
        with mock.patch.dict(su.config_manager.APP_CONFIG, {"auth_enabled": True}), mock.patch.object(self.routes, "start_system_update") as start:
            self.assertEqual(self.client.post("/api/system/update/start").status_code, 403)
            self.assertEqual(self.client.get("/api/system/update/status").status_code, 403)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
