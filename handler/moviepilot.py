# handler/moviepilot.py

import requests
import json
import logging
import threading
from typing import Dict, Any, Optional

import handler.tmdb as tmdb
import constants

logger = logging.getLogger(__name__)

_SUBSCRIPTION_LOCKS_GUARD = threading.Lock()
_SUBSCRIPTION_LOCKS = {}


def _subscription_lock_key(
    config: Dict[str, Any],
    tmdb_id: Any,
    season: Optional[int],
) -> str:
    base_url = str(
        config.get(constants.CONFIG_OPTION_MOVIEPILOT_URL, "") or ""
    ).rstrip("/")
    if season is None:
        season_key = "all"
    else:
        try:
            season_key = str(int(season))
        except (TypeError, ValueError):
            season_key = str(season).strip()
    return f"{base_url}|tmdb:{str(tmdb_id).strip()}|season:{season_key}"


def _acquire_subscription_lock(key: str):
    with _SUBSCRIPTION_LOCKS_GUARD:
        entry = _SUBSCRIPTION_LOCKS.get(key)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _SUBSCRIPTION_LOCKS[key] = entry
        entry["users"] += 1
    entry["lock"].acquire()
    return entry


def _release_subscription_lock(key: str, entry) -> None:
    entry["lock"].release()
    with _SUBSCRIPTION_LOCKS_GUARD:
        entry["users"] -= 1
        if entry["users"] == 0 and _SUBSCRIPTION_LOCKS.get(key) is entry:
            _SUBSCRIPTION_LOCKS.pop(key, None)

# ======================================================================
# 核心基础函数 (Token管理与API请求)
# ======================================================================

def _get_access_token(config: Dict[str, Any]) -> Optional[str]:
    """
    【内部辅助】获取 MoviePilot 的 Access Token。
    """
    try:
        moviepilot_url = config.get(constants.CONFIG_OPTION_MOVIEPILOT_URL, '').rstrip('/')
        mp_username = config.get(constants.CONFIG_OPTION_MOVIEPILOT_USERNAME, '')
        mp_password = config.get(constants.CONFIG_OPTION_MOVIEPILOT_PASSWORD, '')
        
        if not all([moviepilot_url, mp_username, mp_password]):
            # 仅在第一次调用或配置缺失时记录警告，避免刷屏
            return None

        login_url = f"{moviepilot_url}/api/v1/login/access-token"
        login_data = {"username": mp_username, "password": mp_password}
        
        # 设置超时
        login_response = requests.post(
            login_url,
            data=login_data,
            timeout=10,
            allow_redirects=False,
        )
        login_response.raise_for_status()
        
        return login_response.json().get("access_token")
    except Exception as e:
        logger.error(f"  ➜ 获取 MoviePilot Token 失败: {e}")
        return None

def subscribe_with_custom_payload(payload: dict, config: Dict[str, Any]) -> bool:
    """
    【核心订阅函数】直接接收一个完整的订阅 payload 并提交。
    所有其他订阅函数最终都应调用此函数。
    """
    tmdb_id = payload.get("tmdbid")
    season = payload.get("season")
    lock_key = _subscription_lock_key(config, tmdb_id, season)
    lock_entry = _acquire_subscription_lock(lock_key)
    try:
        moviepilot_url = config.get(constants.CONFIG_OPTION_MOVIEPILOT_URL, '').rstrip('/')
        access_token = _get_access_token(config)
        if not access_token:
            logger.error("  ➜ MoviePilot订阅失败：认证失败，未能获取到 Token。")
            return False

        subscribe_url = f"{moviepilot_url}/api/v1/subscribe/"
        subscribe_headers = {"Authorization": f"Bearer {access_token}"}

        logger.trace(f"  ➜ 最终发送给 MoviePilot 的 Payload: {json.dumps(payload, ensure_ascii=False)}")
        
        sub_response = requests.post(
            subscribe_url,
            headers=subscribe_headers,
            json=payload,
            timeout=60,
            allow_redirects=False,
        )
        
        if sub_response.status_code in [200, 201, 204]:
            logger.info(f"  ✅ MoviePilot 已接受订阅任务。")
            return True
        else:
            logger.error(
                "  ➜ MoviePilot 订阅失败，HTTP %s。",
                sub_response.status_code,
            )
            return False
    except Exception as e:
        logger.error(f"  ➜ 使用自定义Payload订阅到MoviePilot时发生错误: {e}", exc_info=True)
        return False
    finally:
        _release_subscription_lock(lock_key, lock_entry)

def cancel_subscription(tmdb_id: str, item_type: str, config: Dict[str, Any], season: Optional[int] = None) -> bool:
    """
    【取消订阅】根据 TMDB ID 和类型取消订阅。
    """
    try:
        moviepilot_url = config.get(constants.CONFIG_OPTION_MOVIEPILOT_URL, '').rstrip('/')
        access_token = _get_access_token(config)
        if not access_token:
            logger.error("  ➜ MoviePilot 取消订阅失败：认证失败。")
            return False

        # 内部函数：执行单次取消请求
        def _do_cancel_request(target_season: Optional[int]) -> bool:
            media_id_for_api = f"tmdb:{tmdb_id}"
            cancel_url = f"{moviepilot_url}/api/v1/subscribe/media/{media_id_for_api}"
            
            params = {}
            if target_season is not None:
                params['season'] = target_season
            
            headers = {"Authorization": f"Bearer {access_token}"}
            
            season_log = f" Season {target_season}" if target_season is not None else ""
            logger.info(f"  ➜ 正在向 MoviePilot 发送取消订阅请求: {media_id_for_api}{season_log}")

            lock_key = _subscription_lock_key(config, tmdb_id, target_season)
            lock_entry = _acquire_subscription_lock(lock_key)
            try:
                response = requests.delete(
                    cancel_url,
                    headers=headers,
                    params=params,
                    timeout=30,
                    allow_redirects=False,
                )
                if response.status_code in [200, 204]:
                    logger.info(f"  ✅ MoviePilot 已成功取消订阅: {media_id_for_api}{season_log}")
                    return True
                elif response.status_code == 404:
                    logger.info(f"  ✅ MoviePilot 中未找到订阅 {media_id_for_api}{season_log}，无需取消。")
                    return True
                else:
                    logger.error(
                        "  ➜ MoviePilot 取消订阅失败，HTTP %s。",
                        response.status_code,
                    )
                    return False
            except Exception as req_e:
                logger.error(f"  ➜ 请求 MoviePilot API 发生异常: {req_e}")
                return False
            finally:
                _release_subscription_lock(lock_key, lock_entry)

        # --- 逻辑分支 ---

        # 情况 1: 电影，或者指定了具体季号的剧集 -> 直接取消
        if item_type == 'Movie' or season is not None:
            return _do_cancel_request(season)

        # 情况 2: 剧集 (Series) 且未指定季号 -> 查询 TMDb 遍历取消所有季
        if item_type == 'Series':
            tmdb_api_key = config.get(constants.CONFIG_OPTION_TMDB_API_KEY)
            if not tmdb_api_key:
                logger.error("  ➜ 取消剧集订阅失败：未配置 TMDb API Key，无法获取分季信息。")
                return False

            logger.info(f"  ➜ 正在查询 TMDb 获取剧集 (ID: {tmdb_id}) 的所有季信息，以便逐个取消...")
            series_details = tmdb.get_tv_details(tmdb_id, tmdb_api_key)
            
            if not series_details:
                logger.error(f"  ➜ 无法从 TMDb 获取剧集详情，取消订阅中止。")
                return False

            seasons = series_details.get('seasons', [])
            if not seasons:
                logger.warning(f"  ➜ 该剧集在 TMDb 上没有季信息，尝试直接取消整剧。")
                return _do_cancel_request(None)

            all_success = True
            # 遍历所有季
            for s in seasons:
                s_num = s.get('season_number')
                # 只处理 season_number > 0 的季，跳过第0季 ★★★
                if s_num is not None and s_num > 0:
                    if not _do_cancel_request(s_num):
                        all_success = False
            
            return all_success

        # 默认 fallback
        return _do_cancel_request(None)

    except Exception as e:
        logger.error(f"  ➜ 调用 MoviePilot 取消订阅 API 时发生未知错误: {e}", exc_info=True)
        return False

class MoviePilotSubscriptionLookupError(RuntimeError):
    pass

def _normalize_subscription_state(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip().upper()
    return value or None

def _normalize_episode_count(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def get_subscription_details(
    tmdb_id: str,
    item_type: str,
    config: Dict[str, Any],
    season: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    try:
        base_url = config.get(
            constants.CONFIG_OPTION_MOVIEPILOT_URL, ""
        ).rstrip("/")
        token = _get_access_token(config)
        if not base_url or not token:
            raise MoviePilotSubscriptionLookupError(
                "MoviePilot 地址或认证信息不可用"
            )

        headers = {"Authorization": f"Bearer {token}"}
        media_id = f"tmdb:{tmdb_id}"
        params = {}
        if item_type in ("Series", "Season") and season is not None:
            params["season"] = season

        lookup = requests.get(
            f"{base_url}/api/v1/subscribe/media/{media_id}",
            headers=headers,
            params=params,
            timeout=15,
        )
        if lookup.status_code == 404:
            return None
        if lookup.status_code != 200:
            raise MoviePilotSubscriptionLookupError(
                f"订阅查询失败: HTTP {lookup.status_code}"
            )

        lookup_data = lookup.json()
        if not isinstance(lookup_data, dict):
            raise MoviePilotSubscriptionLookupError("订阅查询响应不是对象")

        sub_id = lookup_data.get("id")
        if not sub_id:
            return None

        detail = requests.get(
            f"{base_url}/api/v1/subscribe/{sub_id}",
            headers=headers,
            timeout=15,
        )
        if detail.status_code != 200:
            raise MoviePilotSubscriptionLookupError(
                f"订阅详情查询失败: HTTP {detail.status_code}"
            )

        detail_data = detail.json()
        if not isinstance(detail_data, dict):
            raise MoviePilotSubscriptionLookupError("订阅详情响应不是对象")

        merged = dict(lookup_data)
        merged.update(detail_data)
        state = _normalize_subscription_state(
            merged.get("state", merged.get("status"))
        )
        if state is None:
            raise MoviePilotSubscriptionLookupError(
                f"订阅详情缺少 state (ID:{sub_id})"
            )

        return {
            "id": sub_id,
            "state": state,
            "total_episode": _normalize_episode_count(
                merged.get("total_episode")
            ),
            "lack_episode": _normalize_episode_count(
                merged.get("lack_episode")
            ),
            "_detail_payload": dict(detail_data),
        }
    except MoviePilotSubscriptionLookupError:
        raise
    except Exception as exc:
        raise MoviePilotSubscriptionLookupError(
            f"获取完整订阅详情失败: {exc}"
        ) from exc

def check_subscription_exists(
    tmdb_id: str,
    item_type: str,
    config: Dict[str, Any],
    season: Optional[int] = None,
) -> bool:
    try:
        base_url = config.get(
            constants.CONFIG_OPTION_MOVIEPILOT_URL, ""
        ).rstrip("/")
        token = _get_access_token(config)
        if not token:
            return False
        params = {}
        if item_type in ("Series", "Season") and season is not None:
            params["season"] = season
        response = requests.get(
            f"{base_url}/api/v1/subscribe/media/tmdb:{tmdb_id}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        if response.status_code == 200:
            data = response.json()
            return bool(isinstance(data, dict) and data.get("id"))
        return False
    except Exception as exc:
        logger.warning(
            f"  ➜ 检查 MoviePilot 订阅状态时发生错误: {exc}"
        )
        return False

# ======================================================================
# 业务封装函数 (保持原有逻辑，底层复用 subscribe_with_custom_payload)
# ======================================================================

def subscribe_movie_to_moviepilot(movie_info: dict, config: Dict[str, Any], best_version: Optional[int] = None) -> bool:
    """订阅单部电影"""
    payload = {
        "name": movie_info['title'],
        "tmdbid": int(movie_info['tmdb_id']),
        "type": "电影"
    }
    if best_version is not None:
        payload["best_version"] = best_version
        logger.info(f"  ➜ 本次订阅为洗版订阅 (best_version={best_version})")
        
    logger.info(f"  ➜ 正在向 MoviePilot 提交电影订阅: '{movie_info['title']}'")
    return subscribe_with_custom_payload(payload, config)

def subscribe_series_to_moviepilot(series_info: dict, season_number: Optional[int], config: Dict[str, Any], best_version: Optional[int] = None) -> bool:
    """订阅单季或整部剧集"""
    title = series_info.get('title') or series_info.get('item_name')
    if not title:
        logger.error(f"  ➜ 订阅失败：缺少标题。信息: {series_info}")
        return False

    payload = {
        "name": title,
        "tmdbid": int(series_info['tmdb_id']),
        "type": "电视剧"
    }
    if season_number is not None:
        payload["season"] = season_number
    
    if best_version is not None:
        payload["best_version"] = best_version
        logger.info(f"  ➜ 本次订阅为洗版订阅 (best_version={best_version})")

    log_msg = f"  ➜ 正在向 MoviePilot 提交剧集订阅: '{title}'"
    if season_number is not None:
        log_msg += f" 第 {season_number} 季"
    logger.info(log_msg)
    
    return subscribe_with_custom_payload(payload, config)

def update_subscription_status(
    tmdb_id: int,
    season: Optional[int],
    status: str,
    config: Dict[str, Any],
    total_episodes: Optional[int] = None,
    subscription_details: Optional[Dict[str, Any]] = None,
) -> bool:
    lock_key = _subscription_lock_key(config, tmdb_id, season)
    lock_entry = _acquire_subscription_lock(lock_key)
    try:
        # Always re-read under the per-subscription lock.  A payload fetched by
        # the caller before lock acquisition can become stale while another task
        # updates the same subscription, defeating GET/compare/PUT idempotency.
        details = get_subscription_details(
            str(tmdb_id),
            "Series" if season is not None else "Movie",
            config,
            season=season,
        )
        if not isinstance(details, dict) or not details.get("id"):
            return False

        sub_id = details["id"]
        current_state = _normalize_subscription_state(
            details.get("state")
        )
        target_state = _normalize_subscription_state(status)
        current_total = _normalize_episode_count(
            details.get("total_episode")
        )
        target_total = _normalize_episode_count(total_episodes)

        if current_state is None or target_state is None:
            logger.warning(
                f"  ➜ MoviePilot 状态不可核验 (ID:{sub_id})，本轮跳过。"
            )
            return False

        status_changed = current_state != target_state
        total_changed = (
            target_total is not None
            and current_total is not None
            and current_total != target_total
        )

        if target_total is not None and current_total is None:
            logger.warning(
                f"  ➜ MoviePilot 总集数不可核验 (ID:{sub_id})，"
                "不会执行详情 PUT。"
            )

        if not status_changed and not total_changed:
            logger.debug(
                f"  ➜ [MP同步] 订阅 ID:{sub_id} 无真实变化，跳过所有 PUT。"
            )
            return True

        base_url = config.get(
            constants.CONFIG_OPTION_MOVIEPILOT_URL, ""
        ).rstrip("/")
        token = _get_access_token(config)
        if not base_url or not token:
            return False
        headers = {"Authorization": f"Bearer {token}"}
        success = True

        # 先更新详情，再更新状态，避免旧详情载荷覆盖新状态。
        if total_changed:
            raw_payload = details.get("_detail_payload")
            if not isinstance(raw_payload, dict):
                logger.warning(
                    f"  ➜ 完整订阅载荷不可用 (ID:{sub_id})，跳过详情 PUT。"
                )
                success = False
            else:
                payload = dict(raw_payload)
                old_total = _normalize_episode_count(
                    payload.get("total_episode")
                )
                if old_total is None:
                    logger.warning(
                        f"  ➜ 完整订阅载荷缺少 total_episode (ID:{sub_id})。"
                    )
                    success = False
                elif old_total != target_total:
                    old_lack = _normalize_episode_count(
                        payload.get("lack_episode")
                    )
                    old_lack = old_lack if old_lack is not None else 0
                    payload["total_episode"] = target_total

                    if old_total > target_total:
                        diff = old_total - target_total
                        new_lack = max(0, old_lack - diff)
                        payload["lack_episode"] = new_lack
                        logger.info(
                            f"  ➜ [MP修正] 缺失集数 {old_lack}->{new_lack} "
                            f"(总集数 {old_total}->{target_total})"
                        )

                    response = requests.put(
                        f"{base_url}/api/v1/subscribe/",
                        headers=headers,
                        json=payload,
                        timeout=10,
                        allow_redirects=False,
                    )
                    if response.status_code not in (200, 204):
                        logger.warning(
                            "  ➜ 更新 MoviePilot 总集数失败，HTTP %s。",
                            response.status_code,
                        )
                        success = False
                    else:
                        logger.info(
                            f"  ➜ [MP同步] 订阅 ID:{sub_id} 总集数 "
                            f"{old_total}->{target_total}"
                        )

        if status_changed:
            response = requests.put(
                f"{base_url}/api/v1/subscribe/status/{sub_id}",
                headers=headers,
                params={"state": target_state},
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code not in (200, 204):
                logger.warning(
                    "  ➜ 更新 MoviePilot 状态失败，HTTP %s。",
                    response.status_code,
                )
                success = False
            else:
                logger.info(
                    f"  ➜ [MP同步] 订阅 ID:{sub_id} 状态 "
                    f"{current_state}->{target_state}"
                )

        return success
    except MoviePilotSubscriptionLookupError as exc:
        logger.warning(
            f"  ➜ 查询 MoviePilot 订阅失败，本轮不写入: {exc}"
        )
        return False
    except Exception as exc:
        logger.error(f"  ➜ 调用 MoviePilot 更新接口出错: {exc}")
        return False
    finally:
        _release_subscription_lock(lock_key, lock_entry)
    
def delete_transfer_history(tmdb_id: str, season: int, title: str, config: Dict[str, Any]) -> list:
    """
    【清理整理记录】
    修改返回值：返回一个包含被删除记录 download_hash 的列表。
    如果失败或无记录，返回空列表 []。
    """
    collected_hashes = [] # 用于收集 Hash
    
    try:
        moviepilot_url = config.get(constants.CONFIG_OPTION_MOVIEPILOT_URL, '').rstrip('/')
        access_token = _get_access_token(config)
        if not access_token:
            return []

        headers = {"Authorization": f"Bearer {access_token}"}
        search_url = f"{moviepilot_url}/api/v1/history/transfer"
        
        # 1. 循环获取所有相关记录
        all_records = []
        page = 1
        page_size = 500
        
        logger.info(f"  🔍 [MP清理] 正在全量搜索《{title}》的整理记录...")
        
        while True:
            params = {"title": title, "page": page, "count": page_size}
            try:
                res = requests.get(search_url, headers=headers, params=params, timeout=30)
                if res.status_code != 200: break
                data = res.json()
                if not data: break
                
                records_list = []
                if isinstance(data, dict):
                    inner_data = data.get('data')
                    if isinstance(inner_data, list): records_list = inner_data
                    elif isinstance(inner_data, dict) and 'list' in inner_data: records_list = inner_data['list']
                elif isinstance(data, list): records_list = data
                
                if not records_list: break
                all_records.extend(records_list)
                if len(records_list) < page_size: break
                page += 1
            except: break

        if not all_records:
            logger.info(f"  ✅ [MP清理] 未找到《{title}》的任何整理记录。")
            return []

        # 2. 内存筛选
        ids_to_delete = []
        target_tmdb = int(tmdb_id)
        target_season = int(season)
        
        for record in all_records:
            if not isinstance(record, dict): continue
            rec_tmdb = record.get('tmdbid')
            if rec_tmdb != target_tmdb: continue
            
            rec_seasons = str(record.get('seasons', '')).strip().upper()
            import re
            match = re.search(r'(\d+)', rec_seasons)
            if match:
                try:
                    if int(match.group(1)) == target_season:
                        ids_to_delete.append(record)
                except: continue

        if not ids_to_delete:
            logger.info(f"  ✅ [MP清理] 搜索到 {len(all_records)} 条记录，但没有 《{title}》 - 第 {season} 季 的记录。")
            return []

        logger.info(f"  🗑️ [MP清理] 筛选出 {len(ids_to_delete)} 条《{title}》 - 第 {season} 季 的整理记录，开始执行删除...")

        # 3. 逐条删除并收集 Hash
        delete_url = f"{moviepilot_url}/api/v1/history/transfer"
        del_params = {"deletesrc": "false", "deletedest": "false"}
        
        deleted_count = 0
        for rec in ids_to_delete:
            try:
                # ★★★ 顺手牵羊：收集 Hash ★★★
                rec_hash = rec.get('download_hash')
                if rec_hash:
                    collected_hashes.append(rec_hash)

                del_res = requests.delete(
                    delete_url,
                    headers=headers,
                    params=del_params,
                    json=rec,
                    timeout=15,
                    allow_redirects=False,
                )
                if del_res.status_code == 200:
                    deleted_count += 1
            except: pass

        # 去重 Hash
        collected_hashes = list(set(collected_hashes))
        logger.info(f"  ✅ [MP清理] 清理完成，共删除 {deleted_count} 条记录，提取到 {len(collected_hashes)} 个关联种子Hash。")
        
        return collected_hashes

    except Exception as e:
        logger.error(f"  ❌ [MP清理] 执行出错: {e}")
        return []

def delete_download_tasks(keyword: str, config: Dict[str, Any], hashes: list = None) -> bool:
    """
    清理下载任务 - 安全版
    Strict Mode: 仅接受 hashes 列表进行精确删除。
    如果不传 hashes 或为空，直接跳过，绝不使用 keyword 搜索兜底。
    """
    # --- 1. 安全检查：无 Hash 直接熔断 ---
    if not hashes:
        return False

    try:
        moviepilot_url = config.get(constants.CONFIG_OPTION_MOVIEPILOT_URL, '').rstrip('/')
        access_token = _get_access_token(config)
        if not access_token: return False

        headers = {"Authorization": f"Bearer {access_token}"}
        deleted_count = 0

        # --- 2. 策略 A: 精确打击 (仅使用 Hash) ---
        logger.info(f"  🎯 [下载器清理] 正在根据 Hash 精确删除 {len(hashes)} 个任务...")
        
        for task_hash in hashes:
            if not task_hash: continue
            
            del_url = f"{moviepilot_url}/api/v1/download/{task_hash}"
            try:
                # 只有这里才是真正执行删除的地方
                del_res = requests.delete(
                    del_url,
                    headers=headers,
                    timeout=10,
                    allow_redirects=False,
                )
                if del_res.status_code == 200:
                    logger.info(f" 🗑️ [下载器清理] 已精确删除任务 Hash: {task_hash[:8]}...")
                    deleted_count += 1
            except Exception as e:
                logger.debug(f" [下载器清理] 删除 Hash {task_hash[:8]} 失败: {e}")
        
        # --- 3. 结果反馈 ---
        if deleted_count > 0:
            logger.info(f"  ✅ [下载器清理] Hash 精确清理完成，共删除 {deleted_count} 个任务。")
            import time
            time.sleep(2)
            return True
        else:
            # 即使没删掉（比如任务早就不在了），也到此为止，绝不搜索关键词
            logger.info(f"  ℹ️ [下载器清理] 提供的 Hash 均未在下载器中找到活跃任务，无需操作。")
            return True

    except Exception as e:
        logger.error(f"  ❌ [下载器清理] 执行出错: {e}")
        return False
