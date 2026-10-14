# handler/actor_sync.py 

from typing import Optional, Callable
import threading
# 导入必要的模块
import handler.emby as emby
from database.actor_db import ActorDBManager
from database import connection
import logging
from services.person_sync_safety import PersonEnumeration
logger = logging.getLogger(__name__)

class UnifiedSyncHandler:
    def __init__(self, emby_url: str, emby_api_key: str, emby_user_id: Optional[str], tmdb_api_key: str):
        self.actor_db_manager = ActorDBManager()
        self.emby_url = emby_url
        self.emby_api_key = emby_api_key
        self.emby_user_id = emby_user_id
        self.tmdb_api_key = tmdb_api_key
        logger.trace(f"UnifiedSyncHandler 初始化完成。")
        
    def sync_emby_person_map_to_db(self, update_status_callback: Optional[Callable] = None, stop_event: Optional[threading.Event] = None):
        """
        Additive identity synchronization. Absence is reported, never reconciled
        destructively: a full API inventory alone is not a business protection
        snapshot and does not authorize removing an existing mapping.
        """
        logger.trace("  ➜ 开始增补人物映射 (Emby -> 本地数据库，缺失映射仅报告不解绑)")
        
        stats = {
            "total_from_emby": 0, "processed": 0, "db_inserted": 0, "db_updated": 0, 
            "unchanged": 0, "skipped": 0, "errors": 0, "db_cleaned": 0
        }

        try:
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    
                    # --- 阶段一：从本地数据库读取所有现存的 Emby ID ---
                    if update_status_callback: update_status_callback(0, "正在读取本地演员数据...")
                    logger.info("  ➜ 正在从本地数据库读取现有的 Emby 演员 ID...")
                    cursor.execute("SELECT emby_person_id FROM person_identity_map WHERE emby_person_id IS NOT NULL")
                    # 使用 set 以获得 O(1) 的查找效率
                    local_emby_ids = {row['emby_person_id'] for row in cursor.fetchall()}
                    logger.info(f"  ➜ 本地数据库中找到 {len(local_emby_ids)} 个已关联 Emby ID 的演员。")

                    # --- 阶段二：流式处理 Emby 数据并同步到数据库 ---
                    if update_status_callback: update_status_callback(5, "正在从 Emby 扫描并同步演员...")
                    
                    emby_server_ids = set() # 用于存储从 Emby 服务器获取到的所有 ID
                    emby_config_for_upsert = {"url": self.emby_url, "api_key": self.emby_api_key, "user_id": self.emby_user_id}

                    enumeration = PersonEnumeration()
                    person_generator = emby.get_all_persons_from_emby(
                        self.emby_url, self.emby_api_key, self.emby_user_id, stop_event,
                        update_status_callback=update_status_callback,
                        enumeration=enumeration,
                    )
                    
                    for person_batch in person_generator:
                        if stop_event and stop_event.is_set(): 
                            raise InterruptedError("任务在处理批次时被中止")
                        
                        for person_emby in person_batch:
                            stats["total_from_emby"] += 1
                            emby_pid = str(person_emby.get("Id", "")).strip()
                            person_name = str(person_emby.get("Name", "")).strip()

                            if not emby_pid or not person_name:
                                stats["skipped"] += 1
                                continue
                            
                            emby_server_ids.add(emby_pid) # 记录从 Emby 扫描到的 ID
                            
                            provider_ids = person_emby.get("ProviderIds", {})
                            person_data_for_db = { 
                                "emby_id": emby_pid, 
                                "name": person_name, 
                                "tmdb_id": provider_ids.get("Tmdb"), 
                                "imdb_id": provider_ids.get("Imdb"), 
                                "douban_id": provider_ids.get("Douban"), 
                            }
                            
                            try:
                                # 使用你之前修复过的、能准确返回状态的 upsert_person 函数
                                _, status = self.actor_db_manager.upsert_person(cursor, person_data_for_db, emby_config=emby_config_for_upsert)
                                if status == "INSERTED": stats['db_inserted'] += 1
                                elif status == "UPDATED": stats['db_updated'] += 1
                                elif status == "UNCHANGED": stats['unchanged'] += 1
                                elif status == "SKIPPED": stats['skipped'] += 1
                            except Exception as e_upsert:
                                stats['errors'] += 1
                                logger.error(f"处理演员 {person_name} (ID: {emby_pid}) 的 upsert 时失败: {e_upsert}")

                    # Even a complete enumeration is not permission to destroy
                    # identity mappings. Keep missing identities for review.
                    ids_to_clean = local_emby_ids - emby_server_ids
                    if ids_to_clean:
                        logger.warning(
                            '人物同步保留 %s 条未在本次结果中出现的映射；scope=%s complete=%s reason=%s，仅报告不解绑。',
                            len(ids_to_clean), enumeration.scope.value,
                            enumeration.complete, enumeration.error or 'business_review_required',
                        )
                        stats['missing_mapping_review_count'] = len(ids_to_clean)
                    else:
                        logger.info("  ➜ 未发现需要清理的 Emby 演员关联。")

                # 所有操作成功后，统一提交事务
                conn.commit()

        except InterruptedError:
            if 'conn' in locals() and conn: conn.rollback()
            if update_status_callback: update_status_callback(100, "任务已中止")
            return
        except Exception as e_main:
            if 'conn' in locals() and conn: conn.rollback()
            logger.error(f"演员同步任务发生严重错误: {e_main}", exc_info=True)
            if update_status_callback: update_status_callback(-1, "数据库操作失败")
            return

        # --- 最终统计 ---
        logger.info("  ➜ 同步演员数据完成")
        logger.info(f"  📊 : 新增 {stats['db_inserted']}, 更新 {stats['db_updated']}, 清理关联 {stats['db_cleaned']}.")

        if update_status_callback:
            final_message = f"增补完成：新增 {stats['db_inserted']}, 更新 {stats['db_updated']}, 待复核缺失映射 {stats.get('missing_mapping_review_count', 0)}；未解绑映射。"
            update_status_callback(100, final_message)
