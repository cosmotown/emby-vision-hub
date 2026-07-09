import argparse
import base64
import importlib.util
import json
import logging
from pathlib import Path

ENGINE_PATH = Path(__file__).with_name("engine.py")


def _load_engine_class():
    spec = importlib.util.spec_from_file_location("chillposter_engine", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PosterEngine


def main():
    parser = argparse.ArgumentParser(description="Render a ChillPoster cover outside the web process.")
    parser.add_argument("--payload", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    payload_path = Path(args.payload)
    output_path = Path(args.output)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    engine_class = _load_engine_class()
    engine = engine_class(
        fonts_dir=payload["fonts_dir"],
        layouts_dir=payload["layouts_dir"],
    )
    image_b64 = engine.draw(payload["config"], payload["assets"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(image_b64))


if __name__ == "__main__":
    main()
