"""Single-owner EVH replacement. Unknown Docker outcomes never grant ownership."""
from __future__ import annotations

import argparse
import copy
import json
import time
import uuid

import docker
from services import self_update as su

HEALTH_TIMEOUT_SECONDS = 180
HEALTH_POLL_SECONDS = 2


class Ambiguous(su.SelfUpdateError):
    pass


def _get_optional_container(client, identity):
    if not identity:
        return None
    try:
        value = client.containers.get(identity)
        value.reload()
        return value
    except docker.errors.NotFound:
        return None


def _metadata_script():
    return "import json,constants;print(json.dumps({'app_version':constants.APP_VERSION,'schema_contract':constants.SELF_UPDATE_SCHEMA_CONTRACT,'rollback_safe':constants.SELF_UPDATE_BINARY_ROLLBACK_SAFE}))"


def _parse_metadata(output):
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict) and value.get("app_version"):
                return value
        except ValueError:
            pass
    raise su.SelfUpdateError("evh_update_metadata_invalid")


def inspect_image_metadata(client, image, platform, transaction_id):
    tx = su.load_transaction(transaction_id)
    return _parse_metadata(client.containers.run(
        image=image, command=["-c", _metadata_script()], entrypoint=["python"],
        network_mode="none", remove=True, platform=platform,
        tmpfs={"/config": "rw,noexec,nosuid,size=1m"}, mem_limit="256m", nano_cpus=1000000000,
        labels=su.object_labels(tx, "metadata"),
    ))


def inspect_container_metadata(container):
    result = container.exec_run(["python", "-c", _metadata_script()])
    if result.exit_code != 0:
        raise su.SelfUpdateError("evh_update_runtime_metadata_failed")
    return _parse_metadata(result.output)


def _validate_metadata(value, version, contract):
    if (value.get("app_version") != version or value.get("schema_contract") != contract
            or value.get("rollback_safe") is not True):
        raise su.SelfUpdateError("evh_update_metadata_contract_mismatch")


def pull_target_image(client, tx, platform):
    su.append_transaction_event(tx["transaction_id"], "PULLING", "正在拉取正式版本镜像。")
    for line in client.api.pull(tx["target_image"], stream=True, decode=True, platform=platform):
        if not isinstance(line, dict) or line.get("error") or line.get("errorDetail"):
            raise su.SelfUpdateError("evh_update_pull_failed")
    image = client.images.get(tx["target_image"])
    image.reload()
    return image


def _healthy_now(container):
    container.reload()
    state = container.attrs.get("State") or {}
    if state.get("Status") != "running" or state.get("Restarting") or (state.get("Health") or {}).get("Status") != "healthy":
        raise su.SelfUpdateError("evh_update_not_healthy")


def wait_for_healthy(container, timeout=HEALTH_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container.reload()
        su.require_healthcheck(container.attrs)
        state = container.attrs.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        if state.get("Status") == "running" and health == "healthy" and not state.get("Restarting"):
            return
        if health == "unhealthy" or state.get("Status") in {"exited", "dead", "removing", "restarting"} or state.get("Restarting"):
            raise su.SelfUpdateError("evh_update_not_healthy")
        time.sleep(HEALTH_POLL_SECONDS)
    raise su.SelfUpdateError("evh_update_health_timeout")


def _source_attrs(source, tx):
    attrs = copy.deepcopy(source.attrs)
    # Rename does not change network aliases. Normalize only the authorized
    # source object's original name, not arbitrary runtime fields.
    attrs["Name"] = "/" + tx["source_container_name"]
    return attrs


def _verify_target_container(container, tx, fingerprint):
    su.verify_object(container, tx, "candidate")
    _validate_metadata(inspect_container_metadata(container), tx["target_version"], tx["target_schema_contract"])
    _healthy_now(container)
    # All final inspect gates are evaluated from this last observation.
    if container.attrs.get("Image") != tx["target_image_id"]:
        raise su.SelfUpdateError("evh_update_object_image_mismatch")
    if su.runtime_config_fingerprint(container.attrs) != fingerprint:
        raise su.SelfUpdateError("evh_update_config_mismatch")


def _ambiguous(tx, exc):
    return su.append_transaction_event(tx["transaction_id"], "AMBIGUOUS",
        "无法证明容器归属或操作结果；已停止自动操作，请通过原部署方式人工恢复。",
        last_error=su.safe_error(exc), rollback_state="manual_recovery_required", result="ambiguous", completed_at=su.utc_now())


def _rollback(client, tx, cause):
    su.append_transaction_event(tx["transaction_id"], "ROLLING_BACK", "正在验证并恢复原容器。", last_error=su.safe_error(cause))
    try:
        source = _get_optional_container(client, tx["source_container_id"])
        if source is None:
            raise Ambiguous("evh_update_source_missing")
        su.verify_object(source, tx, "source")
        expected = tx.get("config_fingerprint_before")
        if not expected or su.runtime_config_fingerprint(_source_attrs(source, tx)) != expected:
            raise Ambiguous("evh_update_source_config_changed")
        current = _get_optional_container(client, tx["source_container_name"])
        if current is not None and current.id not in {source.id, tx.get("candidate_container_id")}:
            raise Ambiguous("evh_update_name_conflict")
        candidate = _get_optional_container(client, tx.get("candidate_container_id"))
        if candidate is not None:
            su.verify_object(candidate, tx, "candidate")
            candidate.remove(force=True)  # never v=True
        su.verify_object(source, tx, "source")
        if source.name != tx["source_container_name"]:
            source.rename(tx["source_container_name"])
        su.verify_object(source, tx, "source")
        if (source.attrs.get("State") or {}).get("Status") != "running":
            source.start()
        wait_for_healthy(source)
        _validate_metadata(inspect_container_metadata(source), tx["source_version"], tx["source_schema_contract"])
        _healthy_now(source)
        su.verify_object(source, tx, "source")
        if su.runtime_config_fingerprint(_source_attrs(source, tx)) != expected:
            raise Ambiguous("evh_update_rollback_config_mismatch")
        result = su.append_transaction_event(tx["transaction_id"], "ROLLED_BACK", "更新失败，原镜像、版本、配置及健康状态均已恢复。",
            rollback_state="succeeded", result="rolled_back", completed_at=su.utc_now())
        su.release_active_transaction(tx["transaction_id"])
        return result
    except Exception as exc:
        return _ambiguous(su.load_transaction(tx["transaction_id"]), exc)


def _execute(transaction_id, client):
    tx = su.load_transaction(transaction_id)
    try:
        if tx["target_image"] != su.target_image_for_version(tx["target_version"]):
            raise su.SelfUpdateError("evh_update_target_not_official")
        source = _get_optional_container(client, tx["source_container_id"])
        if source is None:
            raise Ambiguous("evh_update_source_missing")
        su.verify_object(source, tx, "source")
        attrs = _source_attrs(source, tx)
        # A previously updated container uses a pinned Config.Image. Source
        # identity is the persisted ID, not that mutable declaration string.
        if not su.detect_deployment_type(attrs)[1]:
            raise su.SelfUpdateError("evh_update_deployment_unsupported")
        su.find_config_mount(attrs)
        su.find_docker_socket_mount(attrs)
        su.require_healthcheck(attrs)
        clone = su.safe_container_config(attrs, tx["source_image_id"])
        fingerprint = su.runtime_config_fingerprint(attrs)
        if tx.get("config_fingerprint_before") and tx["config_fingerprint_before"] != fingerprint:
            raise Ambiguous("evh_update_source_config_changed")
        image = client.images.get(tx["source_image_id"])
        image.reload()
        platform = su.image_platform(image.attrs)
        _validate_metadata(inspect_image_metadata(client, image.id, platform, transaction_id), tx["source_version"], tx["source_schema_contract"])
        if tx.get("target_image_id"):
            target = client.images.get(tx["target_image_id"])
            target.reload()
            if target.id != tx["target_image_id"]:
                raise Ambiguous("evh_update_pinned_image_missing")
        else:
            if tx["state"] != "PREPARING":
                raise su.SelfUpdateError("evh_update_interrupted_before_pin")
            target = pull_target_image(client, tx, platform)
        su.require_healthcheck(target.attrs)
        if su.image_platform(target.attrs) != platform:
            raise su.SelfUpdateError("evh_update_platform_mismatch")
        _validate_metadata(inspect_image_metadata(client, target.id, platform, transaction_id), tx["target_version"], tx["source_schema_contract"])
        if not tx.get("target_image_id"):
            tx = su.update_transaction(transaction_id, target_image_id=target.id,
                target_digest=next((v.split("@", 1)[1] for v in target.attrs.get("RepoDigests", []) if v.startswith(su.OFFICIAL_REPOSITORY + "@")), None),
                target_platform=platform, target_schema_contract=tx["source_schema_contract"], config_fingerprint_before=fingerprint)
            tx = su.append_transaction_event(transaction_id, "TARGET_PINNED", "目标镜像身份已固定。")
        current = _get_optional_container(client, tx["source_container_name"])
        if current is not None and current.id not in {source.id, tx.get("candidate_container_id")}:
            raise Ambiguous("evh_update_name_conflict")
        if target.id == tx["source_image_id"] and tx["source_version"] == tx["target_version"]:
            _validate_metadata(inspect_container_metadata(source), tx["source_version"], tx["source_schema_contract"])
            _healthy_now(source)
            su.verify_object(source, tx, "source")
            if su.runtime_config_fingerprint(_source_attrs(source, tx)) != fingerprint:
                raise su.SelfUpdateError("evh_update_config_mismatch")
            result = su.append_transaction_event(transaction_id, "ALREADY_CURRENT", "当前镜像和版本已经是目标版本。", result="already_current", completed_at=su.utc_now())
            su.release_active_transaction(transaction_id)
            return result
        candidate = _get_optional_container(client, tx.get("candidate_container_id"))
        if tx.get("candidate_container_id") and candidate is None:
            raise Ambiguous("evh_update_candidate_missing")
        if candidate is None:
            if tx.get("create_intent"):
                raise Ambiguous("evh_update_create_outcome_unknown")
            backup_name = su.backup_name(tx["source_container_name"], transaction_id)
            conflict = _get_optional_container(client, backup_name)
            if conflict is not None and conflict.id != source.id:
                raise Ambiguous("evh_update_backup_name_conflict")
            tx = su.append_transaction_event(transaction_id, "RECREATING", "准备替换已验证的源容器。", replace_intent=True, backup_container_name=backup_name)
            su.verify_object(source, tx, "source")
            if su.runtime_config_fingerprint(_source_attrs(source, tx)) != fingerprint:
                raise Ambiguous("evh_update_source_config_changed")
            if source.name == tx["source_container_name"]:
                source.stop(timeout=30)
                su.verify_object(source, tx, "source")
                source.rename(backup_name)
            elif source.name != backup_name:
                raise Ambiguous("evh_update_source_name_changed")
            clone["Image"] = tx["target_image_id"]
            clone["Labels"] = {key: value for key, value in (clone.get("Labels") or {}).items() if key not in su.OWNERSHIP_LABELS}
            clone["Labels"].update(su.object_labels(tx, "candidate"))
            su.update_transaction(transaction_id, create_intent=True)
            try:
                response = client.api.create_container_from_config(clone, name=tx["source_container_name"], platform=platform)
            except Exception:
                # The API may have created the object before losing the reply.
                # A GET can prove a conflict, never prove we own that object.
                if _get_optional_container(client, tx["source_container_name"]) is not None:
                    raise Ambiguous("evh_update_create_outcome_unknown") from None
                raise su.SelfUpdateError("evh_update_create_failed") from None
            tx = su.update_transaction(transaction_id, candidate_container_id=response["Id"])
            candidate = client.containers.get(response["Id"])
        su.verify_object(candidate, tx, "candidate")
        su.require_healthcheck(candidate.attrs)
        if su.runtime_config_fingerprint(candidate.attrs) != fingerprint:
            raise su.SelfUpdateError("evh_update_config_mismatch")
        state = candidate.attrs.get("State") or {}
        if state.get("Status") == "created":
            su.append_transaction_event(transaction_id, "STARTING", "新容器身份与配置已核验，正在启动。")
            su.verify_object(candidate, tx, "candidate")
            candidate.start()
        su.append_transaction_event(transaction_id, "HEALTH_CHECK", "正在等待 Docker 健康检查。")
        wait_for_healthy(candidate)
        su.append_transaction_event(transaction_id, "VERIFYING", "正在执行最终身份、版本、配置和健康核验。")
        _verify_target_container(candidate, tx, fingerprint)
        result = su.append_transaction_event(transaction_id, "SUCCESS", "更新完成，所有提交条件均已通过。", config_fingerprint_after=fingerprint, result="success", completed_at=su.utc_now())
        # Cleanup is explicitly authorized only for the exact verified source.
        # A cleanup failure cannot undo a durable successful commit.
        try:
            su.verify_object(source, tx, "source")
            if source.name == tx["backup_container_name"] and su.runtime_config_fingerprint(_source_attrs(source, tx)) == fingerprint:
                source.remove(force=True)
        except Exception:
            pass
        su.release_active_transaction(transaction_id)
        return result
    except Ambiguous as exc:
        return _ambiguous(su.load_transaction(transaction_id), exc)
    except Exception as exc:
        tx = su.load_transaction(transaction_id)
        if tx.get("state") in su.TERMINAL_STATES:
            return tx
        if tx.get("replace_intent"):
            return _rollback(client, tx, exc)
        result = su.append_transaction_event(transaction_id, "FAILED", "更新在替换源容器之前停止。", last_error=su.safe_error(exc), result="failed", completed_at=su.utc_now())
        su.release_active_transaction(transaction_id)
        return result


def run_transaction(transaction_id, client=None):
    # Take the execution lock before constructing/using a Docker client. No
    # timeout-based stealing: a live owner retains this lock for all actions.
    su.transaction_path(transaction_id)
    try:
        with su.file_lock(f"execution-{transaction_id}.lock", blocking=False):
            tx = su.load_transaction(transaction_id)
            if not tx or tx.get("state") in su.TERMINAL_STATES:
                return tx or {"state": "FAILED"}
            own_client = client is None
            docker_client = client or docker.from_env()
            try:
                if own_client:
                    owner = docker_client.containers.get(tx["worker_container_id"])
                    su.verify_object(owner, tx, "worker")
                    su.assert_self_identity(owner)
                su.update_transaction(transaction_id, worker_id=uuid.uuid4().hex, claimed_at=su.utc_now())
                return _execute(transaction_id, docker_client)
            finally:
                if own_client:
                    docker_client.close()
    except BlockingIOError:
        return {"state": "BUSY", "transaction_id": transaction_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transaction", required=True)
    args = parser.parse_args()
    try:
        run_transaction(args.transaction)
    except Exception:
        # Never emit raw Docker exception text to container logs.
        print("evh_update_worker_stopped_safely")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
