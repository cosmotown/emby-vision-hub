from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageColor
from io import BytesIO
import requests
import base64
import os
import re
import importlib.util
import math
import threading 
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed 

logger = logging.getLogger(__name__)
MAX_DYNAMIC_WIDTH = 1280
MAX_DYNAMIC_FRAMES = 180
MAX_DYNAMIC_WORKERS = 4

def _clamp_int(value, default, min_value, max_value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(parsed, max_value))

class PosterEngine:
    # 注意：layouts_dir 默认指向根目录下的 layouts 文件夹
    def __init__(self, fonts_dir="fonts", layouts_dir="layouts"):
        self.fonts_dir = fonts_dir
        self.layouts_dir = layouts_dir
        self.default_font_path = os.path.join(fonts_dir, "default.ttf")
        self.layout_modules = {} 
        self._load_layouts()
        self._img_cache = {}
        self._lock = threading.Lock()
        
        # [核心修复] 强制不走系统代理
        self.proxies = { "http": None, "https": None }

    def _load_layouts(self):
        print(f">>> [Engine] 正在加载布局模块: {self.layouts_dir}...")
        if not os.path.exists(self.layouts_dir):
            try:
                os.makedirs(self.layouts_dir)
            except: pass
            
        if os.path.exists(self.layouts_dir):
            for filename in os.listdir(self.layouts_dir):
                if filename.endswith(".py") and filename != "__init__.py":
                    module_name = filename[:-3] 
                    file_path = os.path.join(self.layouts_dir, filename)
                    try:
                        spec = importlib.util.spec_from_file_location(module_name, file_path)
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        if hasattr(module, 'render'):
                            self.layout_modules[module_name] = module.render
                            print(f"    ✅ 已加载布局: {module_name}")
                        else:
                            print(f"    ⚠️ 跳过 {filename}: 未找到 render 函数")
                    except Exception as e:
                        print(f"    ❌ 加载失败 {filename}: {e}")

    # === 工具函数 (增加 proxies) ===

    def download_img(self, url):
        if not url: return None
        with self._lock:
            if url in self._img_cache:
                return self._img_cache[url].copy()

        try:
            img = None
            if url.startswith("data:image"):
                base64_data = re.sub('^data:image/.+;base64,', '', url)
                image_data = base64.b64decode(base64_data)
                img = Image.open(BytesIO(image_data)).convert("RGBA")
            else:
                res = requests.get(url, timeout=15, proxies=self.proxies)
                if res.status_code == 200:
                    img = Image.open(BytesIO(res.content)).convert("RGBA")
            
            if img:
                with self._lock:
                    self._img_cache[url] = img.copy()
                return img
            return None
        except Exception as e:
            return None

    def draw_text_wrapper(self, draw, text, x, y, font, max_width, fill, line_spacing=10, align='left'):
        if not text: return y
        lines = []
        current_line = ""
        for char in text:
            test_line = current_line + char
            try: w = font.getlength(test_line)
            except: w = font.getbbox(test_line)[2]
            if w <= max_width: current_line = test_line
            else:
                if current_line: lines.append(current_line)
                current_line = char
        if current_line: lines.append(current_line)

        try: font_height = font.getmetrics()[0] + font.getmetrics()[1] + line_spacing
        except: bbox = font.getbbox("Hg"); font_height = (bbox[3] - bbox[1]) + line_spacing
        
        current_y = y
        for line in lines:
            try: line_w = font.getlength(line)
            except: line_w = font.getbbox(line)[2]
            draw_x = x
            if align == 'center': draw_x = x - (line_w / 2)
            elif align == 'right': draw_x = x - line_w
            draw.text((draw_x, current_y), line, font=font, fill=fill)
            current_y += font_height
        return current_y

    def create_smart_mask(self, width, height, opacity, coverage_percent, direction='horizontal'):
        mask = Image.new('L', (width, height), 0)
        draw = ImageDraw.Draw(mask)
        if direction == 'horizontal':
            end_x = int(width * (coverage_percent / 100))
            for x in range(width):
                if x <= end_x:
                    progress = x / end_x 
                    alpha = int(opacity * (1 - progress))
                    draw.line([(x, 0), (x, height)], fill=alpha)
                else: draw.line([(x, 0), (x, height)], fill=0)
        else:
            start_y = int(height * (1 - coverage_percent / 100))
            for y in range(height):
                if y >= start_y:
                    progress = (y - start_y) / (height - start_y)
                    alpha = int(opacity * progress)
                    draw.line([(0, y), (width, y)], fill=alpha)
                else: draw.line([(0, y), (width, y)], fill=0)
        return mask

    def _draw_badge(self, img, config, count, fonts):
        style = config.get('badge_style', 'none')
        if style == 'none' or not count: return img
        count_str = str(count)
        scale = 4 
        w, h = img.size
        overlay_w, overlay_h = w * scale, h * scale
        overlay = Image.new('RGBA', (overlay_w, overlay_h), (0,0,0,0))
        draw = ImageDraw.Draw(overlay)
        badge_font_file = config.get('badge_font', 'default.ttf')
        base_size = 40 if style == 'box' else 50
        user_size = int(config.get('badge_size', base_size))
        scaled_font = None
        if badge_font_file:
            path = os.path.join(self.fonts_dir, badge_font_file)
            if os.path.exists(path):
                try: scaled_font = ImageFont.truetype(path, user_size * scale)
                except: pass
        if not scaled_font:
            try:
                available_fonts = [f for f in os.listdir(self.fonts_dir) if f.lower().endswith(('.ttf', '.otf'))]
                if available_fonts:
                    fallback_path = os.path.join(self.fonts_dir, available_fonts[0])
                    scaled_font = ImageFont.truetype(fallback_path, user_size * scale)
            except: pass
        if not scaled_font: scaled_font = ImageFont.load_default()
        text_color = config.get('badge_text_color', '#ffffff')
        if style == 'ribbon' and 'badge_text_color' not in config: text_color = '#ffffff'
        bg_hex = config.get('badge_bg_color', '#000000')
        if style == 'ribbon' and 'badge_bg_color' not in config: bg_hex = '#b91c1c'
        elif style == 'box' and 'badge_bg_color' not in config: bg_hex = '#0f172a'
        opacity = int(config.get('badge_opacity', 255))
        try: r, g, b = ImageColor.getrgb(bg_hex); fill_color = (r, g, b, opacity)
        except: fill_color = (0, 0, 0, opacity)
        if style == 'ribbon':
            left, top, right, bottom = draw.textbbox((0, 0), count_str, font=scaled_font)
            text_w, text_h = right - left, bottom - top
            padding_v = 40 * scale
            ribbon_w = text_h + padding_v
            axis_span = ribbon_w * 1.414
            gap_size = int(user_size * 1.0 * scale)
            start, end = gap_size, gap_size + axis_span
            points = [(start, 0), (end, 0), (0, end), (0, start)]
            draw.polygon(points, fill=fill_color)
            layer_size = int(max(text_w, axis_span) * 2.5)
            txt_layer = Image.new('RGBA', (layer_size, layer_size), (0,0,0,0))
            txt_draw = ImageDraw.Draw(txt_layer)
            center = layer_size / 2
            draw_x = center - (text_w / 2) - left
            draw_y = center - (text_h / 2) - top
            txt_draw.text((draw_x, draw_y), count_str, font=scaled_font, fill=text_color)
            rotated_txt = txt_layer.rotate(45, resample=Image.BICUBIC)
            ribbon_center = (start + end) / 4
            paste_x = int(ribbon_center - layer_size / 2)
            paste_y = int(ribbon_center - layer_size / 2)
            overlay.paste(rotated_txt, (paste_x, paste_y), mask=rotated_txt)
        elif style == 'box':
            margin_left = 30 * scale
            margin_top = 30 * scale
            padding_x = int(user_size * 0.6 * scale)
            padding_y = int(user_size * 0.3 * scale)
            bbox = draw.textbbox((0, 0), count_str, font=scaled_font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            box_w = text_w + padding_x * 2
            box_h = text_h + padding_y * 2
            if box_w < box_h: box_w = box_h
            x1 = margin_left
            y1 = margin_top
            x2 = x1 + box_w
            y2 = y1 + box_h
            radius = box_h / 2
            draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=radius, fill=fill_color)
            border_alpha = int(config.get('badge_border_opacity', 40))
            border_rgba = (255, 255, 255, border_alpha)
            draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=radius, outline=border_rgba, width=2*scale)
            txt_x = x1 + (box_w - text_w) / 2 - bbox[0]
            txt_y = y1 + (box_h - text_h) / 2 - bbox[1]
            draw.text((txt_x, txt_y), count_str, font=scaled_font, fill=text_color)
        overlay_resized = overlay.resize((w, h), resample=Image.LANCZOS)
        return Image.alpha_composite(img, overlay_resized)

    # === [核心修改] APNG 生成 (含320x180网页优化) ===
    def draw(self, config, assets):
        bg = None
        if assets.get('bg_url'):
            bg = self.download_img(assets['bg_url'])
        
        if not bg: bg = Image.new("RGBA", (1920, 1080), (20, 30, 50, 255))
        bg = bg.resize((1920, 1080))
        
        blur = int(config.get('blur_radius', 4))
        if blur > 0: bg = bg.filter(ImageFilter.GaussianBlur(blur))
        bg = ImageEnhance.Brightness(bg).enhance(float(config.get('brightness', 0.7)))

        def _load_font(font_filename, size):
            if font_filename:
                path = os.path.join(self.fonts_dir, font_filename)
                if os.path.exists(path):
                    try: return ImageFont.truetype(path, size)
                    except: pass
            try:
                available_fonts = [f for f in os.listdir(self.fonts_dir) if f.lower().endswith(('.ttf', '.otf'))]
                if available_fonts:
                    fallback_path = os.path.join(self.fonts_dir, available_fonts[0])
                    return ImageFont.truetype(fallback_path, size)
            except: pass
            return ImageFont.load_default()

        fonts = {
            'main': _load_font(config.get('font_title'), int(config.get('title_size', 160))),
            'sub': _load_font(config.get('font_subtitle'), int(config.get('subtitle_size', 80))),
            'count': _load_font(config.get('font_count'), int(config.get('count_size', 40)))
        }

        engine_type = config.get('engine', 'classic') 
        is_dynamic = config.get('enable_animation', False)
        
        output = BytesIO()

        if is_dynamic:
            logger.info(
                ">>> [Engine] 启动多线程 APNG 渲染 "
                f"(Max {MAX_DYNAMIC_FRAMES} Frames / {MAX_DYNAMIC_WIDTH}px)"
            )
            
            target_w = _clamp_int(config.get('dynamic_output_width', 960), 960, 320, MAX_DYNAMIC_WIDTH)
            target_h = int(target_w * 9 / 16) 
            
            user_frames = _clamp_int(config.get('anim_frames', 30), 30, 1, MAX_DYNAMIC_FRAMES)
            total_frames = user_frames
            
            duration = _clamp_int(config.get('anim_duration', 100), 100, 20, 1000)
            
            frames_dict = {}

            def render_one_frame(idx):
                step = idx / total_frames if total_frames > 1 else 0
                frame_canvas = bg.copy()
                
                if engine_type in self.layout_modules:
                    render_func = self.layout_modules[engine_type]
                    try:
                        final_frame = render_func(self, frame_canvas, config, assets, fonts, step=step)
                    except TypeError:
                        final_frame = render_func(self, frame_canvas, config, assets, fonts)
                else:
                    final_frame = frame_canvas
                
                count_val = assets.get('count', 0)
                final_frame = self._draw_badge(final_frame, config, count_val, fonts)
                resized = final_frame.resize((target_w, target_h), Image.LANCZOS)
                return idx, resized

            with ThreadPoolExecutor(max_workers=MAX_DYNAMIC_WORKERS) as executor:
                futures = [executor.submit(render_one_frame, i) for i in range(total_frames)]
                for future in as_completed(futures):
                    try:
                        idx, img = future.result()
                        frames_dict[idx] = img
                    except Exception as e:
                        logger.warning(f"Frame Error: {e}")

            sorted_frames = [frames_dict[i] for i in range(total_frames) if i in frames_dict]

            if sorted_frames:
                sorted_frames[0].save(
                    output, 
                    format='PNG', 
                    save_all=True, 
                    append_images=sorted_frames[1:], 
                    duration=duration, 
                    loop=0,
                    optimize=False 
                )
                logger.info(f">>> [Engine] APNG 完成. 帧数: {len(sorted_frames)}, 大小: {output.getbuffer().nbytes / 1024 / 1024:.2f} MB")
        else:
            # 静态图逻辑，保持高清 1920x1080
            if engine_type in self.layout_modules:
                final_img = self.layout_modules[engine_type](self, bg, config, assets, fonts)
            else:
                if 'classic' in self.layout_modules:
                    final_img = self.layout_modules['classic'](self, bg, config, assets, fonts)
                else:
                    final_img = bg

            count_val = assets.get('count', 0)
            final_img = self._draw_badge(final_img, config, count_val, fonts)
            final_img.convert("RGB").save(output, format='JPEG', quality=90)
            
        return base64.b64encode(output.getvalue()).decode('utf-8')
