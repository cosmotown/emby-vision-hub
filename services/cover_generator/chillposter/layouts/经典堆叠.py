from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageColor

# =============================================================================
# 1. 辅助函数
# =============================================================================
def add_rounded_corners(im, radius):
    """给图片添加圆角"""
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

def get_rgba_color(hex_color, opacity):
    """将 hex 颜色和透明度数值合并为 RGBA 元组"""
    try:
        rgb = ImageColor.getrgb(hex_color)
        return rgb + (int(opacity),)
    except:
        return (255, 255, 255, int(opacity))

# =============================================================================
# 2. 定义参数 (Schema) - 已统一顺序
# =============================================================================
def get_schema():
    return [
        # --- 1. 核心资源与内容 ---
        {
            "group": "资源与内容",
            "items": [
                {"key": "poster_count", "label": "海报数量", "type": "range", "min": 0, "max": 10, "default": 5},
                {"key": "backdrop_count", "label": "背景数量", "type": "range", "min": 0, "max": 1, "default": 1},
                {"key": "title", "label": "主标题内容", "type": "text", "default": ""},
                {"key": "subtitle", "label": "副标题内容", "type": "text", "default": ""},
            ]
        },
        # --- 2. 布局与背景 ---
        {
            "group": "布局与背景",
            "items": [
                {"key": "poster_scale", "label": "海报缩放", "type": "range", "min": 0.5, "max": 2.0, "step": 0.1, "default": 1.0},
                {"key": "poster_x_percent", "label": "海报水平位置 %", "type": "range", "min": 0, "max": 100, "default": 55},
                {"key": "poster_y_percent", "label": "海报垂直位置 %", "type": "range", "min": 0, "max": 100, "default": 45},
                {"key": "blur_radius", "label": "背景模糊", "type": "range", "min": 0, "max": 100, "default": 4},
            ]
        },
        # --- 3. 特效与装饰 (海报样式与遮罩) ---
        {
            "group": "特效与装饰",
            "items": [
                # 海报样式
                {"key": "poster_radius", "label": "海报圆角大小", "type": "range", "min": 0, "max": 100, "default": 20},
                {"key": "poster_gap", "label": "海报堆叠间距", "type": "range", "min": 0, "max": 300, "default": 140},
                {"key": "poster_brightness", "label": "海报亮度", "type": "range", "min": 0.1, "max": 1.5, "step": 0.1, "default": 1.0},
                # 阴影设置
                {"key": "poster_shadow_opacity", "label": "阴影浓度", "type": "range", "min": 0, "max": 255, "default": 160},
                {"key": "shadow_blur", "label": "阴影模糊半径", "type": "range", "min": 0, "max": 50, "default": 15},
                {"key": "shadow_offset", "label": "阴影偏移距离", "type": "range", "min": 0, "max": 50, "default": 10},
                # 遮罩设置
                {"key": "mask_opacity", "label": "背景遮罩浓度", "type": "range", "min": 0, "max": 255, "default": 240},
                {"key": "mask_coverage", "label": "遮罩范围 %", "type": "range", "min": 10, "max": 100, "default": 100},
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
                {"key": "title_opacity", "label": "主标题不透明度", "type": "range", "min": 0, "max": 255, "default": 255},
                
                # 副标题
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "副标题字号", "type": "range", "min": 30, "max": 150, "default": 80},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#DDDDDD"},
                {"key": "subtitle_opacity", "label": "副标题不透明度", "type": "range", "min": 0, "max": 255, "default": 180},
                
                # 排版位置
                {"key": "text_left_percent", "label": "文字左边距 %", "type": "range", "min": 0, "max": 100, "default": 10},
                {"key": "text_top_percent", "label": "文字上边距 %", "type": "range", "min": 0, "max": 100, "default": 30},
                {"key": "text_width_percent", "label": "文字最大宽度 %", "type": "range", "min": 10, "max": 100, "default": 50},
                {"key": "gap_1", "label": "标题与副标间距", "type": "range", "min": 0, "max": 100, "default": 20},
            ]
        }
    ]

# =============================================================================
# 3. 渲染逻辑
# =============================================================================
def render(ctx, bg, config, assets, fonts):
    width, height = bg.size
    
    # 1. 背景模糊
    blur_radius = int(config.get('blur_radius', 4))
    if blur_radius > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(blur_radius))

    # 2. 绘制海报
    poster_urls = assets.get('posters', [])
    if poster_urls:
        scale = float(config.get('poster_scale', 1.0))
        base_h, base_w = 550, 366
        front_h, front_w = int(base_h * scale), int(base_w * scale)
        
        anchor_x = int(width * (float(config.get('poster_x_percent', 55)) / 100))
        anchor_y = int(height * (float(config.get('poster_y_percent', 45)) / 100))
        offset_step = int(config.get('poster_gap', 140) * scale)
        radius = int(config.get('poster_radius', 20))
        
        p_bright = float(config.get('poster_brightness', 1.0))
        p_shadow_opacity = int(config.get('poster_shadow_opacity', 160))
        shadow_blur = int(config.get('shadow_blur', 15))
        shadow_offset = int(config.get('shadow_offset', 10))

        # 倒序绘制，为了让最后一张在最上面
        for i in range(len(poster_urls)-1, -1, -1):
            p_img = ctx.download_img(poster_urls[i])
            if not p_img: continue
            
            if p_bright != 1.0: 
                p_img = ImageEnhance.Brightness(p_img).enhance(p_bright)
            
            # 计算简单的景深缩放
            depth_scale = 0.9 ** i 
            target_w, target_h = int(front_w * depth_scale), int(front_h * depth_scale)
            p_img = p_img.resize((target_w, target_h), Image.LANCZOS)
            p_img = add_rounded_corners(p_img, radius)
            
            # 阴影处理
            shadow_canvas = Image.new("RGBA", (target_w + shadow_blur*4, target_h + shadow_blur*4), (0,0,0,0))
            shadow_draw = ImageDraw.Draw(shadow_canvas)
            s_x0 = shadow_blur + shadow_offset
            s_y0 = shadow_blur + shadow_offset
            shadow_draw.rounded_rectangle(
                (s_x0, s_y0, s_x0 + target_w, s_y0 + target_h), 
                radius=radius, 
                fill=(0, 0, 0, p_shadow_opacity)
            )
            shadow_canvas = shadow_canvas.filter(ImageFilter.GaussianBlur(shadow_blur))
            
            img_paste_x = shadow_blur
            img_paste_y = shadow_blur
            shadow_canvas.paste(p_img, (img_paste_x, img_paste_y), mask=p_img)
            
            pos_x = anchor_x + (i * offset_step)
            pos_y = anchor_y + (front_h - target_h)//2
            final_paste_x = pos_x - shadow_blur
            final_paste_y = pos_y - shadow_blur
            
            # 简单的边界检查，防止绘制在画布外过远的地方浪费资源
            if final_paste_x < width + 200: 
                bg.paste(shadow_canvas, (final_paste_x, final_paste_y), mask=shadow_canvas)
    
    # 3. 遮罩
    mask_opacity = int(config.get('mask_opacity', 240))
    mask_coverage = int(config.get('mask_coverage', 100))
    mask = ctx.create_smart_mask(width, height, mask_opacity, mask_coverage, 'horizontal')
    black_layer = Image.new('RGBA', (width, height), (0,0,0,255))
    black_layer.putalpha(mask)
    bg = Image.alpha_composite(bg, black_layer)

    # 4. 绘制文字 (已支持颜色和透明度)
    draw = ImageDraw.Draw(bg)
    cx = int(width * (float(config.get('text_left_percent', 10)) / 100))
    cy = int(height * (float(config.get('text_top_percent', 30)) / 100))
    mw = int(width * (float(config.get('text_width_percent', 50)) / 100))
    
    # 字体与颜色参数
    f_title = fonts.get('font_title') or fonts.get('main')
    f_sub = fonts.get('font_subtitle') or fonts.get('sub') or f_title

    t_color = get_rgba_color(config.get('title_color', '#FFFFFF'), config.get('title_opacity', 255))
    s_color = get_rgba_color(config.get('subtitle_color', '#DDDDDD'), config.get('subtitle_opacity', 180))

    # 绘制主标题
    title = config.get('title', '')
    if title:
        cy = ctx.draw_text_wrapper(draw, title, cx, cy, f_title, mw, t_color)
        cy += int(config.get('gap_1', 20))
    
    # 绘制副标题
    subtitle = config.get('subtitle', '')
    if subtitle:
        ctx.draw_text_wrapper(draw, subtitle, cx, cy, f_sub, mw, s_color)
    
    return bg