import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.cover_generator.chillposter.engine import MAX_DYNAMIC_WIDTH, PosterEngine

logger = logging.getLogger(__name__)

CHILLPOSTER_ROOT = Path(__file__).resolve().parents[1] / "chillposter"
TEMPLATES_DIR = CHILLPOSTER_ROOT / "templates"
LAYOUTS_DIR = CHILLPOSTER_ROOT / "layouts"
REPO_FONTS_DIR = Path(__file__).resolve().parents[3] / "fonts"
DEFAULT_TEMPLATE_ID = "preset_1769062617890"

_ENGINE: Optional[PosterEngine] = None


def _template_path(template_id: str) -> Path:
    safe_id = Path(template_id or DEFAULT_TEMPLATE_ID).stem
    return TEMPLATES_DIR / f"{safe_id}.json"


def _load_template(template_id: str) -> Tuple[str, Dict[str, Any]]:
    path = _template_path(template_id)
    if not path.exists():
        logger.warning("ChillPoster 模板不存在: %s，将使用默认模板。", path.name)
        path = _template_path(DEFAULT_TEMPLATE_ID)
    data = json.loads(path.read_text(encoding="utf-8"))
    config = data.get("config") or {}
    return path.stem, dict(config)


def _get_engine() -> PosterEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = PosterEngine(fonts_dir=str(REPO_FONTS_DIR), layouts_dir=str(LAYOUTS_DIR))
    return _ENGINE


def _file_to_data_url(path: str) -> str:
    suffix = Path(path).suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    data = Path(path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('utf-8')}"


def get_chillposter_templates() -> List[Dict[str, Any]]:
    templates: List[Dict[str, Any]] = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取 ChillPoster 模板失败 %s: %s", path.name, exc)
            continue

        preview = ""
        preview_path = path.with_suffix(".jpg")
        if preview_path.exists():
            preview = _file_to_data_url(str(preview_path))

        templates.append({
            "id": path.stem,
            "name": data.get("name") or path.stem,
            "engine": data.get("engine") or (data.get("config") or {}).get("engine") or "",
            "dynamic": bool((data.get("config") or {}).get("enable_animation")),
            "preview": preview,
        })
    return templates


def get_chillposter_required_count(template_id: str) -> int:
    poster_count, backdrop_count = get_chillposter_asset_counts(template_id)
    return max(poster_count, backdrop_count, 1)


def get_chillposter_asset_counts(template_id: str) -> Tuple[int, int]:
    _, config = _load_template(template_id)
    poster_count = int(float(config.get("poster_count") or 0))
    backdrop_count = int(float(config.get("backdrop_count") or 0))
    if poster_count <= 0 and backdrop_count <= 0:
        poster_count = 1
    return max(poster_count, 0), max(backdrop_count, 0)


def create_chillposter_cover(
    poster_paths: List[str],
    backdrop_paths: Optional[List[str]],
    title: Tuple[str, str],
    item_count: Optional[int],
    config: Dict[str, Any],
) -> bytes:
    if not poster_paths and not backdrop_paths:
        return None

    template_id = config.get("chillposter_template") or DEFAULT_TEMPLATE_ID
    _, render_config = _load_template(template_id)
    title_zh, title_en = title
    render_config["title"] = title_zh or ""
    render_config["subtitle"] = title_en or ""

    if render_config.get("enable_animation"):
        try:
            dynamic_width = int(config.get("chillposter_dynamic_width") or 960)
        except (TypeError, ValueError):
            dynamic_width = 960
        render_config["dynamic_output_width"] = max(320, min(dynamic_width, MAX_DYNAMIC_WIDTH))

    if config.get("show_item_count"):
        render_config["badge_style"] = "ribbon" if config.get("badge_style") == "ribbon" else "box"
    else:
        render_config["badge_style"] = "none"

    poster_urls = [_file_to_data_url(str(path)) for path in poster_paths]
    backdrop_urls = [_file_to_data_url(str(path)) for path in (backdrop_paths or [])]
    bg_url = backdrop_urls[0] if backdrop_urls else (poster_urls[0] if poster_urls else "")

    assets = {
        "bg_url": bg_url,
        "backdrops": backdrop_urls,
        "posters": poster_urls,
        "count": item_count,
    }

    image_b64 = _get_engine().draw(render_config, assets)
    return base64.b64decode(image_b64)
