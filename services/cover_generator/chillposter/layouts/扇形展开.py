from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageOps
import math

# =============================================================================
# 1. 核心图形算法 (保持不变)
# =============================================================================

def add_rounded_corners(im, radius):
    if radius <= 0: return im
    circle = Image.new('L', (radius * 2, radius * 2), 0)
    draw = ImageDraw.Draw(circle)
    draw.ellipse((0, 0, radius * 2, radius * 2), fill=255)
    alpha = Image.new('L', im.size, 255)
    w, h = im.size
    alpha.paste(circle.crop((0, 0, radius, radius)), (0, 0))
    alpha.paste(circle.crop((0, radius, radius, radius * 2)), (0, h - radius))
    alpha.paste(circle.crop((radius, 0, radius * 2, radius)), (w - radius, 0))
    alpha.paste(circle.crop((radius, radius, radius * 2, radius * 2)), (w - radius, h - radius))
    if im.mode != 'RGBA': im = im.convert('RGBA')
    im.putalpha(alpha)
    return im

def create_reflection(img, opacity=40):
    w, h = img.size
    ref = ImageOps.flip(img)
    mask = Image.new('L', (w, h), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(h):
        alpha = int(opacity * (1 - y / h))
        draw.line((0, y, w, y), fill=alpha)
    ref.putalpha(mask)
    return ref

def create_shadow_and_reflection_group(img, reflection_opacity=40, shadow_blur=15):
    w, h = img.size
    padding = 30
    total_w = w + padding * 2
    total_h = h * 2 + padding 
    group = Image.new('RGBA', (total_w, total_h), (0,0,0,0))
    
    if shadow_blur > 0:
        shadow = Image.new('RGBA', (w + padding*2, h + padding*2), (0,0,0,0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle((padding, padding, padding+w, padding+h), radius=15, fill=(0,0,0,150))
        shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
        group.paste(shadow, (0, 0), mask=shadow)

    if reflection_opacity > 0:
        ref = create_reflection(img, reflection_opacity)
        ref_x = padding
        ref_y = padding + h 
        group.paste(ref, (ref_x, ref_y), mask=ref)

    group.paste(img, (padding, padding), mask=img)
    center_offset_x = padding + w // 2
    center_offset_y = padding + h // 2
    return group, (center_offset_x, center_offset_y)

# =============================================================================
# 2. 参数定义 (已统一顺序)
# =============================================================================
def get_schema():
    return [
        # --- 1. 核心资源与内容 ---
        {
            "group": "资源与内容",
            "items": [
                {"key": "poster_count", "label": "海报数量 (奇数)", "type": "range", "min": 3, "max": 9, "step": 2, "default": 5},
                {"key": "title", "label": "主标题内容", "type": "text", "default": ""},
                {"key": "subtitle", "label": "副标题内容", "type": "text", "default": ""},
            ]
        },
        # --- 2. 扇形布局 ---
        {
            "group": "扇形布局",
            "items": [
                {"key": "fan_radius", "label": "扇形半径 (曲率)", "type": "range", "min": 500, "max": 3000, "step": 50, "default": 1200},
                {"key": "fan_spread", "label": "扇形展开总角度", "type": "range", "min": 10, "max": 90, "step": 1, "default": 40},
                {"key": "center_scale", "label": "C位海报大小", "type": "range", "min": 0.3, "max": 0.8, "step": 0.01, "default": 0.55},
                {"key": "side_scale_shrink", "label": "侧边缩小比例", "type": "range", "min": 0.8, "max": 1.0, "step": 0.01, "default": 0.95},
                {"key": "layout_y_offset", "label": "海报整体垂直位置", "type": "range", "min": -500, "max": 500, "default": 100},
            ]
        },
        # --- 3. 背景与特效 ---
        {
            "group": "背景与特效",
            "items": [
                {"key": "bg_darkness", "label": "背景压暗程度", "type": "range", "min": 0, "max": 255, "default": 160},
                {"key": "bg_blur", "label": "背景模糊程度", "type": "range", "min": 0, "max": 200, "default": 100},
            ]
        },
        # --- 4. 字体与排版 (最全设置) ---
        {
            "group": "字体与排版",
            "items": [
                # 主标题
                {"key": "font_title", "label": "主标题字体", "type": "font", "default": "default.ttf"},
                {"key": "title_size", "label": "主标题字号", "type": "range", "min": 20, "max": 200, "default": 90},
                {"key": "title_color", "label": "主标题颜色", "type": "color", "default": "#FFFFFF"},
                {"key": "title_offset_y", "label": "主标题垂直偏移", "type": "range", "min": -600, "max": 600, "default": -280},
                
                # 副标题
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "副标题字号", "type": "range", "min": 10, "max": 150, "default": 50},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#AAAAAA"},
                {"key": "subtitle_offset_y", "label": "副标题垂直偏移", "type": "range", "min": -600, "max": 600, "default": -200},
            ]
        }
    ]

# =============================================================================
# 3. 渲染逻辑
# =============================================================================

def render(ctx, bg, config, assets, fonts):
    canvas_w, canvas_h = bg.size
    
    # 1. 资源准备
    poster_urls = assets.get('posters', [])
    count = int(config.get('poster_count', 5))
    if not poster_urls: return bg
    while len(poster_urls) < count: poster_urls.extend(poster_urls)
    poster_urls = poster_urls[:count]
    
    # 2. 动态背景
    mid_idx = count // 2
    hero_img_raw = ctx.download_img(poster_urls[mid_idx])
    if hero_img_raw:
        blur_r = int(config.get('bg_blur', 100))
        darkness = int(config.get('bg_darkness', 160))
        bg = hero_img_raw.resize((canvas_w, canvas_h), Image.BICUBIC)
        bg = bg.filter(ImageFilter.GaussianBlur(blur_r))
        overlay = Image.new('RGBA', bg.size, (0, 0, 0, darkness))
        bg = Image.alpha_composite(bg.convert('RGBA'), overlay)
    else:
        bg = Image.new('RGBA', (canvas_w, canvas_h), (30,30,30,255))
        
    # 3. 几何参数
    fan_radius = int(config.get('fan_radius', 1200))
    fan_spread = int(config.get('fan_spread', 40))
    center_scale = float(config.get('center_scale', 0.55))
    shrink_ratio = float(config.get('side_scale_shrink', 0.95))
    y_offset = int(config.get('layout_y_offset', 100))
    ref_op = 50 
    
    base_h = int(canvas_h * center_scale)
    base_w = int(base_h * 0.666)
    
    pivot_x = canvas_w // 2
    pivot_y = (canvas_h // 2) + fan_radius - (base_h // 2) + y_offset

    if count > 1:
        angle_step = fan_spread / (count - 1)
    else:
        angle_step = 0
        
    render_list = []
    
    for i in range(count):
        idx_offset = i - mid_idx
        theta_deg = idx_offset * angle_step
        theta_rad = math.radians(theta_deg)
        
        pos_x = pivot_x + fan_radius * math.sin(theta_rad)
        pos_y = pivot_y - fan_radius * math.cos(theta_rad)
        
        scale = math.pow(shrink_ratio, abs(idx_offset))
        w = int(base_w * scale)
        h = int(base_h * scale)
        z = -abs(idx_offset)
        
        render_list.append({
            "url": poster_urls[i],
            "w": w,
            "h": h,
            "x": pos_x,
            "y": pos_y,
            "angle": theta_deg,
            "z": z
        })

    render_list.sort(key=lambda x: x['z'])

    # 4. 绘制海报
    for item in render_list:
        img = ctx.download_img(item['url'])
        if not img: continue
        
        img = img.resize((item['w'], item['h']), Image.LANCZOS)
        img = add_rounded_corners(img, 15)
        
        s_blur = 20 if item['z'] == 0 else 10
        group, center_pt = create_shadow_and_reflection_group(img, ref_op, s_blur)
        
        rotated_group = group.rotate(-item['angle'], resample=Image.BICUBIC, expand=True)
        
        rw, rh = rotated_group.size
        offset_y_fix = (item['h'] / 2) 
        
        paste_x = int(item['x'] - rw / 2)
        paste_y = int(item['y'] - rh / 2 + offset_y_fix) 

        bg.paste(rotated_group, (paste_x, paste_y), mask=rotated_group)

    # 5. 绘制文字 (使用新版标准逻辑)
    draw = ImageDraw.Draw(bg)
    title = config.get('title', '')
    subtitle = config.get('subtitle', '')
    
    # 字体与颜色
    f_title = fonts.get('font_title') or fonts.get('main')
    f_sub = fonts.get('font_subtitle') or fonts.get('sub') or f_title
    
    c_title = config.get('title_color', '#FFFFFF')
    c_sub = config.get('subtitle_color', '#AAAAAA')
    
    # 绘制主标题
    if title:
        # 使用独立的偏移量
        offset_y = int(config.get('title_offset_y', -280))
        text_y = (canvas_h // 2) + offset_y
        
        # 居中绘制
        ctx.draw_text_wrapper(draw, title, canvas_w // 2, text_y, f_title, canvas_w, c_title, align='center')
        
    # 绘制副标题
    if subtitle:
        # 使用独立的偏移量 (如果不设置，默认给一个相对位置)
        sub_offset_y = int(config.get('subtitle_offset_y', -200))
        
        # 如果用户没有特意去调副标题位置（还是默认值），我们可以做一个简单的智能判断
        # 但为了参数逻辑清晰，这里完全遵从 sub_offset_y，即相对于画面中心
        sub_y = (canvas_h // 2) + sub_offset_y
        
        ctx.draw_text_wrapper(draw, subtitle, canvas_w // 2, sub_y, f_sub, canvas_w, c_sub, align='center')

    return bg