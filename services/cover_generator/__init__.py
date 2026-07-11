# services/cover_generator/__init__.py

import logging
import shutil
import yaml
import json
import random
import requests
import base64
import binascii
import time
from io import BytesIO
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional, Union
from gevent import spawn_later, sleep as gevent_sleep
from PIL import Image
from database import custom_collection_db, queries_db
import config_manager
import handler.emby as emby 
from extensions import UPDATING_IMAGES
from .styles.style_single_1 import create_style_single_1
from .styles.style_single_2 import create_style_single_2
from .styles.style_multi_1 import create_style_multi_1
from .styles.style_chillposter import create_chillposter_cover, get_chillposter_asset_counts, get_chillposter_required_count

logger = logging.getLogger(__name__)

class CoverGeneratorService:
    SORT_BY_DISPLAY_NAME = { "Random": "随机", "Latest": "最新添加" }

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._sort_by = self.config.get("sort_by", "Random")
        self._covers_output = self.config.get("covers_output")
        self._covers_input = self.config.get("covers_input")
        self._title_config_str = self.config.get("title_config", "")
        self._cover_style = self.config.get("cover_style", "single_1")
        self._chillposter_template = self.config.get("chillposter_template", "preset_1769062617890")
        self._multi_1_blur = self.config.get("multi_1_blur", False)
        self._multi_1_use_primary = self.config.get("multi_1_use_primary", True)
        self._single_use_primary = self.config.get("single_use_primary", False)
        self.data_path = Path(config_manager.PERSISTENT_DATA_PATH) / "cover_generator"
        self.covers_path = self.data_path / "covers"
        self.font_path = self.data_path / "fonts"
        self.covers_path.mkdir(parents=True, exist_ok=True)
        self.font_path.mkdir(parents=True, exist_ok=True)
        self.zh_font_path = None
        self.en_font_path = None
        self.zh_font_path_multi_1 = None
        self.en_font_path_multi_1 = None
        self._fonts_checked_and_ready = False

    def generate_for_library(self, emby_server_id: str, library: Dict[str, Any], item_count: Optional[int] = None, content_types: Optional[List[str]] = None, custom_collection_data: Optional[Dict] = None):
        sort_by_name = self.SORT_BY_DISPLAY_NAME.get(self._sort_by, self._sort_by)
        logger.info(f"  ➜ 开始以排序方式: {sort_by_name} 为媒体库 '{library['Name']}' 生成封面...")
        self.__get_fonts()
        image_data = self.__generate_image_data(emby_server_id, library, item_count, content_types, custom_collection_data)
        if not image_data:
            logger.error(f"  ➜ 为媒体库 '{library['Name']}' 生成封面图片失败。")
            return False
        success = self.__set_library_image(emby_server_id, library, image_data)
        if success:
            logger.info(f"  ✅ 成功更新媒体库 '{library['Name']}' 的封面！")
        else:
            logger.error(f"  ➜ 上传封面到媒体库 '{library['Name']}' 失败。")
        return success

    def __generate_image_data(self, server_id: str, library: Dict[str, Any], item_count: Optional[int] = None, content_types: Optional[List[str]] = None, custom_collection_data: Optional[Dict] = None) -> Union[bytes, str, None]:
        library_name = library['Name']
        title = self.__get_library_title_from_yaml(library_name)
        custom_image_paths = self.__check_custom_image(library_name)
        if custom_image_paths:
            logger.info(f"  ➜ 发现媒体库 '{library_name}' 的自定义图片，将使用路径模式生成。")
            return self.__generate_image_from_path(library_name, title, custom_image_paths, item_count)
        
        # ★★★ 真实海报兜底 (针对“即将上线”等本地无资源的榜单) ★★★
        if custom_collection_data and custom_collection_data.get('type') in ['list', 'ai_recommendation_global']:
            tmdb_image_data = self.__generate_from_local_tmdb_metadata(library_name, title, custom_collection_data, item_count)
            if tmdb_image_data:
                return tmdb_image_data

        logger.trace(f"  ➜ 未发现自定义图片，将从服务器 '{server_id}' 获取媒体项作为封面来源。")
        return self.__generate_from_server(server_id, library, title, item_count, content_types, custom_collection_data)

    def __generate_from_local_tmdb_metadata(self, library_name: str, title: Tuple[str, str], custom_collection_data: Dict, item_count: Optional[int]) -> Optional[bytes]:
        """
        当本地没有 Emby 媒体项时，利用数据库里存储的 poster_path 下载海报。
        """
        try:
            media_info_list = custom_collection_data.get('generated_media_info_json') or []
            if isinstance(media_info_list, str):
                media_info_list = json.loads(media_info_list)

            # 检查是否有足够的 Emby ID
            valid_emby_ids = [i for i in media_info_list if i.get('emby_id')]
            
            # 如果本地已经有不少于 3 个的匹配项，优先用 Emby 的
            if len(valid_emby_ids) >= 3:
                return None

            logger.info(f"  ➜ 合集 '{library_name}' 本地资源不足 (Emby匹配数: {len(valid_emby_ids)})，尝试使用 TMDB 元数据生成真实封面...")

            # 提取 TMDB ID
            candidates = [i for i in media_info_list if i.get('tmdb_id')]
            
            if not candidates:
                return None

            # 如果是随机模式，洗牌
            if self._sort_by == "Random":
                random.shuffle(candidates)
            
            # 限制数量
            if self._cover_style == 'chillposter':
                limit = get_chillposter_required_count(self._chillposter_template)
            else:
                limit = 1 if self._cover_style.startswith('single') else 9
            candidates = candidates[:limit]
            
            # 提取纯 ID 列表
            tmdb_ids = [str(item['tmdb_id']) for item in candidates]
            
            # 从数据库批量查询 poster_path
            metadata_map = queries_db.get_missing_items_metadata(tmdb_ids)
            
            image_paths = []
            
            for tmdb_id in tmdb_ids:
                meta = metadata_map.get(tmdb_id)
                if meta and meta.get('poster_path'):
                    poster_path = meta['poster_path']
                    # 构造完整 URL
                    full_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
                    
                    # 下载
                    save_name = f"tmdb_{tmdb_id}.jpg"
                    local_path = self.__download_external_image(full_url, library_name, save_name)
                    if local_path:
                        image_paths.append(local_path)
            
            if not image_paths:
                logger.warning(f"  ➜ 数据库中未找到有效的 poster_path。")
                return None

            logger.info(f"  ➜ 成功获取到 {len(image_paths)} 张真实海报，正在生成封面...")
            
            # ==================================================================
            # ★★★ 核心修复：清理旧的缓存图片 ★★★
            # 必须删除 1.jpg - 9.jpg，否则 __prepare_multi_images 会复用旧的占位符图片
            # ==================================================================
            subdir = self.covers_path / library_name
            if subdir.exists():
                for i in range(1, 10):
                    old_cache = subdir / f"{i}.jpg"
                    if old_cache.exists():
                        try:
                            old_cache.unlink()
                        except Exception:
                            pass
            # ==================================================================

            return self.__generate_image_from_path(library_name, title, [str(p) for p in image_paths], item_count)

        except Exception as e:
            logger.error(f"  ➜ TMDB 海报兜底流程出错: {e}", exc_info=True)
            return None

    def __download_external_image(self, url: str, library_name: str, filename: str) -> Optional[Path]:
        """通用的外部图片下载方法 (支持代理)"""
        subdir = self.covers_path / library_name
        subdir.mkdir(parents=True, exist_ok=True)
        filepath = subdir / filename
        
        # 简单的缓存机制
        if filepath.exists() and filepath.stat().st_size > 0:
            return filepath

        try:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(max_retries=3)
            session.mount('https://', adapter)
            
            # ★★★ 注入代理 ★★★
            proxies = config_manager.get_proxies_for_requests()
            if proxies:
                session.proxies.update(proxies)
            
            resp = session.get(url, stream=True, timeout=15)
            if resp.status_code == 200:
                with open(filepath, 'wb') as f:
                    shutil.copyfileobj(resp.raw, f)
                return filepath
        except Exception as e:
            logger.warning(f"  ➜ 下载外部图片失败 {url}: {e}")
        return None

    def __generate_from_server(self, server_id: str, library: Dict[str, Any], title: Tuple[str, str], item_count: Optional[int] = None, content_types: Optional[List[str]] = None, custom_collection_data: Optional[Dict] = None) -> Union[bytes, str, None]:
        if self._cover_style == 'chillposter':
            required_items_count = get_chillposter_required_count(self._chillposter_template)
        else:
            required_items_count = 1 if self._cover_style.startswith('single') else 9
        items = self.__get_valid_items_from_library(server_id, library, required_items_count, content_types, custom_collection_data)
        if not items:
            logger.warning(f"  ➜ 在媒体库 '{library['Name']}' 中找不到任何带有可用图片的媒体项。")
            return None
        if self._cover_style == 'chillposter':
            poster_paths = []
            backdrop_paths = []
            poster_limit, backdrop_limit = get_chillposter_asset_counts(self._chillposter_template)
            for item in items:
                if len(poster_paths) < poster_limit:
                    primary_url = self.__get_primary_image_url(item)
                    if primary_url:
                        path = self.__download_image(server_id, primary_url, library['Name'], len(poster_paths) + 1, prefix="chillposter_poster_")
                        if path:
                            poster_paths.append(path)
                if len(backdrop_paths) < backdrop_limit:
                    backdrop_url = self.__get_backdrop_image_url(item)
                    if backdrop_url:
                        path = self.__download_image(server_id, backdrop_url, library['Name'], len(backdrop_paths) + 1, prefix="chillposter_backdrop_")
                        if path:
                            backdrop_paths.append(path)
            if not poster_paths and not backdrop_paths:
                logger.warning(f"  ➜ 为 ChillPoster 模板下载图片失败。")
                return None
            return self.__generate_image_from_path(library['Name'], title, poster_paths or backdrop_paths, item_count, backdrop_paths=backdrop_paths)
        if self._cover_style.startswith('single'):
            image_url = self.__get_image_url(items[0])
            if not image_url: return None
            image_path = self.__download_image(server_id, image_url, library['Name'], 1)
            if not image_path: return None
            return self.__generate_image_from_path(library['Name'], title, [image_path], item_count)
        else:
            image_paths = []
            for i, item in enumerate(items[:9]):
                image_url = self.__get_image_url(item)
                if image_url:
                    path = self.__download_image(server_id, image_url, library['Name'], i + 1)
                    if path:
                        image_paths.append(path)
            if not image_paths:
                logger.warning(f"  ➜ 为多图模式下载图片失败。")
                return None
            return self.__generate_image_from_path(library['Name'], title, image_paths, item_count)

    def __get_valid_items_from_library(self, server_id: str, library: Dict[str, Any], limit: int, content_types: Optional[List[str]] = None, custom_collection_data: Optional[Dict] = None) -> List[Dict]:
        library_id = library.get("Id") or library.get("ItemId")
        library_name = library.get("Name")
        base_url = config_manager.APP_CONFIG.get('emby_server_url')
        api_key = config_manager.APP_CONFIG.get('emby_api_key')
        user_id = config_manager.APP_CONFIG.get('emby_user_id')

        # ======================================================================
        # ★★★ 0. 统一计算安全分级上限 (Safe Rating Limit) ★★★
        # ======================================================================
        # 1. 获取用户配置的上限 (默认 8/PG-13)
        config_limit = self.config.get('max_safe_rating', 8)
        
        # 2. 判断是否命中白名单 (库名包含 R级/限制/成人 等)
        is_whitelisted_library = any(keyword.lower() in library_name.lower() for keyword in ['R级', '限制', '成人', 'Adult', 'Porn', '18+'])
        
        # 3. 确定最终限制
        safe_rating_limit = None
        if is_whitelisted_library:
            safe_rating_limit = None # 白名单库 -> 无限制
        elif config_limit >= 999:
            safe_rating_limit = None # 用户配置为无限制 -> 无限制
        else:
            safe_rating_limit = config_limit # 应用配置的限制

        if safe_rating_limit is not None:
            logger.trace(f"  🛡️ 媒体库 '{library_name}' 将应用分级限制: 等级 <= {safe_rating_limit}")

        # ======================================================================
        # 策略 A: 实时筛选类合集 (Filter / AI Recommendation)
        # ======================================================================
        if custom_collection_data and custom_collection_data.get('type') in ['filter', 'ai_recommendation']:
            logger.info(f"  ➜ 检测到 '{library_name}' 为实时筛选/推荐合集，正在调用查询引擎...")
            try:
                definition = custom_collection_data.get('definition_json', {})
                rules = definition.get('rules', [])
                
                # 如果规则里显式指定了分级筛选，则信任规则，不强制覆盖
                has_rating_rule = any(r.get('field') == 'unified_rating' for r in rules)
                current_limit = safe_rating_limit if not has_rating_rule else None

                db_sort_by = 'Random' if self._sort_by == 'Random' else 'DateCreated'
                
                items_from_db, _ = queries_db.query_virtual_library_items(
                    rules=rules,
                    logic=definition.get('logic', 'AND'),
                    user_id=user_id,
                    limit=limit * 4,
                    offset=0,
                    sort_by=db_sort_by,
                    item_types=definition.get('item_type', ['Movie']),
                    target_library_ids=definition.get('target_library_ids'),
                    max_rating_override=current_limit # ★ 传入限制
                )
                
                return self.__fetch_emby_items_by_ids(items_from_db, base_url, api_key, user_id, limit)

            except Exception as e:
                logger.error(f"  ➜ 处理实时合集 '{library_name}' 出错: {e}", exc_info=True)

        # ======================================================================
        # 策略 B: 静态/缓存类合集 (List / Global AI)
        # ======================================================================
        custom_collection = custom_collection_data
        if not custom_collection:
            custom_collection = custom_collection_db.get_custom_collection_by_emby_id(library_id)
    
        if custom_collection and custom_collection.get('type') in ['list', 'ai_recommendation_global']:
            # 静态列表通常是用户手动挑选的，一般不应用分级过滤，或者应用后会导致列表变空
            # 这里我们选择：如果不是白名单库，依然应用过滤 (防止手动把 R 级片加到首页推荐)
            # 但由于静态列表没有 SQL 查询过程，我们需要在获取到 Emby Item 后进行过滤 (后置过滤)
            # 为了简单，这里暂不处理静态列表的强过滤，假设用户手动添加即为允许。
            # 如果需要过滤，可以在 __fetch_emby_items_by_ids 后遍历检查 OfficialRating。
            
            logger.info(f"  ➜ 检测到 '{library_name}' 为榜单/全局推荐合集...")
            try:
                media_info_list = custom_collection.get('generated_media_info_json') or []
                if isinstance(media_info_list, str): media_info_list = json.loads(media_info_list)
                    
                valid_emby_ids = [
                    str(item['emby_id']) 
                    for item in media_info_list 
                    if item.get('emby_id') and str(item.get('emby_id')).lower() != 'none'
                ]

                if valid_emby_ids:
                    if self._sort_by == "Random": random.shuffle(valid_emby_ids)
                    # 构造伪对象传给 fetcher
                    items_payload = [{'Id': i} for i in valid_emby_ids[:limit * 5]]
                    return self.__fetch_emby_items_by_ids(items_payload, base_url, api_key, user_id, limit)
                
                # Fallback: 现有成员
                fallback_items = emby.get_emby_library_items(
                    base_url=base_url, api_key=api_key, user_id=user_id,
                    library_ids=[library_id],
                    media_type_filter="Movie,Series,Season,Episode", 
                    fields="Id,Name,Type,ImageTags,BackdropImageTags,PrimaryImageTag,PrimaryImageItemId",
                    limit=limit
                )
                return self.__deduplicate_items_by_image(fallback_items, limit)

            except Exception as e:
                logger.error(f"  ➜ 处理自定义合集 '{library_name}' 出错: {e}", exc_info=True)
        
        # ======================================================================
        # 策略 C: 普通媒体库 (Native Library) - ★★★ 核心修改 ★★★
        # ======================================================================
        # 以前是直接调 API，现在改为：优先查 DB (应用分级限制) -> 失败则调 API
        
        # 1. 确定类型
        media_type_to_fetch = None
        if content_types:
            media_type_to_fetch = content_types # List
        else:
            TYPE_MAP = {
                'movies': ['Movie'], 'tvshows': ['Series'], 'music': ['MusicAlbum'],
                'boxsets': ['Movie', 'Series'], 'mixed': ['Movie', 'Series'], 
                'audiobooks': ['AudioBook']
            }
            c_type = library.get('CollectionType')
            media_type_to_fetch = TYPE_MAP.get(c_type, ['Movie', 'Series'])
            
            if library.get('Type') == 'BoxSet':
                media_type_to_fetch = ['Movie'] # 简化处理

        # 2. 确定排序
        db_sort_by = 'Random' if self._sort_by == 'Random' else 'DateCreated'
        
        # 3. ★★★ 尝试从数据库查询 (这是堵住漏洞的关键) ★★★
        # 利用 query_virtual_library_items 的 target_library_ids 功能
        try:
            items_from_db, _ = queries_db.query_virtual_library_items(
                rules=[], # 无额外规则
                logic='AND',
                user_id=None, # 使用管理员视角，但通过 override 限制分级
                limit=limit * 4,
                offset=0,
                sort_by=db_sort_by,
                item_types=media_type_to_fetch,
                target_library_ids=[library_id], # ★ 指定原生库 ID
                max_rating_override=safe_rating_limit # ★ 应用分级限制
            )

            if items_from_db:
                logger.trace(f"  ➜ 原生库 '{library_name}' 通过数据库查询命中 {len(items_from_db)} 个项目 (已过滤分级)。")
                return self.__fetch_emby_items_by_ids(items_from_db, base_url, api_key, user_id, limit)
            else:
                logger.debug(f"  ➜ 原生库 '{library_name}' 数据库查询为空 (可能是新库未同步)，回退到 API 直接调用。")

        except Exception as e:
            logger.warning(f"  ➜ 原生库 '{library_name}' 数据库查询失败: {e}，回退到 API。")

        # 4. API 回退 (兜底逻辑，保持原有行为，但无法精确过滤分级)
        # 如果数据库没数据，说明还没同步，此时只能调 API。
        # API 调用的缺点是无法利用我们的 max_rating_override 逻辑 (除非去解析 OfficialRating 字符串)
        
        api_limit = limit * 5 if limit < 10 else limit * 2 
        str_types = ",".join(media_type_to_fetch)
        
        sort_by_param = "Random" if self._sort_by == "Random" else "DateCreated"
        sort_order_param = "Descending" if sort_by_param == "DateCreated" else None

        all_items = emby.get_emby_library_items(
            base_url=base_url, api_key=api_key, user_id=user_id,
            library_ids=[library_id],
            media_type_filter=str_types,
            fields="Id,Name,Type,ImageTags,BackdropImageTags,DateCreated,PrimaryImageTag,PrimaryImageItemId",
            sort_by=sort_by_param,
            sort_order=sort_order_param,
            limit=api_limit,
            force_user_endpoint=True
        )
        
        if not all_items: return []
        valid_items = [item for item in all_items if self.__get_image_url(item)]
        
        if self._sort_by == "Random":
            random.shuffle(valid_items)
            
        return self.__deduplicate_items_by_image(valid_items, limit)

    # ★★★ 辅助方法：根据 ID 列表批量获取 Emby 详情 (带图片Tag) ★★★
    def __fetch_emby_items_by_ids(self, items_from_db: List[Dict], base_url: str, api_key: str, user_id: str, limit: int) -> List[Dict]:
        if not items_from_db: return []
        
        target_ids = [item['Id'] for item in items_from_db]
        ids_str = ",".join(target_ids)
        
        url = f"{base_url.rstrip('/')}/Users/{user_id}/Items"
        headers = {"X-Emby-Token": api_key, "Content-Type": "application/json"}
        params = {
            'Ids': ids_str,
            'Fields': "Id,Name,Type,ImageTags,BackdropImageTags,PrimaryImageTag,PrimaryImageItemId",
        }
        
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items_from_emby = data.get('Items', [])
            
            valid_items = [item for item in items_from_emby if self.__get_image_url(item)]
            
            # 如果是随机排序，这里再洗一次牌，因为 API 返回的顺序可能被 ID 顺序影响
            if self._sort_by == "Random":
                random.shuffle(valid_items)
            
            return self.__deduplicate_items_by_image(valid_items, limit)
        except Exception as e:
            logger.error(f"  ➜ 批量获取 Emby 项目详情失败: {e}")
            return []

    def __deduplicate_items_by_image(self, items: List[Dict], limit: int) -> List[Dict]:
        """Prevent multi-poster templates from repeating shared primary artwork."""
        unique_items = []
        seen_image_urls = set()
        for item in items:
            image_url = self.__get_image_url(item)
            if not image_url or image_url in seen_image_urls:
                continue
            seen_image_urls.add(image_url)
            unique_items.append(item)
            if len(unique_items) >= limit:
                break
        return unique_items

    def __get_image_url(self, item: Dict[str, Any]) -> str:
        primary_url = self.__get_primary_image_url(item)
        backdrop_url = self.__get_backdrop_image_url(item)
        should_use_primary = (self._cover_style.startswith('single') and self._single_use_primary) or \
                             (self._cover_style.startswith('multi') and self._multi_1_use_primary) or \
                             self._cover_style == 'chillposter'

        if should_use_primary:
            return primary_url or backdrop_url
        else:
            return backdrop_url or primary_url

    def __get_primary_image_url(self, item: Dict[str, Any]) -> str:
        item_id = item.get("Id")
        if not item_id: return None
        primary_tag_in_dict = item.get("ImageTags", {}).get("Primary")
        if primary_tag_in_dict:
            return f'/emby/Items/{item_id}/Images/Primary?tag={primary_tag_in_dict}'
        else:
            referenced_item_id = item.get("PrimaryImageItemId")
            referenced_tag = item.get("PrimaryImageTag")
            if referenced_item_id and referenced_tag:
                return f'/emby/Items/{referenced_item_id}/Images/Primary?tag={referenced_tag}'
        return None

    def __get_backdrop_image_url(self, item: Dict[str, Any]) -> str:
        item_id = item.get("Id")
        if not item_id: return None
        backdrop_tags = item.get("BackdropImageTags")
        if backdrop_tags:
            return f'/emby/Items/{item_id}/Images/Backdrop/0?tag={backdrop_tags[0]}'
        return None

    def __download_image(self, server_id: str, api_path: str, library_name: str, count: int, prefix: str = "") -> Path:
        subdir = self.covers_path / library_name
        subdir.mkdir(parents=True, exist_ok=True)
        filepath = subdir / f"{prefix}{count}.jpg"
        try:
            base_url = config_manager.APP_CONFIG.get('emby_server_url')
            api_key = config_manager.APP_CONFIG.get('emby_api_key')
            path_only, _, query_string = api_path.partition('?')
            path_parts = path_only.strip('/').split('/')
            image_tag = None
            if 'tag=' in query_string:
                image_tag = query_string.split('tag=')[1].split('&')[0]
            if len(path_parts) >= 4 and path_parts[1] == 'Items' and path_parts[3] == 'Images':
                item_id = path_parts[2]
                image_type = path_parts[4]
                success = emby.download_emby_image(
                    item_id=item_id, image_type=image_type, image_tag=image_tag,
                    save_path=str(filepath), emby_server_url=base_url, emby_api_key=api_key
                )
                if success: return filepath
            else:
                logger.error(f"  ➜ 无法从API路径解析有效的项目ID和图片类型: {api_path}")
        except Exception as e:
            logger.error(f"  ➜ 下载图片失败 ({api_path}): {e}", exc_info=True)
        return None

    def __generate_image_from_path(self, library_name: str, title: Tuple[str, str], image_paths: List[str], item_count: Optional[int] = None, backdrop_paths: Optional[List[str]] = None) -> Union[bytes, str, None]:
        logger.trace(f"  ➜ 正在为 '{library_name}' 从本地路径生成封面...")
        if self._cover_style == 'chillposter':
            return create_chillposter_cover(
                poster_paths=[str(path) for path in image_paths],
                backdrop_paths=[str(path) for path in (backdrop_paths or [])],
                title=title,
                item_count=item_count,
                config=self.config,
            )
        zh_font_size = self.config.get("zh_font_size", 1)
        en_font_size = self.config.get("en_font_size", 1)
        blur_size = self.config.get("blur_size", 50)
        color_ratio = self.config.get("color_ratio", 0.8)
        font_size = (float(zh_font_size), float(en_font_size))
        if self._cover_style == 'single_1':
            return create_style_single_1(str(image_paths[0]), title, (str(self.zh_font_path), str(self.en_font_path)), 
                                         font_size=font_size, blur_size=blur_size, color_ratio=color_ratio,
                                         item_count=item_count, config=self.config)
        elif self._cover_style == 'single_2':
            return create_style_single_2(str(image_paths[0]), title, (str(self.zh_font_path), str(self.en_font_path)), 
                                         font_size=font_size, blur_size=blur_size, color_ratio=color_ratio,
                                         item_count=item_count, config=self.config)
        elif self._cover_style == 'multi_1':
            if self.zh_font_path_multi_1 and self.zh_font_path_multi_1.exists():
                zh_font_path_multi = self.zh_font_path_multi_1
            else:
                logger.warning(f"  ➜ 未找到多图专用中文字体 ({self.zh_font_path_multi_1})，将回退使用单图字体。")
                zh_font_path_multi = self.zh_font_path
            if self.en_font_path_multi_1 and self.en_font_path_multi_1.exists():
                en_font_path_multi = self.en_font_path_multi_1
            else:
                logger.warning(f"  ➜ 未找到多图专用英文字体 ({self.en_font_path_multi_1})，将回退使用单图字体。")
                en_font_path_multi = self.en_font_path
            font_path_multi = (str(zh_font_path_multi), str(en_font_path_multi))
            zh_font_size_multi = self.config.get("zh_font_size_multi_1", 1)
            en_font_size_multi = self.config.get("en_font_size_multi_1", 1)
            font_size_multi = (float(zh_font_size_multi), float(en_font_size_multi))
            blur_size_multi = self.config.get("blur_size_multi_1", 50)
            color_ratio_multi = self.config.get("color_ratio_multi_1", 0.8)
            library_dir = self.covers_path / library_name
            self.__prepare_multi_images(library_dir, image_paths)
            return create_style_multi_1(str(library_dir), title, font_path_multi, 
                                      font_size=font_size_multi, is_blur=self._multi_1_blur, 
                                      blur_size=blur_size_multi, color_ratio=color_ratio_multi,
                                      item_count=item_count, config=self.config)
        return None

    def __set_library_image(self, server_id: str, library: Dict[str, Any], image_data: Union[bytes, str]) -> bool:
        library_id = library.get("Id") or library.get("ItemId")
        base_url = config_manager.APP_CONFIG.get('emby_server_url')
        api_key = config_manager.APP_CONFIG.get('emby_api_key')
        upload_url = f"{base_url.rstrip('/')}/Items/{library_id}/Images/Primary?api_key={api_key}"
        raw_image_data = self.__normalize_image_data(image_data)
        if not raw_image_data:
            logger.error(f"  ➜ 媒体库 '{library['Name']}' 的封面数据格式无效，无法上传。")
            return False
        _content_type, extension = self.__get_image_upload_type(raw_image_data)
        upload_candidates = self.__build_emby_upload_candidates(raw_image_data)
        if self._covers_output:
            try:
                save_path = Path(self._covers_output) / f"{library['Name']}{extension}"
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(raw_image_data)
                logger.info(f"  ➜ 封面已另存到: {save_path}")
                for candidate in upload_candidates[1:]:
                    emby_save_path = Path(self._covers_output) / f"{library['Name']}.{candidate['name']}{candidate['extension']}"
                    with open(emby_save_path, "wb") as f:
                        f.write(candidate["data"])
                    logger.info(f"  ➜ Emby 备用封面已另存到: {emby_save_path}")
            except Exception as e:
                logger.error(f"  ➜ 另存封面失败: {e}")

        if self.__is_animated_image(raw_image_data):
            logger.info("  ➜ 检测到动态封面，将尝试原样上传；若 Emby 未确认生效，再降级为静态 JPEG。")

        before_tag = self.__get_primary_image_tag(library_id)
        try:
            if library_id:
                UPDATING_IMAGES.add(library_id)
                
                def _clear_flag():
                    UPDATING_IMAGES.discard(library_id)
                spawn_later(30, _clear_flag)
            for index, candidate in enumerate(upload_candidates):
                if index > 0:
                    logger.warning(
                        "  ➜ Emby 未确认上一种封面格式可用，改用备用格式: %s。",
                        candidate["label"],
                    )
                if not self.__upload_primary_image(upload_url, api_key, library, candidate):
                    continue
                gevent_sleep(0.8)
                if self.__verify_primary_image_upload(library_id, before_tag, candidate):
                    self.__refresh_emby_image_cache(library_id)
                    return True
                before_tag = self.__get_primary_image_tag(library_id)

            logger.error(f"  ➜ Emby 未能确认媒体库 '{library['Name']}' 的封面已生效。")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"  ➜ 上传封面到媒体库 '{library['Name']}' 时发生网络错误: {e}")
            if e.response is not None:
                logger.error(f"  ➜ 响应状态: {e.response.status_code}, 响应内容: {e.response.text[:200]}")
            return False

    def __normalize_image_data(self, image_data: Union[bytes, str]) -> Optional[bytes]:
        if isinstance(image_data, bytes):
            return image_data
        if not isinstance(image_data, str):
            return None

        normalized = image_data.strip()
        if not normalized:
            return None
        if "," in normalized and normalized.lower().startswith("data:image/"):
            normalized = normalized.split(",", 1)[1]

        try:
            return base64.b64decode(normalized, validate=True)
        except (binascii.Error, ValueError) as e:
            logger.error(f"  ➜ 解码模板输出的 base64 封面失败: {e}")
            return None

    def __build_emby_upload_candidates(self, image_data: bytes) -> List[Dict[str, Any]]:
        content_type, extension = self.__get_image_upload_type(image_data)
        candidates = [{
            "name": "original",
            "label": f"原始格式 {content_type}",
            "data": image_data,
            "content_type": content_type,
            "extension": extension,
            "expect_animation": self.__is_animated_image(image_data),
        }]

        jpeg_data = self.__convert_image_to_jpeg(image_data)
        if jpeg_data and (content_type != "image/jpeg" or candidates[0]["expect_animation"]):
            candidates.append({
                "name": "static-jpeg",
                "label": "静态 JPEG 兼容版",
                "data": jpeg_data,
                "content_type": "image/jpeg",
                "extension": ".jpg",
                "expect_animation": False,
            })

        return candidates

    def __get_image_upload_type(self, image_data: bytes) -> Tuple[str, str]:
        if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if image_data.startswith(b"GIF87a") or image_data.startswith(b"GIF89a"):
            return "image/gif", ".gif"
        if image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
            return "image/webp", ".webp"
        return "image/jpeg", ".jpg"

    def __is_animated_image(self, image_data: bytes) -> bool:
        if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
            return b"acTL" in image_data and b"fcTL" in image_data
        if image_data.startswith(b"GIF87a") or image_data.startswith(b"GIF89a"):
            return True
        if image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
            return b"ANIM" in image_data[:2048]
        try:
            with Image.open(BytesIO(image_data)) as img:
                return bool(getattr(img, "is_animated", False)) or int(getattr(img, "n_frames", 1) or 1) > 1
        except Exception:
            return False

    def __convert_image_to_jpeg(self, image_data: bytes) -> Optional[bytes]:
        try:
            with Image.open(BytesIO(image_data)) as img:
                img.seek(0)
                frame = img.convert("RGBA")
                background = Image.new("RGB", frame.size, (0, 0, 0))
                alpha = frame.getchannel("A")
                background.paste(frame, mask=alpha)
                output = BytesIO()
                background.save(output, format="JPEG", quality=92)
                return output.getvalue()
        except Exception as e:
            logger.warning(f"  ➜ 图片转 JPEG 兼容版失败: {e}")
            return None

    def __upload_primary_image(self, upload_url: str, api_key: str, library: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
        headers = {
            "Content-Type": candidate["content_type"],
            "X-Emby-Token": api_key or "",
        }
        try:
            encoded_image = base64.b64encode(candidate["data"]).decode("ascii")
            logger.info(
                "  ➜ 正在上传 Emby 封面: %s, %s bytes (%s)。",
                candidate["content_type"],
                len(candidate["data"]),
                candidate["label"],
            )
            response = requests.post(upload_url, data=encoded_image, headers=headers, timeout=60)
            response.raise_for_status()
            logger.debug(f"  ➜ 已提交封面到媒体库 '{library['Name']}'。")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"  ➜ 上传封面到媒体库 '{library['Name']}' 时发生网络错误: {e}")
            if e.response is not None:
                logger.error(f"  ➜ 响应状态: {e.response.status_code}, 响应内容: {e.response.text[:200]}")
            return False

    def __get_primary_image_tag(self, item_id: str) -> Optional[str]:
        base_url = config_manager.APP_CONFIG.get('emby_server_url')
        api_key = config_manager.APP_CONFIG.get('emby_api_key')
        user_id = config_manager.APP_CONFIG.get('emby_user_id')
        if not all([item_id, base_url, api_key, user_id]):
            return None
        try:
            item = emby.get_emby_item_details(
                item_id,
                base_url,
                api_key,
                user_id,
                fields="ImageTags,Name,Type",
                silent_404=True,
            )
            image_tags = item.get("ImageTags") if isinstance(item, dict) else None
            if isinstance(image_tags, dict):
                return image_tags.get("Primary")
        except Exception as e:
            logger.warning(f"  ➜ 读取 Emby 当前封面 Tag 失败 (ItemID: {item_id}): {e}")
        return None

    def __download_current_primary_image(self, item_id: str) -> Optional[bytes]:
        base_url = config_manager.APP_CONFIG.get('emby_server_url')
        api_key = config_manager.APP_CONFIG.get('emby_api_key')
        if not all([item_id, base_url, api_key]):
            return None
        url = f"{base_url.rstrip('/')}/Items/{item_id}/Images/Primary"
        params = {
            "api_key": api_key,
            "_": str(int(time.time() * 1000)),
        }
        headers = {
            "X-Emby-Token": api_key or "",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        try:
            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.warning(f"  ➜ 下载 Emby 当前封面用于校验失败 (ItemID: {item_id}): {e}")
            return None

    def __verify_primary_image_upload(self, item_id: str, before_tag: Optional[str], candidate: Dict[str, Any]) -> bool:
        after_tag = self.__get_primary_image_tag(item_id)
        tag_changed = bool(after_tag and after_tag != before_tag)
        if tag_changed:
            logger.info(f"  ➜ Emby 封面 Tag 已更新: {before_tag or '-'} -> {after_tag}")
        elif after_tag:
            logger.warning(f"  ➜ Emby 封面 Tag 未变化，继续通过图片内容校验: {after_tag}")
        else:
            logger.warning("  ➜ 未能读取 Emby 封面 Tag，继续通过图片内容校验。")

        if candidate.get("expect_animation"):
            current_image = self.__download_current_primary_image(item_id)
            if current_image and self.__is_animated_image(current_image):
                logger.info("  ✅ Emby 已保留动态 PNG 封面。")
                return True
            if current_image:
                logger.warning("  ➜ Emby 已接收封面但未保留动画块，准备降级为静态 JPEG。")
                return False
            if tag_changed:
                logger.warning("  ➜ Emby 封面 Tag 已变化，但未能回读确认动画；暂按动态封面上传成功处理。")
                return True
            logger.warning("  ➜ Emby 未确认动态封面已替换，准备降级为静态 JPEG。")
            return False

        current_image = self.__download_current_primary_image(item_id)
        if current_image and (tag_changed or not before_tag):
            logger.info("  ✅ Emby 当前封面已可读取，上传生效。")
            return True

        return tag_changed

    def __refresh_emby_image_cache(self, item_id: str) -> None:
        base_url = config_manager.APP_CONFIG.get('emby_server_url')
        api_key = config_manager.APP_CONFIG.get('emby_api_key')
        if not all([item_id, base_url, api_key]):
            return
        refresh_url = f"{base_url.rstrip('/')}/Items/{item_id}/Refresh"
        params = {
            "api_key": api_key,
            "Recursive": "false",
            "ImageRefreshMode": "Default",
            "MetadataRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
        }
        try:
            response = requests.post(refresh_url, params=params, timeout=15)
            if response.status_code in (200, 204):
                logger.debug(f"  ➜ 已请求 Emby 刷新封面缓存 (ItemID: {item_id})。")
            else:
                logger.warning(f"  ➜ Emby 封面缓存刷新返回异常状态: {response.status_code}")
        except Exception as e:
            logger.warning(f"  ➜ Emby 封面缓存刷新失败 (ItemID: {item_id}): {e}")

    def __get_library_title_from_yaml(self, library_name: str) -> Tuple[str, str]:
        zh_title, en_title = library_name, ''
        if not self._title_config_str:
            return zh_title, en_title
        try:
            title_config = yaml.safe_load(self._title_config_str)
            if isinstance(title_config, dict) and library_name in title_config:
                titles = title_config[library_name]
                if isinstance(titles, list) and len(titles) >= 2:
                    zh_title = str(titles[0]).strip() if titles[0] is not None else ''
                    en_title = str(titles[1]).strip() if titles[1] is not None else ''
                    if not zh_title:
                        zh_title = library_name
        except yaml.YAMLError as e:
            logger.error(f"  ➜ 解析标题配置失败: {e}")
        return zh_title, en_title

    def __prepare_multi_images(self, library_dir: Path, source_paths: List[str]):
        library_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 10):
            target_path = library_dir / f"{i}.jpg"
            if not target_path.exists():
                source_to_copy = random.choice(source_paths)
                shutil.copy(source_to_copy, target_path)

    def __check_custom_image(self, library_name: str) -> List[str]:
        if not self._covers_input: return []
        library_dir = Path(self._covers_input) / library_name
        if not library_dir.is_dir(): return []
        images = sorted([
            str(p) for p in library_dir.iterdir()
            if p.suffix.lower() in [".jpg", ".jpeg", ".png"]
        ])
        return images

    def __download_file(self, url: str, dest_path: Path):
        if dest_path.exists():
            logger.trace(f"  ➜ 字体文件已存在，跳过下载: {dest_path.name}")
            return
        logger.info(f"  ➜ 字体文件不存在，正在从URL下载: {dest_path.name}...")
        try:
            # ★★★ 注入代理 ★★★
            proxies = config_manager.get_proxies_for_requests()
            response = requests.get(url, stream=True, timeout=60, proxies=proxies)
            response.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"  ➜ 字体 '{dest_path.name}' 下载成功。")
        except requests.RequestException as e:
            logger.error(f"  ➜ 下载字体 '{dest_path.name}' 失败: {e}")
            if dest_path.exists():
                dest_path.unlink()

    def __get_fonts(self):
        if self._fonts_checked_and_ready:
            return
        font_definitions = [
            {"target_attr": "zh_font_path", "filename": "zh_font.ttf", "local_key": "zh_font_path_local", "url_key": "zh_font_url"},
            {"target_attr": "en_font_path", "filename": "en_font.ttf", "local_key": "en_font_path_local", "url_key": "en_font_url"},
            {"target_attr": "zh_font_path_multi_1", "filename": "zh_font_multi_1.ttf", "local_key": "zh_font_path_multi_1_local", "url_key": "zh_font_url_multi_1"},
            {"target_attr": "en_font_path_multi_1", "filename": "en_font_multi_1.otf", "local_key": "en_font_path_multi_1_local", "url_key": "en_font_url_multi_1"}
        ]
        for font_def in font_definitions:
            font_path_to_set = None
            expected_font_file = self.font_path / font_def["filename"]
            if expected_font_file.exists():
                font_path_to_set = expected_font_file
            local_path_str = self.config.get(font_def["local_key"])
            if local_path_str:
                local_path = Path(local_path_str)
                if local_path.exists():
                    logger.trace(f"  ➜ 发现并优先使用用户指定的外部字体: {local_path_str}")
                    font_path_to_set = local_path
                else:
                    logger.warning(f"  ➜ 配置的外部字体路径不存在: {local_path_str}，将忽略此配置。")
            if not font_path_to_set:
                url = self.config.get(font_def["url_key"])
                if url:
                    self.__download_file(url, expected_font_file)
                    if expected_font_file.exists():
                        font_path_to_set = expected_font_file
            setattr(self, font_def["target_attr"], font_path_to_set)
        if self.zh_font_path and self.en_font_path:
            logger.trace("  ➜ 核心字体文件已准备就绪。后续任务将不再重复检查。")
            self._fonts_checked_and_ready = True
        else:
            logger.warning("  ➜ 一个或多个核心字体文件缺失且无法下载。请检查UI中的本地路径或下载链接是否有效。")
