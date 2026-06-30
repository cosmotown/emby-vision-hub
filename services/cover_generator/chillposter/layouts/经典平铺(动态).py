from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
import math

# =============================================================================
# 辅助函数：圆角处理
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

# =============================================================================
# 定义布局参数 (已统一顺序)
# =============================================================================
def get_schema():
    return [
        # --- 1. 核心资源与内容 ---
        {
            "group": "资源与内容",
            "items": [
                {"key": "poster_count", "label": "获取海报数量", "type": "range", "min": 3, "max": 20, "default": 8},
                {"key": "backdrop_count", "label": "获取背景数量", "type": "range", "min": 0, "max": 1, "default": 1},
                {"key": "title", "label": "主标题内容", "type": "text", "default": ""},
                {"key": "subtitle", "label": "副标题内容", "type": "text", "default": ""},
            ]
        },
        # --- 2. 动画设置 (特有) ---
        {
            "group": "动画设置",
            "items": [
                {"key": "enable_animation", "label": "开启无限循环", "type": "boolean", "default": True},
                {"key": "anim_direction", "label": "方向 (1:左往右, -1:右往左)", "type": "select", "options": [
                    {"value": 1, "label": "从左往右 >>>"},
                    {"value": -1, "label": "<<< 从右往左"}
                ], "default": 1},
                {"key": "anim_frames", "label": "动画帧数 (总长)", "type": "range", "min": 20, "max": 300, "default": 90},
                {"key": "anim_duration", "label": "帧间隔 (ms)", "type": "range", "min": 20, "max": 200, "default": 33},
            ]
        },
        # --- 3. 布局与背景 ---
        {
            "group": "布局与背景",
            "items": [
                {"key": "poster_scale", "label": "海报缩放 (建议0.5-0.6)", "type": "range", "min": 0.3, "max": 1.5, "step": 0.05, "default": 0.55},
                {"key": "layout_start_x", "label": "整体起始X坐标", "type": "range", "min": -200, "max": 500, "default": 0},
                {"key": "poster_y_percent", "label": "海报垂直位置 %", "type": "range", "min": 0, "max": 100, "default": 45},
                {"key": "poster_spacing", "label": "海报间距 (px)", "type": "range", "min": 0, "max": 100, "default": 20},
                {"key": "blur_radius", "label": "背景模糊程度", "type": "range", "min": 0, "max": 50, "default": 4},
            ]
        },
        # --- 4. 特效与装饰 ---
        {
            "group": "特效与装饰",
            "items": [
                {"key": "poster_corner_radius", "label": "海报圆角半径", "type": "range", "min": 0, "max": 50, "default": 12},
                {"key": "poster_brightness", "label": "海报亮度 (1.0原图)", "type": "range", "min": 0.1, "max": 1.5, "step": 0.1, "default": 1.0},
                {"key": "poster_shadow_opacity", "label": "海报阴影浓度", "type": "range", "min": 0, "max": 255, "default": 140},
                {"key": "mask_opacity", "label": "黑色遮罩浓度", "type": "range", "min": 0, "max": 255, "default": 200},
            ]
        },
        # --- 5. 字体与排版 (最全设置) ---
        {
            "group": "字体与排版",
            "items": [
                # 主标题
                {"key": "font_title", "label": "主标题字体", "type": "font", "default": "default.ttf"},
                {"key": "title_size", "label": "主标题字号", "type": "range", "min": 30, "max": 200, "default": 80},
                {"key": "title_color", "label": "主标题颜色", "type": "color", "default": "#FFFFFF"},
                
                # 副标题
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "副标题字号", "type": "range", "min": 20, "max": 150, "default": 40},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#DDDDDD"},
                
                # 排版
                {"key": "text_left_percent", "label": "文字左边距 %", "type": "range", "min": 0, "max": 100, "default": 5},
                {"key": "text_top_percent", "label": "文字上边距 %", "type": "range", "min": 0, "max": 100, "default": 75},
                {"key": "text_gap", "label": "标题与副标间距", "type": "range", "min": 0, "max": 100, "default": 20},
            ]
        }
    ]

# =============================================================================
# 渲染入口函数
# =============================================================================
def render(ctx, bg, config, assets, fonts, step=0.0):
    width, height = bg.size
    
    # 背景模糊
    blur_radius = int(config.get('blur_radius', 4))
    if blur_radius > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(blur_radius))
    
    # 1. 绘制水平遮罩
    mask_opacity = int(config.get('mask_opacity', 200))
    if mask_opacity > 0:
        mask = ctx.create_smart_mask(width, height, mask_opacity, 100, 'horizontal')
        black_layer = Image.new('RGBA', (width, height), (0,0,0,255))
        black_layer.putalpha(mask)
        bg = Image.alpha_composite(bg, black_layer)

    poster_urls = assets.get('posters', [])
    count = int(config.get('poster_count', 8))
    
    # 自动补齐海报
    if poster_urls:
        while len(poster_urls) < count:
            poster_urls.extend(poster_urls)
        poster_urls = poster_urls[:count]

    if poster_urls:
        scale = float(config.get('poster_scale', 0.55))
        base_h, base_w = 550, 366
        target_h, target_w = int(base_h * scale), int(base_w * scale)
        
        spacing = int(config.get('poster_spacing', 20))
        radius = int(config.get('poster_corner_radius', 12))
        unit_width = target_w + spacing
        total_loop_width = count * unit_width
        
        layout_start_x = int(config.get('layout_start_x', 0))
        pos_y = int(height * (float(config.get('poster_y_percent', 45)) / 100)) - (target_h // 2)

        p_bright = float(config.get('poster_brightness', 1.0))
        p_shadow = int(config.get('poster_shadow_opacity', 140))

        shadow_margin = 10
        shadow = Image.new("RGBA", (target_w + shadow_margin*2, target_h + shadow_margin*2), (0,0,0,0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle(
            [(shadow_margin, shadow_margin), (target_w+shadow_margin, target_h+shadow_margin)], 
            radius=radius, fill=(0, 0, 0, p_shadow)
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))

        anim_offset = 0
        if config.get('enable_animation', True):
            direction = int(config.get('anim_direction', 1)) 
            anim_offset = step * total_loop_width * direction

        for i, url in enumerate(poster_urls):
            p_img = ctx.download_img(url)
            if not p_img: continue
            
            p_img = p_img.resize((target_w, target_h), Image.LANCZOS)
            if p_bright != 1.0: 
                p_img = ImageEnhance.Brightness(p_img).enhance(p_bright)
            p_img = add_rounded_corners(p_img, radius)
            
            base_x = i * unit_width
            current_x_relative = (base_x + anim_offset) % total_loop_width
            final_draw_x = layout_start_x + current_x_relative
            
            def draw_one_poster(x, y):
                # 优化性能：只绘制在画布范围内（稍微放宽一点边界）
                if x > -target_w - 50 and x < width + 50:
                    bg.paste(shadow, (int(x) - shadow_margin + 5, int(y) - shadow_margin + 5), mask=shadow)
                    bg.paste(p_img, (int(x), int(y)), mask=p_img)

            draw_one_poster(final_draw_x, pos_y)
            draw_one_poster(final_draw_x - total_loop_width, pos_y)
            draw_one_poster(final_draw_x + total_loop_width, pos_y)

    # 3. 绘制文字 (修复版：支持自定义颜色和字体)
    draw = ImageDraw.Draw(bg)
    
    # 字体与颜色
    f_title = fonts.get('font_title') or fonts.get('main')
    f_sub = fonts.get('font_subtitle') or fonts.get('sub') or f_title
    
    c_title = config.get('title_color', '#FFFFFF')
    c_sub = config.get('subtitle_color', '#DDDDDD')
    
    # 布局
    cx = int(width * (float(config.get('text_left_percent', 5)) / 100))
    cy = int(height * (float(config.get('text_top_percent', 75)) / 100))
    mw = int(width * 0.9) 
    gap = int(config.get('text_gap', 20))
    
    # 绘制标题
    title = config.get('title', '')
    if title:
        cy = ctx.draw_text_wrapper(draw, title, cx, cy, f_title, mw, c_title)
        cy += gap
        
    # 绘制副标题
    sub = config.get('subtitle', '')
    if sub:
        ctx.draw_text_wrapper(draw, sub, cx, cy, f_sub, mw, c_sub)

    return bg