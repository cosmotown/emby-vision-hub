from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageOps, ImageFont, ImageColor
import numpy as np
import math
import os
import traceback

# =============================================================================
# 1. 资源加载器
# =============================================================================

def get_real_asset(assets, filename):
    if assets.get(filename): return assets[filename]
    try:
        curr_dir = os.path.dirname(os.path.abspath(__file__))
    except:
        curr_dir = os.path.join(os.getcwd(), 'layouts')
    local_path = os.path.join(curr_dir, filename)
    if os.path.exists(local_path):
        try: return Image.open(local_path).convert('RGBA')
        except: pass
    return None

# =============================================================================
# 2. 核心算法 (透视与遮罩)
# =============================================================================

def find_coeffs(pa, pb):
    matrix = []
    for p1, p2 in zip(pa, pb):
        matrix.append([p1[0], p1[1], 1, 0, 0, 0, -p2[0]*p1[0], -p2[0]*p1[1]])
        matrix.append([0, 0, 0, p1[0], p1[1], 1, -p2[1]*p1[0], -p2[1]*p1[1]])
    A = np.array(matrix, dtype=np.float32)
    B = np.array(pb, dtype=np.float32).reshape(8)
    try:
        res = np.linalg.solve(A, B)
        return res.reshape(8)
    except: return None

def warp_image_to_quad(img, quad):
    w, h = img.size
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    
    new_w = int(max_x - min_x)
    new_h = int(max_y - min_y)
    if new_w < 1: new_w = 1
    if new_h < 1: new_h = 1

    dst_zero = [(p[0]-min_x, p[1]-min_y) for p in quad]
    coeffs = find_coeffs(dst_zero, src)
    if coeffs is None: return img, 0, 0
    
    res = img.transform((new_w, new_h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    return res, int(min_x), int(min_y)

def create_radial_mask(size, radius_scale=1.0, intensity=1.0):
    w, h = size
    cx, cy = w // 2, h // 2
    max_dist = math.sqrt(cx**2 + cy**2) * radius_scale
    
    try:
        y, x = np.ogrid[:h, :w]
        dist = np.sqrt((x - cx)**2 + (y - cy)**2)
        norm_dist = dist / max_dist
        norm_dist = np.clip(norm_dist, 0, 1)
        norm_dist = norm_dist * norm_dist * (3 - 2 * norm_dist)
        
        alpha = (norm_dist * 255 * intensity).astype(np.uint8)
        mask = Image.fromarray(alpha, mode='L')
        
        vignette = Image.new('RGBA', size, (0, 0, 0, 0))
        black = Image.new('RGBA', size, (0, 0, 0, 255))
        vignette.paste(black, (0, 0), mask=mask)
        return vignette
    except:
        return Image.new('RGBA', size, (0,0,0, int(100*intensity)))

# =============================================================================
# 3. 字体与排版工具
# =============================================================================

def get_font_obj(fonts, config_font_name, size, fallback_key='main'):
    font_file = config_font_name
    if fonts.get(font_file):
        f = fonts.get(font_file)
        if hasattr(f, 'path'): return ImageFont.truetype(f.path, size)
        return f
    try:
        possible_paths = [font_file, os.path.join('fonts', font_file), os.path.join(os.getcwd(), 'layouts', font_file)]
        for path in possible_paths:
            if os.path.exists(path) and os.path.isfile(path): return ImageFont.truetype(path, size)
    except: pass
    if fallback_key and fonts.get(fallback_key):
        f = fonts.get(fallback_key)
        if hasattr(f, 'path'): return ImageFont.truetype(f.path, size)
        return f
    try: return ImageFont.truetype("arial.ttf", size)
    except: return ImageFont.load_default()

def measure_text_width(text, font, spacing=0):
    if not text: return 0
    try:
        widths = [font.getlength(char) for char in text]
        return sum(widths) + (len(text) - 1) * spacing
    except: return font.getbbox(text)[2]

def create_gradient_text(text, font, color_top, color_bottom, spacing=0):
    if not text: return None, 0, 0
    widths = [font.getlength(char) for char in text]
    total_w = int(sum(widths) + (len(text) - 1) * spacing)
    try:
        ascent, descent = font.getmetrics()
        total_h = ascent + descent
    except:
        bbox = font.getbbox(text)
        total_h = bbox[3] - bbox[1] + 10

    canvas_w = total_w + 10
    canvas_h = total_h + 20
    mask = Image.new('L', (canvas_w, canvas_h), 0)
    draw = ImageDraw.Draw(mask)
    curr_x = 0
    for i, char in enumerate(text):
        draw.text((curr_x, 0), char, font=font, fill=255)
        curr_x += widths[i] + spacing
        
    c1 = ImageColor.getrgb(color_top)
    c2 = ImageColor.getrgb(color_bottom)
    grad_src = Image.new('RGBA', (1, 2))
    grad_src.putpixel((0, 0), c1)
    grad_src.putpixel((0, 1), c2)
    gradient_img = grad_src.resize((canvas_w, canvas_h), Image.BICUBIC)
    gradient_img.putalpha(mask)
    return gradient_img, total_w, total_h

def draw_text_simple(draw, text, center_x, y, font, color, spacing=0, stroke_width=0, stroke_fill=None):
    if not text: return
    widths = [font.getlength(char) for char in text]
    total_w = sum(widths) + (len(text) - 1) * spacing
    curr_x = center_x - total_w / 2
    for i, char in enumerate(text):
        draw.text((curr_x, y), char, font=font, fill=color, 
                  stroke_width=stroke_width, stroke_fill=stroke_fill, anchor='la')
        curr_x += widths[i] + spacing

# =============================================================================
# 4. 辅助函数
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

def add_reflection(image, opacity=60):
    w, h = image.size
    reflection = ImageOps.flip(image)
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(h):
        alpha = int(opacity * (1 - y / h))
        draw.line((0, y, w, y), fill=alpha)
    reflection.putalpha(mask)
    new_h = h + h
    new_img = Image.new('RGBA', (w, new_h), (0,0,0,0))
    new_img.paste(image, (0, 0))
    new_img.paste(reflection, (0, h), mask=reflection)
    return new_img, h 

def resize_asset(img, height=None):
    if not img: return None
    w, h = img.size
    if height:
        scale = height / h
        return img.resize((int(w*scale), int(height)), Image.LANCZOS)
    return img

# =============================================================================
# 5. 配置参数 (已添加描边参数)
# =============================================================================

def get_schema():
    return [
        {
            "group": "核心布局",
            "items": [
                {"key": "poster_count", "label": "海报数量", "type": "range", "min": 3, "max": 9, "step": 2, "default": 5},
                {"key": "hero_h", "label": "C位海报高度", "type": "range", "min": 300, "max": 700, "default": 580},
                {"key": "base_y", "label": "海报基准线Y", "type": "range", "min": 500, "max": 1000, "default": 860},
                {"key": "side_growth", "label": "折扇展开幅度", "type": "range", "min": 0, "max": 40, "default": 15},
                {"key": "fan_lift", "label": "扇形上扬弧度", "type": "range", "min": 0, "max": 100, "default": 40},
                {"key": "gap", "label": "海报间隙", "type": "range", "min": 0, "max": 50, "default": 0},
            ]
        },
        {
            "group": "全局遮罩 (Vignette)",
            "items": [
                {"key": "vignette_strength", "label": "暗角浓度", "type": "range", "min": 0, "max": 100, "default": 60},
                {"key": "vignette_radius", "label": "亮部范围 (越小越聚光)", "type": "range", "min": 50, "max": 200, "default": 120},
            ]
        },
        {
            "group": "主标题 (Title)",
            "items": [
                {"key": "title", "label": "内容", "type": "text", "default": "华语电影"},
                {"key": "font_title", "label": "字体文件名", "type": "font", "default": "default.ttf"},
                {"key": "title_size", "label": "字号", "type": "range", "min": 50, "max": 300, "default": 160},
                {"key": "title_spacing", "label": "字间距", "type": "range", "min": -20, "max": 100, "default": 0},
                {"key": "title_y", "label": "垂直位置Y", "type": "range", "min": 0, "max": 600, "default": 140},
                {"key": "title_gradient_top", "label": "渐变色(上)", "type": "color", "default": "#FFF8DC"},
                {"key": "title_gradient_bottom", "label": "渐变色(下)", "type": "color", "default": "#DAA520"},
            ]
        },
        {
            "group": "副标题 (Subtitle)",
            "items": [
                {"key": "subtitle", "label": "内容", "type": "text", "default": "CHINESE MOVIES"},
                {"key": "font_subtitle", "label": "字体文件名", "type": "font", "default": "default.ttf"},
                {"key": "subtitle_size", "label": "字号", "type": "range", "min": 20, "max": 150, "default": 60},
                {"key": "subtitle_spacing", "label": "字间距", "type": "range", "min": -10, "max": 100, "default": 10},
                {"key": "subtitle_y", "label": "垂直位置Y", "type": "range", "min": 500, "max": 1200, "default": 980},
                {"key": "subtitle_color", "label": "颜色", "type": "color", "default": "#FFFFFF"},
                # 新增参数
                {"key": "subtitle_stroke_width", "label": "描边粗细", "type": "range", "min": 0, "max": 20, "default": 3},
                {"key": "subtitle_stroke_color", "label": "描边颜色", "type": "color", "default": "#000000"},
            ]
        },
        {
            "group": "装饰 (麦穗 & 光效)",
            "items": [
                {"key": "wheat_scale", "label": "麦穗大小 %", "type": "range", "min": 50, "max": 200, "default": 100},
                {"key": "wheat_padding", "label": "麦穗-文字距离", "type": "range", "min": 0, "max": 200, "default": 40},
                {"key": "wheat_y", "label": "麦穗位置Y", "type": "range", "min": 0, "max": 600, "default": 140},
                {"key": "light_y", "label": "光效线位置Y", "type": "range", "min": 0, "max": 600, "default": 260},
                {"key": "light_width_scale", "label": "光效宽度 %", "type": "range", "min": 50, "max": 200, "default": 160},
                {"key": "light_opacity", "label": "光效亮度", "type": "range", "min": 0, "max": 255, "default": 255},
            ]
        }
    ]

# =============================================================================
# 6. 渲染逻辑
# =============================================================================

def render(ctx, bg, config, assets, fonts):
    try:
        canvas_w, canvas_h = bg.size
        center_x = canvas_w // 2
        
        # 1. 背景绘制 (Layer 1)
        bg_img = get_real_asset(assets, '电影封面模板 V1_背景.jpg')
        if bg_img:
            bg = bg_img.resize((canvas_w, canvas_h), Image.LANCZOS).convert('RGBA')
        else:
            bg.paste(Image.new('RGBA', bg.size, (0,0,0,255)), (0,0))

        # 2. 绘制光效线 (Layer 2)
        img_light = get_real_asset(assets, '电影封面模板 V1_光效.png')
        if img_light:
            light_y = int(config.get('light_y', 260))
            l_scale = float(config.get('light_width_scale', 160)) / 100.0
            l_op = int(config.get('light_opacity', 255))
            
            lw = int(canvas_w * l_scale)
            lh = int(img_light.height * (lw / img_light.width))
            img_light = img_light.resize((lw, lh), Image.LANCZOS)
            
            if l_op < 255:
                r,g,b,a = img_light.split()
                a = ImageEnhance.Brightness(a).enhance(l_op/255.0)
                img_light = Image.merge('RGBA', (r,g,b,a))
            
            lx = center_x - lw // 2
            ly = light_y - lh // 2
            bg.paste(img_light, (lx, ly), mask=img_light)

        # 3. 海报骨架计算
        count = int(config.get('poster_count', 5))
        if count % 2 == 0: count += 1
        mid_idx = count // 2
        
        hero_h = int(config.get('hero_h', 580))
        poster_ratio = 0.68
        hero_w = int(hero_h * poster_ratio)
        base_y = int(config.get('base_y', 860)) 
        
        growth_rate = float(config.get('side_growth', 15)) / 100.0
        fan_lift = int(config.get('fan_lift', 40))
        gap = int(config.get('gap', 0))

        poster_urls = assets.get('posters', [])
        if not poster_urls: poster_urls = [None] * count
        while len(poster_urls) < count: poster_urls.extend(poster_urls)
        
        quads = [None] * count
        
        c_tl = (center_x - hero_w/2, base_y - hero_h)
        c_tr = (center_x + hero_w/2, base_y - hero_h)
        c_br = (center_x + hero_w/2, base_y)
        c_bl = (center_x - hero_w/2, base_y)
        quads[mid_idx] = [c_tl, c_tr, c_br, c_bl]
        
        prev_tr, prev_br = c_tr, c_br
        for i in range(mid_idx + 1, count):
            near_x_shift = gap
            near_tl = (prev_tr[0] + near_x_shift, prev_tr[1])
            near_bl = (prev_br[0] + near_x_shift, prev_br[1])
            side_w = hero_w 
            current_h_near = abs(prev_tr[1] - prev_br[1])
            current_h_far = current_h_near * (1.0 + growth_rate)
            far_x = near_tl[0] + side_w
            near_cy = (near_tl[1] + near_bl[1]) / 2
            far_cy = near_cy - fan_lift
            far_tl = (far_x, far_cy - current_h_far / 2)
            far_bl = (far_x, far_cy + current_h_far / 2)
            quads[i] = [near_tl, far_tl, far_bl, near_bl]
            prev_tr, prev_br = far_tl, far_bl

        prev_tl, prev_bl = c_tl, c_bl
        for i in range(mid_idx - 1, -1, -1):
            near_x_shift = -gap
            near_tr = (prev_tl[0] + near_x_shift, prev_tl[1])
            near_br = (prev_bl[0] + near_x_shift, prev_bl[1])
            side_w = hero_w
            current_h_near = abs(prev_tl[1] - prev_bl[1])
            current_h_far = current_h_near * (1.0 + growth_rate)
            far_x = near_tr[0] - side_w
            near_cy = (near_tr[1] + near_br[1]) / 2
            far_cy = near_cy - fan_lift
            far_tl = (far_x, far_cy - current_h_far / 2)
            far_bl = (far_x, far_cy + current_h_far / 2)
            quads[i] = [far_tl, near_tr, near_br, far_bl]
            prev_tl, prev_bl = far_tl, far_bl

        # 4. 渲染海报 (Layer 3)
        draw_order = []
        l, r = 0, count - 1
        while l < mid_idx:
            draw_order.append(l); draw_order.append(r)
            l += 1; r -= 1
        draw_order.append(mid_idx)
        
        for i in draw_order:
            if not quads[i]: continue
            img = ctx.download_img(poster_urls[i]) if poster_urls[i] else None
            if not img: continue
            
            q = quads[i]
            max_h = max(abs(q[3][1]-q[0][1]), abs(q[2][1]-q[1][1]))
            render_h = int(max_h * 1.5)
            img = resize_asset(img, height=render_h)
            img = add_rounded_corners(img, 16)
            
            dist = abs(i - mid_idx)
            brightness = 1.0 - (dist * 0.1)
            if brightness < 1.0: img = ImageEnhance.Brightness(img).enhance(brightness)
            full_img, orig_h = add_reflection(img, opacity=45)
            
            tl, tr, br, bl = q[0], q[1], q[2], q[3]
            vec_l = (bl[0]-tl[0], bl[1]-tl[1]); vec_r = (br[0]-tr[0], br[1]-tr[1])
            new_bl = (bl[0] + vec_l[0], bl[1] + vec_l[1])
            new_br = (br[0] + vec_r[0], br[1] + vec_r[1])
            
            warped, off_x, off_y = warp_image_to_quad(full_img, [tl, tr, new_br, new_bl])
            bg.paste(warped, (off_x, off_y), mask=warped)

        # 5. 文字绘制 (Layer 4)
        draw = ImageDraw.Draw(bg)
        
        # --- 主标题 ---
        t_str = config.get('title', '华语电影')
        t_size = int(config.get('title_size', 160))
        t_spacing = int(config.get('title_spacing', 0))
        t_y = int(config.get('title_y', 140))
        t_font = get_font_obj(fonts, config.get('font_title'), t_size)
        text_width = measure_text_width(t_str, t_font, t_spacing)
        
        # 麦穗
        wl = get_real_asset(assets, '电影封面模板 V1_麦穗L.png')
        wr = get_real_asset(assets, '电影封面模板 V1_麦穗R.png')
        if wl and wr:
            w_scale = float(config.get('wheat_scale', 100)) / 100.0
            w_padding = int(config.get('wheat_padding', 40))
            base_h = int(t_size * 1.4 * w_scale)
            wl, wr = resize_asset(wl, height=base_h), resize_asset(wr, height=base_h)
            wy = int(config.get('wheat_y', 140)) - (base_h // 2)
            bg.paste(wl, (int(center_x - text_width/2 - w_padding - wl.width), int(wy)), mask=wl)
            bg.paste(wr, (int(center_x + text_width/2 + w_padding), int(wy)), mask=wr)

        draw_text_simple(draw, t_str, center_x, t_y, t_font, '#B8860B', spacing=t_spacing, stroke_width=4, stroke_fill='#B8860B')
        grad_img, gw, gh = create_gradient_text(t_str, t_font, config.get('title_gradient_top', '#FFF8DC'), config.get('title_gradient_bottom', '#DAA520'), spacing=t_spacing)
        if grad_img: bg.paste(grad_img, (int(center_x - gw // 2), int(t_y)), mask=grad_img)

        # --- 副标题 (应用描边参数) ---
        s_str = config.get('subtitle', 'CHINESE MOVIES')
        s_font = get_font_obj(fonts, config.get('font_subtitle'), int(config.get('subtitle_size', 60)), fallback_key=None)
        
        # 获取新添加的描边参数
        s_stroke_w = int(config.get('subtitle_stroke_width', 3))
        s_stroke_c = config.get('subtitle_stroke_color', '#000000')
        
        draw_text_simple(
            draw, 
            s_str, 
            center_x, 
            int(config.get('subtitle_y', 980)), 
            s_font, 
            config.get('subtitle_color', '#FFFFFF'), 
            spacing=int(config.get('subtitle_spacing', 10)), 
            stroke_width=s_stroke_w,  # 使用参数
            stroke_fill=s_stroke_c    # 使用参数
        )

        # 6. 全局遮罩 (Layer 5 - 最顶层)
        v_strength = float(config.get('vignette_strength', 60)) / 100.0
        v_radius = float(config.get('vignette_radius', 120)) / 100.0
        
        if v_strength > 0:
            vignette_layer = create_radial_mask((canvas_w, canvas_h), radius_scale=v_radius, intensity=v_strength)
            bg.alpha_composite(vignette_layer)

    except Exception as e:
        print(f"❌ 渲染错误: {e}")
        traceback.print_exc()

    return bg