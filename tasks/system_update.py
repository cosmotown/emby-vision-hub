"""Coordinator for the EVH-only transactional self updater."""

from __future__ import annotations

import logging

import docker

import config_manager
import constants
import handler.github as github
import task_manager
from services import self_update


logger = logging.getLogger(__name__)


def fetch_latest_stable_release():
    releases = github.get_github_releases(
        owner=constants.GITHUB_REPO_OWNER,
        repo=constants.GITHUB_REPO_NAME,
        token=config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_GITHUB_TOKEN),
        proxies=config_manager.get_proxies_for_requests(),
    ) or []
    release = github.get_latest_stable_release(releases)
    if not release:
        raise self_update.SelfUpdateError("没有发现可用的正式稳定 GitHub Release。")
    return release


def start_system_update(client=None):
    own_client = client is None
    docker_client = client or docker.from_env()
    try:
        release = fetch_latest_stable_release()
        return self_update.start_update_transaction(docker_client, release)
    finally:
        if own_client:
            docker_client.close()


def cleanup_stale_updater_containers(target_container_name=None, client=None):
    """Compatibility wrapper: remove only stopped terminal EVH update workers."""
    own_client = client is None
    docker_client = client or docker.from_env()
    try:
        return self_update.cleanup_terminal_workers(docker_client, target_container_name)
    finally:
        if own_client:
            docker_client.close()


def recover_interrupted_system_update(client=None):
    own_client = client is None
    docker_client = client or docker.from_env()
    try:
        return self_update.recover_update_transaction(docker_client)
    finally:
        if own_client:
            docker_client.close()


def task_check_and_update_container(processor):
    """Scheduled-task adapter; the persistent transaction owns actual progress."""
    task_manager.update_status_from_thread(0, "正在执行自更新预检...")
    try:
        transaction = start_system_update()
    except Exception as exc:
        logger.error("系统更新启动失败: %s", self_update.safe_error(exc))
        task_manager.update_status_from_thread(-1, f"更新启动失败: {self_update.safe_error(exc)}")
        return
    logger.info("已启动 EVH 更新事务: %s", transaction["transaction_id"])
    task_manager.update_status_from_thread(
        100,
        f"更新事务已启动: {transaction['transaction_id']}。后续进度由持久化事务状态报告。",
    )
