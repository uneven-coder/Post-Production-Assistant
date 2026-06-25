import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import get_config


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = get_config()
        cls.project = cls.config.get("project", {})

    def test_config_loaded(self):
        self.assertIsNotNone(self.config)

    def test_project_section_exists(self):
        self.assertIsNotNone(self.project)

    def test_videos_defined(self):
        videos = self.project.get("videos", [])
        self.assertTrue(videos, "No videos defined in project configuration")

    def test_nine_patch_files_exist(self):
        videos = self.project.get("videos", [])
        missing = []
        for idx, video in enumerate(videos):
            border = video.get("overlay", {}).get("border", {})
            nine_patch = border.get("nine_patch_path")
            path = nine_patch.get("file") if isinstance(nine_patch, dict) else nine_patch
            if border.get("enabled") and path and not os.path.isfile(path):
                missing.append(f"video[{idx}]: {path}")
        self.assertFalse(missing, "Missing 9-patch files:\n" + "\n".join(missing))

    def test_ffmpeg_on_path(self):
        from shutil import which
        self.assertIsNotNone(which("ffmpeg"), "ffmpeg not found in PATH")

    def test_openai_key_set(self):
        from dotenv import load_dotenv
        load_dotenv()
        models = self.project.get("models", {})
        missing = []
        for key, cfg in models.items():
            env_var = cfg.get("api_key_env")
            if env_var and not os.environ.get(env_var):
                missing.append(f"{key}: ${env_var}")
        self.assertFalse(missing, "Missing API key env vars:\n" + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
