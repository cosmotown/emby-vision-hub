from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageColor
import colorsys
import random
from collections import Counter

# =============================================================================
# 1. 辅助函数
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

def adjust_color_macaron(color):
    h, s, v = rgb_to_hsv(color)
    target_saturation_range = (0.3, 0.7)
    target_value_range = (0.6, 0.85)
    
    if s < target_saturation_range[0]: s = target_saturation_range[0]
    elif s > target_saturation_range[1]: s = target_saturation_range[1]
    
    if v < target_value_range[0]: v = target_value_range[0]
    elif v > target_value_range[1]: v = target_value_range[1]
    
    return hsv_to_rgb(h, s, v)

def color_distance(color1, color2):
    h1, s1, v1 = rgb_to_hsv(color1)
    h2, s2, v2 = rgb_to_hsv(color2)
    h_dist = min(abs(h1 - h2), 1 - abs(h1 - h2))
    return h_dist * 5 + abs(s1 - s2) + abs(v1 - v2)

def find_dominant_macaron_colors(image, num_colors=5):
    img = image.copy()
    img.thumbnail((150, 150))
    img = img.convert('RGB')
    pixels = list(img.getdata())
    
    filtered_pixels = [p for p in pixels if is_not_black_white_gray_near(p)]
    if not filtered_pixels: return []
    
    color_counter = Counter(filtered_pixels)
    candidate_colors = color_counter.most_common(num_colors * 5)
    
    macaron_colors = []
    min_color_distance = 0.15
    
    for color, _ in candidate_colors:
        adjusted_color = adjust_color_macaron(color)
        if not any(color_distance(adjusted_color, existing) < min_color_distance for existing in macaron_colors):
            macaron_colors.append(adjusted_color)
            if len(macaron_colors) >= num_colors: break
    
    return macaron_colors

def darken_color(color, factor=0.7):
    r, g, b = color
    return (int(r * factor), int(g * factor), int(b * factor))

def get_rgba_color(hex_color, default_color, opacity=255):
    """解析 hex 颜色并附加透明度"""
    if not hex_color:
        r, g, b = default_color[:3]
        return (r, g, b, opacity)
    try:
        rgb = ImageColor.getrgb(hex_color)
        return rgb + (opacity,)
    except:
        r, g, b = default_color[:3]
        return (r, g, b, opacity)

def add_film_grain(image, intensity=0.05):
    if intensity <= 0: return image
    w, h = image.size
    sigma = intensity * 255 * 2 
    noise = Image.effect_noise((w, h), sigma)
    return Image.blend(image.convert('RGB'), noise.convert('RGB'), intensity * 0.5).convert('RGBA')

def crop_to_square(img):
    width, height = img.size
    size = min(width, height)
    left = (width - size) // 2
    top = (height - size) // 2
    return img.crop((left, top, left + size, top + size))

def add_rounded_corners(img, radius=30):
    if radius <= 0: return img
    factor = 2
    width, height = img.size
    enlarged_img = img.resize((width * factor, height * factor), Image.LANCZOS).convert("RGBA")
    mask = Image.new('L', (width * factor, height * factor), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), (width * factor, height * factor)], radius=radius * factor, fill=255)
    background = Image.new("RGBA", (width * factor, height * factor), (255, 255, 255, 0))
    high_res_result = Image.composite(enlarged_img, background, mask)
    return high_res_result.resize((width, height), Image.LANCZOS)

def rotate_image(img, angle):
    return img.rotate(angle, Image.BICUBIC, expand=True)

def add_shadow_and_rotate(canvas, img, angle, offset=(10, 10), radius=10, opacity=0.5, center_pos=None):
    width, height = img.size
    if center_pos is None:
        center_pos = (canvas.width // 2, canvas.height // 2)
    
    padding = max(radius * 4, 100)
    shadow_size = (width + padding * 2, height + padding * 2)
    shadow = Image.new("RGBA", shadow_size, (0, 0, 0, 0))
    
    shadow_mask = Image.new("L", (width, height), 255)
    if img.mode == "RGBA":
        shadow_mask = img.split()[3]
    
    shadow.paste((0, 0, 0, int(255 * opacity)), (padding, padding), shadow_mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
    
    rotated_shadow = rotate_image(shadow, angle)
    s_w, s_h = rotated_shadow.size
    shadow_x = center_pos[0] - s_w // 2 + offset[0]
    shadow_y = center_pos[1] - s_h // 2 + offset[1]
    
    rotated_img = rotate_image(img, angle)
    i_w, i_h = rotated_img.size
    img_x = center_pos[0] - i_w // 2
    img_y = center_pos[1] - i_h // 2
    
    canvas.paste(rotated_shadow, (shadow_x, shadow_y), rotated_shadow)
    canvas.paste(rotated_img, (img_x, img_y), rotated_img)
    return canvas

# =============================================================================
# 2. Schema 参数定义
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
            "group": "卡片布局",
            "items": [
                {"key": "card_scale", "label": "卡片尺寸缩放", "type": "range", "min": 0.3, "max": 0.9, "step": 0.05, "default": 0.65},
                {"key": "card_x_percent", "label": "卡片中心 X%", "type": "range", "min": 0, "max": 100, "default": 68},
                {"key": "card_y_percent", "label": "卡片中心 Y%", "type": "range", "min": 0, "max": 100, "default": 50},
                {"key": "card_radius", "label": "卡片圆角大小", "type": "range", "min": 0, "max": 100, "default": 40},
                {"key": "angle_top", "label": "顶层旋转", "type": "range", "min": -180, "max": 180, "default": 0},
                {"key": "angle_mid", "label": "中层旋转", "type": "range", "min": -180, "max": 180, "default": 12},
                {"key": "angle_bot", "label": "底层旋转", "type": "range", "min": -180, "max": 180, "default": 24},
            ]
        },
        {
            "group": "卡片光影",
            "items": [
                {"key": "shadow_opacity", "label": "阴影浓度", "type": "range", "min": 0.0, "max": 1.0, "step": 0.1, "default": 0.5},
                {"key": "shadow_blur", "label": "阴影模糊", "type": "range", "min": 0, "max": 100, "default": 20},
                {"key": "shadow_offset_x", "label": "阴影偏移 X", "type": "range", "min": -50, "max": 50, "default": 15},
                {"key": "shadow_offset_y", "label": "阴影偏移 Y", "type": "range", "min": -50, "max": 50, "default": 20},
            ]
        },
        {
            "group": "主标题样式",
            "items": [
                {"key": "font_title", "label": "主标题字体", "type": "font", "default": "default.ttf"},
                {"key": "title_color", "label": "主标题颜色", "type": "color", "default": "#FFFFFF"},
                {"key": "title_pos_x", "label": "主标题 X%", "type": "range", "min": 0, "max": 100, "default": 15},
                {"key": "title_pos_y", "label": "主标题 Y%", "type": "range", "min": 0, "max": 100, "default": 42},
                {"key": "title_width_pct", "label": "主标题最大宽度 %", "type": "range", "min": 10, "max": 100, "default": 40},
            ]
        },
        {
            "group": "副标题样式",
            "items": [
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#DDDDDD"},
                # 新增独立坐标控制
                {"key": "subtitle_pos_x", "label": "副标题 X%", "type": "range", "min": 0, "max": 100, "default": 15},
                {"key": "subtitle_pos_y", "label": "副标题 Y%", "type": "range", "min": 0, "max": 100, "default": 55},
                {"key": "sub_width_pct", "label": "副标题最大宽度 %", "type": "range", "min": 10, "max": 100, "default": 40},
            ]
        },
        {
            "group": "背景与装饰",
            "items": [
                {"key": "bg_blur", "label": "背景模糊", "type": "range", "min": 0, "max": 200, "default": 60},
                {"key": "color_ratio", "label": "背景染色强度", "type": "range", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.85},
                {"key": "grain_intensity", "label": "背景噪点", "type": "range", "min": 0.0, "max": 0.2, "step": 0.01, "default": 0.05},
                {"key": "text_shadow_opacity", "label": "文字阴影浓度", "type": "range", "min": 0, "max": 255, "default": 100},
            ]
        }
    ]

# =============================================================================
# 3. 渲染逻辑
# =============================================================================
def render(ctx, bg, config, assets, fonts):
    poster_urls = assets.get('posters', [])
    if not poster_urls: return bg
    
    # 下载主图
    original_img = ctx.download_img(poster_urls[0])
    if not original_img: return bg
    original_img = original_img.convert("RGB")
    
    width, height = bg.size
    
    # --- 1. 颜色与背景 ---
    num_colors = 6
    extracted_colors = find_dominant_macaron_colors(original_img, num_colors=num_colors)
    soft_macaron_colors = [(237, 159, 77), (186, 225, 255), (255, 223, 186), (202, 231, 200)]
    while len(extracted_colors) < num_colors:
        extracted_colors.append(random.choice(soft_macaron_colors))
            
    auto_bg_color = darken_color(extracted_colors[0], 0.85)
    auto_card_color1 = extracted_colors[1]
    auto_card_color2 = extracted_colors[2]
    
    blur_radius = int(config.get('bg_blur', 60))
    color_ratio = float(config.get('color_ratio', 0.85))
    grain_val = float(config.get('grain_intensity', 0.05))
    
    bg_img = original_img.copy()
    bg_img = ImageOps.fit(bg_img, (width, height), method=Image.LANCZOS)
    bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    
    solid_bg = Image.new("RGB", (width, height), auto_bg_color)
    blended_bg = Image.blend(bg_img, solid_bg, color_ratio)
    bg = add_film_grain(blended_bg, intensity=grain_val).convert("RGBA")
    
    # --- 2. 绘制卡片 ---
    square_img = crop_to_square(original_img)
    card_scale = float(config.get('card_scale', 0.65))
    card_size = int(height * card_scale)
    card_radius = int(config.get('card_radius', 40))
    
    square_img = square_img.resize((card_size, card_size), Image.LANCZOS)
    
    # 顶层 (原图)
    main_card = add_rounded_corners(square_img, radius=card_radius).convert("RGBA")
    
    # 中间层
    aux_card1 = square_img.copy().filter(ImageFilter.GaussianBlur(radius=8))
    solid_card1 = Image.new("RGB", aux_card1.size, auto_card_color1)
    aux_card1 = Image.blend(aux_card1, solid_card1, 0.5)
    aux_card1 = add_rounded_corners(aux_card1, radius=card_radius).convert("RGBA")
    
    # 底层
    aux_card2 = square_img.copy().filter(ImageFilter.GaussianBlur(radius=16))
    solid_card2 = Image.new("RGB", aux_card2.size, auto_card_color2)
    aux_card2 = Image.blend(aux_card2, solid_card2, 0.6)
    aux_card2 = add_rounded_corners(aux_card2, radius=card_radius).convert("RGBA")
    
    cards_canvas = Image.new("RGBA", (width, height), (0,0,0,0))
    cx = int(width * (float(config.get('card_x_percent', 68)) / 100))
    cy = int(height * (float(config.get('card_y_percent', 50)) / 100))
    center_pos = (cx, cy)
    
    angles = [
        int(config.get('angle_bot', 24)),
        int(config.get('angle_mid', 12)),
        int(config.get('angle_top', 0))
    ]
    
    s_opacity = float(config.get('shadow_opacity', 0.5))
    s_blur = int(config.get('shadow_blur', 20))
    s_off_x = int(config.get('shadow_offset_x', 15))
    s_off_y = int(config.get('shadow_offset_y', 20))
    
    cards_list = [aux_card2, aux_card1, main_card]
    for i, (card, angle) in enumerate(zip(cards_list, angles)):
        cards_canvas = add_shadow_and_rotate(
            cards_canvas, card, angle, 
            offset=(s_off_x, s_off_y), 
            radius=s_blur, 
            opacity=s_opacity,
            center_pos=center_pos
        )
    bg = Image.alpha_composite(bg, cards_canvas)
    
    # --- 3. 绘制文字 (独立坐标) ---
    draw = ImageDraw.Draw(bg)
    
    title_str = config.get('title', '')
    subtitle_str = config.get('subtitle', '')
    
    f_title_path = fonts.get('font_title') or fonts.get('main')
    f_sub_path = fonts.get('font_subtitle') or fonts.get('sub') or f_title_path
    
    # 颜色
    t_color = get_rgba_color(config.get('title_color'), (255, 255, 255), 255)
    s_color = get_rgba_color(config.get('subtitle_color'), (255, 255, 255), 200)
    shadow_op = int(config.get('text_shadow_opacity', 100))
    shadow_color = (0, 0, 0, shadow_op)
    
    # 主标题坐标
    tx = int(width * (float(config.get('title_pos_x', 15)) / 100))
    ty = int(height * (float(config.get('title_pos_y', 42)) / 100))
    mw_title = int(width * (float(config.get('title_width_pct', 40)) / 100))
    
    # 副标题坐标 (独立)
    sx = int(width * (float(config.get('subtitle_pos_x', 15)) / 100))
    sy = int(height * (float(config.get('subtitle_pos_y', 55)) / 100))
    mw_sub = int(width * (float(config.get('sub_width_pct', 40)) / 100))
    
    if title_str:
        if shadow_op > 0:
            ctx.draw_text_wrapper(draw, title_str, tx + 3, ty + 3, f_title_path, mw_title, shadow_color)
        ctx.draw_text_wrapper(draw, title_str, tx, ty, f_title_path, mw_title, t_color)
        
    if subtitle_str:
        if shadow_op > 0:
            ctx.draw_text_wrapper(draw, subtitle_str, sx + 2, sy + 2, f_sub_path, mw_sub, shadow_color)
        ctx.draw_text_wrapper(draw, subtitle_str, sx, sy, f_sub_path, mw_sub, s_color)

    return bg