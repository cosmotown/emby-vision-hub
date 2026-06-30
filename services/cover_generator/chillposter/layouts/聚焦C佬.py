from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageOps, ImageChops
import math

# =============================================================================
# 1. 图形处理核心算法
# =============================================================================

def add_rounded_corners(im, radius):
    """圆角处理"""
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

def add_reflection(image, opacity=60, decay_rate=1.0):
    """制作倒影"""
    w, h = image.size
    reflection = ImageOps.flip(image)
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(h):
        progress = y / h
        current_alpha = int(opacity * (1 - math.pow(progress, decay_rate)))
        if current_alpha < 0: current_alpha = 0
        draw.line((0, y, w, y), fill=current_alpha)
    reflection.putalpha(mask)
    return reflection

def create_radial_gradient(size, center_color, edge_color):
    """创建径向渐变背景"""
    w, h = size
    light_source = Image.new('L', (w, h), 0)
    draw_ls = ImageDraw.Draw(light_source)
    radius = min(w, h) // 1.5
    cx, cy = w // 2, h // 2
    draw_ls.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=255)
    mask = light_source.filter(ImageFilter.GaussianBlur(radius=radius//2))
    base = Image.new('RGBA', size, edge_color)
    center = Image.new('RGBA', size, center_color)
    return Image.composite(center, base, mask)

# =============================================================================
# 2. 参数配置 (已统一顺序)
# =============================================================================

def get_schema():
    return [
        # --- 1. 核心资源与内容 ---
        {
            "group": "资源与内容",
            "items": [
                {"key": "poster_count", "label": "展示海报总数 (奇数最佳)", "type": "range", "min": 3, "max": 15, "step": 2, "default": 7},
                {"key": "title", "label": "主标题内容", "type": "text", "default": ""},
                {"key": "subtitle", "label": "副标题内容", "type": "text", "default": ""},
            ]
        },
        # --- 2. 舞台布局与背景 ---
        {
            "group": "舞台布局与背景",
            "items": [
                {"key": "hero_height_percent", "label": "C位海报高度 %", "type": "range", "min": 30, "max": 90, "step": 1, "default": 65},
                {"key": "poster_ratio", "label": "海报长宽比(宽/高)", "type": "range", "min": 0.5, "max": 1.8, "step": 0.01, "default": 0.66},
                {"key": "baseline_percent", "label": "舞台垂直基线 %", "type": "range", "min": 50, "max": 100, "step": 1, "default": 85},
                {"key": "side_scale_step", "label": "侧边海报缩放级差", "type": "range", "min": 0.5, "max": 0.99, "step": 0.01, "default": 0.85},
                {"key": "overlap_percent", "label": "重叠程度 %", "type": "range", "min": 0, "max": 80, "step": 5, "default": 40},
                {"key": "bg_blur", "label": "背景模糊", "type": "range", "min": 0, "max": 200, "default": 100},
            ]
        },
        # --- 3. 光影与特效 ---
        {
            "group": "光影与特效",
            "items": [
                {"key": "corner_radius", "label": "海报圆角", "type": "range", "min": 0, "max": 40, "default": 16},
                {"key": "perspective_darken", "label": "侧边压暗程度", "type": "range", "min": 0, "max": 255, "default": 40},
                {"key": "reflection_opacity", "label": "倒影强度", "type": "range", "min": 0, "max": 100, "default": 50},
                {"key": "hero_shadow_opacity", "label": "C位悬浮阴影浓度", "type": "range", "min": 0, "max": 255, "default": 180},
                {"key": "bg_overlay_opacity", "label": "背景压暗遮罩", "type": "range", "min": 0, "max": 255, "default": 100},
                {"key": "spotlight_intensity", "label": "中心聚光灯强度", "type": "range", "min": 0, "max": 255, "default": 30},
                {"key": "bg_vignette", "label": "边缘暗角强度", "type": "range", "min": 0, "max": 255, "default": 200},
            ]
        },
        # --- 4. 字体与排版 (最全设置) ---
        {
            "group": "字体与排版",
            "items": [
                # 主标题
                {"key": "font_title", "label": "主标题字体", "type": "font", "default": "default.ttf"},
                {"key": "title_size", "label": "主标题字号", "type": "range", "min": 50, "max": 300, "default": 140},
                {"key": "title_color", "label": "主标题颜色", "type": "color", "default": "#FFFFFF"},
                {"key": "title_y_offset", "label": "主标题垂直偏移", "type": "range", "min": -500, "max": 500, "default": 0},
                
                # 副标题 (新增)
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "副标题字号", "type": "range", "min": 20, "max": 150, "default": 60},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#AAAAAA"},
                {"key": "subtitle_offset_y", "label": "副标题垂直偏移", "type": "range", "min": -200, "max": 200, "default": 20},
            ]
        }
    ]

# =============================================================================
# 3. 渲染逻辑
# =============================================================================

def render(ctx, bg, config, assets, fonts):
    # --- 1. 参数读取 ---
    canvas_w, canvas_h = bg.size
    
    p_count = int(config.get('poster_count', 7))
    # 强制奇数，保证有C位
    if p_count % 2 == 0: p_count += 1
    
    # 布局参数
    hero_h_pct = float(config.get('hero_height_percent', 65)) / 100
    scale_step = float(config.get('side_scale_step', 0.85))
    overlap_pct = float(config.get('overlap_percent', 40)) / 100
    poster_ratio = float(config.get('poster_ratio', 0.66)) 
    baseline_pct = float(config.get('baseline_percent', 85)) / 100
    
    # 光影参数
    darken_step = int(config.get('perspective_darken', 40))
    radius = int(config.get('corner_radius', 16))
    ref_opacity = int(config.get('reflection_opacity', 50))
    bg_vignette = int(config.get('bg_vignette', 200))
    bg_blur = int(config.get('bg_blur', 100))
    bg_overlay_op = int(config.get('bg_overlay_opacity', 100))
    spotlight_int = int(config.get('spotlight_intensity', 30))
    shadow_op = int(config.get('hero_shadow_opacity', 180))

    poster_urls = assets.get('posters', [])
    if not poster_urls: return bg
    
    while len(poster_urls) < p_count:
        poster_urls.extend(poster_urls)
    poster_urls = poster_urls[:p_count]

    # --- 2. 制作背景 ---
    if bg_blur > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(bg_blur))
    
    # [应用] 背景遮罩浓度
    overlay = Image.new('RGBA', bg.size, (0, 0, 0, bg_overlay_op))
    bg = Image.alpha_composite(bg.convert('RGBA'), overlay)

    # [应用] 中心光照强度
    spotlight = create_radial_gradient(
        bg.size, 
        center_color=(255, 255, 255, spotlight_int), 
        edge_color=(0, 0, 0, bg_vignette)
    )
    bg = Image.alpha_composite(bg, spotlight)
    
    # --- 3. 计算海报位置 ---
    render_queue = [] 
    mid_idx = p_count // 2
    hero_h = int(canvas_h * hero_h_pct)
    hero_w = int(hero_h * poster_ratio)
    
    center_x = canvas_w // 2
    base_y = int(canvas_h * baseline_pct)

    for i in range(p_count):
        distance = abs(i - mid_idx)
        current_scale = math.pow(scale_step, distance)
        current_h = int(hero_h * current_scale)
        current_w = int(hero_w * current_scale)
        
        darken_amount = distance * darken_step
        brightness = max(0.2, 1.0 - (darken_amount / 255.0))
        
        # 计算X轴位置
        x_dist = 0
        for k in range(distance):
            s = math.pow(scale_step, k)
            w = hero_w * s
            x_dist += w * (1 - overlap_pct)
            
        if i < mid_idx: # 左侧
            pos_x = center_x - x_dist
            z_index = -distance 
        elif i > mid_idx: # 右侧
            pos_x = center_x + x_dist
            z_index = -distance
        else: # C位
            pos_x = center_x
            z_index = 100 
            
        render_queue.append({
            "url": poster_urls[i],
            "width": current_w,
            "height": current_h,
            "x": int(pos_x),
            "y": base_y,
            "brightness": brightness,
            "z_index": z_index,
            "is_hero": (i == mid_idx)
        })

    render_queue.sort(key=lambda item: item['z_index'])

    # --- 4. 渲染海报 ---
    for item in render_queue:
        img = ctx.download_img(item['url'])
        if not img: continue
        
        img = img.resize((item['width'], item['height']), Image.LANCZOS)
        img = add_rounded_corners(img, radius)
        
        if item['brightness'] < 1.0:
            enhancer = ImageEnhance.Brightness(img)
            img = enhancer.enhance(item['brightness'])
            
        paste_x = item['x'] - item['width'] // 2
        paste_y = item['y'] - item['height']

        # 倒影
        if ref_opacity > 0:
            ref_img = add_reflection(img, opacity=ref_opacity, decay_rate=1.5)
            bg.paste(ref_img, (paste_x, item['y']), mask=ref_img)

        # 阴影 (C位)
        if item['is_hero'] and shadow_op > 0:
            shadow_radius = 40
            shadow = Image.new('RGBA', (item['width'] + shadow_radius*2, item['height'] + shadow_radius*2), (0,0,0,0))
            s_draw = ImageDraw.Draw(shadow)
            s_draw.rounded_rectangle(
                (shadow_radius, shadow_radius, shadow_radius+item['width'], shadow_radius+item['height']), 
                radius=radius, fill=(0,0,0, shadow_op)
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(20))
            bg.paste(shadow, (paste_x - shadow_radius, paste_y - shadow_radius + 10), mask=shadow)
        
        bg.paste(img, (paste_x, paste_y), mask=img)

    # --- 5. 绘制文字 (新增副标题支持) ---
    draw = ImageDraw.Draw(bg)
    
    f_title = fonts.get('font_title') or fonts.get('main')
    f_sub = fonts.get('font_subtitle') or fonts.get('sub') or f_title
    
    title_str = config.get('title', '')
    sub_str = config.get('subtitle', '')
    
    # 计算基础位置 (默认在C位海报上方)
    text_cx = canvas_w // 2
    base_text_y = (base_y - hero_h) // 2 
    if base_text_y < 50: base_text_y = 50
    max_w = int(canvas_w * 0.8)

    # 绘制主标题
    if title_str and f_title:
        c_title = config.get('title_color', '#FFFFFF')
        y_offset = int(config.get('title_y_offset', 0))
        # 居中对齐，返回绘制结束的Y坐标
        next_y = ctx.draw_text_wrapper(draw, title_str, text_cx, base_text_y + y_offset, f_title, max_w, c_title, align='center')
        
        # 绘制副标题 (紧接在标题下方)
        if sub_str:
            c_sub = config.get('subtitle_color', '#AAAAAA')
            sub_offset = int(config.get('subtitle_offset_y', 20))
            # 注意：draw_text_wrapper 返回的是文本底部的 Y，所以这里作为起始点
            # 但原始 draw_text_wrapper 的 y 是中心点还是顶部取决于实现，
            # 假设 ctx.draw_text_wrapper 的 y 参数是文字中心(center)或顶部(top)，这里做一个简单的相对位移
            # 为了保险，重新计算副标题位置
            
            # 使用 title_size 估算高度来叠加
            title_size = int(config.get('title_size', 140))
            sub_y = base_text_y + y_offset + title_size + sub_offset
            
            ctx.draw_text_wrapper(draw, sub_str, text_cx, sub_y, f_sub, max_w, c_sub, align='center')

    return bg