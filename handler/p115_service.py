# handler/p115_service.py
import logging
import requests
import random
import os
import re
import threading
import time
import config_manager
import constants
from database import settings_db
import handler.tmdb as tmdb
import utils
try:
    from p115client import P115Client
except ImportError:
    P115Client = None

logger = logging.getLogger(__name__)

# --- CMS通知防抖定时器 ---
_cms_timer = None
_cms_lock = threading.Lock()

def get_config():
    return settings_db.get_setting('p115_config') or settings_db.get_setting('nullbr_config') or {}

def get_sorting_rules():
    return settings_db.get_setting('p115_sorting_rules') or settings_db.get_setting('nullbr_sorting_rules') or []

class P115Service:
    _instance = None
    _lock = threading.Lock()
    _client = None
    _last_request_time = 0
    _cookies_cache = None

    @classmethod
    def get_client(cls):
        """获取全局唯一的 P115Client 实例 (带自动重载和限流)"""
        if P115Client is None:
            raise ImportError("未安装 p115client")

        # 获取配置
        config = get_config()
        cookies = config.get('p115_cookies')
        
        if not cookies:
            return None

        with cls._lock:
            # 如果 Cookies 变了，或者客户端还没初始化，就重新初始化
            if cls._client is None or cookies != cls._cookies_cache:
                try:
                    cls._client = P115Client(cookies)
                    cls._cookies_cache = cookies
                    logger.debug("  ✅ P115Client 实例已(重新)初始化")
                except Exception as e:
                    logger.error(f"  ❌ P115Client 初始化失败: {e}")
                    return None
            
            # ★★★ 全局限流逻辑 ★★★
            interval = int(config.get('request_interval', 5))
            current_time = time.time()
            elapsed = current_time - cls._last_request_time
            
            if elapsed < interval:
                sleep_time = interval - elapsed
                # 只有等待时间超过1秒才打印日志，避免刷屏
                if sleep_time > 1:
                    logger.debug(f"  ⏳ [115限流] 全局等待 {sleep_time:.2f} 秒...")
                time.sleep(sleep_time)
            
            cls._last_request_time = time.time()
            
            return cls._client

    @classmethod
    def get_cookies(cls):
        config = get_config()
        return config.get('p115_cookies')
    
_directory_cid_cache = {} # 全局目录 CID 缓存，key 格式: f"{parent_cid}_{dir_name}"
class SmartOrganizer:
    def __init__(self, client, tmdb_id, media_type, original_title):
        self.client = client
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        self.original_title = original_title
        self.api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)

        self.studio_map = settings_db.get_setting('studio_mapping') or utils.DEFAULT_STUDIO_MAPPING
        self.keyword_map = settings_db.get_setting('keyword_mapping') or utils.DEFAULT_KEYWORD_MAPPING
        self.rating_map = settings_db.get_setting('rating_mapping') or utils.DEFAULT_RATING_MAPPING
        self.rating_priority = settings_db.get_setting('rating_priority') or utils.DEFAULT_RATING_PRIORITY

        self.raw_metadata = self._fetch_raw_metadata()
        self.details = self.raw_metadata
        self.rules = get_sorting_rules()

    def _fetch_raw_metadata(self):
        """
        获取 TMDb 原始元数据 (ID/Code)，不进行任何中文转换。
        """
        if not self.api_key: return {}

        data = {
            'genre_ids': [],
            'country_codes': [],
            'lang_code': None,
            'company_ids': [],
            'network_ids': [],
            'keyword_ids': [],
            'rating_label': '未知' # 分级是特例，必须计算出标签才能匹配
        }

        try:
            raw_details = {}
            if self.media_type == 'tv':
                raw_details = tmdb.get_tv_details(
                    self.tmdb_id, self.api_key,
                    append_to_response="keywords,content_ratings,networks"
                )
            else:
                raw_details = tmdb.get_movie_details(
                    self.tmdb_id, self.api_key,
                    append_to_response="keywords,release_dates"
                )

            if not raw_details: return {}

            # 1. 基础 ID/Code 提取
            data['genre_ids'] = [g.get('id') for g in raw_details.get('genres', [])]
            data['country_codes'] = [c.get('iso_3166_1') for c in raw_details.get('production_countries', [])]
            if not data['country_codes'] and raw_details.get('origin_country'):
                data['country_codes'] = raw_details.get('origin_country')

            data['lang_code'] = raw_details.get('original_language')

            data['company_ids'] = [c.get('id') for c in raw_details.get('production_companies', [])]
            data['network_ids'] = [n.get('id') for n in raw_details.get('networks', [])] if self.media_type == 'tv' else []

            # 2. 关键词 ID 提取
            kw_container = raw_details.get('keywords', {})
            raw_kw_list = kw_container.get('keywords', []) if self.media_type == 'movie' else kw_container.get('results', [])
            data['keyword_ids'] = [k.get('id') for k in raw_kw_list]

            # 3. 分级计算 
            data['rating_label'] = utils.get_rating_label(
                raw_details,
                self.media_type,
                self.rating_map,
                self.rating_priority
            )

            # 补充标题日期供重命名
            data['title'] = raw_details.get('title') or raw_details.get('name')
            date_str = raw_details.get('release_date') or raw_details.get('first_air_date')
            data['date'] = date_str
            data['year'] = 0
            
            if date_str and len(str(date_str)) >= 4:
                try:
                    data['year'] = int(str(date_str)[:4])
                except: 
                    pass
            # 补充评分供规则匹配
            data['vote_average'] = raw_details.get('vote_average', 0)

            return data

        except Exception as e:
            logger.warning(f"  ⚠️ [整理] 获取原始元数据失败: {e}", exc_info=True)
            return {}

    def _match_rule(self, rule):
        """
        规则匹配逻辑：
        - 标准字段：直接比对 ID/Code
        - 集合字段（工作室/关键词）：通过 Label 反查 Config 中的 ID 列表，再比对 TMDb ID
        """
        if not self.raw_metadata: return False

        # 1. 媒体类型
        if rule.get('media_type') and rule['media_type'] != 'all':
            if rule['media_type'] != self.media_type: return False

        # 2. 类型 (Genres) - ID 匹配
        if rule.get('genres'):
            # rule['genres'] 存的是 ID 列表 (如 [16, 35])
            # self.raw_metadata['genre_ids'] 是 TMDb ID 列表
            # 只要有一个交集就算命中
            rule_ids = [int(x) for x in rule['genres']]
            if not any(gid in self.raw_metadata['genre_ids'] for gid in rule_ids): return False

        # 3. 国家 (Countries) - Code 匹配
        if rule.get('countries'):
            # rule['countries'] 存的是 Code (如 ['US', 'CN'])
            if not any(c in self.raw_metadata['country_codes'] for c in rule['countries']): return False

        # 4. 语言 (Languages) - Code 匹配
        if rule.get('languages'):
            if self.raw_metadata['lang_code'] not in rule['languages']: return False

        # 5. 工作室 (Studios) - Label -> ID 匹配
        if rule.get('studios'):
            # rule['studios'] 存的是 Label (如 ['漫威', 'Netflix'])
            # 我们需要遍历这些 Label，去 self.studio_map 里找对应的 ID
            target_ids = set()
            for label in rule['studios']:
                # 找到配置项
                config_item = next((item for item in self.studio_map if item['label'] == label), None)
                if config_item:
                    target_ids.update(config_item.get('company_ids', []))
                    target_ids.update(config_item.get('network_ids', []))

            # 检查 TMDb 的 company/network ID 是否在 target_ids 中
            has_company = any(cid in target_ids for cid in self.raw_metadata['company_ids'])
            has_network = any(nid in target_ids for nid in self.raw_metadata['network_ids'])

            if not (has_company or has_network): return False

        # 6. 关键词 (Keywords) - Label -> ID 匹配
        if rule.get('keywords'):
            target_ids = set()
            for label in rule['keywords']:
                config_item = next((item for item in self.keyword_map if item['label'] == label), None)
                if config_item:
                    target_ids.update(config_item.get('ids', []))

            # 兼容字符串/数字 ID
            tmdb_kw_ids = [int(k) for k in self.raw_metadata['keyword_ids']]
            target_ids_int = [int(k) for k in target_ids]

            if not any(kid in target_ids_int for kid in tmdb_kw_ids): return False

        # 7. 分级 (Rating) - Label 匹配
        if rule.get('ratings'):
            if self.raw_metadata['rating_label'] not in rule['ratings']: return False

        # 8. 年份 (Year) 
        year_min = rule.get('year_min')
        year_max = rule.get('year_max')
        
        if year_min or year_max:
            current_year = self.raw_metadata.get('year', 0)
            
            # 如果获取不到年份，且设置了年份限制，则视为不匹配
            if current_year == 0: return False
            
            if year_min and current_year < int(year_min): return False
            if year_max and current_year > int(year_max): return False

        # 9. 时长 (Runtime) 
        # 逻辑：电影取 runtime，剧集取 episode_run_time (列表取平均或第一个)
        run_min = rule.get('runtime_min')
        run_max = rule.get('runtime_max')

        if run_min or run_max:
            current_runtime = 0
            if self.media_type == 'movie':
                current_runtime = self.details.get('runtime') or 0
            else:
                # 剧集时长通常是一个列表 [45, 60]，取第一个作为参考
                runtimes = self.details.get('episode_run_time', [])
                if runtimes and len(runtimes) > 0:
                    current_runtime = runtimes[0]

            # 如果获取不到时长，且设置了限制，视为不匹配
            if current_runtime == 0: return False

            if run_min and current_runtime < int(run_min): return False
            if run_max and current_runtime > int(run_max): return False

        # 10. 评分 (Min Rating) - 数值比较
        if rule.get('min_rating') and float(rule['min_rating']) > 0:
            vote_avg = self.details.get('vote_average', 0)
            if vote_avg < float(rule['min_rating']):
                return False

        return True

    def get_target_cid(self):
        """遍历规则，返回命中的 CID。未命中返回 None"""
        for rule in self.rules:
            if not rule.get('enabled', True): continue
            if self._match_rule(rule):
                logger.info(f"  🎯 [115] 命中规则: {rule.get('name')} -> 目录: {rule.get('dir_name')}")
                return rule.get('cid')
        return None

    def _extract_video_info(self, filename):
        """
        从文件名提取视频信息 (来源 · 分辨率 · 编码 · 音频 · 制作组)
        参考格式: BluRay · 1080p · X264 · DDP 7.1 · CMCT
        """
        info_tags = []
        name_upper = filename.upper()

        # 1. 来源/质量 (Source)
        source = ""
        if re.search(r'REMUX', name_upper): source = 'Remux'
        elif re.search(r'BLU-?RAY|BD', name_upper): source = 'BluRay'
        elif re.search(r'WEB-?DL', name_upper): source = 'WEB-DL'
        elif re.search(r'WEB-?RIP', name_upper): source = 'WEBRip'
        elif re.search(r'HDTV', name_upper): source = 'HDTV'
        elif re.search(r'DVD', name_upper): source = 'DVD'

        # ★★★ 修复：UHD 识别 ★★★
        if 'UHD' in name_upper:
            if source == 'BluRay': source = 'UHD BluRay'
            elif not source: source = 'UHD'

        # 2. 特效 (Effect: HDR/DV)
        effect = ""
        is_dv = re.search(r'(?:^|[\.\s\-\_])(DV|DOVI|DOLBY\s?VISION)(?:$|[\.\s\-\_])', name_upper)
        is_hdr = re.search(r'(?:^|[\.\s\-\_])(HDR|HDR10\+?)(?:$|[\.\s\-\_])', name_upper)

        if is_dv and is_hdr: effect = "HDR DV"
        elif is_dv: effect = "DV"
        elif is_hdr: effect = "HDR"

        if source:
            info_tags.append(f"{source} {effect}".strip())
        elif effect:
            info_tags.append(effect)

        # 3. 分辨率 (Resolution)
        res_match = re.search(r'(2160|1080|720|480)[pP]', filename)
        if res_match:
            info_tags.append(res_match.group(0).lower())
        elif '4K' in name_upper:
            info_tags.append('2160p')

        # 4. 编码 (Codec)
        codec = ""
        if re.search(r'[HX]265|HEVC', name_upper): info_tags.append('x265')
        elif re.search(r'[HX]264|AVC', name_upper): info_tags.append('H264')
        elif re.search(r'AV1', name_upper): info_tags.append('AV1')
        elif re.search(r'MPEG-?2', name_upper): info_tags.append('MPEG2')
        # 比特率提取 (Bit Depth) 
        bit_depth = ""
        bit_match = re.search(r'(\d{1,2})BIT', name_upper)
        if bit_match:
            bit_depth = f"{bit_match.group(1)}bit" # 统一格式为小写 bit

        # 将编码和比特率组合，比如 "H265 10bit" 或单独 "H265"
        if codec:
            full_codec = f"{codec} {bit_depth}".strip()
            info_tags.append(full_codec)
        elif bit_depth:
            info_tags.append(bit_depth)

        # 5. 音频 (Audio) - ★★★ 修复重点 ★★★
        audio_info = []
        
        # (1) 优先匹配带数字的音轨 (2Audio, 3Audios) 并统一格式为 "xAudios"
        # 正则说明: 匹配边界 + 数字 + 空格(可选) + Audio + s(可选) + 边界
        num_audio_match = re.search(r'\b(\d+)\s?Audios?\b', name_upper, re.IGNORECASE)
        if num_audio_match:
            # 统一格式化为: 数字 + Audios (例如: 2Audios)
            audio_info.append(f"{num_audio_match.group(1)}Audios")
        else:
            # (2) 如果没有数字音轨，再匹配 Multi/Dual 等通用标签
            if re.search(r'\b(Multi|双语|多音轨|Dual-Audio)\b', name_upper, re.IGNORECASE):
                audio_info.append('Multi')

        # (3) 其他具体音频编码
        if re.search(r'ATMOS', name_upper): audio_info.append('Atmos')
        elif re.search(r'TRUEHD', name_upper): audio_info.append('TrueHD')
        elif re.search(r'DTS-?HD(\s?MA)?', name_upper): audio_info.append('DTS-HD')
        elif re.search(r'DTS', name_upper): audio_info.append('DTS')
        elif re.search(r'DDP|EAC3|DOLBY\s?DIGITAL\+', name_upper): audio_info.append('DDP')
        elif re.search(r'AC3|DD', name_upper): audio_info.append('AC3')
        elif re.search(r'AAC', name_upper): audio_info.append('AAC')
        elif re.search(r'FLAC', name_upper): audio_info.append('FLAC')
        elif re.search(r'OPUS', name_upper): audio_info.append('Opus')
        
        chan_match = re.search(r'\b(7\.1|5\.1|2\.0)\b', filename)
        if chan_match:
            audio_info.append(chan_match.group(1))
            
        if audio_info:
            info_tags.append(" ".join(audio_info))

        # 流媒体平台识别
        # 匹配 NF, AMZN, DSNP, HMAX, HULU, NETFLIX, DISNEY+, APPLETV+
        stream_match = re.search(r'\b(NF|AMZN|DSNP|HMAX|HULU|NETFLIX|DISNEY\+|APPLETV\+|B-GLOBAL)\b', name_upper)
        if stream_match:
            info_tags.append(stream_match.group(1))

        # 6. 发布组 (Release Group)
        group_found = False
        try:
            from tasks import helpers
            for group_name, patterns in helpers.RELEASE_GROUPS.items():
                for pattern in patterns:
                    try:
                        match = re.search(pattern, filename, re.IGNORECASE)
                        if match:
                            info_tags.append(match.group(0))
                            group_found = True
                            break
                    except: pass
                if group_found: break

            if not group_found:
                name_no_ext = os.path.splitext(filename)[0]
                match_suffix = re.search(r'-([a-zA-Z0-9]+)$', name_no_ext)
                if match_suffix:
                    possible_group = match_suffix.group(1)
                    if len(possible_group) > 2 and possible_group.upper() not in ['1080P', '2160P', '4K', 'HDR', 'H265', 'H264']:
                        info_tags.append(possible_group)
        except ImportError:
            pass

        return " · ".join(info_tags) if info_tags else ""

    def _rename_file_node(self, file_node, new_base_name, year=None, is_tv=False):
        """
        重命名单个文件节点
        修复：字幕文件先剥离语言标签，再提取Tags，确保能识别到被语言标签挡住的发布组。
        """
        original_name = file_node.get('n', '')
        if '.' not in original_name: return original_name, None

        # 分离文件名和扩展名
        parts = original_name.rsplit('.', 1)
        name_body = parts[0]
        ext = parts[1].lower()

        is_sub = ext in ['srt', 'ass', 'ssa', 'sub', 'vtt', 'sup']

        # -------------------------------------------------
        # 1. 优先计算字幕语言后缀 (为了后续剥离它)
        # -------------------------------------------------
        lang_suffix = ""
        if is_sub:
            # 常见语言代码白名单
            lang_keywords = [
                'zh', 'cn', 'tw', 'hk', 'en', 'jp', 'kr',
                'chs', 'cht', 'eng', 'jpn', 'kor', 'fre', 'spa',
                'default', 'forced', 'tc', 'sc'
            ]

            # 策略A: 检查文件名最后一段 (Movie.chs.srt)
            sub_parts = name_body.split('.')
            if len(sub_parts) > 1:
                last_part = sub_parts[-1].lower()
                if last_part in lang_keywords or '-' in last_part:
                    lang_suffix = f".{sub_parts[-1]}" # 保持原大小写

            # 策略B: 正则搜索
            if not lang_suffix:
                match = re.search(r'(?:\.|-|_|\s)(chs|cht|zh-cn|zh-tw|eng|jpn|kor|tc|sc)(?:\.|-|_|$)', name_body, re.IGNORECASE)
                if match:
                    lang_suffix = f".{match.group(1)}"

        # -------------------------------------------------
        # 2. 提取 Tags (关键修复步骤)
        # -------------------------------------------------
        tag_suffix = ""
        try:
            # 构造用于提取信息的“搜索名”
            search_name = original_name

            if is_sub:
                # 如果是字幕，把语言后缀和扩展名都去掉，伪装成纯视频文件名
                if lang_suffix and name_body.endswith(lang_suffix):
                    # 去掉 .zh
                    clean_body = name_body[:-len(lang_suffix)]
                    search_name = f"{clean_body}.mkv" # 补个假后缀防报错
                else:
                    # 如果没找到标准后缀，直接用 name_body
                    search_name = f"{name_body}.mkv"

            video_info = self._extract_video_info(search_name)
            if video_info:
                tag_suffix = f" · {video_info}"
        except Exception as e:
            # logger.debug(f"Tags提取失败: {e}")
            pass

        # -------------------------------------------------
        # 3. 构建新文件名
        # -------------------------------------------------
        if is_tv:
            # === 剧集模式 ===
            pattern = r'(?:s|S)(\d{1,2})(?:e|E)(\d{1,2})|Ep?(\d{1,2})|第(\d{1,3})[集话]'
            match = re.search(pattern, original_name)
            if match:
                s, e, ep_only, zh_ep = match.groups()
                season_num = int(s) if s else 1
                episode_num = int(e) if e else (int(ep_only) if ep_only else int(zh_ep))

                s_str = f"S{season_num:02d}"
                e_str = f"E{episode_num:02d}"

                # 格式：Title - S01E01 · Tags[.Lang].ext
                new_name = f"{new_base_name} - {s_str}{e_str}{tag_suffix}{lang_suffix}.{ext}"
                return new_name, season_num
            else:
                return original_name, None
        else:
            # === 电影模式 ===
            movie_base = f"{new_base_name} ({year})" if year else new_base_name

            # 格式：Title (Year) · Tags[.Lang].ext
            new_name = f"{movie_base}{tag_suffix}{lang_suffix}.{ext}"

            return new_name, None

    def _scan_files_recursively(self, cid, depth=0, max_depth=3):
        """递归扫描文件夹，返回所有文件的扁平列表"""
        all_files = []
        if depth > max_depth: return []

        try:
            # limit 调大一点，防止文件过多漏掉
            res = self.client.fs_files({'cid': cid, 'limit': 2000})
            if res.get('data'):
                for item in res['data']:
                    # 如果是文件 (有 fid)
                    if item.get('fid'):
                        all_files.append(item)
                    # 如果是文件夹 (无 fid)，且未达深度限制，递归
                    elif item.get('cid'):
                        sub_files = self._scan_files_recursively(item.get('cid'), depth + 1, max_depth)
                        all_files.extend(sub_files)
        except Exception as e:
            logger.warning(f"  ⚠️ 扫描目录出错 (CID: {cid}): {e}")

        return all_files

    def _is_junk_file(self, filename):
        """
        检查是否为垃圾文件/样本/花絮 (基于 MP 规则)
        """
        # 垃圾文件正则列表 (合并了通用规则和你提供的 MP 规则)
        junk_patterns = [
            # 基础关键词
            r'(?i)\b(sample|trailer|featurette|bonus)\b',

            # MP 规则集
            r'(?i)Special Ending Movie',
            r'(?i)\[((TV|BD|\bBlu-ray\b)?\s*CM\s*\d{2,3})\]',
            r'(?i)\[Teaser.*?\]',
            r'(?i)\[PV.*?\]',
            r'(?i)\[NC[OPED]+.*?\]',
            r'(?i)\[S\d+\s+Recap(\s+\d+)?\]',
            r'(?i)Menu',
            r'(?i)Preview',
            r'(?i)\b(CDs|SPs|Scans|Bonus|映像特典|映像|specials|特典CD|Menu|Logo|Preview|/mv)\b',
            r'(?i)\b(NC)?(Disc|片头|OP|SP|ED|Advice|Trailer|BDMenu|片尾|PV|CM|Preview|MENU|Info|EDPV|SongSpot|BDSpot)(\d{0,2}|_ALL)\b',
            r'(?i)WiKi\.sample'
        ]

        for pattern in junk_patterns:
            if re.search(pattern, filename):
                return True
        return False

    def execute(self, root_item, target_cid):
        """
        执行整理
        """
        # 1. 准备标准名称
        title = self.details.get('title') or self.original_title
        date_str = self.details.get('date') or ''
        year = date_str[:4] if date_str else ''

        safe_title = re.sub(r'[\\/:*?"<>|]', '', title).strip()
        std_root_name = f"{safe_title} ({year}) {{tmdb={self.tmdb_id}}}" if year else f"{safe_title} {{tmdb={self.tmdb_id}}}"

        source_root_id = root_item.get('fid') or root_item.get('cid')
        is_source_file = bool(root_item.get('fid'))

        dest_parent_cid = target_cid if (target_cid and str(target_cid) != '0') else root_item.get('cid')

        MIN_VIDEO_SIZE = 10 * 1024 * 1024

        video_exts = ['mp4', 'mkv', 'avi', 'ts', 'iso', 'rmvb', 'wmv', 'mov', 'm2ts']
        sub_exts = ['srt', 'ass', 'ssa', 'sub', 'vtt', 'sup']

        logger.info(f"  🚀 [115] 开始整理: {root_item.get('n')} -> {std_root_name}")

        # ==================================================
        # 步骤 A: 获取或创建目标标准文件夹 (带缓存优化)
        # ==================================================
        final_home_cid = None
        
        # 1. 构建缓存 Key (父目录CID + 目标目录名)
        cache_key = f"{dest_parent_cid}-{std_root_name}"
        
        # 2. 先查缓存
        if cache_key in _directory_cid_cache:
            final_home_cid = _directory_cid_cache[cache_key]
            logger.info(f"  ⚡ [缓存命中] 目录 CID: {final_home_cid}")
        
        # 3. 缓存未命中，走 API (乐观锁策略)
        if not final_home_cid:
            # 尝试直接创建
            mk_res = self.client.fs_mkdir(std_root_name, dest_parent_cid)
            
            if mk_res.get('state'):
                # 创建成功
                final_home_cid = mk_res.get('cid')
                logger.info(f"  🆕 创建新目录成功: {std_root_name}")
                # ★★★ 写入缓存 ★★★
                if self.media_type == 'tv': # 只有剧集模式才缓存目录 CID，因为电影模式可能每个文件夹都不一样
                    _directory_cid_cache[cache_key] = final_home_cid
                    logger.info(f"  ⚡ [缓存更新] 目录 CID: {final_home_cid}")
            else:
                # 创建失败，回退搜索
                try:
                    search_res = self.client.fs_files({
                        'cid': dest_parent_cid, 
                        'search_value': std_root_name, 
                        'limit': 1000, 
                    })
                    if search_res.get('data'):
                        for item in search_res['data']:
                            if item.get('n') == std_root_name and (item.get('ico') == 'folder' or not item.get('fid')):
                                final_home_cid = item.get('cid')
                                logger.info(f"  📂 发现已存在的目录: {std_root_name}")
                                if self.media_type == 'tv': # 只有剧集模式才缓存目录 CID，因为电影模式可能每个文件夹都不一样
                                    _directory_cid_cache[cache_key] = final_home_cid
                                    logger.info(f"  ⚡ [缓存更新] 目录 CID: {final_home_cid}")
                                break
                except Exception as e:
                    logger.warning(f"  ⚠️ 查找目录异常: {e}")

        # 如果经过创建和查找都拿不到 CID，说明真的出问题了
        if not final_home_cid:
            logger.error(f"  ❌ 无法获取目标目录 CID (创建失败且查找未果): {std_root_name}")
            return False

        # ==================================================
        # 步骤 B: 扫描源文件
        # ==================================================
        candidates = []
        if is_source_file:
            candidates.append(root_item)
        else:
            candidates = self._scan_files_recursively(source_root_id, max_depth=3)

        if not candidates:
            logger.warning("  ⚠️ 源目录为空或未扫描到文件。")
            return True

        # ==================================================
        # 步骤 C: 筛选 -> 重命名 -> 移动
        # ==================================================
        season_folders_cache = {}
        moved_count = 0

        for file_item in candidates:
            time.sleep(random.uniform(0.5, 1.0))
            fid = file_item.get('fid')
            file_name = file_item.get('n', '')
            ext = file_name.split('.')[-1].lower() if '.' in file_name else ''

            # 优先进行垃圾词过滤
            if self._is_junk_file(file_name):
                logger.info(f"  🗑️ [过滤] 命中屏蔽词，跳过垃圾文件: {file_name}")
                continue

            # 大小解析
            raw_size = file_item.get('s')
            if raw_size is None: raw_size = file_item.get('size')
            file_size = _parse_115_size(raw_size)

            is_video = ext in video_exts
            is_sub = ext in sub_exts

            if not (is_video or is_sub): continue

            # 过滤小样 (大小兜底)
            # 如果正则没拦住，但文件很小，依然会被这里拦住
            if is_video:
                if 0 < file_size < MIN_VIDEO_SIZE:
                    logger.info(f"  🗑️ [过滤] 跳过小视频 (Size): {file_name}")
                    continue
                elif file_size == 0:
                    # 如果解析出来是0，可能是API问题，打印日志但保留文件
                    logger.debug(f"  ⚠️ [注意] 文件大小解析为0 (Raw: {raw_size})，强制保留: {file_name}")
                else:
                    logger.debug(f"  📄 文件: {file_name}, 大小: {file_size/1024/1024:.2f} MB")

            # 2. 计算新文件名
            new_filename = file_name
            season_num = None

            # 视频和字幕都参与重命名计算
            if is_video or is_sub:
                try:
                    new_filename, season_num = self._rename_file_node(
                        file_item,
                        safe_title,       # 基础标题 (不含年份)
                        year=year,        # 传入年份
                        is_tv=(self.media_type=='tv')
                    )
                except Exception as e:
                    logger.error(f"  ❌ 重命名计算出错: {e}")
                    new_filename = file_name

            # 3. 执行重命名 (在源位置)
            if new_filename != file_name:
                rename_res = self.client.fs_rename((fid, new_filename))
                if rename_res.get('state'):
                    logger.info(f"  ✏️ [重命名] {file_name} -> {new_filename}")
                else:
                    logger.warning(f"  ⚠️ 重命名失败: {file_name}")
                    new_filename = file_name

            # 4. 确定移动的目标文件夹
            target_folder_cid = final_home_cid

            # 只有剧集且成功解析出季号时，才放入 Season 文件夹
            if self.media_type == 'tv' and season_num is not None:
                if season_num not in season_folders_cache:
                    s_name = f"Season {season_num:02d}"
                    s_mk = self.client.fs_mkdir(s_name, final_home_cid)
                    if s_mk.get('state'):
                        season_folders_cache[season_num] = s_mk.get('cid')
                    else:
                        s_search = self.client.fs_files({'cid': final_home_cid, 'search_value': s_name, 'limit': 10})
                        if s_search.get('data'):
                            for item in s_search['data']:
                                if item.get('n') == s_name and not item.get('fid'):
                                    season_folders_cache[season_num] = item.get('cid')
                                    break

                if season_folders_cache.get(season_num):
                    target_folder_cid = season_folders_cache[season_num]

            # 5. 执行移动
            move_res = self.client.fs_move(fid, target_folder_cid)
            if move_res.get('state'):
                moved_count += 1
            else:
                logger.error(f"  ❌ 移动文件失败: {new_filename}")

        # ==================================================
        # 步骤 D: 销毁源目录
        # ==================================================
        if not is_source_file:
            if moved_count > 0:
                logger.info(f"  🧹 [清理] 删除源目录: {root_item.get('n')}")
                self.client.fs_delete([source_root_id])
            else:
                logger.warning("  ⚠️ 未移动任何有效文件，保留源目录以防数据丢失。")

        logger.info(f"  ✅ [整理] 完成。共迁移 {moved_count} 个文件。")
        return True

def _parse_115_size(size_val):
    """
    统一解析 115 返回的文件大小为字节(Int)
    支持: 12345(int), "12345"(str), "1.2GB", "500KB"
    """
    try:
        if size_val is None: return 0

        # 1. 如果已经是数值 (115 API 's' 字段通常是 int)
        if isinstance(size_val, (int, float)):
            return int(size_val)

        # 2. 如果是字符串
        if isinstance(size_val, str):
            s = size_val.strip()
            if not s: return 0
            # 纯数字字符串
            if s.isdigit():
                return int(s)

            s_upper = s.upper().replace(',', '')
            mult = 1
            if 'TB' in s_upper: mult = 1024**4
            elif 'GB' in s_upper: mult = 1024**3
            elif 'MB' in s_upper: mult = 1024**2
            elif 'KB' in s_upper: mult = 1024

            match = re.search(r'([\d\.]+)', s_upper)
            if match:
                return int(float(match.group(1)) * mult)
    except Exception:
        pass
    return 0

def _perform_cms_notify():
    """
    真正执行 CMS 通知的函数 (被定时器调用)
    """
    config = get_config()
    cms_url = config.get('cms_url')
    cms_token = config.get('cms_token')

    if not cms_url or not cms_token:
        return

    cms_url = cms_url.rstrip('/')
    enable_smart_organize = config.get('enable_smart_organize', False)

    # 根据模式选择参数
    if enable_smart_organize:
        api_url = f"{cms_url}/api/sync/lift_by_token"
        params = {"type": "lift_sync", "token": cms_token}
        log_msg = "增量同步"
    else:
        api_url = f"{cms_url}/api/sync/lift_by_token"
        params = {"type": "auto_organize", "token": cms_token}
        log_msg = "自动整理"

    logger.info(f"  📣 [CMS] 防抖结束，开始: {log_msg} ...")

    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        res_json = response.json()
        if res_json.get('code') == 200 or res_json.get('success'):
            logger.info(f"  ✅ CMS 通知成功: {res_json.get('msg', 'OK')}")
        else:
            logger.warning(f"  ⚠️ CMS 通知返回异常: {res_json}")
    except Exception as e:
        logger.warning(f"  ⚠️ CMS 通知发送失败: {e}")


def notify_cms_scan():
    """
    通知 CMS 执行目录整理 (防抖入口)
    机制：每次调用都会重置计时器，只有静默 60 秒后才会真正发送请求。
    """
    global _cms_timer

    with _cms_lock:
        # 如果已有计时器在运行，取消它 (说明1分钟内又有新入库)
        if _cms_timer is not None:
            _cms_timer.cancel()
            logger.debug("  ⏳ 检测到连续入库，重置 CMS 通知计时器 (60s)")
        else:
            logger.info("  ⏳ 启动 CMS 通知计时器，等待 60s 无新入库后发送...")

        # 创建新计时器：60秒后执行 _perform_cms_notify
        _cms_timer = threading.Timer(60.0, _perform_cms_notify)
        _cms_timer.daemon = True # 设置为守护线程，防止阻塞主程序退出
        _cms_timer.start()

def get_115_account_info():
    """
    极简状态检查：只验证 Cookie 是否有效，不获取任何详情
    """
    client = P115Service.get_client()
    if not client: raise Exception("无法初始化 115 客户端")

    config = get_config()
    cookies = config.get('p115_cookies')

    if not cookies:
        raise Exception("未配置 Cookies")

    try:
        # 尝试列出 1 个文件，这是验证 Cookie 最快最准的方法
        resp = client.fs_files({'limit': 1})

        if not resp.get('state'):
            raise Exception("Cookie 已失效")

        # 只要没报错，就是有效
        return {
            "valid": True,
            "msg": "Cookie 状态正常，可正常推送"
        }

    except Exception as e:
        raise Exception("Cookie 无效或网络不通")


def _identify_media_enhanced(filename, forced_media_type=None):
    """
    增强识别逻辑：
    1. 支持多种 TMDb ID 标签格式: {tmdb=xxx}
    2. 支持标准命名格式: Title (Year)
    3. 接收外部强制指定的类型 (forced_media_type)，不再轮询猜测
    
    返回: (tmdb_id, media_type, title) 或 (None, None, None)
    """
    tmdb_id = None
    media_type = 'movie' # 默认
    title = filename
    
    # 1. 优先提取 TMDb ID 标签 (最稳)
    match_tag = re.search(r'\{?tmdb(?:id)?[=\-](\d+)\}?', filename, re.IGNORECASE)
    
    if match_tag:
        tmdb_id = match_tag.group(1)
        
        # 如果外部指定了类型，直接用；否则看文件名特征
        if forced_media_type:
            media_type = forced_media_type
        elif re.search(r'(?:S\d{1,2}|E\d{1,2}|第\d+季|Season)', filename, re.IGNORECASE):
            media_type = 'tv'
        
        # 提取标题
        clean_name = re.sub(r'\{?tmdb(?:id)?[=\-]\d+\}?', '', filename, flags=re.IGNORECASE).strip()
        match_title = re.match(r'^(.+?)\s*[\(\[]\d{4}[\)\]]', clean_name)
        if match_title:
            title = match_title.group(1).strip()
        else:
            title = clean_name
            
        return tmdb_id, media_type, title

    # 2. 其次提取标准格式 Title (Year)
    match_std = re.match(r'^(.+?)\s+[\(\[](\d{4})[\)\]]', filename)
    if match_std:
        name_part = match_std.group(1).strip()
        year_part = match_std.group(2)
        
        # === 关键修正：类型判断逻辑 ===
        if forced_media_type:
            # 如果外部透视过目录，确定是 TV，直接信赖
            media_type = forced_media_type
        else:
            # 否则才根据文件名特征判断
            if re.search(r'(?:S\d{1,2}|E\d{1,2}|第\d+季|Season)', filename, re.IGNORECASE):
                media_type = 'tv'
            else:
                media_type = 'movie'
            
        # 尝试通过 TMDb API 确认 ID
        try:
            api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)
            if api_key:
                # 精准搜索，不轮询，不瞎猜
                results = tmdb.search_media(
                    query=name_part, 
                    api_key=api_key, 
                    item_type=media_type, 
                    year=year_part
                )
                
                if results and len(results) > 0:
                    best = results[0]
                    return best['id'], media_type, (best.get('title') or best.get('name'))
                else:
                    logger.warning(f"  ⚠️ TMDb 未找到资源: {name_part} ({year_part}) 类型: {media_type}")

        except Exception as e:
            pass

    return None, None, None


def task_scan_and_organize_115(processor=None):
    """
    [任务链] 主动扫描 115 待整理目录
    - 识别成功 -> 归类到目标目录
    - 识别失败 -> 移动到 '未识别' 目录
    ★ 修复：增加子文件探测逻辑，防止剧集文件夹因命名不规范被误判为电影
    """
    logger.info("=== 开始执行 115 待整理目录扫描 ===")

    client = P115Service.get_client()
    if not client: raise Exception("无法初始化 115 客户端")

    config = get_config()
    cookies = config.get('p115_cookies')
    cid_val = config.get('p115_save_path_cid')
    save_val = config.get('p115_save_path_name', '待整理')
    enable_organize = config.get('enable_smart_organize', False)

    if not cookies:
        logger.error("  ⚠️ 未配置 115 Cookies，跳过。")
        return
    if not cid_val or str(cid_val) == '0':
        logger.error("  ⚠️ 未配置待整理目录 (CID)，跳过。")
        return
    if not enable_organize:
        logger.warning("  ⚠️ 未开启智能整理开关，仅扫描不处理。")
        return

    try:
        save_cid = int(cid_val)
        save_name = str(save_val)

        # 1. 准备 '未识别' 目录 
        unidentified_folder_name = "未识别"
        unidentified_cid = None
        try:
            search_res = client.fs_files({'cid': save_cid, 'search_value': unidentified_folder_name, 'limit': 1})
            if search_res.get('data'):
                for item in search_res['data']:
                    if item.get('n') == unidentified_folder_name and (item.get('ico') == 'folder' or not item.get('fid')):
                        unidentified_cid = item.get('cid')
                        break
        except: pass

        if not unidentified_cid:
            try:
                mk_res = client.fs_mkdir(unidentified_folder_name, save_cid)
                if mk_res.get('state'):
                    unidentified_cid = mk_res.get('cid')
            except: pass

        # 2. 扫描目录
        logger.info(f"  🔍 正在扫描目录: {save_name} ...")
        res = client.fs_files({'cid': save_cid, 'limit': 50, 'o': 'user_ptime', 'asc': 0})

        if not res.get('data'):
            logger.info(f"  📂 [{save_name}] 目录为空。")
            return

        processed_count = 0
        moved_to_unidentified = 0

        for item in res['data']:
            name = item.get('n')
            item_id = item.get('fid') or item.get('cid')
            is_folder = not item.get('fid') # 判断是否为文件夹

            if str(item_id) == str(unidentified_cid) or name == unidentified_folder_name:
                continue

            forced_type = None
            if is_folder:
                try:
                    # 偷看一眼文件夹里面的内容 (取前20个足矣)
                    sub_res = client.fs_files({'cid': item.get('cid'), 'limit': 20})
                    if sub_res.get('data'):
                        for sub_item in sub_res['data']:
                            sub_name = sub_item.get('n', '')
                            # 只要包含 Season XX, S01, EP01, 第X季，就是电视剧
                            # 你的截图里是 "Season 01"，这个正则能完美匹配
                            if re.search(r'(Season\s?\d+|S\d+|Ep?\d+|第\d+季)', sub_name, re.IGNORECASE):
                                forced_type = 'tv'
                                logger.info(f"  🕵️‍♂️ [结构探测] 目录 '{name}' 包含子项 '{sub_name}' -> 判定为 TV")
                                break
                except Exception as e:
                    logger.warning(f"  ⚠️ 目录透视失败: {e}")

            # 3. 识别 (传入 forced_type)
            tmdb_id, media_type, title = _identify_media_enhanced(name, forced_media_type=forced_type)
            
            if tmdb_id:
                logger.info(f"  ➜ 识别成功: {name} -> ID:{tmdb_id} ({media_type})")
                try:
                    # 4. 归类
                    organizer = SmartOrganizer(client, tmdb_id, media_type, title)
                    target_cid = organizer.get_target_cid()
                    if organizer.execute(item, target_cid):
                        processed_count += 1
                except Exception as e:
                    logger.error(f"  ❌ 整理出错: {e}")
            else:
                # 5. 识别失败 -> 移动到 '未识别'
                if unidentified_cid:
                    logger.info(f"  ⚠️ 无法识别: {name} -> 移动到 '未识别'")
                    try:
                        client.fs_move(item_id, unidentified_cid)
                        moved_to_unidentified += 1
                    except: pass

        logger.info(f"=== 扫描结束，成功归类 {processed_count} 个，移入未识别 {moved_to_unidentified} 个 ===")

        if processed_count > 0:
            notify_cms_scan()

    except Exception as e:
        logger.error(f"  ⚠️ 115 扫描任务异常: {e}", exc_info=True)
