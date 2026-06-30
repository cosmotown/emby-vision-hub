from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

# === 1. 定义布局参数 (已重构顺序) ===
def get_schema():
    return [
        # --- 1. 核心资源与内容 ---
        {
            "group": "资源与内容",
            "items": [
                # 极简模式主要使用背景图 (backdrop)，数量设为 1
                {"key": "backdrop_count", "label": "获取背景数量", "type": "range", "min": 1, "max": 1, "default": 1},
                {"key": "title", "label": "主标题内容", "type": "text", "default": ""},
                {"key": "subtitle", "label": "副标题内容", "type": "text", "default": ""},
            ]
        },
        # --- 2. 布局与背景 ---
        {
            "group": "布局与背景",
            "items": [
                {"key": "blur_radius", "label": "背景模糊程度", "type": "range", "min": 0, "max": 100, "default": 0},
                {"key": "brightness", "label": "背景亮度", "type": "range", "min": 0.1, "max": 1.0, "step": 0.1, "default": 0.7},
            ]
        },
        # --- 3. 特效与装饰 (遮罩设置) ---
        {
            "group": "特效与装饰",
            "items": [
                {"key": "mask_opacity", "label": "黑色遮罩浓度", "type": "range", "min": 0, "max": 255, "default": 180},
                {"key": "mask_coverage", "label": "遮罩覆盖范围 %", "type": "range", "min": 10, "max": 100, "default": 100},
                {"key": "mask_direction", "label": "遮罩方向", "type": "select", "options": [
                    {"label": "水平渐变 (左黑右亮)", "value": "horizontal"},
                    {"label": "垂直渐变 (上黑下亮)", "value": "vertical"}
                ], "default": "horizontal"},
            ]
        },
        # --- 4. 字体与排版 (统一整合) ---
        {
            "group": "字体与排版",
            "items": [
                # 主标题
                {"key": "font_title", "label": "主标题字体", "type": "font", "default": "default.ttf"},
                {"key": "title_size", "label": "主标题字号", "type": "range", "min": 50, "max": 400, "default": 180},
                {"key": "title_color", "label": "主标题颜色", "type": "color", "default": "#FFFFFF"},
                
                # 副标题
                {"key": "font_subtitle", "label": "副标题字体", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "副标题字号", "type": "range", "min": 30, "max": 200, "default": 90},
                {"key": "subtitle_color", "label": "副标题颜色", "type": "color", "default": "#DDDDDD"},
                
                # 排版位置
                {"key": "text_align", "label": "文字对齐方式", "type": "select", "options": [
                    {"label": "左对齐", "value": "left"},
                    {"label": "居中对齐", "value": "center"},
                    {"label": "右对齐", "value": "right"}
                ], "default": "left"},
                {"key": "text_left_percent", "label": "文字水平位置 %", "type": "range", "min": 0, "max": 100, "default": 10},
                {"key": "text_top_percent", "label": "文字垂直位置 %", "type": "range", "min": 0, "max": 100, "default": 60},
                {"key": "text_width_percent", "label": "文字最大宽度 %", "type": "range", "min": 10, "max": 100, "default": 80},
                {"key": "gap_1", "label": "标题与副标间距", "type": "range", "min": 0, "max": 200, "default": 30},
            ]
        }
    ]

# === 2. 渲染入口函数 ===
def render(ctx, bg, config, assets, fonts):
    width, height = bg.size
    
    # === 1. 背景处理 ===
    # 模糊
    blur_radius = int(config.get('blur_radius', 0))
    if blur_radius > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(blur_radius))
        
    # 亮度
    brightness = float(config.get('brightness', 0.7))
    if brightness != 1.0:
        bg = ImageEnhance.Brightness(bg).enhance(brightness)

    # === 2. 绘制渐变遮罩 ===
    mask_opacity = int(config.get('mask_opacity', 180))
    mask_coverage = int(config.get('mask_coverage', 100))
    direction = config.get('mask_direction', 'horizontal')
    
    mask = ctx.create_smart_mask(width, height, mask_opacity, mask_coverage, direction)
    black_layer = Image.new('RGBA', (width, height), (0,0,0,255))
    black_layer.putalpha(mask)
    bg = Image.alpha_composite(bg, black_layer)

    # === 3. 绘制文字 ===
    draw = ImageDraw.Draw(bg)
    
    # 布局参数
    align = config.get('text_align', 'left')
    cx = int(width * (float(config.get('text_left_percent', 10)) / 100))
    cy = int(height * (float(config.get('text_top_percent', 60)) / 100))
    mw = int(width * (float(config.get('text_width_percent', 80)) / 100))
    
    # 字体与颜色参数
    # 优先获取新版 key，兼容旧版 key
    f_title = fonts.get('font_title') or fonts.get('main')
    f_sub = fonts.get('font_subtitle') or fonts.get('sub') or f_title

    c_title = config.get('title_color', '#FFFFFF')
    c_sub = config.get('subtitle_color', '#DDDDDD')
    
    # 1. 主标题
    # draw_text_wrapper 通常返回绘制结束后的 Y 坐标
    title = config.get('title', '')
    if title:
        cy = ctx.draw_text_wrapper(draw, title, cx, cy, f_title, mw, c_title, align=align)
        # 间距
        cy += int(config.get('gap_1', 30))
    
    # 2. 副标题
    subtitle = config.get('subtitle', '')
    if subtitle:
        cy = ctx.draw_text_wrapper(draw, subtitle, cx, cy, f_sub, mw, c_sub, align=align)
    
    return bg