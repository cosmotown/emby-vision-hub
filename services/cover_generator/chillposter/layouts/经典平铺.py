from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

# === 辅助函数：圆角处理 ===
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
    
    # 确保图片有 alpha 通道
    if im.mode != 'RGBA': im = im.convert('RGBA')
    im.putalpha(alpha)
    return im

# === 1. 定义布局参数 (已统一顺序) ===
def get_schema():
    return [
        # --- 1. 核心资源与内容 ---
        {
            "group": "资源与内容",
            "items": [
                {"key": "poster_count", "label": "获取海报数量", "type": "range", "min": 0, "max": 10, "default": 5},
                {"key": "backdrop_count", "label": "获取背景数量", "type": "range", "min": 0, "max": 1, "default": 1},
                {"key": "title", "label": "主标题内容", "type": "text", "default": ""},
                {"key": "subtitle", "label": "副标题内容", "type": "text", "default": ""},
            ]
        },
        # --- 2. 布局与背景 ---
        {
            "group": "布局与背景",
            "items": [
                {"key": "poster_scale", "label": "海报整体缩放", "type": "range", "min": 0.3, "max": 1.5, "step": 0.1, "default": 0.8},
                {"key": "poster_x_percent", "label": "海报起始位置 %", "type": "range", "min": 0, "max": 100, "default": 45},
                {"key": "poster_y_percent", "label": "海报垂直位置 %", "type": "range", "min": 0, "max": 100, "default": 45},
                {"key": "poster_spacing", "label": "海报间距 (px)", "type": "range", "min": -50, "max": 100, "default": 20},
                {"key": "blur_radius", "label": "背景模糊程度", "type": "range", "min": 0, "max": 50, "default": 4},
            ]
        },
        # --- 3. 特效与装饰 ---
        {
            "group": "特效与装饰",
            "items": [
                {"key": "poster_corner_radius", "label": "海报圆角半径", "type": "range", "min": 0, "max": 50, "default": 15},
                {"key": "poster_brightness", "label": "海报亮度 (1.0原图)", "type": "range", "min": 0.1, "max": 1.5, "step": 0.1, "default": 1.0},
                {"key": "poster_shadow_opacity", "label": "海报阴影浓度", "type": "range", "min": 0, "max": 255, "default": 140},
                {"key": "mask_opacity", "label": "背景遮罩浓度", "type": "range", "min": 0, "max": 255, "default": 240},
                {"key": "mask_coverage", "label": "遮罩覆盖范围 %", "type": "range", "min": 10, "max": 100, "default": 100},
            ]
        },
        # --- 4. 字体与排版 (最全设置) ---
        {
            "group": "字体与排版",
            "items": [
                # 主标题
                {"key": "font_title", "label": "主标题字体", "type": "font", "default": "default.ttf"},
                {"key": "title_size", "label": "主标题字号", "type": "range", "min": 50, "max": 300, "default": 160},
                {"key": "title_color", "label": "主标题颜色", "type": "color", "default": "#FFFFFF"},
                
                # 副标题
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "副标题字号", "type": "range", "min": 30, "max": 150, "default": 80},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#DDDDDD"},
                
                # 排版
                {"key": "text_left_percent", "label": "文字左边距 %", "type": "range", "min": 0, "max": 100, "default": 10},
                {"key": "text_top_percent", "label": "文字上边距 %", "type": "range", "min": 0, "max": 100, "default": 30},
                {"key": "text_width_percent", "label": "文字最大宽度 %", "type": "range", "min": 10, "max": 100, "default": 50},
                {"key": "gap_1", "label": "标题与副标间距", "type": "range", "min": 0, "max": 100, "default": 20},
            ]
        }
    ]

# === 2. 渲染入口函数 ===
def render(ctx, bg, config, assets, fonts):
    width, height = bg.size
    
    # 背景模糊处理
    blur_radius = int(config.get('blur_radius', 4))
    if blur_radius > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(blur_radius))

    # === 1. 绘制海报墙 (水平圆角排列) ===
    poster_urls = assets.get('posters', [])
    if poster_urls:
        scale = float(config.get('poster_scale', 0.8)) # 默认缩放改小以适应横排
        # 基础尺寸定义
        base_h, base_w = 550, 366
        target_h, target_w = int(base_h * scale), int(base_w * scale)
        
        # [参数] 计算起始坐标
        start_x = int(width * (float(config.get('poster_x_percent', 45)) / 100))
        start_y = int(height * (float(config.get('poster_y_percent', 45)) / 100))
        
        # [参数] 间距与圆角
        spacing = int(config.get('poster_spacing', 20))
        radius = int(config.get('poster_corner_radius', 15))
        
        # [参数] 亮度和阴影
        p_bright = float(config.get('poster_brightness', 1.0))
        p_shadow = int(config.get('poster_shadow_opacity', 140))

        # 正序循环：水平从左到右排列
        for i, url in enumerate(poster_urls):
            # 调用 ctx 下载图片
            p_img = ctx.download_img(url)
            if not p_img: continue
            
            p_img = p_img.resize((target_w, target_h))

            # 亮度调整
            if p_bright != 1.0: 
                p_img = ImageEnhance.Brightness(p_img).enhance(p_bright)
            
            # === 圆角处理 ===
            p_img = add_rounded_corners(p_img, radius)
            
            # === 阴影处理 (针对圆角优化) ===
            # 创建一个稍大的画布画阴影
            shadow_margin = 10
            shadow = Image.new("RGBA", (target_w + shadow_margin*2, target_h + shadow_margin*2), (0,0,0,0))
            shadow_draw = ImageDraw.Draw(shadow)
            # 画圆角矩形阴影
            shadow_draw.rounded_rectangle(
                [(shadow_margin, shadow_margin), (target_w+shadow_margin, target_h+shadow_margin)], 
                radius=radius, 
                fill=(0, 0, 0, p_shadow)
            )
            # 模糊阴影
            shadow = shadow.filter(ImageFilter.GaussianBlur(10))
            
            # 计算位置: 起始X + (序号 * (宽度+间距))
            pos_x = start_x + (i * (target_w + spacing))
            # 垂直位置保持居中对齐逻辑
            pos_y = start_y + (int(base_h * float(config.get('poster_scale', 1.0))) - target_h)//2
            
            # 绘制阴影和图片
            if pos_x < width:
                # 阴影略微偏移
                bg.paste(shadow, (pos_x - shadow_margin + 5, pos_y - shadow_margin + 5), mask=shadow)
                bg.paste(p_img, (pos_x, pos_y), mask=p_img)
    
    # === 2. 绘制水平渐变遮罩 ===
    mask_opacity = int(config.get('mask_opacity', 240))
    mask_coverage = int(config.get('mask_coverage', 100))
    
    mask = ctx.create_smart_mask(width, height, mask_opacity, mask_coverage, 'horizontal')
    black_layer = Image.new('RGBA', (width, height), (0,0,0,255))
    black_layer.putalpha(mask)
    bg = Image.alpha_composite(bg, black_layer)

    # === 3. 绘制文字 ===
    draw = ImageDraw.Draw(bg)
    cx = int(width * (float(config.get('text_left_percent', 10)) / 100))
    cy = int(height * (float(config.get('text_top_percent', 30)) / 100))
    mw = int(width * (float(config.get('text_width_percent', 50)) / 100))
    
    # 字体与颜色
    f_title = fonts.get('font_title') or fonts.get('main')
    f_sub = fonts.get('font_subtitle') or fonts.get('sub') or f_title

    c_title = config.get('title_color', '#FFFFFF')
    c_sub = config.get('subtitle_color', '#DDDDDD')
    
    # 标题
    title = config.get('title', '')
    if title:
        cy = ctx.draw_text_wrapper(draw, title, cx, cy, f_title, mw, c_title)
        cy += int(config.get('gap_1', 20))
    
    # 副标题
    subtitle = config.get('subtitle', '')
    if subtitle:
        ctx.draw_text_wrapper(draw, subtitle, cx, cy, f_sub, mw, c_sub)
    
    return bg