"""Fail-closed primitives for the EVH transactional self updater.

This module intentionally manages one EVH container only.  It is not a
general-purpose Docker orchestration API and none of its public entry points
accept a container name or repository from an HTTP client.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import socket
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import docker

import config_manager
import constants


OFFICIAL_REPOSITORY = "tzyzero186/emby-vision-hub"
TRANSACTION_SCHEMA = 1
TERMINAL_STATES = frozenset({"SUCCESS", "ALREADY_CURRENT", "ROLLED_BACK", "FAILED", "AMBIGUOUS"})
CLEANUP_TERMINAL_STATES = frozenset({"SUCCESS", "ALREADY_CURRENT", "ROLLED_BACK", "FAILED"})
ACTIVE_STATES = frozenset({
    "PREPARING",
    "PULLING",
    "RECREATING",
    "STARTING",
    "HEALTH_CHECK",
    "VERIFYING",
    "ROLLING_BACK",
    "TARGET_PINNED",
})
UPDATER_ROLE_LABEL = "com.emby-vision-hub.role"
UPDATER_TARGET_LABEL = "com.emby-vision-hub.target"
UPDATER_TRANSACTION_LABEL = "com.emby-vision-hub.transaction"
UPDATER_ROLE_VALUE = "worker"
UPDATER_SOURCE_LABEL = "com.emby-vision-hub.source-id"
OWNERSHIP_LABELS = frozenset({UPDATER_ROLE_LABEL, UPDATER_TARGET_LABEL, UPDATER_TRANSACTION_LABEL, UPDATER_SOURCE_LABEL})
MANAGED_IMAGE_REFERENCE_LABEL = "com.emby-vision-hub.managed-image-reference"
COMPOSE_IMAGE_LABEL = "com.docker.compose.image"
COMPOSE_IDENTITY_LABELS = (
    "com.docker.compose.project",
    "com.docker.compose.service",
    "com.docker.compose.container-number",
    "com.docker.compose.oneoff",
    "com.docker.compose.config-hash",
    "com.docker.compose.project.working_dir",
    "com.docker.compose.project.config_files",
)
FINGERPRINT_EXCLUDED_LABELS = OWNERSHIP_LABELS | frozenset({
    MANAGED_IMAGE_REFERENCE_LABEL,
    COMPOSE_IMAGE_LABEL,
})
_STABLE_VERSION_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class SelfUpdateError(RuntimeError):
    """A safe, user-facing updater failure."""


def safe_error(exc: Exception) -> str:
    # Never serialize SDK/HTTP exception text, URLs, environment or tracebacks.
    if isinstance(exc, SelfUpdateError) and str(exc).startswith("evh_update_"):
        value = str(exc)
        if re.fullmatch(r"evh_update_[a-z_]+", value):
            return value
    return "evh_update_operation_failed"


SAFE_ERROR_MESSAGES = {
    "evh_update_active_transaction": "已有更新事务正在进行或需要人工恢复。",
    "evh_update_portainer_identity_incomplete": "Portainer Stack 身份信息不完整，未执行更新。",
    "evh_update_portainer_image_must_be_latest": "Portainer Stack 必须声明官方 latest 镜像，才能安全使用内建更新。",
    "evh_update_portainer_multiple_instances": "Portainer Stack 中该 EVH 服务不是单实例，未执行更新。",
    "evh_update_portainer_source_mismatch": "Portainer Stack 服务身份与当前 EVH 容器不一致，未执行更新。",
    "evh_update_deployment_unsupported": "当前部署方式不满足内建更新的安全条件，请使用原部署管理器。",
    "evh_update_config_mount_required": "内建更新需要唯一、可写且持久化的 /config 挂载。",
    "evh_update_docker_socket_required": "内建更新需要可写 Docker socket 挂载。",
    "evh_update_healthcheck_required": "当前 EVH 容器缺少 Docker healthcheck，未执行更新。",
    "evh_update_lock_filesystem_unknown": "无法确认 /config 锁文件系统类型，未执行更新。",
    "evh_update_lock_filesystem_unsupported": "/config 所在文件系统不支持可靠事务锁，未执行更新。",
}


def safe_error_message(exc: Exception) -> str:
    return SAFE_ERROR_MESSAGES.get(safe_error(exc), "无法启动安全更新；当前容器未被修改。")


def strict_bool(value: Any) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if isinstance(value, str) and value.lower() in {"true", "false", "1", "0"}:
        return value.lower() in {"true", "1"}
    raise SelfUpdateError("evh_update_invalid_boolean")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def file_lock(name: str, *, blocking: bool = True):
    root = transaction_root()
    root.mkdir(parents=True, exist_ok=True)
    # flock support is a deployment prerequisite; remote/unknown filesystems
    # are rejected instead of relying on advisory locks across hosts.
    import ctypes
    buffer = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.statfs(os.fsencode(root), buffer) != 0:
        raise SelfUpdateError("evh_update_lock_filesystem_unknown")
    kind = ctypes.c_long.from_buffer(buffer).value & 0xffffffff
    if kind not in {0xef53, 0x58465342, 0x9123683e, 0x794c7630, 0x01021994, 0x2fc12fc1}:
        raise SelfUpdateError("evh_update_lock_filesystem_unsupported")
    path = root / name
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        owner = root.stat()
        if os.geteuid() == 0:
            os.fchown(descriptor, owner.st_uid, owner.st_gid)
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


def object_labels(transaction: Dict[str, Any], role: str) -> Dict[str, str]:
    return {
        UPDATER_ROLE_LABEL: role,
        UPDATER_TRANSACTION_LABEL: transaction["transaction_id"],
        UPDATER_SOURCE_LABEL: transaction["source_container_id"],
        UPDATER_TARGET_LABEL: transaction["source_container_name"],
    }


def verify_object(container, transaction: Dict[str, Any], role: str) -> None:
    container.reload()
    field = "source_container_id" if role == "source" else f"{role}_container_id"
    if container.id != transaction.get(field):
        raise SelfUpdateError("evh_update_object_identity_mismatch")
    attrs = container.attrs or {}
    image = transaction["source_image_id"] if role in {"source", "worker"} else transaction["target_image_id"]
    if attrs.get("Image") != image:
        raise SelfUpdateError("evh_update_object_image_mismatch")
    if role != "source":
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        if any(labels.get(key) != value for key, value in object_labels(transaction, role).items()):
            raise SelfUpdateError("evh_update_object_labels_mismatch")


def require_healthcheck(attrs: Dict[str, Any]) -> None:
    check = ((attrs.get("Config") or {}).get("Healthcheck") or {}).get("Test")
    if not isinstance(check, list) or not check or check[0] not in {"CMD", "CMD-SHELL"}:
        raise SelfUpdateError("evh_update_healthcheck_required")


def assert_self_identity(container) -> None:
    # Names/labels are not proof of being the API process's own container.
    namespaces = ("mnt", "pid", "uts")
    expected = [os.readlink(f"/proc/self/ns/{name}") for name in namespaces]
    result = container.exec_run(["python", "-c", "import os,json;print(json.dumps([os.readlink('/proc/self/ns/'+n) for n in ('mnt','pid','uts')]))"])
    if result.exit_code != 0 or json.loads(result.output) != expected:
        raise SelfUpdateError("evh_update_self_identity_unproven")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_stable_version(value: Any) -> Tuple[int, int, int]:
    match = _STABLE_VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError(f"不是正式稳定语义版本: {value!r}")
    return tuple(int(part) for part in match.groups())


def normalize_stable_version(value: Any) -> str:
    return ".".join(str(part) for part in parse_stable_version(value))


def compare_stable_versions(left: Any, right: Any) -> int:
    left_key = parse_stable_version(left)
    right_key = parse_stable_version(right)
    return (left_key > right_key) - (left_key < right_key)


def split_image_reference(value: str) -> Tuple[str, Optional[str]]:
    reference = str(value or "").strip()
    if not reference:
        return "", None
    if "@" in reference:
        repository, digest = reference.split("@", 1)
        return repository, f"@{digest}"
    last_component = reference.rsplit("/", 1)[-1]
    if ":" in last_component:
        repository, tag = reference.rsplit(":", 1)
        return repository, tag
    return reference, None


def configured_repository() -> str:
    configured = config_manager.get_docker_image_name()
    repository, _ = split_image_reference(configured)
    if repository != OFFICIAL_REPOSITORY:
        raise SelfUpdateError(
            "内建更新器只允许官方 EVH 镜像仓库；当前配置不在允许范围内，未执行容器修改。"
        )
    return repository


def target_image_for_version(version: str) -> str:
    normalized = normalize_stable_version(version)
    return f"{configured_repository()}:{normalized}"


def transaction_root() -> Path:
    base = Path(os.environ.get("APP_DATA_DIR") or config_manager.PERSISTENT_DATA_PATH)
    return base / "system-update"


def transaction_path(transaction_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(transaction_id or "")):
        raise SelfUpdateError("无效的更新事务 ID。")
    return transaction_root() / "transactions" / f"{transaction_id}.json"


def active_transaction_path() -> Path:
    return transaction_root() / "active.json"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ownership_source = path if path.exists() else path.parent
    ownership = ownership_source.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        try:
            os.chown(temporary_name, ownership.st_uid, ownership.st_gid)
        except PermissionError:
            pass
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _with_transaction_lock(transaction_id: str):
    lock_path = transaction_path(transaction_id).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def load_transaction(transaction_id: str) -> Optional[Dict[str, Any]]:
    path = transaction_path(transaction_id)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("transaction_id") != transaction_id:
        raise SelfUpdateError("更新事务状态文件无效。")
    return value


def update_transaction(transaction_id: str, **changes: Any) -> Dict[str, Any]:
    lock_handle = _with_transaction_lock(transaction_id)
    try:
        transaction = load_transaction(transaction_id)
        if transaction is None:
            raise SelfUpdateError("更新事务不存在。")
        if transaction.get("state") in TERMINAL_STATES:
            raise SelfUpdateError("evh_update_terminal_transaction")
        for key in ("target_image_id", "target_digest", "target_version", "target_image", "source_container_id", "source_image_id", "config_fingerprint_before", "deployment_image_reference", "deployment_identity_fingerprint"):
            if key in changes and transaction.get(key) is not None and changes[key] != transaction[key]:
                raise SelfUpdateError("evh_update_immutable_transaction_field")
        transaction.update(changes)
        transaction["updated_at"] = utc_now()
        _atomic_write_json(transaction_path(transaction_id), transaction)
        return transaction
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def append_transaction_event(
    transaction_id: str,
    state: str,
    message: str,
    **changes: Any,
) -> Dict[str, Any]:
    lock_handle = _with_transaction_lock(transaction_id)
    try:
        transaction = load_transaction(transaction_id)
        if transaction is None:
            raise SelfUpdateError("更新事务不存在。")
        if transaction.get("state") in TERMINAL_STATES:
            raise SelfUpdateError("evh_update_terminal_transaction")
        for key in ("target_image_id", "target_digest", "target_version", "target_image", "source_container_id", "source_image_id", "config_fingerprint_before", "deployment_image_reference", "deployment_identity_fingerprint"):
            if key in changes and transaction.get(key) is not None and changes[key] != transaction[key]:
                raise SelfUpdateError("evh_update_immutable_transaction_field")
        events = list(transaction.get("events") or [])
        events.append({"at": utc_now(), "state": state, "message": str(message)[:1000]})
        transaction.update(changes)
        transaction.update({"state": state, "message": str(message)[:1000], "events": events[-100:]})
        transaction["updated_at"] = utc_now()
        _atomic_write_json(transaction_path(transaction_id), transaction)
        return transaction
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def release_active_transaction(transaction_id: str) -> None:
    with file_lock("coordinator.lock"):
        _release_active_transaction(transaction_id)


def _release_active_transaction(transaction_id: str) -> None:
    active_path = active_transaction_path()
    try:
        with active_path.open("r", encoding="utf-8") as handle:
            active = json.load(handle)
    except (FileNotFoundError, ValueError, TypeError):
        return
    if active.get("transaction_id") == transaction_id:
        try:
            active_path.unlink()
            _fsync_directory(active_path.parent)
        except FileNotFoundError:
            pass


def get_active_transaction() -> Optional[Dict[str, Any]]:
    try:
        with active_transaction_path().open("r", encoding="utf-8") as handle:
            active = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(active, dict):
        raise SelfUpdateError("更新锁状态无效。")
    transaction_id = active.get("transaction_id")
    if not transaction_id:
        raise SelfUpdateError("更新锁缺少事务 ID。")
    return load_transaction(transaction_id)


def get_latest_transaction() -> Optional[Dict[str, Any]]:
    directory = transaction_root() / "transactions"
    if not directory.exists():
        return None
    candidates = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        transaction_id = path.stem
        if re.fullmatch(r"[0-9a-f]{32}", transaction_id):
            return load_transaction(transaction_id)
    return None


def create_transaction(
    *,
    source_container_id: str,
    source_container_name: str,
    source_image_id: str,
    source_version: str,
    source_schema_contract: str,
    target_version: str,
    target_image: str,
    deployment_type: str,
    deployment_image_reference: Optional[str] = None,
    deployment_identity_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    with file_lock("coordinator.lock"):
        root = transaction_root()
        root.mkdir(parents=True, exist_ok=True)
        active_path = active_transaction_path()
        if active_path.exists():
            active = get_active_transaction()
            if not active or active.get("state") == "AMBIGUOUS" or active.get("state") not in TERMINAL_STATES:
                raise SelfUpdateError("evh_update_active_transaction")
            try:
                active_path.unlink()
            except FileNotFoundError:
                pass

        transaction_id = uuid.uuid4().hex
        now = utc_now()
        transaction = {
            "schema": TRANSACTION_SCHEMA,
            "transaction_id": transaction_id,
            "source_container_id": source_container_id,
            "source_container_name": source_container_name,
            "source_image_id": source_image_id,
            "source_version": source_version,
            "source_schema_contract": source_schema_contract,
            "target_version": normalize_stable_version(target_version),
            "target_image": target_image,
            "target_image_id": None,
            "target_digest": None,
            "target_repo_digests": [],
            "target_platform": None,
            "target_schema_contract": None,
            "deployment_type": deployment_type,
            "deployment_image_reference": deployment_image_reference,
            "deployment_identity_fingerprint": deployment_identity_fingerprint,
            "state": "PREPARING",
            "message": "更新事务已创建。",
            "last_error": None,
            "rollback_state": "not_started",
            "result": None,
            "config_fingerprint_before": None,
            "config_fingerprint_after": None,
            "backup_container_name": None,
            "candidate_container_id": None,
            "worker_container_id": None,
            "worker_id": None,
            "claimed_at": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "events": [{"at": now, "state": "PREPARING", "message": "更新事务已创建。"}],
        }
        _atomic_write_json(transaction_path(transaction_id), transaction)
        lock_path = transaction_path(transaction_id).with_suffix(".lock")
        lock_path.touch(exist_ok=True)
        os.chmod(lock_path, 0o600)
        try:
            descriptor = os.open(active_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            transaction_path(transaction_id).unlink(missing_ok=True)
            raise SelfUpdateError("evh_update_active_transaction") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"transaction_id": transaction_id, "created_at": now}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(active_path.parent)
        return transaction


def resolve_self_container(client) -> Any:
    explicit_name = str(os.environ.get(constants.ENV_VAR_CONTAINER_NAME) or "").strip()
    if explicit_name:
        try:
            container = client.containers.get(explicit_name)
            container.reload()
            return container
        except docker.errors.NotFound as exc:
            raise SelfUpdateError(
                f"CONTAINER_NAME 指定的 EVH 容器不存在: {explicit_name}"
            ) from exc
    legacy_name = str(config_manager.APP_CONFIG.get("container_name") or "").strip()
    candidates = [value for value in (legacy_name,) if value]

    hostname = socket.gethostname().strip()
    if hostname:
        candidates.append(hostname)
    candidates.extend(["emby-vision-hub", "emby-toolkit"])

    found = []
    seen_ids = set()
    for candidate in candidates:
        try:
            container = client.containers.get(candidate)
        except docker.errors.NotFound:
            continue
        container.reload()
        if container.id not in seen_ids:
            found.append(container)
            seen_ids.add(container.id)
    if hostname:
        for container in client.containers.list(all=True):
            if container.id.startswith(hostname) and container.id not in seen_ids:
                found.append(container)
                seen_ids.add(container.id)

    running = [container for container in found if container.status in {"running", "restarting"}]
    if len(running) == 1:
        return running[0]
    if not running:
        raise SelfUpdateError(
            "无法确定当前 EVH 容器。请显式设置 CONTAINER_NAME；未执行任何容器修改。"
        )
    raise SelfUpdateError(
        "检测到多个可能的 EVH 容器。请显式设置 CONTAINER_NAME；未执行任何容器修改。"
    )


def _portainer_stack_number(labels: Dict[str, Any]) -> Optional[str]:
    values = (
        str(labels.get("com.docker.compose.project.working_dir") or ""),
        str(labels.get("com.docker.compose.project.config_files") or ""),
    )
    matches = []
    for value in values:
        found = re.search(r"(?:^|[,;])\s*/data/compose/([^/,;]+)(?:/|$)", value)
        matches.append(found.group(1) if found else None)
    if matches[0] and matches[0] == matches[1]:
        return matches[0]
    return None


def portainer_stack_contract(attrs: Dict[str, Any]) -> Dict[str, str]:
    config = attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    if any(not str(labels.get(key) or "").strip() for key in COMPOSE_IDENTITY_LABELS):
        raise SelfUpdateError("evh_update_portainer_identity_incomplete")
    explicit_portainer_stack = str(labels.get("io.portainer.stack.name") or "").strip()
    if _portainer_stack_number(labels) is None and not explicit_portainer_stack:
        raise SelfUpdateError("evh_update_portainer_identity_incomplete")
    if str(labels.get("com.docker.compose.oneoff")).lower() != "false":
        raise SelfUpdateError("evh_update_portainer_identity_incomplete")
    if str(labels.get("com.docker.compose.container-number")) != "1":
        raise SelfUpdateError("evh_update_portainer_multiple_instances")
    if not re.fullmatch(r"[0-9a-f]{64}", str(labels.get("com.docker.compose.config-hash") or "")):
        raise SelfUpdateError("evh_update_portainer_identity_incomplete")
    compose_image = str(labels.get(COMPOSE_IMAGE_LABEL) or "")
    if not compose_image.startswith("sha256:"):
        raise SelfUpdateError("evh_update_portainer_identity_incomplete")

    configured_reference = str(config.get("Image") or "").strip()
    if configured_reference.startswith("sha256:"):
        configured_reference = str(labels.get(MANAGED_IMAGE_REFERENCE_LABEL) or "").strip()
    repository, tag = split_image_reference(configured_reference)
    if repository != OFFICIAL_REPOSITORY or tag != "latest":
        raise SelfUpdateError("evh_update_portainer_image_must_be_latest")

    identity = {key: str(labels[key]) for key in COMPOSE_IDENTITY_LABELS}
    identity_payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "image_reference": f"{OFFICIAL_REPOSITORY}:latest",
        "identity_fingerprint": hashlib.sha256(identity_payload.encode("utf-8")).hexdigest(),
        "project": identity["com.docker.compose.project"],
        "service": identity["com.docker.compose.service"],
    }


def validate_deployment_scope(client, source_container: Any, attrs: Dict[str, Any], deployment_type: str) -> Dict[str, str]:
    if deployment_type != "portainer_compose":
        return {}
    contract = portainer_stack_contract(attrs)
    filters = {"label": [
        f"com.docker.compose.project={contract['project']}",
        f"com.docker.compose.service={contract['service']}",
    ]}
    matches = []
    for container in client.containers.list(all=True, filters=filters):
        container.reload()
        labels = ((container.attrs or {}).get("Config") or {}).get("Labels") or {}
        if (str(labels.get("com.docker.compose.project")) == contract["project"]
                and str(labels.get("com.docker.compose.service")) == contract["service"]):
            matches.append(container)
    if len(matches) != 1:
        raise SelfUpdateError("evh_update_portainer_multiple_instances")
    if matches[0].id != source_container.id:
        raise SelfUpdateError("evh_update_portainer_source_mismatch")
    return contract


def detect_deployment_type(attrs: Dict[str, Any]) -> Tuple[str, bool, str]:
    labels = ((attrs.get("Config") or {}).get("Labels") or {})
    label_keys = {str(key).lower() for key in labels}
    label_values = {str(value).lower() for value in labels.values() if value is not None}
    if any(key.startswith("io.kubernetes.") or key.startswith("annotation.kubernetes.") for key in label_keys):
        return "kubernetes", False, "Kubernetes 管理的容器不支持内建替换。"
    if "com.docker.swarm.service.name" in label_keys or "com.docker.stack.namespace" in label_keys:
        return "swarm", False, "Docker Swarm/Stack 管理的容器不支持内建替换。"
    compose_paths = [
        str(labels.get("com.docker.compose.project.working_dir") or "").lower(),
        str(labels.get("com.docker.compose.project.config_files") or "").lower(),
    ]
    if (any(re.search(r"(?:^|/|:)data/compose(?:/|$)", value) for value in compose_paths)
            or str(labels.get("io.portainer.stack.name") or "").strip()):
        try:
            portainer_stack_contract(attrs)
        except SelfUpdateError as exc:
            return "portainer_compose", False, safe_error_message(exc)
        return "portainer_compose", True, "受严格约束的单实例 Portainer Compose Stack。"
    if any("portainer" in value for value in (*label_keys, *label_values)):
        return "portainer", False, "Portainer 非 Compose Stack 部署不支持内建替换。"
    if any(
        key.startswith("com.1panel.") or key.startswith("io.1panel.")
        for key in label_keys
    ) or any("1panel" in value for value in label_values):
        return "1panel", False, "1Panel 管理标记已存在，请通过 1Panel 升级。"
    if any("nomad" in value for value in (*label_keys, *label_values)):
        return "nomad", False, "Nomad 管理的容器不支持内建替换。"

    if any(key.startswith("com.docker.compose.") for key in label_keys):
        return "docker_compose", False, "Compose 部署请通过原 Compose/管理器更新；内建更新不会替换受管理容器。"
    host_config = attrs.get("HostConfig") or {}
    if str(host_config.get("NetworkMode") or "").startswith("container:"):
        return "standalone_container_network", False, "container: 网络模式无法安全重建。"
    return "standalone_docker", True, "独立 Docker 容器。"


def find_config_mount(attrs: Dict[str, Any]) -> Dict[str, Any]:
    matches = [mount for mount in (attrs.get("Mounts") or []) if mount.get("Destination") == "/config"]
    if len(matches) != 1 or not matches[0].get("RW"):
        raise SelfUpdateError("evh_update_config_mount_required")
    mount = matches[0]
    if mount.get("Type") not in {"bind", "volume"}:
        raise SelfUpdateError("evh_update_config_mount_required")
    return mount


def find_docker_socket_mount(attrs: Dict[str, Any]) -> Dict[str, Any]:
    matches = [
        mount for mount in (attrs.get("Mounts") or [])
        if mount.get("Destination") == "/var/run/docker.sock"
    ]
    if len(matches) != 1 or not matches[0].get("RW") or matches[0].get("Type") != "bind":
        raise SelfUpdateError("evh_update_docker_socket_required")
    return matches[0]


def image_platform(image_attrs: Dict[str, Any]) -> str:
    os_name = str(image_attrs.get("Os") or "linux")
    architecture = str(image_attrs.get("Architecture") or "").strip()
    variant = str(image_attrs.get("Variant") or "").strip()
    if not architecture:
        raise SelfUpdateError("无法确定当前 EVH 镜像架构。")
    return f"{os_name}/{architecture}" + (f"/{variant}" if variant else "")


def safe_container_config(attrs: Dict[str, Any], target_image: str) -> Dict[str, Any]:
    """Build a Docker create payload from the current EVH inspect result."""
    config = copy.deepcopy(attrs.get("Config") or {})
    host_config = copy.deepcopy(attrs.get("HostConfig") or {})
    old_id = str(attrs.get("Id") or "")
    if config.get("Hostname") in {old_id, old_id[:12]}:
        config["Hostname"] = ""
    config["Image"] = target_image

    existing_destinations = set()
    for mount in host_config.get("Mounts") or []:
        if mount.get("Type") not in {"bind", "volume"} or set(mount) - {"Type", "Source", "Target", "ReadOnly", "Consistency", "BindOptions"}:
            raise SelfUpdateError("evh_update_mount_options_unsupported")
        if set(mount.get("BindOptions") or {}) - {"Propagation"}:
            raise SelfUpdateError("evh_update_mount_options_unsupported")
        destination = mount.get("Target")
        if not destination or destination in existing_destinations:
            raise SelfUpdateError("evh_update_mount_conflict")
        existing_destinations.add(destination)
    if host_config.get("Tmpfs"):
        raise SelfUpdateError("evh_update_tmpfs_unsupported")
    for bind in host_config.get("Binds") or []:
        parts = str(bind).split(":")
        if len(parts) not in (2, 3):
            raise SelfUpdateError("evh_update_mount_options_unsupported")
        modes = set(parts[2].split(",")) if len(parts) == 3 else set()
        if modes - {"ro", "rw", "z", "Z", "rprivate", "rshared", "rslave", "private", "shared", "slave"}:
            raise SelfUpdateError("evh_update_mount_options_unsupported")
        if parts[1] in existing_destinations:
            raise SelfUpdateError("evh_update_mount_conflict")
        existing_destinations.add(parts[1])
    binds = list(host_config.get("Binds") or [])
    for mount in attrs.get("Mounts") or []:
        if mount.get("Type") not in {"bind", "volume"}:
            raise SelfUpdateError("evh_update_mount_type_unsupported")
        if mount.get("Type") != "volume":
            continue
        destination = mount.get("Destination")
        name = mount.get("Name")
        if destination and name and destination not in existing_destinations:
            binds.append(f"{name}:{destination}:{'rw' if mount.get('RW') else 'ro'}")
            existing_destinations.add(destination)
    host_config["Binds"] = binds or None

    endpoint_config = {}
    generated_aliases = {old_id, old_id[:12], str(attrs.get("Name") or "").lstrip("/")}
    for network_name, network in ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).items():
        endpoint = {}
        aliases = [alias for alias in (network.get("Aliases") or []) if alias not in generated_aliases]
        if aliases:
            endpoint["Aliases"] = aliases
        if network.get("IPAMConfig") or network.get("Links"):
            raise SelfUpdateError("evh_update_static_network_unsupported")
        for key in ("DriverOpts", "GwPriority"):
            value = network.get(key)
            if value not in (None, "", [], {}):
                endpoint[key] = value
        endpoint_config[network_name] = endpoint

    config["HostConfig"] = host_config
    if endpoint_config:
        config["NetworkingConfig"] = {"EndpointsConfig": endpoint_config}
    return config


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def runtime_config_projection(attrs: Dict[str, Any]) -> Dict[str, Any]:
    config = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    mounts = []
    for mount in attrs.get("Mounts") or []:
        mounts.append({
            "Type": mount.get("Type"),
            "Source": mount.get("Source") if mount.get("Type") == "bind" else mount.get("Name"),
            "Destination": mount.get("Destination"),
            "RW": bool(mount.get("RW")),
            "Mode": sorted(set(str(mount.get("Mode") or "").split(",")) - {"", "ro", "rw"}),
            "Propagation": mount.get("Propagation") or "",
        })
    networks = {}
    generated = {str(attrs.get("Id") or ""), str(attrs.get("Id") or "")[:12], str(attrs.get("Name") or "").lstrip("/")}
    for name, network in ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).items():
        # Docker Compose records an unspecified endpoint IPAM contract as {},
        # while a semantically identical raw Docker create is inspected as
        # null.  Only normalize the empty form; non-empty static IPAM remains
        # observable (and is rejected by safe_container_config()).
        ipam_config = network.get("IPAMConfig") or None
        networks[name] = {
            "Aliases": sorted(alias for alias in (network.get("Aliases") or []) if alias not in generated),
            "Links": network.get("Links") or [],
            "IPAMConfig": ipam_config,
            "DriverOpts": network.get("DriverOpts"),
            "GwPriority": network.get("GwPriority") or 0,
        }
    labels = {key: value for key, value in (config.get("Labels") or {}).items() if key not in FINGERPRINT_EXCLUDED_LABELS}
    projection = {
        "Env": sorted(config.get("Env") or []),
        "Cmd": config.get("Cmd"),
        "Entrypoint": config.get("Entrypoint"),
        "WorkingDir": config.get("WorkingDir") or "",
        "User": config.get("User") or "",
        "Healthcheck": config.get("Healthcheck"),
        "Labels": labels,
        "ExposedPorts": config.get("ExposedPorts"),
        "StopSignal": config.get("StopSignal"),
        "StopTimeout": config.get("StopTimeout"),
        "Mounts": sorted(mounts, key=lambda item: (str(item["Destination"]), str(item["Source"]))),
        "PortBindings": host.get("PortBindings"),
        "RestartPolicy": host.get("RestartPolicy"),
        "NetworkMode": host.get("NetworkMode"),
        "CapAdd": sorted(host.get("CapAdd") or []),
        "CapDrop": sorted(host.get("CapDrop") or []),
        "Devices": sorted(host.get("Devices") or [], key=lambda item: json.dumps(item, sort_keys=True)),
        "SecurityOpt": sorted(host.get("SecurityOpt") or []),
        "Dns": host.get("Dns") or [],
        "DnsOptions": host.get("DnsOptions") or [],
        "DnsSearch": host.get("DnsSearch") or [],
        "ExtraHosts": host.get("ExtraHosts") or [],
        "Privileged": bool(host.get("Privileged")),
        "ReadonlyRootfs": bool(host.get("ReadonlyRootfs")),
        "Runtime": host.get("Runtime") or "",
        "IpcMode": host.get("IpcMode") or "",
        "PidMode": host.get("PidMode") or "",
        "UTSMode": host.get("UTSMode") or "",
        "UsernsMode": host.get("UsernsMode") or "",
        "Networks": networks,
        "Hostname": "" if config.get("Hostname") in {str(attrs.get("Id") or ""), str(attrs.get("Id") or "")[:12]} else (config.get("Hostname") or ""),
        "Domainname": config.get("Domainname") or "",
    }
    # Preserve and verify the rest of HostConfig, including resources, devices,
    # groups and sysctls. Mount declaration spelling is checked separately.
    projected = set(projection) | {"Binds", "Mounts", "Tmpfs"}
    projection["OtherHostConfig"] = {key: value for key, value in host.items() if key not in projected and value not in (None, [], {}, "", 0, False)}
    projection["MountOptions"] = sorted(host.get("Mounts") or [], key=lambda item: item.get("Target", ""))
    projection["PortBindings"] = {key: sorted(value or [], key=lambda item: (item.get("HostIp") or "", item.get("HostPort") or "")) for key, value in (host.get("PortBindings") or {}).items()}
    return _canonical(projection)


def runtime_config_fingerprint(attrs: Dict[str, Any]) -> str:
    payload = json.dumps(runtime_config_projection(attrs), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def worker_name(container_name: str, transaction_id: Optional[str] = None) -> str:
    suffix = f"-{transaction_id[:12]}" if transaction_id else ""
    return f"{container_name}-evh-self-updater{suffix}"


def backup_name(container_name: str, transaction_id: str) -> str:
    return f"{container_name}-evh-rollback-{transaction_id[:12]}"


def _mount_for_worker(config_mount: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    source = config_mount.get("Source") if config_mount.get("Type") == "bind" else config_mount.get("Name")
    if not source:
        raise SelfUpdateError("无法解析 EVH /config 挂载来源。")
    return {str(source): {"bind": "/config", "mode": "rw"}}


def start_worker(client, transaction: Dict[str, Any], source_container: Any) -> Any:
    with file_lock("worker-launch.lock"):
        return _start_worker(client, transaction, source_container)


def _start_worker(client, transaction: Dict[str, Any], source_container: Any) -> Any:
    transaction = load_transaction(transaction["transaction_id"])
    if transaction.get("state") in TERMINAL_STATES:
        return None
    verify_object(source_container, transaction, "source")
    attrs = source_container.attrs or {}
    config_mount = find_config_mount(attrs)
    socket_mount = find_docker_socket_mount(attrs)
    name = worker_name(transaction["source_container_name"], transaction["transaction_id"])
    try:
        existing = client.containers.get(name)
        existing.reload()
        verify_object(existing, transaction, "worker")
        if existing.status in {"running", "restarting", "created"}:
            raise SelfUpdateError("已有 EVH 更新 worker 正在运行。")
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass

    image = client.images.get(transaction["source_image_id"])
    platform = image_platform(image.attrs or {})
    labels = object_labels(transaction, "worker")
    try:
        # Create and persist ID before starting; response loss is ambiguous,
        # never grounds to delete a same-name object.
        created = client.containers.create(
            image=transaction["source_image_id"],
            name=name,
            command=["-m", "tasks.system_update_worker", "--transaction", transaction["transaction_id"]],
            entrypoint=["python"],
            detach=True,
            network_mode="none",
            restart_policy={"Name": "no"},
            labels=labels,
            environment={"APP_DATA_DIR": "/config", "CONFIG_DIR": "/config"},
            volumes={
                **_mount_for_worker(config_mount),
                str(socket_mount["Source"]): {"bind": "/var/run/docker.sock", "mode": "rw"},
            },
            platform=platform,
            mem_limit="512m",
            nano_cpus=1000000000,
        )
        update_transaction(transaction["transaction_id"], worker_container_id=created.id)
        created.start()
        return created
    except docker.errors.APIError as exc:
        raise SelfUpdateError("evh_update_worker_launch_failed") from None


def redact_transaction(transaction: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "transaction_id",
        "source_container_id",
        "source_image_id",
        "source_version",
        "target_version",
        "target_image",
        "target_image_id",
        "target_digest",
        "target_repo_digests",
        "target_platform",
        "deployment_type",
        "state",
        "message",
        "last_error",
        "rollback_state",
        "result",
        "created_at",
        "updated_at",
        "completed_at",
        "events",
    }
    result = {key: transaction.get(key) for key in allowed}
    if result.get("last_error"):
        result["last_error"] = safe_error(SelfUpdateError(str(result["last_error"])))
    # Historical files may contain raw pre-hardening errors. Never echo those.
    result["message"] = None
    result["events"] = [{"at": event.get("at"), "state": event.get("state")} for event in (transaction.get("events") or [])]
    return result


def worker_container_is_active(client, transaction: Dict[str, Any]) -> bool:
    if not transaction.get("worker_container_id"):
        return False
    try:
        worker = client.containers.get(transaction["worker_container_id"])
        verify_object(worker, transaction, "worker")
    except docker.errors.NotFound:
        return False
    labels = ((worker.attrs or {}).get("Config") or {}).get("Labels") or {}
    return (
        labels.get(UPDATER_ROLE_LABEL) == UPDATER_ROLE_VALUE
        and labels.get(UPDATER_TRANSACTION_LABEL) == transaction["transaction_id"]
        and worker.status in {"created", "running", "restarting"}
    )


def validate_source_repository(attrs: Dict[str, Any]) -> None:
    source_reference = str((attrs.get("Config") or {}).get("Image") or "")
    if source_reference.startswith("sha256:"):
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        previous_id = labels.get(UPDATER_TRANSACTION_LABEL)
        if previous_id:
            previous = load_transaction(previous_id)
            if (previous and previous.get("state") == "SUCCESS"
                    and previous.get("candidate_container_id") == attrs.get("Id")
                    and previous.get("target_image_id") == attrs.get("Image") == source_reference
                    and all(labels.get(k) == v for k, v in object_labels(previous, "candidate").items())):
                return
        raise SelfUpdateError("evh_update_pinned_source_unproven")
    source_repository, _ = split_image_reference(source_reference)
    if source_repository != OFFICIAL_REPOSITORY:
        raise SelfUpdateError(
            "当前运行容器不是官方 EVH 镜像，内建更新器未执行任何容器修改。"
        )


def start_update_transaction(client, release: Dict[str, Any]) -> Dict[str, Any]:
    """Preflight and start one official EVH self-update transaction."""
    if (
        not isinstance(release, dict)
        or release.get("source") != "release"
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not release.get("published_at")
    ):
        raise SelfUpdateError("目标不是正式 GitHub Release，未启动更新。")

    target_version = normalize_stable_version(release.get("version"))
    source_container = resolve_self_container(client)
    source_container.reload()
    attrs = source_container.attrs or {}
    validate_source_repository(attrs)
    assert_self_identity(source_container)
    deployment_type, supported, reason = detect_deployment_type(attrs)
    if not supported:
        if deployment_type == "portainer_compose":
            try:
                portainer_stack_contract(attrs)
            except SelfUpdateError:
                raise
        raise SelfUpdateError("evh_update_deployment_unsupported")
    deployment_contract = validate_deployment_scope(client, source_container, attrs, deployment_type)
    find_config_mount(attrs)
    find_docker_socket_mount(attrs)
    require_healthcheck(attrs)
    safe_container_config(attrs, attrs["Image"])

    source_version = normalize_stable_version(constants.APP_VERSION)
    if compare_stable_versions(target_version, source_version) < 0:
        raise SelfUpdateError("目标版本低于当前版本，内建更新器不会执行降级。")

    source_image_id = str(attrs.get("Image") or "")
    if not source_image_id.startswith("sha256:"):
        raise SelfUpdateError("无法确定当前运行容器的镜像 ID。")
    container_name = str(attrs.get("Name") or source_container.name or "").lstrip("/")
    if not container_name:
        raise SelfUpdateError("无法确定当前 EVH 容器名称。")

    transaction = create_transaction(
        source_container_id=source_container.id,
        source_container_name=container_name,
        source_image_id=source_image_id,
        source_version=source_version,
        source_schema_contract=constants.SELF_UPDATE_SCHEMA_CONTRACT,
        target_version=target_version,
        target_image=target_image_for_version(target_version),
        deployment_type=deployment_type,
        deployment_image_reference=deployment_contract.get("image_reference"),
        deployment_identity_fingerprint=deployment_contract.get("identity_fingerprint"),
    )
    try:
        start_worker(client, transaction, source_container)
    except Exception as exc:
        append_transaction_event(
            transaction["transaction_id"],
            "AMBIGUOUS",
            "更新 worker 启动未确认；未替换源容器，请检查事务后通过外部管理器恢复。",
            last_error=safe_error(exc),
            result="failed",
            completed_at=utc_now(),
        )
        raise
    return load_transaction(transaction["transaction_id"])


def recover_update_transaction(client) -> Optional[Dict[str, Any]]:
    """Resume an interrupted transaction without selecting a new target."""
    transaction = get_active_transaction()
    if not transaction:
        return None
    if transaction.get("state") in TERMINAL_STATES:
        if transaction["state"] != "AMBIGUOUS":
            release_active_transaction(transaction["transaction_id"])
        return transaction
    if worker_container_is_active(client, transaction):
        return transaction

    source = None
    for identity in (transaction.get("source_container_id"),):
        if not identity:
            continue
        try:
            source = client.containers.get(identity)
            verify_object(source, transaction, "source")
            break
        except docker.errors.NotFound:
            continue
    if source is None:
        append_transaction_event(
            transaction["transaction_id"],
            "AMBIGUOUS",
            "更新中断且无法定位当前或回滚容器，需要人工恢复。",
            last_error="update_worker_missing_and_source_unavailable",
            rollback_state="manual_recovery_required",
            result="failed",
            completed_at=utc_now(),
        )
        return load_transaction(transaction["transaction_id"])
    if not transaction.get("target_image_id") and transaction.get("state") != "PREPARING":
        append_transaction_event(transaction["transaction_id"], "FAILED", "目标尚未固定时执行中断；旧容器未替换，请重新发起。", last_error="evh_update_interrupted_before_pin")
        release_active_transaction(transaction["transaction_id"])
        return load_transaction(transaction["transaction_id"])
    start_worker(client, transaction, source)
    return load_transaction(transaction["transaction_id"])


def cleanup_terminal_workers(client, target_container_name: Optional[str] = None) -> int:
    """Remove only stopped workers whose persisted transaction is terminal."""
    removed = 0
    for worker in client.containers.list(all=True, filters={"label": f"{UPDATER_ROLE_LABEL}={UPDATER_ROLE_VALUE}"}):
        try:
            worker.reload()
            labels = ((worker.attrs or {}).get("Config") or {}).get("Labels") or {}
            transaction_id = labels.get(UPDATER_TRANSACTION_LABEL)
            target = labels.get(UPDATER_TARGET_LABEL)
            if target_container_name and target != target_container_name:
                continue
            transaction = load_transaction(transaction_id) if transaction_id else None
            if not transaction or transaction.get("state") not in CLEANUP_TERMINAL_STATES:
                continue
            if worker.status not in {"created", "exited", "dead"}:
                continue
            if transaction.get("worker_container_id") != worker.id:
                continue
            verify_object(worker, transaction, "worker")
            worker.remove(force=True)
            removed += 1
        except docker.errors.NotFound:
            continue
        except Exception:
            continue
    return removed
