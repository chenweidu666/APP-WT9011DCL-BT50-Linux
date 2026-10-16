"""用户配置：记住上次连接的设备地址等（~/.config/wt9011/config.json）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_CFG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "wt9011"
_CFG_FILE = _CFG_DIR / "config.json"

DEFAULTS = {
    "last_address": "",
    "auto_connect": True,
    "auto_reconnect": True,
    "record_dir": str(Path.home()),
}


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(_CFG_FILE) as fh:
            data.update(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return data


def save(cfg: dict) -> None:
    try:
        _CFG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CFG_FILE, "w") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def update(**kwargs) -> dict:
    cfg = load()
    cfg.update(kwargs)
    save(cfg)
    return cfg


def last_address() -> Optional[str]:
    addr = load().get("last_address") or ""
    return addr or None


def path() -> Path:
    return _CFG_FILE
