# tasks/covers.py
# 封面生成任务模块

import logging

# 导入需要的底层模块和共享实例
import handler.emby as emby
import task_manager
from database import custom_collection_db, settings_db
from services.cover_generator import CoverGeneratorService
from .custom_collections import _get_cover_badge_text_for_collection

logger = logging.getLogger(__name__)


def task_generate_single_cover(processor, target_type: str, target_id: str):
    """使用统一封面配置为一个原生媒体库或自建合集生成封面。"""
    task_name = "生成单个封面"
    try:
        cover_config = settings_db.get_setting('cover_generator_config') or {}
        if not cover_config.get('enabled'):
            task_manager.update_status_from_thread(100, "任务跳过：封面生成器未启用。")
            return

        cover_service = CoverGeneratorService(config=cover_config)
        task_manager.update_status_from_thread(10, "正在读取封面目标...")

        if target_type == 'native':
            libraries = emby.get_emby_libraries(
                emby_server_url=processor.emby_url,
                emby_api_key=processor.emby_api_key,
                user_id=processor.emby_user_id,
            ) or []
            library = next((item for item in libraries if str(item.get('Id')) == str(target_id)), None)
            if not library:
                raise ValueError(f"未找到 Emby 原生媒体库: {target_id}")

            type_map = {
                'movies': 'Movie',
                'tvshows': 'Series',
                'music': 'MusicAlbum',
                'boxsets': 'BoxSet',
                'mixed': 'Movie,Series',
                'audiobooks': 'AudioBook',
            }
            item_type = type_map.get(library.get('CollectionType'))
            if not item_type and library.get('Type') == 'CollectionFolder':
                item_type = 'Movie,Series'
            item_count = 0
            if item_type:
                item_count = emby.get_item_count(
                    base_url=processor.emby_url,
                    api_key=processor.emby_api_key,
                    user_id=processor.emby_user_id,
                    parent_id=library.get('Id'),
                    item_type=item_type,
                ) or 0
            task_manager.update_status_from_thread(40, f"正在生成原生媒体库封面: {library.get('Name')}")
            success = cover_service.generate_for_library('main_emby', library, item_count=item_count)

        elif target_type == 'custom':
            collection = custom_collection_db.get_custom_collection_by_id(int(target_id))
            if not collection or not collection.get('emby_collection_id'):
                raise ValueError(f"未找到已创建的自建合集: {target_id}")
            details = emby.get_emby_item_details(
                collection.get('emby_collection_id'),
                processor.emby_url,
                processor.emby_api_key,
                processor.emby_user_id,
            )
            if not details:
                raise ValueError(f"无法从 Emby 获取自建合集详情: {collection.get('name')}")
            definition = collection.get('definition_json', {})
            content_types = definition.get('item_type', ['Movie'])
            item_count = _get_cover_badge_text_for_collection(collection)
            task_manager.update_status_from_thread(40, f"正在生成自建合集封面: {collection.get('name')}")
            success = cover_service.generate_for_library(
                'main_emby',
                details,
                item_count=item_count,
                content_types=content_types,
                custom_collection_data=collection,
            )
        else:
            raise ValueError(f"不支持的封面目标类型: {target_type}")

        if not success:
            raise RuntimeError("封面生成或上传失败，请查看任务日志。")
        task_manager.update_status_from_thread(100, "单个封面已生成并上传。")
    except Exception as e:
        logger.error(f"执行 '{task_name}' 失败: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"任务失败: {e}")

# ★★★ 立即生成所有媒体库封面的后台任务 ★★★
def task_generate_all_covers(processor):
    """
    后台任务：统一为原生媒体库和已创建的自建合集生成封面。
    """
    task_name = "一键生成全部封面"
    logger.trace(f"--- 开始执行 '{task_name}' 任务 ---")
    
    try:
        # 1. 读取配置
        cover_config = settings_db.get_setting('cover_generator_config') or {}

        if not cover_config:
            # 如果数据库里连配置都没有，可以认为功能未配置
            task_manager.update_status_from_thread(-1, "错误：未找到封面生成器配置，请先在设置页面保存一次。")
            return

        if not cover_config.get("enabled"):
            logger.info("  ⚠️ 封面生成器未启用，跳过封面生成任务。")
            task_manager.update_status_from_thread(100, "任务跳过：封面生成器未启用。")
            return

        # 2. 获取媒体库列表
        task_manager.update_status_from_thread(5, "正在获取所有媒体库列表...")
        all_libraries = emby.get_emby_libraries(
            emby_server_url=processor.emby_url,
            emby_api_key=processor.emby_api_key,
            user_id=processor.emby_user_id
        )
        if not all_libraries:
            logger.warning("  ⚠️ 未能从 Emby 获取原生媒体库，将继续检查自建合集。")
            all_libraries = []
        
        # 3. 筛选媒体库
        # ★★★ 核心修复：直接使用原始ID进行比较 ★★★
        exclude_ids = set(cover_config.get("exclude_libraries", []))
        exclude_custom_collection_ids = {
            str(collection_id)
            for collection_id in cover_config.get("exclude_custom_collections", [])
        }
        # 允许处理的媒体库类型列表，增加了 'audiobooks'
        ALLOWED_COLLECTION_TYPES = ['movies', 'tvshows', 'boxsets', 'mixed', 'music', 'audiobooks']

        libraries_to_process = [
            lib for lib in all_libraries 
            if lib.get('Id') not in exclude_ids
            and (
                # 条件1：满足常规的 CollectionType
                lib.get('CollectionType') in ALLOWED_COLLECTION_TYPES
                # 条件2：或者，是“混合库测试”这种特殊的 CollectionFolder
                or lib.get('Type') == 'CollectionFolder' 
            )
        ]
        
        collections_to_process = [
            collection
            for collection in custom_collection_db.get_all_active_custom_collections()
            if collection.get('emby_collection_id')
            and str(collection.get('id')) not in exclude_custom_collection_ids
        ]

        total = len(libraries_to_process) + len(collections_to_process)
        if total == 0:
            task_manager.update_status_from_thread(100, "任务完成：没有需要处理的原生媒体库或自建合集。")
            return

        logger.info(
            "  ➜ 统一封面任务将处理 %s 个原生媒体库和 %s 个自建合集。",
            len(libraries_to_process),
            len(collections_to_process),
        )
        
        # 4. 实例化服务并循环处理
        cover_service = CoverGeneratorService(config=cover_config)
        
        TYPE_MAP = {
            'movies': 'Movie', 
            'tvshows': 'Series', 
            'music': 'MusicAlbum',
            'boxsets': 'BoxSet', 
            'mixed': 'Movie,Series',
            'audiobooks': 'AudioBook'  # <-- 增加有声读物的映射
        }

        processed = 0
        for library in libraries_to_process:
            if processor.is_stop_requested(): break

            progress = 10 + int((processed / total) * 90)
            task_manager.update_status_from_thread(progress, f"({processed+1}/{total}) 正在处理原生媒体库: {library.get('Name')}")
            
            try:
                library_id = library.get('Id')
                collection_type = library.get('CollectionType')
                item_type_to_query = None # 先重置

                # --- ★★★ 核心修复 3：使用更精确的 if/elif 逻辑判断查询类型 ★★★ ---
                # 优先使用 CollectionType 进行判断，这是最准确的
                if collection_type:
                    item_type_to_query = TYPE_MAP.get(collection_type)
                
                # 如果 CollectionType 不存在，再使用 Type == 'CollectionFolder' 作为备用方案
                # 这专门用于处理像“混合库测试”那样的特殊库
                elif library.get('Type') == 'CollectionFolder':
                    logger.info(f"媒体库 '{library.get('Name')}' 是一个特殊的 CollectionFolder，将查询电影和剧集。")
                    item_type_to_query = 'Movie,Series'
                # --- 修复结束 ---

                item_count = 0
                if library_id and item_type_to_query:
                    item_count = emby.get_item_count(
                        base_url=processor.emby_url,
                        api_key=processor.emby_api_key,
                        user_id=processor.emby_user_id,
                        parent_id=library_id,
                        item_type=item_type_to_query
                    ) or 0

                cover_service.generate_for_library(
                    emby_server_id='main_emby', # 这里的 server_id 只是一个占位符，不影响忽略逻辑
                    library=library,
                    item_count=item_count
                )
            except Exception as e_gen:
                logger.error(f"为媒体库 '{library.get('Name')}' 生成封面时发生错误: {e_gen}", exc_info=True)
            finally:
                processed += 1

        if not processor.is_stop_requested():
            for collection_db_info in collections_to_process:
                if processor.is_stop_requested(): break

                collection_name = collection_db_info.get('name')
                emby_collection_id = collection_db_info.get('emby_collection_id')
                progress = 10 + int((processed / total) * 90)
                task_manager.update_status_from_thread(progress, f"({processed+1}/{total}) 正在处理自建合集: {collection_name}")

                try:
                    emby_collection_details = emby.get_emby_item_details(
                        emby_collection_id,
                        processor.emby_url,
                        processor.emby_api_key,
                        processor.emby_user_id,
                    )
                    if not emby_collection_details:
                        logger.warning(f"无法获取合集 '{collection_name}' (Emby ID: {emby_collection_id}) 的详情，跳过。")
                        continue

                    definition = collection_db_info.get('definition_json', {})
                    content_types = definition.get('item_type', ['Movie'])
                    item_count = _get_cover_badge_text_for_collection(collection_db_info)
                    cover_service.generate_for_library(
                        emby_server_id='main_emby',
                        library=emby_collection_details,
                        item_count=item_count,
                        content_types=content_types,
                        custom_collection_data=collection_db_info,
                    )
                except Exception as e_gen:
                    logger.error(f"为自建合集 '{collection_name}' 生成封面时发生错误: {e_gen}", exc_info=True)
                finally:
                    processed += 1

        final_message = "原生媒体库和自建合集封面已全部处理完毕！"
        if processor.is_stop_requested(): final_message = "任务已中止。"
        task_manager.update_status_from_thread(100, final_message)

    except Exception as e:
        logger.error(f"执行 '{task_name}' 任务时发生严重错误: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"任务失败: {e}")

# ★★★ 只为所有自建合集生成封面的后台任务 ★★★
def task_generate_all_custom_collection_covers(processor):
    """
    后台任务：为所有已启用、且已在Emby中创建的自定义合集生成封面。
    """
    task_name = "一键生成所有自建合集封面"
    logger.trace(f"--- 开始执行 '{task_name}' 任务 ---")
    
    try:
        # 1. 读取封面生成器的配置
        cover_config = settings_db.get_setting('cover_generator_config') or {}
        if not cover_config.get("enabled"):
            logger.info("  ⚠️ 封面生成器未启用，跳过自建合集封面生成任务。")
            task_manager.update_status_from_thread(100, "任务跳过：封面生成器未启用。")
            return

        # 2. 从数据库获取所有已启用的自定义合集
        task_manager.update_status_from_thread(5, "正在获取所有已启用的自建合集...")
        all_active_collections = custom_collection_db.get_all_active_custom_collections()
        
        # 3. 筛选出那些已经在Emby中成功创建的合集
        collections_to_process = [
            c for c in all_active_collections if c.get('emby_collection_id')
        ]
        
        total = len(collections_to_process)
        if total == 0:
            task_manager.update_status_from_thread(100, "任务完成：没有找到已在Emby中创建的自建合集。")
            return
            
        logger.info(f"  ➜ 将为 {total} 个自建合集生成封面。")
        
        # 4. 实例化服务并循环处理
        cover_service = CoverGeneratorService(config=cover_config)
        
        for i, collection_db_info in enumerate(collections_to_process):
            if processor.is_stop_requested(): break
            
            collection_name = collection_db_info.get('name')
            emby_collection_id = collection_db_info.get('emby_collection_id')
            
            progress = 10 + int((i / total) * 90)
            task_manager.update_status_from_thread(progress, f"({i+1}/{total}) 正在处理: {collection_name}")
            
            try:
                # a. 获取完整的Emby合集详情，这是封面生成器需要的
                emby_collection_details = emby.get_emby_item_details(
                    emby_collection_id, processor.emby_url, processor.emby_api_key, processor.emby_user_id
                )
                if not emby_collection_details:
                    logger.warning(f"无法获取合集 '{collection_name}' (Emby ID: {emby_collection_id}) 的详情，跳过。")
                    continue

                # 1. 从数据库记录中获取合集定义
                definition = collection_db_info.get('definition_json', {})
                content_types = definition.get('item_type', ['Movie'])

                # 2. 直接将当前循环中的合集信息传递给辅助函数
                item_count_to_pass = _get_cover_badge_text_for_collection(collection_db_info)

                # 3. 调用封面生成服务
                cover_service.generate_for_library(
                    emby_server_id='main_emby',
                    library=emby_collection_details,
                    item_count=item_count_to_pass, # <-- 使用计算好的角标参数
                    content_types=content_types,
                    # ★★★ 修复：传入 custom_collection_data，激活策略 A/B ★★★
                    custom_collection_data=collection_db_info
                )
            except Exception as e_gen:
                logger.error(f"为自建合集 '{collection_name}' 生成封面时发生错误: {e_gen}", exc_info=True)
                continue
        
        final_message = "所有自建合集封面已处理完毕！"
        if processor.is_stop_requested(): final_message = "任务已中止。"
        task_manager.update_status_from_thread(100, final_message)

    except Exception as e:
        logger.error(f"执行 '{task_name}' 任务时发生严重错误: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"任务失败: {e}")
