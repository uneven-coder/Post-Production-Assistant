import copy
import os
import re
import json
from datetime import datetime as _datetime
from typing import Optional, Dict, Any

from dotenv import load_dotenv

load_dotenv()


def _expand_template(template: str, project_name: str,
                     date_fmt: str = "%Y-%m-%d", time_fmt: str = "%H%M%S") -> str:
    """Expand {project_name}, {date}, and {time} placeholders in a path template."""
    safe_name = re.sub(r"[^\w\s-]", "", project_name).strip().replace(" ", "_")
    now = _datetime.now()
    return (template
            .replace("{project_name}", safe_name)
            .replace("{date}", now.strftime(date_fmt))
            .replace("{time}", now.strftime(time_fmt)))


def path_field(path: str, regex: Optional[str] = None) -> Dict[str, Any]:
    resolved = path if os.path.isabs(path) else os.path.abspath(path)
    result = {"path": path, "file": "", "exists": False}

    if regex:
        dir_path = resolved if os.path.isdir(resolved) else os.path.dirname(resolved)
        matched = None
        if os.path.isdir(dir_path):
            pattern = re.compile(regex)
            for fname in os.listdir(dir_path):
                if fname.endswith("_extracted_audio.wav"):
                    continue
                if pattern.search(fname):
                    matched = os.path.join(dir_path, fname)
                    break
        if matched:
            result["file"] = matched
            result["exists"] = os.path.exists(matched)
        else:
            result["file"] = os.path.join(dir_path, f"[regex:{regex}]")
            result["exists"] = False
    else:
        result["file"] = resolved
        result["exists"] = os.path.exists(resolved)

    return result


def apply_profile(config: dict, profile_name: str) -> dict:
    cfg = copy.deepcopy(config)
    project = cfg.setdefault("project", {})
    profile = project.pop(f"{profile_name}_profile", None)
    if profile:
        project.update(profile)
    return cfg


def resolve_paths(config: dict) -> dict:
    project = config.get("project", {})

    for video in project.get("videos", []):
        pc = video.get("path", {})
        video["path"] = path_field(pc.get("path"), pc.get("regex"))
        border = video.get("overlay", {}).get("border", {})
        np_path = border.get("nine_patch_path")
        if np_path:
            border["nine_patch_path"] = path_field(np_path.get("path"))

    output_raw = project.get("output_directory", "./output/")
    project_name = project.get("name", "project")
    opts = project.get("output_options") or {}
    date_fmt = opts.get("date_format", "%Y-%m-%d")
    time_fmt = opts.get("time_format", "%H%M%S")
    output_expanded = _expand_template(output_raw, project_name, date_fmt, time_fmt)
    resolved_out = path_field(output_expanded)
    project["output_directory"] = resolved_out

    config["env"] = dict(os.environ)
    return config


def get_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return resolve_paths(config)
