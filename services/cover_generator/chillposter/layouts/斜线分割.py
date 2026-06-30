from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageColor
import colorsys
import random
from collections import Counter

# =============================================================================
# 1. 辅助函数 (纯 PIL 实现)
# =============================================================================

def is_not_black_white_gray_near(color, threshold=20):
    r, g, b = color
    if (r < threshold and g < threshold and b < threshold) or \
       (r > 255 - threshold and g > 255 - threshold and b > 255 - threshold):
        return False
    gray_diff_threshold = 10
    if abs(r - g) < gray_diff_threshold and abs(g - b) < gray_diff_threshold and abs(r - b) < gray_diff_threshold:
        return False
    return True

def rgb_to_hsv(color):
    r, g, b = [x / 255.0 for x in color]
    return colorsys.rgb_to_hsv(r, g, b)

def hsv_to_rgb(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))

def adjust_to_macaron(h, s, v):
    # 调整为马卡龙色系
    target_saturation_range = (0.2, 0.7)
    target_value_range = (0.55, 0.85)
    adjusted_s = min(max(s, target_saturation_range[0]), target_saturation_range[1])
    adjusted_v = min(max(v, target_value_range[0]), target_value_range[1])
    return hsv_to_rgb(h, adjusted_s, adjusted_v)

def find_dominant_vibrant_colors(image, num_colors=5):
    img = image.copy()
    img.thumbnail((100, 100))
    img = img.convert('RGB')
    pixels = list(img.getdata())
    filtered_pixels = [p for p in pixels if is_not_black_white_gray_near(p)]
    if not filtered_pixels: return []
    
    color_counter = Counter(filtered_pixels)
    dominant_colors = color_counter.most_common(num_colors * 3)
    
    macaron_colors = []
    for color, _ in dominant_colors:
        h, s, v = rgb_to_hsv(color)
        adjusted_rgb = adjust_to_macaron(h, s, v)
        if adjusted_rgb not in macaron_colors:
            macaron_colors.append(adjusted_rgb)
            if len(macaron_colors) >= num_colors: break
    return macaron_colors

def darken_color(color, factor=0.7):
    r, g, b = color
    return (int(r * factor), int(g * factor), int(b * factor))

def get_rgba_color(hex_color, default_color, opacity=255):
    if not hex_color:
        return default_color[:3] + (opacity,)
    try:
        return ImageColor.getrgb(hex_color) + (opacity,)
    except:
        return default_color[:3] + (opacity,)

def add_film_grain(image, intensity=0.05):
    """PIL 版噪点"""
    if intensity <= 0: return image
    w, h = image.size
    # effect_noise 生成高斯噪点
    sigma = intensity * 255 * 3
    noise = Image.effect_noise((w, h), sigma).convert('RGB')
    return Image.blend(image.convert('RGB'), noise, intensity * 0.6).convert('RGBA')

def align_image_right(img, canvas_size):
    """将图片主体靠右对齐裁切"""
    cw, ch = canvas_size
    target_width = int(cw * 0.675)
    iw, ih = img.size
    
    scale = ch / ih
    new_w = int(iw * scale)
    resized = img.resize((new_w, ch), Image.LANCZOS)
    
    if new_w < target_width:
        # 如果缩放后宽度不足，按宽度缩放
        scale = target_width / iw
        new_h = int(ih * scale)
        resized = img.resize((target_width, new_h), Image.LANCZOS)
        # 垂直居中裁剪
        top = (new_h - ch) // 2
        resized = resized.crop((0, top, target_width, top + ch))
        final = Image.new("RGBA", canvas_size)
        final.paste(resized, (cw - target_width, 0))
    else:
        # 宽度足够，取中心偏左一点
        center_x = new_w / 2
        crop_left = max(0, center_x - target_width / 2)
        if crop_left + target_width > new_w: crop_left = new_w - target_width
        cropped = resized.crop((int(crop_left), 0, int(crop_left) + target_width, ch))
        final = Image.new("RGBA", canvas_size)
        # 放置位置
        paste_x = cw - target_width + int(cw * 0.075)
        final.paste(cropped, (paste_x, 0))
        
    return final

def create_diagonal_mask(size, split_top=0.55, split_bottom=0.4):
    mask = Image.new('L', size, 255)
    draw = ImageDraw.Draw(mask)
    w, h = size
    top_x = int(w * split_top)
    bottom_x = int(w * split_bottom)
    # 右侧前景区域为黑色(0)，即保留
    draw.polygon([(top_x, 0), (w, 0), (w, h), (bottom_x, h)], fill=0)
    return mask

def create_shadow_mask(size, split_top=0.55, split_bottom=0.4, feather=30):
    w, h = size
    top_x = int(w * split_top)
    bottom_x = int(w * split_bottom)
    mask = Image.new('L', size, 0)
    draw = ImageDraw.Draw(mask)
    shadow_width = feather // 3
    # 绘制一条细线作为阴影源
    draw.polygon([
        (top_x - 5, 0), (top_x - 5 + shadow_width, 0),
        (bottom_x - 5 + shadow_width, h), (bottom_x - 5, h)
    ], fill=255)
    return mask.filter(ImageFilter.GaussianBlur(feather // 3))

# =============================================================================
# 2. 参数 Schema
# =============================================================================
def get_schema():
    return [
        {
            "group": "内容设置",
            "items": [
                {"key": "title", "label": "主标题内容", "type": "text", "default": ""},
                {"key": "subtitle", "label": "副标题内容", "type": "text", "default": ""},
            ]
        },
        {
            "group": "斜线分割布局",
            "items": [
                {"key": "split_top", "label": "顶部切割点 X%", "type": "range", "min": 20, "max": 80, "default": 55},
                {"key": "split_bottom", "label": "底部切割点 X%", "type": "range", "min": 20, "max": 80, "default": 40},
                {"key": "shadow_feather", "label": "分割线阴影柔化", "type": "range", "min": 0, "max": 100, "default": 30},
            ]
        },
        {
            "group": "主标题样式",
            "items": [
                {"key": "font_title", "label": "主标题字体", "type": "font", "default": "default.ttf"},
                {"key": "title_size", "label": "主标题字号", "type": "range", "min": 50, "max": 300, "default": 160},
                {"key": "title_color", "label": "主标题颜色", "type": "color", "default": "#FFFFFF"},
                {"key": "title_x", "label": "主标题 X%", "type": "range", "min": 0, "max": 100, "default": 25},
                {"key": "title_y", "label": "主标题 Y%", "type": "range", "min": 0, "max": 100, "default": 40},
                {"key": "title_width_pct", "label": "主标题最大宽 %", "type": "range", "min": 10, "max": 100, "default": 40},
            ]
        },
        {
            "group": "副标题样式",
            "items": [
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "副标题字号", "type": "range", "min": 20, "max": 150, "default": 60},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#FFFFFF"},
                {"key": "sub_x", "label": "副标题 X%", "type": "range", "min": 0, "max": 100, "default": 25},
                {"key": "sub_y", "label": "副标题 Y%", "type": "range", "min": 0, "max": 100, "default": 60},
                {"key": "sub_width_pct", "label": "副标题最大宽 %", "type": "range", "min": 10, "max": 100, "default": 40},
            ]
        },
        {
            "group": "背景与氛围",
            "items": [
                {"key": "bg_blur", "label": "背景模糊半径", "type": "range", "min": 0, "max": 200, "default": 50},
                {"key": "color_ratio", "label": "背景色覆盖浓度", "type": "range", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.9},
                {"key": "grain_val", "label": "噪点强度", "type": "range", "min": 0.0, "max": 0.2, "step": 0.01, "default": 0.05},
            ]
        }
    ]

# =============================================================================
# 3. 渲染逻辑
# =============================================================================
def render(ctx, bg, config, assets, fonts):
    poster_urls = assets.get('posters', [])
    if not poster_urls: return bg
    
    # 1. 准备图片
    img_raw = ctx.download_img(poster_urls[0])
    if not img_raw: return bg
    img_raw = img_raw.convert("RGB")
    
    w, h = bg.size
    
    # 2. 智能取色
    vibrant_colors = find_dominant_vibrant_colors(img_raw)
    if vibrant_colors:
        theme_color = vibrant_colors[0]
    else:
        theme_color = (237, 159, 77) # 默认橙
        
    # 3. 处理前景图 (右侧)
    fg_img = align_image_right(img_raw, (w, h)).convert("RGBA")
    
    # 4. 处理背景图 (左侧)
    blur_r = int(config.get('bg_blur', 50))
    color_ratio = float(config.get('color_ratio', 0.9))
    grain = float(config.get('grain_val', 0.05))
    
    bg_base = img_raw.copy()
    bg_base = ImageOps.fit(bg_base, (w, h), method=Image.LANCZOS)
    bg_base = bg_base.filter(ImageFilter.GaussianBlur(blur_r))
    
    # 混合颜色 (不使用 numpy)
    bg_theme = darken_color(theme_color, 0.85)
    solid_layer = Image.new("RGB", (w, h), bg_theme)
    blended_bg = Image.blend(bg_base, solid_layer, color_ratio)
    blended_bg = add_film_grain(blended_bg, grain).convert("RGBA")
    
    # 5. 蒙版合成
    split_top = float(config.get('split_top', 55)) / 100
    split_bot = float(config.get('split_bottom', 40)) / 100
    feather = int(config.get('shadow_feather', 30))
    
    # 斜线蒙版 (左白右黑) -> 注意：这里蒙版用在 blend 时，alpha=0显示第一张图(bg)，alpha=255显示第二张图(fg)
    # create_diagonal_mask 返回的是：左侧255(白)，右侧0(黑)
    # 我们希望：左侧显示背景，右侧显示前景
    diag_mask = create_diagonal_mask((w, h), split_top, split_bot)
    
    # 阴影层
    shadow_mask = create_shadow_mask((w, h), split_top, split_bot, feather)
    shadow_layer = Image.new("RGBA", (w, h), darken_color(bg_theme, 0.5))
    
    # 组合: 先把前景画在画布上
    final_canvas = fg_img.copy()
    # 加上阴影条
    final_canvas.paste(shadow_layer, (0, 0), mask=shadow_mask)
    # 用斜线蒙版把左侧替换成背景
    # composite(image1, image2, mask): mask为255时取image1
    final_canvas = Image.composite(blended_bg, final_canvas, diag_mask)
    
    bg = final_canvas # 赋值给最终输出
    
    # 6. 绘制文字 (独立坐标)
    draw = ImageDraw.Draw(bg)
    title = config.get('title', '')
    sub = config.get('subtitle', '')
    
    f_title = fonts.get('font_title') or fonts.get('main')
    f_sub = fonts.get('font_subtitle') or fonts.get('sub') or f_title
    
    # 字体颜色 (带透明度)
    c_title = get_rgba_color(config.get('title_color'), (255, 255, 255), 229)
    c_sub = get_rgba_color(config.get('subtitle_color'), (255, 255, 255), 229)
    c_shadow = darken_color(bg_theme, 0.8) + (75,)
    
    # 坐标与宽度
    tx = int(w * float(config.get('title_x', 25)) / 100)
    ty = int(h * float(config.get('title_y', 40)) / 100)
    tw = int(w * float(config.get('title_width_pct', 40)) / 100)
    
    sx = int(w * float(config.get('sub_x', 25)) / 100)
    sy = int(h * float(config.get('sub_y', 60)) / 100)
    sw = int(w * float(config.get('sub_width_pct', 40)) / 100)
    
    # 绘制主标题
    if title:
        # 阴影 (手动绘制多次以模拟柔和阴影)
        for off in range(3, 13, 2):
             ctx.draw_text_wrapper(draw, title, tx+off, ty+off, f_title, tw, c_shadow, align='center')
        # 正文 (注意：draw_text_wrapper默认可能垂直居中或顶部对齐，这里假设它接受的是(x,y))
        ctx.draw_text_wrapper(draw, title, tx, ty, f_title, tw, c_title, align='center')
        
    # 绘制副标题
    if sub:
        for off in range(2, 8, 2):
            ctx.draw_text_wrapper(draw, sub, sx+off, sy+off, f_sub, sw, c_shadow, align='center')
        ctx.draw_text_wrapper(draw, sub, sx, sy, f_sub, sw, c_sub, align='center')

    return bg