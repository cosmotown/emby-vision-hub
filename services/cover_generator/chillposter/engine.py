from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageColor, ImageOps
from io import BytesIO
import requests
import base64
import os
import re
import importlib.util
import math
import threading
import logging

logger = logging.getLogger(__name__)
MAX_DYNAMIC_WIDTH = 640
MAX_DYNAMIC_FRAMES = 72
MAX_FOCUS_DYNAMIC_FRAMES = 36
MAX_FAN_DYNAMIC_FRAMES = 77
MAX_ROTATE_STACK_DYNAMIC_FRAMES = 160
DYNAMIC_BACKGROUND_OVERLAY_OPACITY = 31

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

    def _rounded(self, im, radius):
        if radius <= 0:
            return im.convert('RGBA')
        circle = Image.new('L', (radius * 2, radius * 2), 0)
        draw = ImageDraw.Draw(circle)
        draw.ellipse((0, 0, radius * 2, radius * 2), fill=255)
        alpha = Image.new('L', im.size, 255)
        w, h = im.size
        alpha.paste(circle.crop((0, 0, radius, radius)), (0, 0))
        alpha.paste(circle.crop((0, radius, radius, radius * 2)), (0, h - radius))
        alpha.paste(circle.crop((radius, 0, radius * 2, radius)), (w - radius, 0))
        alpha.paste(circle.crop((radius, radius, radius * 2, radius * 2)), (w - radius, h - radius))
        im = im.convert('RGBA')
        im.putalpha(alpha)
        return im

    def _reflection(self, image, opacity=45, decay_rate=1.4):
        w, h = image.size
        reflection = ImageOps.flip(image)
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        for y in range(h):
            progress = y / h
            alpha = max(0, int(opacity * (1 - math.pow(progress, decay_rate))))
            draw.line((0, y, w, y), fill=alpha)
        reflection.putalpha(mask)
        return reflection

    def _relative_loop_position(self, index, active_position, count):
        return ((index - active_position + count / 2) % count) - count / 2

    def _dynamic_badge_config(self, config, scale_factor):
        """Scale the template badge from its 1920px design canvas to animation output."""
        badge_config = dict(config)
        try:
            badge_size = float(config.get('badge_size', 40))
        except (TypeError, ValueError):
            badge_size = 40
        badge_config['badge_size'] = max(8, int(badge_size * scale_factor))
        return badge_config

    def _draw_dynamic_layout_text(self, canvas, config, fonts, scale_factor, layout):
        draw = ImageDraw.Draw(canvas)
        shadow_offset = max(1, int(6 * scale_factor))
        width, height = canvas.size
        title = config.get('title', '')
        subtitle = config.get('subtitle', '')
        title_font = fonts.get('main')
        subtitle_font = fonts.get('sub') or title_font

        if layout == 'rotate':
            title_x = int(width * (float(config.get('title_pos_x', 15)) / 100))
            title_y = int(height * (float(config.get('title_pos_y', 42)) / 100))
            title_width = int(width * (float(config.get('title_width_pct', 40)) / 100))
            subtitle_x = int(width * (float(config.get('subtitle_pos_x', 15)) / 100))
            subtitle_y = int(height * (float(config.get('subtitle_pos_y', 55)) / 100))
            subtitle_width = int(width * (float(config.get('sub_width_pct', 40)) / 100))
        elif layout == 'rotate_stack':
            title_x = int(width * (float(config.get('title_x', 5)) / 100))
            title_y = int(height * (float(config.get('title_y', 40)) / 100))
            title_width = width // 2
            subtitle_x = int(width * (float(config.get('sub_x', 8)) / 100))
            subtitle_y = int(height * (float(config.get('sub_y', 58)) / 100))
            subtitle_width = width // 3
        else:
            title_x = int(width * (float(config.get('text_left_percent', 10)) / 100))
            title_y = int(height * (float(config.get('text_top_percent', 30)) / 100))
            title_width = int(width * (float(config.get('text_width_percent', 50)) / 100))
            subtitle_x = title_x
            subtitle_y = title_y + int(float(config.get('title_size', 160)) * scale_factor) + int(float(config.get('gap_1', 20)) * scale_factor)
            subtitle_width = title_width

        if title and title_font:
            self.draw_text_wrapper(draw, title, title_x + shadow_offset, title_y + shadow_offset, title_font, title_width, '#000000')
            self.draw_text_wrapper(draw, title, title_x, title_y, title_font, title_width, config.get('title_color', '#FFFFFF'))
        if subtitle and subtitle_font:
            self.draw_text_wrapper(draw, subtitle, subtitle_x + shadow_offset, subtitle_y + shadow_offset, subtitle_font, subtitle_width, '#000000')
            self.draw_text_wrapper(draw, subtitle, subtitle_x, subtitle_y, subtitle_font, subtitle_width, config.get('subtitle_color', '#DDDDDD'))

    def _make_dynamic_card(self, image, size, radius, brightness=1.0):
        card = ImageOps.fit(image, (size, size), method=Image.LANCZOS).convert('RGBA')
        if brightness != 1.0:
            card = ImageEnhance.Brightness(card).enhance(brightness)
        return self._rounded(card, radius)

    def _make_dynamic_backgrounds(self, posters, size, blur_radius, overlay_opacity):
        overlay = Image.new('RGBA', size, (0, 0, 0, DYNAMIC_BACKGROUND_OVERLAY_OPACITY))
        backgrounds = []
        for poster in posters:
            source = poster.convert('RGBA')
            background = ImageOps.fit(source, size, method=Image.LANCZOS).convert('RGBA')
            # Dynamic templates apply their own light readability mask. Do not
            # dim the source first, otherwise multiple overlays make Backdrops
            # look both dark and washed out in Emby.
            if blur_radius:
                background = background.filter(ImageFilter.GaussianBlur(blur_radius))
            backgrounds.append(Image.alpha_composite(background, overlay))
        return backgrounds

    @staticmethod
    def _quantize_animation_frames(frames, colors=192):
        """Build one representative palette for compact, compatible APNG output."""
        sample_count = min(16, len(frames))
        sample_width, sample_height = 64, 36
        columns = 4
        rows = max(1, math.ceil(sample_count / columns))
        palette_sheet = Image.new('RGB', (columns * sample_width, rows * sample_height))
        for sample_index in range(sample_count):
            frame_index = round(sample_index * (len(frames) - 1) / max(1, sample_count - 1))
            sample = ImageOps.fit(frames[frame_index].convert('RGB'), (sample_width, sample_height), method=Image.LANCZOS)
            palette_sheet.paste(sample, ((sample_index % columns) * sample_width, (sample_index // columns) * sample_height))
        palette = palette_sheet.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
        return [
            frame.convert('RGB').quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
            for frame in frames
        ]

    def _dynamic_background_sources(self, assets, posters):
        """Prefer one landscape backdrop per poster; fall back to poster art if incomplete."""
        backdrop_urls = list(assets.get('backdrops') or [])
        backdrops = [image.convert('RGBA') for image in (self.download_img(url) for url in backdrop_urls) if image]
        if len(backdrops) >= len(posters):
            return backdrops[:len(posters)]
        return posters

    def _dynamic_background_transition(self, backgrounds, active_position):
        current_index = int(active_position) % len(backgrounds)
        progress = active_position - int(active_position)
        if progress < 0.75:
            return backgrounds[current_index].copy()
        blend = (progress - 0.75) / 0.25
        blend = blend * blend * (3 - 2 * blend)
        return Image.blend(backgrounds[current_index], backgrounds[(current_index + 1) % len(backgrounds)], blend)

    def _paste_rotated_card(self, canvas, card, center_x, center_y, angle, opacity=255, shadow_opacity=120, shadow_blur=4):
        if opacity < 255:
            alpha = card.getchannel('A').point(lambda value: value * opacity // 255)
            card = card.copy()
            card.putalpha(alpha)
        rotated = card.rotate(angle, resample=Image.BICUBIC, expand=True)
        paste_x = int(center_x - rotated.width / 2)
        paste_y = int(center_y - rotated.height / 2)
        if shadow_opacity:
            shadow = Image.new('RGBA', rotated.size, (0, 0, 0, 0))
            shadow.putalpha(rotated.getchannel('A').point(lambda value: value * shadow_opacity // 255))
            shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
            canvas.paste(shadow, (paste_x + 3, paste_y + 4), mask=shadow)
        canvas.paste(rotated, (paste_x, paste_y), mask=rotated)

    def _make_rotated_card_layer(self, card, angle, shadow_opacity=120, shadow_blur=4):
        """Pre-render a rotated card and its shadow for animations that only move it."""
        rotated = card.rotate(angle, resample=Image.BICUBIC, expand=True)
        layer = Image.new('RGBA', rotated.size, (0, 0, 0, 0))
        if shadow_opacity:
            shadow = Image.new('RGBA', rotated.size, (0, 0, 0, 0))
            shadow.putalpha(rotated.getchannel('A').point(lambda value: value * shadow_opacity // 255))
            shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
            layer.paste(shadow, (3, 4), mask=shadow)
        layer.paste(rotated, (0, 0), mask=rotated)
        return layer

    def _draw_dynamic_classic_stack_cover(self, bg, config, assets, font_loader, output):
        target_w = _clamp_int(config.get('dynamic_output_width', 480), 480, 320, 480)
        target_h = int(target_w * 9 / 16)
        scale_factor = target_w / 1920
        total_frames = _clamp_int(config.get('anim_frames', 48), 48, 1, 54)
        page_hold_duration = _clamp_int(config.get('page_hold_duration', 650), 650, 250, 3000)
        page_transition_duration = _clamp_int(config.get('page_transition_duration', 150), 150, 60, 500)
        poster_urls = list(assets.get('posters') or [])
        if not poster_urls:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)
        poster_count = max(1, int(float(config.get('poster_count') or len(poster_urls))))
        while len(poster_urls) < poster_count:
            poster_urls.extend(poster_urls)
        poster_urls = poster_urls[:poster_count]

        raw_posters = [image.convert('RGBA') for image in (self.download_img(url) for url in poster_urls) if image]
        if not raw_posters:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)

        background_blur = max(0, int(float(config.get('dynamic_bg_blur', 0)) * scale_factor))
        backgrounds = self._make_dynamic_backgrounds(
            self._dynamic_background_sources(assets, raw_posters),
            (target_w, target_h),
            background_blur,
            0,
        )
        fonts = self._dynamic_fonts(config, font_loader, scale_factor, title_default=160, subtitle_default=80)
        badge_config = self._dynamic_badge_config(config, scale_factor)
        card_h = max(56, int(target_h * 0.5 * float(config.get('poster_scale', 0.9))))
        card_w = max(36, int(card_h * 0.666))
        radius = max(2, int(float(config.get('poster_radius', 20)) * scale_factor))
        anchor_x = int(target_w * (float(config.get('poster_x_percent', 55)) / 100))
        anchor_y = int(target_h * (float(config.get('poster_y_percent', 50)) / 100))
        gap = max(10, int(float(config.get('poster_gap', 140)) * scale_factor))

        frames = []
        frame_durations = []
        frames_per_page = max(8, total_frames // len(raw_posters))
        transition_start = frames_per_page - 2
        render_steps = [
            (0, page_hold_duration * transition_start),
            (transition_start, page_transition_duration),
            (transition_start + 1, page_transition_duration),
        ]
        for page in range(len(raw_posters)):
            for page_frame, frame_duration in render_steps:
                if page_frame < transition_start:
                    transition = 0.0
                else:
                    transition = (page_frame - transition_start + 1) / 3
                    transition = transition * transition * (3 - 2 * transition)

                frame = backgrounds[page].copy()
                if transition:
                    frame = Image.blend(frame, backgrounds[(page + 1) % len(backgrounds)], transition)

                # During the short transition, each card advances exactly one stack slot.
                # The outgoing front card exits left while a new card enters at the back.
                if transition:
                    incoming_position = len(raw_posters) - transition
                    incoming_scale = math.pow(0.9, incoming_position)
                    incoming_w = max(24, int(card_w * incoming_scale))
                    incoming_h = max(36, int(card_h * incoming_scale))
                    incoming = self._rounded(
                        ImageOps.fit(raw_posters[(page + len(raw_posters)) % len(raw_posters)], (incoming_w, incoming_h), method=Image.LANCZOS).convert('RGBA'),
                        radius,
                    )
                    frame.paste(
                        incoming,
                        (int(anchor_x + incoming_position * gap), int(anchor_y + (card_h - incoming_h) / 2)),
                        mask=incoming,
                    )

                for depth in range(len(raw_posters) - 1, 0, -1):
                    position = depth - transition
                    depth_scale = math.pow(0.9, position)
                    width = max(24, int(card_w * depth_scale))
                    height = max(36, int(card_h * depth_scale))
                    source = raw_posters[(page + depth) % len(raw_posters)]
                    card = self._rounded(ImageOps.fit(source, (width, height), method=Image.LANCZOS).convert('RGBA'), radius)
                    x = int(anchor_x + position * gap)
                    y = int(anchor_y + (card_h - height) / 2)
                    if x < target_w + card_w:
                        frame.paste(card, (x, y), mask=card)

                current = self._rounded(ImageOps.fit(raw_posters[page], (card_w, card_h), method=Image.LANCZOS).convert('RGBA'), radius)
                if transition:
                    current.putalpha(current.getchannel('A').point(lambda value: value * int(255 * (1 - transition)) // 255))
                    frame.paste(current, (int(anchor_x - transition * gap), anchor_y), mask=current)
                else:
                    frame.paste(current, (anchor_x, anchor_y), mask=current)
                self._draw_dynamic_layout_text(frame, config, fonts, scale_factor, 'classic_stack')
                frames.append(self._draw_badge(frame, badge_config, assets.get('count', 0), fonts))
                frame_durations.append(frame_duration)

        frames[0].save(output, format='PNG', save_all=True, append_images=frames[1:], duration=frame_durations, loop=0, optimize=False)

    def _draw_dynamic_rotate_cover(self, bg, config, assets, font_loader, output):
        target_w = _clamp_int(config.get('dynamic_output_width', 480), 480, 320, 480)
        target_h = int(target_w * 9 / 16)
        scale_factor = target_w / 1920
        total_frames = _clamp_int(config.get('anim_frames', 36), 36, 1, 48)
        page_hold_duration = _clamp_int(config.get('page_hold_duration', 3200), 3200, 1000, 6000)
        page_transition_duration = _clamp_int(config.get('page_transition_duration', 80), 80, 50, 250)
        poster_urls = list(assets.get('posters') or [])
        if not poster_urls:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)
        poster_count = max(1, int(float(config.get('poster_count') or len(poster_urls))))
        while len(poster_urls) < poster_count:
            poster_urls.extend(poster_urls)
        poster_urls = poster_urls[:poster_count]
        raw_posters = [image.convert('RGBA') for image in (self.download_img(url) for url in poster_urls) if image]
        if not raw_posters:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)

        blur = max(0, int(float(config.get('dynamic_bg_blur', 0)) * scale_factor))
        backgrounds = self._make_dynamic_backgrounds(
            self._dynamic_background_sources(assets, raw_posters),
            (target_w, target_h),
            blur,
            0,
        )
        fonts = self._dynamic_fonts(config, font_loader, scale_factor, title_default=160, subtitle_default=80)
        badge_config = self._dynamic_badge_config(config, scale_factor)
        card_size = max(80, int(target_h * float(config.get('card_scale', 0.65))))
        radius = max(3, int(float(config.get('card_radius', 40)) * scale_factor))
        center_x = int(target_w * (float(config.get('card_x_percent', 68)) / 100))
        center_y = int(target_h * (float(config.get('card_y_percent', 50)) / 100))
        angles = [float(config.get('angle_bot', 24)), float(config.get('angle_mid', 12)), float(config.get('angle_top', 0))]

        cards = [self._make_dynamic_card(poster, card_size, radius) for poster in raw_posters]
        blurred_layers = [
            card.filter(ImageFilter.GaussianBlur(max(1, int(4 * scale_factor))))
            for card in cards
        ]
        frames = []
        frame_durations = []
        front_x = center_x
        middle_x = center_x + int(card_size * 0.11)
        rear_x = center_x + int(card_size * 0.22)
        incoming_x = center_x + int(card_size * 0.33)

        def _lerp(start, end, progress):
            return int(start + (end - start) * progress)

        # Keep three fixed stack positions. On each page change the front card
        # exits, the two following cards advance, and a new card fills the rear.
        # This reads as a deliberate replacement, rather than one card rotating.
        transition_steps = max(3, min(5, (total_frames // len(cards)) - 1))
        for page in range(len(cards)):
            for step in range(transition_steps + 1):
                transition = step / transition_steps
                eased = transition * transition * (3 - 2 * transition)
                frame = backgrounds[page].copy()
                if transition:
                    frame = Image.blend(frame, backgrounds[(page + 1) % len(backgrounds)], eased)

                outgoing = page
                front = (page + 1) % len(cards)
                middle = (page + 2) % len(cards)
                rear = (page + 3) % len(cards)

                # Draw from rear to front, then let the outgoing front card
                # leave to the left. This gives each poster a visible stage
                # position instead of making the whole stack spin in place.
                self._paste_rotated_card(
                    frame,
                    blurred_layers[rear],
                    _lerp(incoming_x, rear_x, eased),
                    center_y,
                    angles[0],
                    opacity=int(190 * eased),
                    shadow_opacity=50,
                    shadow_blur=max(1, int(3 * scale_factor)),
                )
                self._paste_rotated_card(
                    frame,
                    blurred_layers[middle],
                    _lerp(rear_x, middle_x, eased),
                    center_y,
                    angles[0] - (angles[0] - angles[1]) * eased,
                    opacity=int(210 + 25 * eased),
                    shadow_opacity=65,
                    shadow_blur=max(1, int(3 * scale_factor)),
                )
                self._paste_rotated_card(
                    frame,
                    cards[front],
                    _lerp(middle_x, front_x, eased),
                    center_y,
                    angles[1] * (1 - eased),
                    opacity=int(225 + 30 * eased),
                    shadow_opacity=90,
                    shadow_blur=max(1, int(4 * scale_factor)),
                )
                if transition < 1:
                    self._paste_rotated_card(
                        frame,
                        cards[outgoing],
                        _lerp(front_x, front_x - int(card_size * 0.3), eased),
                        center_y,
                        angles[2] - 8 * eased,
                        opacity=int(255 * (1 - eased)),
                        shadow_opacity=130,
                        shadow_blur=max(1, int(5 * scale_factor)),
                    )
                self._draw_dynamic_layout_text(frame, config, fonts, scale_factor, 'rotate')
                frames.append(self._draw_badge(frame, badge_config, assets.get('count', 0), fonts))
                frame_durations.append(page_hold_duration if step == 0 else page_transition_duration)

        frames[0].save(output, format='PNG', save_all=True, append_images=frames[1:], duration=frame_durations, loop=0, optimize=False)

    def _draw_dynamic_rotate_stack_cover(self, bg, config, assets, font_loader, output):
        target_w = _clamp_int(config.get('dynamic_output_width', 360), 360, 320, 360)
        target_h = int(target_w * 9 / 16)
        scale_factor = target_w / 1920
        # This layout needs enough temporal resolution to keep three vertical
        # tracks smooth. Legacy 54 x 333ms settings rendered at roughly 3fps.
        total_frames = MAX_ROTATE_STACK_DYNAMIC_FRAMES
        duration = 150
        poster_urls = list(assets.get('posters') or [])
        if not poster_urls:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)
        while len(poster_urls) < 9:
            poster_urls.extend(poster_urls)
        raw_posters = [image.convert('RGBA') for image in (self.download_img(url) for url in poster_urls[:9]) if image]
        if not raw_posters:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)
        while len(raw_posters) < 9:
            raw_posters.extend(raw_posters)

        background_source = self._dynamic_background_sources(assets, raw_posters)[0]
        background_blur = max(0, int(float(config.get('dynamic_bg_blur', 0)) * scale_factor))
        base = self._make_dynamic_backgrounds([background_source], (target_w, target_h), background_blur, 0)[0]
        fonts = self._dynamic_fonts(config, font_loader, scale_factor, title_default=160, subtitle_default=50)
        badge_config = self._dynamic_badge_config(config, scale_factor)
        cell_h = max(60, int(target_h * 0.48))
        cell_w = max(44, int(cell_h * 0.666))
        radius = max(3, int(float(config.get('corner_radius', 46)) * scale_factor))
        rotation = float(config.get('rotation', -16))
        columns = []
        shadow_blur = max(1, int(4 * scale_factor))
        for column_index in range(3):
            columns.append([
                self._make_rotated_card_layer(
                    self._rounded(
                        ImageOps.fit(raw_posters[column_index * 3 + row_index], (cell_w, cell_h), method=Image.LANCZOS).convert('RGBA'),
                        radius,
                    ),
                    rotation,
                    shadow_opacity=105,
                    shadow_blur=shadow_blur,
                )
                for row_index in range(3)
            ])

        frames = []
        # Keep the left title area clear and let each lane loop independently.
        # Drawing the neighbouring loop copies prevents a lane from disappearing
        # while its first card wraps from one edge to the other.
        lane_centers = [int(target_w * 0.55), int(target_w * 0.75), int(target_w * 0.95)]
        row_stride = int(cell_h * 0.92)
        loop_height = row_stride * 3
        lane_offsets = [0, -int(row_stride * 0.18), -int(row_stride * 0.36)]
        for frame_idx in range(total_frames):
            frame = base.copy()
            for column_index, cards in enumerate(columns):
                # Outer lanes move downward while the centre lane moves upward.
                direction = -1 if column_index == 1 else 1
                scroll = direction * (frame_idx / total_frames) * loop_height
                for row_index, card_layer in enumerate(cards):
                    phase = (row_index * row_stride + scroll) % loop_height
                    center_y = phase - row_stride + lane_offsets[column_index]
                    for wrap_offset in (-loop_height, 0, loop_height):
                        draw_center_y = center_y + wrap_offset
                        if draw_center_y < -card_layer.height / 2 or draw_center_y > target_h + card_layer.height / 2:
                            continue
                        frame.paste(
                            card_layer,
                            (
                                int(lane_centers[column_index] - card_layer.width / 2),
                                int(draw_center_y - card_layer.height / 2),
                            ),
                            mask=card_layer,
                        )
            self._draw_dynamic_layout_text(frame, config, fonts, scale_factor, 'rotate_stack')
            frames.append(self._draw_badge(frame, badge_config, assets.get('count', 0), fonts))

        quantized_frames = self._quantize_animation_frames(frames)
        quantized_frames[0].save(
            output,
            format='PNG',
            save_all=True,
            append_images=quantized_frames[1:],
            duration=duration,
            loop=0,
            optimize=False,
        )

    def _focus_page_state(self, frame_idx, total_frames, poster_count):
        """Return the C-position and whether this frame belongs to a page hold."""
        frames_per_page = max(4, total_frames // poster_count)
        page = (frame_idx // frames_per_page) % poster_count
        page_frame = frame_idx % frames_per_page
        hold_frames = max(2, frames_per_page // 2)
        if page_frame < hold_frames:
            return page, True

        transition_frames = frames_per_page - hold_frames
        transition = (page_frame - hold_frames + 1) / (transition_frames + 1)
        eased = transition * transition * (3 - 2 * transition)
        return page + eased, False

    def _draw_dynamic_text(self, canvas, config, fonts, scale_factor, mode='center'):
        draw = ImageDraw.Draw(canvas)
        shadow_offset = max(1, int(6 * scale_factor))
        width, height = canvas.size
        title = config.get('title', '')
        subtitle = config.get('subtitle', '')
        title_color = config.get('title_color', '#FFFFFF')
        subtitle_color = config.get('subtitle_color', '#AAAAAA')
        title_font = fonts.get('main')
        subtitle_font = fonts.get('sub') or title_font

        if mode == 'focus':
            baseline_percent = float(config.get('baseline_percent', 85)) / 100
            hero_h = int(height * (float(config.get('hero_height_percent', 65)) / 100))
            base_y = int(height * baseline_percent)
            base_text_y = max(16, (base_y - hero_h) // 2)
            title_y = base_text_y + int(float(config.get('title_y_offset', 0)) * scale_factor)
            subtitle_y = title_y + max(10, int(float(config.get('title_size', 140)) * scale_factor)) + int(float(config.get('subtitle_offset_y', 20)) * scale_factor)
        elif mode == 'fan':
            title_y = (height // 2) + int(float(config.get('title_offset_y', -280)) * scale_factor)
            subtitle_y = (height // 2) + int(float(config.get('subtitle_offset_y', -200)) * scale_factor)
        else:
            title_y = int(height * (float(config.get('text_top_percent', 75)) / 100))
            subtitle_y = title_y + int(float(config.get('text_gap', 20)) * scale_factor)

        max_width = int(width * 0.88)
        if title and title_font:
            self.draw_text_wrapper(draw, title, width // 2 + shadow_offset, title_y + shadow_offset, title_font, max_width, '#000000', line_spacing=max(2, int(10 * scale_factor)), align='center')
            self.draw_text_wrapper(draw, title, width // 2, title_y, title_font, max_width, title_color, line_spacing=max(2, int(10 * scale_factor)), align='center')
        if subtitle and subtitle_font:
            self.draw_text_wrapper(draw, subtitle, width // 2 + shadow_offset, subtitle_y + shadow_offset, subtitle_font, max_width, '#000000', line_spacing=max(2, int(10 * scale_factor)), align='center')
            self.draw_text_wrapper(draw, subtitle, width // 2, subtitle_y, subtitle_font, max_width, subtitle_color, line_spacing=max(2, int(10 * scale_factor)), align='center')

    def _dynamic_fonts(self, config, font_loader, scale_factor, title_default=140, subtitle_default=60):
        return {
            'main': font_loader(config.get('font_title'), max(8, int(float(config.get('title_size', title_default)) * scale_factor))),
            'sub': font_loader(config.get('font_subtitle'), max(8, int(float(config.get('subtitle_size', subtitle_default)) * scale_factor))),
            'count': font_loader(config.get('font_count'), max(8, int(float(config.get('count_size', 40)) * scale_factor))),
        }

    def _draw_dynamic_tiled_cover(self, bg, config, assets, font_loader, output):
        """Render the moving ChillPoster cover directly at output size.

        The old path rendered a full 1920x1080 poster for every animation frame
        and then downscaled it. This path keeps the static layers and poster
        transforms cached, so each frame only composites the moving strip.
        """
        target_w = _clamp_int(config.get('dynamic_output_width', 480), 480, 320, MAX_DYNAMIC_WIDTH)
        target_h = int(target_w * 9 / 16)
        scale_factor = target_w / 1920
        total_frames = _clamp_int(config.get('anim_frames', 72), 72, 1, MAX_DYNAMIC_FRAMES)
        duration = _clamp_int(config.get('anim_duration', 250), 250, 20, 1000)

        logger.info(
            ">>> [Engine] 动态 PNG 优化渲染: %sx%s, %s frames, %sms",
            target_w,
            target_h,
            total_frames,
            duration,
        )

        blur_radius = int(float(config.get('dynamic_bg_blur', 0)) * scale_factor)
        base = self._make_dynamic_backgrounds([bg], (target_w, target_h), blur_radius, 0)[0]
        base = ImageEnhance.Brightness(base).enhance(max(0.9, float(config.get('brightness', 1.0))))

        mask_opacity = 0
        if mask_opacity > 0:
            mask = self.create_smart_mask(target_w, target_h, mask_opacity, 100, 'horizontal')
            black_layer = Image.new('RGBA', (target_w, target_h), (0, 0, 0, 255))
            black_layer.putalpha(mask)
            base = Image.alpha_composite(base, black_layer)

        font_title_size = max(8, int(float(config.get('title_size', 80)) * scale_factor))
        font_sub_size = max(8, int(float(config.get('subtitle_size', 40)) * scale_factor))
        font_count_size = max(8, int(float(config.get('count_size', 40)) * scale_factor))
        fonts = {
            'main': font_loader(config.get('font_title'), font_title_size),
            'sub': font_loader(config.get('font_subtitle'), font_sub_size),
            'count': font_loader(config.get('font_count'), font_count_size),
        }

        poster_urls = list(assets.get('posters') or [])
        count = int(float(config.get('poster_count') or len(poster_urls) or 1))
        if poster_urls:
            while len(poster_urls) < count:
                poster_urls.extend(poster_urls)
            poster_urls = poster_urls[:count]

        poster_layers = []
        if poster_urls:
            poster_scale = float(config.get('poster_scale', 0.55))
            target_h_poster = max(1, int(550 * poster_scale * scale_factor))
            target_w_poster = max(1, int(366 * poster_scale * scale_factor))
            radius = max(0, int(float(config.get('poster_corner_radius', 12)) * scale_factor))
            shadow_margin = max(2, int(10 * scale_factor))
            shadow_blur = max(1, int(10 * scale_factor))
            p_shadow = int(config.get('poster_shadow_opacity', 140))
            p_bright = float(config.get('poster_brightness', 1.0))

            shadow = Image.new(
                "RGBA",
                (target_w_poster + shadow_margin * 2, target_h_poster + shadow_margin * 2),
                (0, 0, 0, 0),
            )
            sd = ImageDraw.Draw(shadow)
            sd.rounded_rectangle(
                [(shadow_margin, shadow_margin), (target_w_poster + shadow_margin, target_h_poster + shadow_margin)],
                radius=radius,
                fill=(0, 0, 0, p_shadow),
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))

            for url in poster_urls:
                p_img = self.download_img(url)
                if not p_img:
                    continue
                p_img = ImageOps.fit(p_img, (target_w_poster, target_h_poster), method=Image.LANCZOS)
                if p_bright != 1.0:
                    p_img = ImageEnhance.Brightness(p_img).enhance(p_bright)
                poster_layers.append((p_img, shadow))

        # The dynamic layout module owns add_rounded_corners, so keep a tiny
        # local fallback here for the optimized path.
        def _rounded(im, radius):
            if radius <= 0:
                return im
            circle = Image.new('L', (radius * 2, radius * 2), 0)
            draw = ImageDraw.Draw(circle)
            draw.ellipse((0, 0, radius * 2, radius * 2), fill=255)
            alpha = Image.new('L', im.size, 255)
            w, h = im.size
            alpha.paste(circle.crop((0, 0, radius, radius)), (0, 0))
            alpha.paste(circle.crop((0, radius, radius, radius * 2)), (0, h - radius))
            alpha.paste(circle.crop((radius, 0, radius * 2, radius)), (w - radius, 0))
            alpha.paste(circle.crop((radius, radius, radius * 2, radius * 2)), (w - radius, h - radius))
            im = im.convert('RGBA')
            im.putalpha(alpha)
            return im

        if poster_layers:
            poster_layers = [(_rounded(p, max(0, int(float(config.get('poster_corner_radius', 12)) * scale_factor))), s) for p, s in poster_layers]

        frames = []
        spacing = max(0, int(float(config.get('poster_spacing', 20)) * scale_factor))
        layout_start_x = int(float(config.get('layout_start_x', 0)) * scale_factor)
        direction = int(float(config.get('anim_direction', 1)))

        if poster_layers:
            poster_w, poster_h = poster_layers[0][0].size
            unit_width = poster_w + spacing
            loop_width = max(1, len(poster_layers) * unit_width)
            pos_y = int(target_h * (float(config.get('poster_y_percent', 45)) / 100)) - (poster_h // 2)
        else:
            loop_width = 1
            pos_y = 0

        badge_config = self._dynamic_badge_config(config, scale_factor)

        def _draw_text(canvas):
            draw = ImageDraw.Draw(canvas)
            shadow_offset = max(1, int(6 * scale_factor))
            cx = int(target_w * (float(config.get('text_left_percent', 5)) / 100))
            cy = int(target_h * (float(config.get('text_top_percent', 75)) / 100))
            max_width = int(target_w * 0.9)
            gap = int(float(config.get('text_gap', 20)) * scale_factor)
            title = config.get('title', '')
            subtitle = config.get('subtitle', '')
            if title:
                self.draw_text_wrapper(draw, title, cx + shadow_offset, cy + shadow_offset, fonts['main'], max_width, '#000000', line_spacing=max(2, int(10 * scale_factor)))
                cy = self.draw_text_wrapper(draw, title, cx, cy, fonts['main'], max_width, config.get('title_color', '#FFFFFF'), line_spacing=max(2, int(10 * scale_factor)))
                cy += gap
            if subtitle:
                self.draw_text_wrapper(draw, subtitle, cx + shadow_offset, cy + shadow_offset, fonts['sub'], max_width, '#000000', line_spacing=max(2, int(10 * scale_factor)))
                self.draw_text_wrapper(draw, subtitle, cx, cy, fonts['sub'], max_width, config.get('subtitle_color', '#DDDDDD'), line_spacing=max(2, int(10 * scale_factor)))

        for idx in range(total_frames):
            step = idx / total_frames if total_frames > 1 else 0
            frame = base.copy()
            if poster_layers:
                anim_offset = step * loop_width * direction
                for i, (poster, shadow) in enumerate(poster_layers):
                    x = layout_start_x + ((i * unit_width + anim_offset) % loop_width)
                    for draw_x in (x, x - loop_width, x + loop_width):
                        if draw_x > -poster.size[0] - 20 and draw_x < target_w + 20:
                            frame.paste(shadow, (int(draw_x) - shadow_margin + 2, int(pos_y) - shadow_margin + 2), mask=shadow)
                            frame.paste(poster, (int(draw_x), int(pos_y)), mask=poster)

            _draw_text(frame)
            frame = self._draw_badge(frame, badge_config, assets.get('count', 0), fonts)
            frames.append(frame)

        frames[0].save(
            output,
            format='PNG',
            save_all=True,
            append_images=frames[1:],
            duration=duration,
            loop=0,
            optimize=False,
        )
        logger.info(">>> [Engine] 动态 PNG 完成. 帧数: %s, 大小: %.2f MB", len(frames), output.getbuffer().nbytes / 1024 / 1024)

    def _draw_dynamic_focus_cover(self, bg, config, assets, font_loader, output):
        target_w = _clamp_int(config.get('dynamic_output_width', 480), 480, 320, MAX_DYNAMIC_WIDTH)
        target_h = int(target_w * 9 / 16)
        scale_factor = target_w / 1920
        total_frames = _clamp_int(config.get('anim_frames', 36), 36, 1, MAX_FOCUS_DYNAMIC_FRAMES)
        page_hold_duration = _clamp_int(config.get('page_hold_duration', 1950), 1950, 300, 3000)
        page_transition_duration = _clamp_int(config.get('page_transition_duration', 150), 150, 60, 500)
        logger.info(
            ">>> [Engine] 聚焦C佬动态渲染: %sx%s, %s frames, 页面停留 %sms, 切换 %sms",
            target_w,
            target_h,
            total_frames,
            page_hold_duration,
            page_transition_duration,
        )

        poster_urls = list(assets.get('posters') or [])
        if not poster_urls:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)
        count = int(float(config.get('poster_count') or len(poster_urls) or 7))
        if count % 2 == 0:
            count += 1
        count = max(3, min(count, len(poster_urls) if len(poster_urls) >= 3 else count))
        while len(poster_urls) < count:
            poster_urls.extend(poster_urls)
        poster_urls = poster_urls[:count]

        fonts = self._dynamic_fonts(config, font_loader, scale_factor, title_default=140, subtitle_default=60)
        hero_h = int(target_h * (float(config.get('hero_height_percent', 65)) / 100))
        hero_w = int(hero_h * float(config.get('poster_ratio', 0.66)))
        base_y = int(target_h * (float(config.get('baseline_percent', 85)) / 100))
        scale_step = float(config.get('side_scale_step', 0.85))
        overlap_pct = float(config.get('overlap_percent', 40)) / 100
        darken_step = min(18, int(config.get('perspective_darken', 18)))
        corner_radius = max(0, int(float(config.get('corner_radius', 16)) * scale_factor))
        reflection_opacity = int(config.get('reflection_opacity', 50))
        hero_shadow_opacity = int(config.get('hero_shadow_opacity', 180))
        travel = hero_w * (1 - overlap_pct) * 0.95
        center_x = target_w // 2
        badge_config = self._dynamic_badge_config(config, scale_factor)

        raw_posters = []
        for url in poster_urls:
            img = self.download_img(url)
            if img:
                raw_posters.append(img.convert('RGBA'))
        if not raw_posters:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)

        bg_blur = int(float(config.get('dynamic_bg_blur', 0)) * scale_factor)
        overlay_opacity = 0
        backgrounds = self._make_dynamic_backgrounds(
            self._dynamic_background_sources(assets, raw_posters),
            (target_w, target_h),
            bg_blur,
            overlay_opacity,
        )

        vignette = 0
        spotlight = 0
        if vignette or spotlight:
            light = Image.new('L', (target_w, target_h), 0)
            draw_light = ImageDraw.Draw(light)
            radius = int(min(target_w, target_h) * 0.9)
            cx, cy = target_w // 2, target_h // 2
            draw_light.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=255)
            mask = light.filter(ImageFilter.GaussianBlur(max(1, radius // 2)))
            edge = Image.new('RGBA', (target_w, target_h), (0, 0, 0, vignette))
            center = Image.new('RGBA', (target_w, target_h), (255, 255, 255, spotlight))
            lighting = Image.composite(center, edge, mask)
            backgrounds = [Image.alpha_composite(background, lighting) for background in backgrounds]

        render_plan = []
        for frame_idx in range(total_frames):
            active, is_page_hold = self._focus_page_state(frame_idx, total_frames, len(raw_posters))
            frame_duration = page_hold_duration if is_page_hold else page_transition_duration
            if is_page_hold and render_plan and render_plan[-1][1] and render_plan[-1][2] == active:
                previous = render_plan[-1]
                render_plan[-1] = (previous[0], True, active, previous[3] + frame_duration)
            else:
                render_plan.append((frame_idx, is_page_hold, active, frame_duration))

        frames = []
        frame_durations = []
        try:
            for frame_idx, is_page_hold, active, frame_duration in render_plan:
                page = int(active) % len(backgrounds)
                transition = active - int(active)
                frame = backgrounds[page].copy()
                if transition:
                    frame = Image.blend(frame, backgrounds[(page + 1) % len(backgrounds)], transition)
                queue = []
                for i, raw in enumerate(raw_posters):
                    rel = self._relative_loop_position(i, active, len(raw_posters))
                    abs_rel = abs(rel)
                    if abs_rel > (count // 2 + 0.75):
                        continue
                    current_scale = math.pow(scale_step, abs_rel)
                    w = max(1, int(hero_w * current_scale))
                    h = max(1, int(hero_h * current_scale))
                    x = center_x + rel * travel
                    y = base_y
                    brightness = max(0.25, 1.0 - (abs_rel * darken_step / 255.0))
                    queue.append((abs_rel, raw, w, h, x, y, brightness))

                queue.sort(key=lambda item: item[0], reverse=True)
                for abs_rel, raw, w, h, x, y, brightness in queue:
                    poster = ImageOps.fit(raw, (w, h), method=Image.LANCZOS)
                    if brightness < 1.0:
                        poster = ImageEnhance.Brightness(poster).enhance(brightness)
                    poster = self._rounded(poster, corner_radius)
                    paste_x = int(x - w / 2)
                    paste_y = int(y - h)

                    if reflection_opacity > 0:
                        reflection = self._reflection(poster, opacity=max(5, int(reflection_opacity * max(0.15, 1 - abs_rel / 5))))
                        frame.paste(reflection, (paste_x, int(y)), mask=reflection)

                    if abs_rel < 0.65 and hero_shadow_opacity > 0:
                        shadow_pad = max(4, int(40 * scale_factor))
                        shadow = Image.new('RGBA', (w + shadow_pad * 2, h + shadow_pad * 2), (0, 0, 0, 0))
                        sd = ImageDraw.Draw(shadow)
                        sd.rounded_rectangle(
                            (shadow_pad, shadow_pad, shadow_pad + w, shadow_pad + h),
                            radius=corner_radius,
                            fill=(0, 0, 0, hero_shadow_opacity),
                        )
                        shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, int(20 * scale_factor))))
                        frame.paste(shadow, (paste_x - shadow_pad, paste_y - shadow_pad + int(10 * scale_factor)), mask=shadow)

                    frame.paste(poster, (paste_x, paste_y), mask=poster)

                self._draw_dynamic_text(frame, config, fonts, scale_factor, mode='focus')
                frame = self._draw_badge(frame, badge_config, assets.get('count', 0), fonts)
                frames.append(frame)
                frame_durations.append(frame_duration)
        except Exception:
            logger.exception(">>> [Engine] 聚焦C佬动态在第 %s/%s 帧渲染失败", frame_idx + 1, total_frames)
            raise

        frames[0].save(
            output,
            format='PNG',
            save_all=True,
            append_images=frames[1:],
            duration=frame_durations,
            loop=0,
            optimize=False,
        )
        logger.info(">>> [Engine] 聚焦C佬动态 PNG 完成. 帧数: %s, 大小: %.2f MB", len(frames), output.getbuffer().nbytes / 1024 / 1024)

    def _draw_dynamic_fan_cover(self, bg, config, assets, font_loader, output):
        target_w = _clamp_int(config.get('dynamic_output_width', 480), 480, 320, MAX_DYNAMIC_WIDTH)
        target_h = int(target_w * 9 / 16)
        scale_factor = target_w / 1920
        page_hold_duration = _clamp_int(config.get('page_hold_duration', 3100), 3100, 1000, 6000)
        page_transition_duration = _clamp_int(config.get('page_transition_duration', 100), 100, 50, 250)
        transition_steps = 10

        poster_urls = list(assets.get('posters') or [])
        if not poster_urls:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)
        count = int(float(config.get('poster_count') or len(poster_urls) or 5))
        if count % 2 == 0:
            count += 1
        count = max(3, min(count, len(poster_urls) if len(poster_urls) >= 3 else count))
        while len(poster_urls) < count:
            poster_urls.extend(poster_urls)
        poster_urls = poster_urls[:count]

        raw_posters = []
        for url in poster_urls:
            img = self.download_img(url)
            if img:
                raw_posters.append(img.convert('RGBA'))
        if not raw_posters:
            return self._draw_dynamic_tiled_cover(bg, config, assets, font_loader, output)

        bg_blur = int(float(config.get('dynamic_bg_blur', 0)) * scale_factor)
        darkness = 0
        backgrounds = self._make_dynamic_backgrounds(
            self._dynamic_background_sources(assets, raw_posters),
            (target_w, target_h),
            max(0, bg_blur),
            darkness,
        )

        fonts = self._dynamic_fonts(config, font_loader, scale_factor, title_default=90, subtitle_default=50)
        fan_radius = float(config.get('fan_radius', 1200)) * scale_factor
        fan_spread = float(config.get('fan_spread', 40))
        center_scale = float(config.get('center_scale', 0.55))
        shrink_ratio = float(config.get('side_scale_shrink', 0.95))
        y_offset = float(config.get('layout_y_offset', 100)) * scale_factor
        base_h = int(target_h * center_scale)
        base_w = int(base_h * 0.666)
        pivot_x = target_w // 2
        pivot_y = (target_h // 2) + fan_radius - (base_h // 2) + y_offset
        angle_step = fan_spread / max(1, count - 1)
        badge_config = self._dynamic_badge_config(config, scale_factor)

        half_count = count // 2
        padding = max(4, int(30 * scale_factor))
        corner_radius = max(2, int(15 * scale_factor))
        slot_layers = []
        for relative in range(-half_count, half_count + 1):
            abs_relative = abs(relative)
            theta_deg = relative * angle_step
            theta = math.radians(theta_deg)
            x = pivot_x + fan_radius * math.sin(theta)
            y = pivot_y - fan_radius * math.cos(theta)
            scale = math.pow(shrink_ratio, abs_relative)
            width = max(1, int(base_w * scale))
            height = max(1, int(base_h * scale))
            layers = []
            for raw in raw_posters:
                poster = self._rounded(ImageOps.fit(raw, (width, height), method=Image.LANCZOS), corner_radius)
                group = Image.new('RGBA', (width + padding * 2, height * 2 + padding), (0, 0, 0, 0))
                shadow = Image.new('RGBA', (width + padding * 2, height + padding * 2), (0, 0, 0, 0))
                shadow_draw = ImageDraw.Draw(shadow)
                shadow_draw.rounded_rectangle(
                    (padding, padding, padding + width, padding + height),
                    radius=corner_radius,
                    fill=(0, 0, 0, 130),
                )
                shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, int(12 * scale_factor))))
                group.paste(shadow, (0, 0), mask=shadow)
                reflection = self._reflection(poster, opacity=35)
                group.paste(reflection, (padding, padding + height), mask=reflection)
                group.paste(poster, (padding, padding), mask=poster)
                layers.append(group.rotate(-theta_deg, resample=Image.BICUBIC, expand=True))
            slot_layers.append((relative, abs_relative, x, y + height / 2, layers))

        frames = []
        frame_durations = []
        for page in range(len(raw_posters)):
            for step in range(transition_steps + 1):
                transition = 0 if step == 0 else step / (transition_steps + 1)
                center_start = 0.15
                center_progress = max(0.0, min(1.0, (transition - center_start) / 0.7))
                center_eased = center_progress * center_progress * (3 - 2 * center_progress)
                frame = backgrounds[page].copy()
                if transition:
                    frame = Image.blend(frame, backgrounds[(page + 1) % len(backgrounds)], center_eased)

                for relative, abs_relative, x, y, layers in sorted(slot_layers, key=lambda item: item[1], reverse=True):
                    current = layers[(page + relative) % len(raw_posters)]
                    if transition:
                        upcoming = layers[(page + relative + 1) % len(raw_posters)]
                        slot_rank = relative + half_count
                        slot_start = (slot_rank / max(1, count - 1)) * 0.3
                        slot_progress = max(0.0, min(1.0, (transition - slot_start) / 0.7))
                        slot_eased = slot_progress * slot_progress * (3 - 2 * slot_progress)
                        card = Image.blend(current, upcoming, slot_eased)
                    else:
                        card = current
                    frame.paste(card, (int(x - card.width / 2), int(y - card.height / 2)), mask=card)

                self._draw_dynamic_text(frame, config, fonts, scale_factor, mode='fan')
                frame = self._draw_badge(frame, badge_config, assets.get('count', 0), fonts)
                frames.append(frame)
                frame_durations.append(page_hold_duration if step == 0 else page_transition_duration)

        quantized_frames = self._quantize_animation_frames(frames)
        quantized_frames[0].save(
            output,
            format='PNG',
            save_all=True,
            append_images=quantized_frames[1:],
            duration=frame_durations,
            loop=0,
            optimize=False,
        )
        logger.info(">>> [Engine] 扇形展开动态 PNG 完成. 帧数: %s, 大小: %.2f MB", len(frames), output.getbuffer().nbytes / 1024 / 1024)

    # === 动态 PNG 生成 ===
    def draw(self, config, assets):
        bg = None
        if assets.get('bg_url'):
            bg = self.download_img(assets['bg_url'])
        
        if not bg: bg = Image.new("RGBA", (1920, 1080), (20, 30, 50, 255))
        is_dynamic = config.get('enable_animation', False)
        if not is_dynamic:
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
        
        output = BytesIO()

        if is_dynamic:
            if engine_type == '聚焦C佬':
                self._draw_dynamic_focus_cover(bg, config, assets, _load_font, output)
            elif engine_type == '扇形展开':
                self._draw_dynamic_fan_cover(bg, config, assets, _load_font, output)
            elif engine_type == '经典堆叠':
                self._draw_dynamic_classic_stack_cover(bg, config, assets, _load_font, output)
            elif engine_type == '旋转':
                self._draw_dynamic_rotate_cover(bg, config, assets, _load_font, output)
            elif engine_type == '旋转堆叠':
                self._draw_dynamic_rotate_stack_cover(bg, config, assets, _load_font, output)
            else:
                self._draw_dynamic_tiled_cover(bg, config, assets, _load_font, output)
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
