from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import platform
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

APP_NAME = "ClashRoyale"
APP_VERSION = "1.0.0"
REQUIRED_PYTHON = (3, 12, 10)
DEPENDENCIES = (
    "Pillow>=10.2,<13",
    "numpy>=1.26,<3",
    "opencv-python-headless>=4.9,<5",
)
FEATURE_COUNT = 10
ACTION_COUNT = 25
WAIT_ACTION = 24


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def desktop_path() -> Path:
    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            return Path(os.path.expandvars(value)).resolve()
        except Exception:
            pass
    candidate = Path.home() / "Desktop"
    return candidate if candidate.exists() else Path.home()


@dataclass(frozen=True)
class AppPaths:
    root: Path
    launcher: Path
    config: Path
    model: Path
    experiences: Path
    checksums: Path
    vendor: Path
    logs: Path
    temp: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        root = desktop_path() / APP_NAME
        return cls(
            root=root,
            launcher=root / "ClashRoyale.py",
            config=root / "config.json",
            model=root / "model" / "policy.json",
            experiences=root / "data" / "experiences.jsonl",
            checksums=root / "checksums.json",
            vendor=root / "vendor",
            logs=root / "logs",
            temp=root / "temp",
        )

    def ensure_dirs(self) -> None:
        for path in (self.root, self.model.parent, self.experiences.parent, self.vendor, self.logs, self.temp):
            path.mkdir(parents=True, exist_ok=True)


PATHS = AppPaths.discover()


def relocate_to_desktop() -> None:
    if os.name != "nt":
        return
    try:
        PATHS.ensure_dirs()
        current = Path(__file__).resolve()
        target = PATHS.launcher.resolve()
        if current == target:
            return
        shutil.copy2(current, target)
        subprocess.Popen([sys.executable, str(target)], cwd=str(PATHS.root), close_fds=True)
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:
        return


def add_vendor_path() -> None:
    vendor = str(PATHS.vendor)
    if vendor not in sys.path:
        sys.path.insert(0, vendor)


DEFAULT_CONFIG: dict[str, Any] = {
    "schema": 1,
    "app_version": APP_VERSION,
    "update_manifest_url": "",
    "window_title_keywords": ["Clash Royale", "皇室战争", "ClashRoyale"],
    "launcher_name_keywords": [
        "Clash Royale",
        "皇室战争",
        "Google Play Games",
        "BlueStacks",
        "LDPlayer",
        "MuMu",
    ],
    "card_costs": [3, 4, 4, 5],
    "decision_interval_seconds": 1.25,
    "exploration": 0.04,
    "auto_launch_game": True,
    "auto_queue_battle": False,
    "training_epochs": 5,
    "training_learning_rate": 0.012,
    "max_experiences": 50000,
}


class AppLog:
    def __init__(self, paths: AppPaths) -> None:
        paths.logs.mkdir(parents=True, exist_ok=True)
        self.path = paths.logs / "ClashRoyale.log"
        self._lock = threading.Lock()

    def write(self, level: str, message: str) -> None:
        line = f"{utc_now()} [{level}] {message}\n"
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass

    def exception(self, message: str) -> None:
        self.write("ERROR", message + "\n" + traceback.format_exc())


LOG = AppLog(PATHS)


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return dict(DEFAULT_CONFIG)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("config must be a JSON object")
        except Exception:
            return dict(DEFAULT_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        merged.update(value)
        return merged

    def repair(self) -> tuple[dict[str, Any], bool]:
        current = self.load()
        changed = False
        for key, value in DEFAULT_CONFIG.items():
            if key not in current or not isinstance(current[key], type(value)):
                current[key] = value
                changed = True
        if current.get("schema") != DEFAULT_CONFIG["schema"]:
            current["schema"] = DEFAULT_CONFIG["schema"]
            changed = True
        if not self.path.exists() or changed:
            atomic_write_json(self.path, current)
            return current, True
        return current, False


class ModelStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    @staticmethod
    def default_model() -> dict[str, Any]:
        rng = random.Random(20260802)
        weights = [[rng.uniform(-0.03, 0.03) for _ in range(FEATURE_COUNT)] for _ in range(ACTION_COUNT)]
        for card in range(4):
            base = card * 6
            weights[base + 2][2] += 0.8
            weights[base + 3][3] += 0.8
            weights[base + 4][1] += 0.20
            weights[base + 5][1] += 0.20
            weights[base + 4][8] += 0.15
            weights[base + 5][8] += 0.15
        weights[WAIT_ACTION][1] -= 0.65
        weights[WAIT_ACTION][2] -= 0.35
        weights[WAIT_ACTION][3] -= 0.35
        return {
            "schema": 1,
            "app_version": APP_VERSION,
            "generation": 0,
            "created_utc": utc_now(),
            "updated_utc": utc_now(),
            "feature_count": FEATURE_COUNT,
            "action_count": ACTION_COUNT,
            "weights": weights,
            "training_samples": 0,
            "benchmark": {
                "status": "unverified",
                "note": "No validated professional-level benchmark is bundled.",
            },
        }

    @staticmethod
    def valid(model: Any) -> bool:
        try:
            if not isinstance(model, dict):
                return False
            if model.get("feature_count") != FEATURE_COUNT or model.get("action_count") != ACTION_COUNT:
                return False
            weights = model["weights"]
            if len(weights) != ACTION_COUNT:
                return False
            for row in weights:
                if len(row) != FEATURE_COUNT:
                    return False
                if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in row):
                    return False
            return True
        except Exception:
            return False

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if self.valid(value):
                    return value
            except Exception:
                pass
            value = self.default_model()
            atomic_write_json(self.path, value)
            return value

    def repair(self) -> bool:
        existed = self.path.exists()
        try:
            if existed and self.valid(json.loads(self.path.read_text(encoding="utf-8"))):
                return False
        except Exception:
            pass
        atomic_write_json(self.path, self.default_model())
        return True

    def save(self, model: dict[str, Any]) -> None:
        if not self.valid(model):
            raise ValueError("refusing to save an invalid model")
        with self._lock:
            atomic_write_json(self.path, model)


class ExperiencePool:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def ensure(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
            return True
        good_lines: list[str] = []
        damaged = False
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        item = json.loads(stripped)
                        if self.valid_item(item):
                            good_lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                        else:
                            damaged = True
                    except Exception:
                        damaged = True
        except Exception:
            damaged = True
        if damaged:
            atomic_write_text(self.path, "".join(line + "\n" for line in good_lines))
        return damaged

    @staticmethod
    def valid_item(item: Any) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("features"), list)
            and len(item["features"]) == FEATURE_COUNT
            and isinstance(item.get("action"), int)
            and 0 <= item["action"] < ACTION_COUNT
            and isinstance(item.get("reward"), (int, float))
        )

    def append(self, features: list[float], action: int, reward: float, meta: Optional[dict[str, Any]] = None) -> None:
        item = {
            "ts": utc_now(),
            "features": [round(float(v), 6) for v in features],
            "action": int(action),
            "reward": round(float(max(-2.0, min(2.0, reward))), 6),
            "meta": meta or {},
        }
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def load(self, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result: list[dict[str, Any]] = []
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if len(result) >= limit:
                            break
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        if self.valid_item(item):
                            result.append(item)
            except OSError:
                return []
        return result

    def compact(self, max_items: int) -> None:
        items = self.load(max_items * 2)
        if len(items) <= max_items:
            return
        kept = items[-max_items:]
        atomic_write_text(
            self.path,
            "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in kept),
        )


class DependencyManager:
    MODULES = ("PIL", "numpy", "cv2")

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def available(self) -> bool:
        add_vendor_path()
        try:
            for name in self.MODULES:
                __import__(name)
            return True
        except Exception:
            return False

    def install(self, progress: Callable[[str], None], stop: threading.Event) -> None:
        if self.available():
            progress("本地依赖完整")
            return
        if stop.is_set():
            return
        progress("下载并安装图像与数值计算依赖…")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            progress("初始化 pip…")
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--upgrade"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--no-cache-dir",
            "--upgrade",
            "--target",
            str(self.paths.vendor),
            *DEPENDENCIES,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        install_env = os.environ.copy()
        install_env["PIP_CACHE_DIR"] = str(self.paths.temp / "pip-cache")
        install_env["TMP"] = str(self.paths.temp)
        install_env["TEMP"] = str(self.paths.temp)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            env=install_env,
        )
        last_line = ""
        assert process.stdout is not None
        while process.poll() is None:
            if stop.is_set():
                process.terminate()
                raise RuntimeError("依赖安装已取消")
            line = process.stdout.readline().strip()
            if line and line != last_line:
                last_line = line
                if "Downloading" in line or "Installing" in line or "Successfully" in line:
                    progress(line[:110])
        output = process.stdout.read()
        if process.returncode != 0:
            raise RuntimeError("依赖下载失败：" + (output[-600:] if output else "pip exited with error"))
        add_vendor_path()
        if not self.available():
            raise RuntimeError("依赖已下载，但无法导入；请重新进入“文件”模式")
        progress("依赖安装完成")


class RemoteUpdater:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def apply(self, manifest_url: str, progress: Callable[[str], None], stop: threading.Event) -> int:
        if not manifest_url:
            return 0
        if not manifest_url.lower().startswith("https://"):
            raise ValueError("update_manifest_url 必须使用 HTTPS")
        progress("检查远程更新清单…")
        request = urllib.request.Request(manifest_url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            manifest = json.loads(response.read().decode("utf-8"))
        files = manifest.get("files", [])
        if not isinstance(files, list):
            raise ValueError("更新清单格式错误")
        changed = 0
        for index, item in enumerate(files, 1):
            if stop.is_set():
                break
            relative = str(item.get("path", "")).replace("\\", "/")
            expected = str(item.get("sha256", "")).lower()
            url = str(item.get("url", ""))
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                raise ValueError(f"拒绝不安全的更新路径：{relative}")
            if len(expected) != 64 or not url.lower().startswith("https://"):
                raise ValueError(f"更新项无效：{relative}")
            target = (self.paths.root / relative).resolve()
            if self.paths.root.resolve() not in target.parents:
                raise ValueError(f"更新目标越界：{relative}")
            if target.exists() and sha256_file(target) == expected:
                continue
            progress(f"下载更新 {index}/{len(files)}：{relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".download", dir=str(target.parent))
            os.close(fd)
            try:
                with urllib.request.urlopen(url, timeout=45) as response, open(temp_name, "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        if stop.is_set():
                            raise RuntimeError("更新已取消")
                        handle.write(chunk)
                if sha256_file(Path(temp_name)) != expected:
                    raise RuntimeError(f"更新文件校验失败：{relative}")
                os.replace(temp_name, target)
                changed += 1
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        return changed


class IntegrityService:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.config_store = ConfigStore(paths.config)
        self.model_store = ModelStore(paths.model)
        self.pool = ExperiencePool(paths.experiences)
        self.dependencies = DependencyManager(paths)
        self.remote = RemoteUpdater(paths)

    @staticmethod
    def platform_errors() -> list[str]:
        errors: list[str] = []
        if os.name != "nt":
            errors.append("仅支持 Windows 11 x64")
        else:
            try:
                build = sys.getwindowsversion().build
                if build < 22000:
                    errors.append("需要 Windows 11（系统版本号至少 22000）")
            except Exception:
                errors.append("无法确认 Windows 11 版本")
            machine = platform.machine().lower()
            if machine not in {"amd64", "x86_64"}:
                errors.append("需要 x64 系统")
            if ctypes.sizeof(ctypes.c_void_p) != 8:
                errors.append("需要 64 位 Python")
        if sys.version_info[:3] != REQUIRED_PYTHON:
            errors.append(
                "需要 Python 3.12.10；当前为 "
                + ".".join(str(value) for value in sys.version_info[:3])
            )
        return errors

    def repair_launcher_backup(self) -> bool:
        backup = self.paths.root / "backup" / "ClashRoyale.py"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not self.paths.launcher.exists():
            return False
        if not backup.exists():
            shutil.copy2(self.paths.launcher, backup)
            return True
        try:
            current_hash = sha256_file(self.paths.launcher)
            backup_hash = sha256_file(backup)
            if current_hash == backup_hash:
                return False
            backup_text = backup.read_text(encoding="utf-8", errors="ignore")[:12000]
            marker = f'APP_VERSION = "{APP_VERSION}"'
            if marker in backup_text:
                shutil.copy2(backup, self.paths.launcher)
            else:
                shutil.copy2(self.paths.launcher, backup)
            return True
        except OSError:
            return False

    def run(self, progress: Callable[[str], None], stop: threading.Event) -> dict[str, Any]:
        self.paths.ensure_dirs()
        progress("检查运行环境…")
        errors = self.platform_errors()
        if errors:
            raise RuntimeError("；".join(errors))
        repaired: list[str] = []

        progress("检查启动文件备份…")
        if self.repair_launcher_backup():
            repaired.append("backup/ClashRoyale.py")

        progress("检查配置文件…")
        config, config_changed = self.config_store.repair()
        if config_changed:
            repaired.append("config.json")

        progress("检查 AI 模型…")
        if self.model_store.repair():
            repaired.append("model/policy.json")

        progress("检查经验池…")
        if self.pool.ensure():
            repaired.append("data/experiences.jsonl")

        self.dependencies.install(progress, stop)
        if stop.is_set():
            return {"repaired": repaired, "updated": 0}

        updated = self.remote.apply(str(config.get("update_manifest_url", "")).strip(), progress, stop)
        if updated:
            repaired.append(f"远程更新 {updated} 个文件")

        progress("生成完整性记录…")
        records: dict[str, Any] = {
            "schema": 1,
            "generated_utc": utc_now(),
            "app_version": APP_VERSION,
            "files": {},
        }
        for path in (self.paths.launcher, self.paths.config, self.paths.model, self.paths.experiences):
            if path.exists() and path.is_file():
                records["files"][str(path.relative_to(self.paths.root)).replace("\\", "/")] = {
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
        atomic_write_json(self.paths.checksums, records)
        progress("文件完整性检查完成")
        return {"repaired": repaired, "updated": updated}

    def quick_ready(self, progress: Callable[[str], None], stop: threading.Event) -> dict[str, Any]:
        self.paths.ensure_dirs()
        errors = self.platform_errors()
        if errors:
            raise RuntimeError("；".join(errors))
        config, _ = self.config_store.repair()
        self.model_store.repair()
        self.pool.ensure()
        self.dependencies.install(progress, stop)
        return config


class WindowsControl:
    user32 = ctypes.windll.user32 if os.name == "nt" else None
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @staticmethod
    def _text(hwnd: int) -> str:
        assert WindowsControl.user32 is not None
        length = WindowsControl.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        WindowsControl.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def find_game_window(self) -> Optional[int]:
        if os.name != "nt":
            return None
        user32 = self.user32
        assert user32 is not None
        keywords = [str(value).casefold() for value in self.config.get("window_title_keywords", [])]
        candidates: list[tuple[int, int, str]] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def enum_callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            title = self._text(hwnd).strip()
            folded = title.casefold()
            if not title or not any(keyword in folded for keyword in keywords):
                return True
            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                candidates.append((area, int(hwnd), title))
            return True

        user32.EnumWindows(callback_type(enum_callback), 0)
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def find_launcher(self) -> Optional[Path]:
        keywords = [str(value).casefold() for value in self.config.get("launcher_name_keywords", [])]
        roots = [
            desktop_path(),
            Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
        ]
        matches: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            try:
                for path in root.rglob("*.lnk"):
                    folded = path.stem.casefold()
                    if any(keyword in folded for keyword in keywords):
                        matches.append(path)
            except OSError:
                continue
        direct_candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / r"Google\Play Games\Bootstrapper.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / r"BlueStacks_nxt\HD-Player.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / r"BlueStacks\HD-Player.exe",
        ]
        matches.extend(path for path in direct_candidates if path.exists())
        if not matches:
            return None
        matches.sort(key=lambda item: ("clash" not in item.name.casefold(), len(str(item))))
        return matches[0]

    def launch_or_find(self, progress: Callable[[str], None], stop: threading.Event) -> Optional[int]:
        hwnd = self.find_game_window()
        if hwnd:
            return hwnd
        if not self.config.get("auto_launch_game", True):
            return None
        launcher = self.find_launcher()
        if launcher is None:
            return None
        progress(f"启动游戏环境：{launcher.stem}")
        try:
            os.startfile(str(launcher))
        except Exception:
            subprocess.Popen([str(launcher)], close_fds=True)
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline and not stop.is_set():
            time.sleep(0.5)
            hwnd = self.find_game_window()
            if hwnd:
                return hwnd
        return None

    def focus(self, hwnd: int) -> None:
        if os.name != "nt":
            return
        assert self.user32 is not None
        self.user32.ShowWindow(hwnd, 9)
        self.user32.SetForegroundWindow(hwnd)

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        if os.name != "nt":
            raise RuntimeError("Windows APIs unavailable")
        assert self.user32 is not None
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("无法读取游戏窗口范围")
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 480 or height < 640:
            raise RuntimeError("游戏窗口尺寸过小")
        return rect.left, rect.top, rect.right, rect.bottom

    def click_normalized(self, hwnd: int, x: float, y: float) -> None:
        if os.name != "nt":
            return
        left, top, right, bottom = self.rect(hwnd)
        px = int(left + max(0.0, min(1.0, x)) * (right - left))
        py = int(top + max(0.0, min(1.0, y)) * (bottom - top))
        assert self.user32 is not None
        self.user32.SetCursorPos(px, py)
        self.user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.035)
        self.user32.mouse_event(0x0004, 0, 0, 0, 0)


class VisionEngine:
    def __init__(self) -> None:
        add_vendor_path()
        from PIL import ImageGrab
        import cv2
        import numpy as np

        self.ImageGrab = ImageGrab
        self.cv2 = cv2
        self.np = np

    def capture(self, rect: tuple[int, int, int, int]) -> Any:
        image = self.ImageGrab.grab(bbox=rect, all_screens=True)
        array = self.np.asarray(image.convert("RGB"))
        height, width = array.shape[:2]
        if width > 540:
            scale = 540.0 / width
            array = self.cv2.resize(array, (540, max(1, int(height * scale))), interpolation=self.cv2.INTER_AREA)
        return array

    def analyze(self, frame: Any) -> dict[str, Any]:
        np = self.np
        cv2 = self.cv2
        height, width = frame.shape[:2]
        top, bottom = int(height * 0.10), int(height * 0.82)
        left, right = int(width * 0.06), int(width * 0.94)
        arena = frame[top:bottom, left:right]
        if arena.size == 0:
            raise RuntimeError("无法定位战场画面")

        red = arena[:, :, 0].astype(np.int16)
        green = arena[:, :, 1].astype(np.int16)
        blue = arena[:, :, 2].astype(np.int16)
        red_mask = ((red > 145) & (red > green * 1.28) & (red > blue * 1.22)).astype(np.uint8)
        blue_mask = ((blue > 140) & (blue > red * 1.20) & (blue > green * 1.08)).astype(np.uint8)
        red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, _labels, stats, centers = cv2.connectedComponentsWithStats(red_mask, 8)
        blobs: list[tuple[float, float, float]] = []
        arena_area = float(arena.shape[0] * arena.shape[1])
        for index in range(1, count):
            area = float(stats[index, cv2.CC_STAT_AREA])
            if area < max(7.0, arena_area * 0.000025) or area > arena_area * 0.035:
                continue
            cx, cy = centers[index]
            nx = float(cx / max(1, arena.shape[1]))
            ny = float(cy / max(1, arena.shape[0]))
            if 0.05 <= nx <= 0.95 and 0.08 <= ny <= 0.95:
                blobs.append((nx, ny, area / arena_area))

        left_threat = 0.0
        right_threat = 0.0
        center_threat = 0.0
        deepest = 0.0
        for x, y, size in blobs:
            proximity = max(0.0, (y - 0.35) / 0.65)
            score = min(1.0, size * 180.0) * (0.25 + 0.75 * proximity)
            deepest = max(deepest, y)
            if x < 0.46:
                left_threat += score
            elif x > 0.54:
                right_threat += score
            else:
                center_threat += score
        left_threat = min(1.0, left_threat)
        right_threat = min(1.0, right_threat)
        center_threat = min(1.0, center_threat)

        bar = frame[int(height * 0.86):int(height * 0.93), int(width * 0.10):int(width * 0.90)]
        if bar.size:
            r = bar[:, :, 0].astype(np.int16)
            g = bar[:, :, 1].astype(np.int16)
            b = bar[:, :, 2].astype(np.int16)
            purple = (r > 110) & (b > 125) & (r > g * 1.18) & (b > g * 1.20)
            columns = purple.mean(axis=0) > 0.10
            elixir_ratio = float(columns.mean()) if columns.size else 0.0
        else:
            elixir_ratio = 0.0
        elixir = max(0.0, min(10.0, elixir_ratio * 12.0))

        hsv = cv2.cvtColor(arena, cv2.COLOR_RGB2HSV)
        saturation = float(hsv[:, :, 1].mean() / 255.0)
        brightness = float(hsv[:, :, 2].mean() / 255.0)

        slot_scores: list[float] = []
        for center_x in (0.285, 0.43, 0.57, 0.715):
            x1 = max(0, int(width * (center_x - 0.055)))
            x2 = min(width, int(width * (center_x + 0.055)))
            y1 = max(0, int(height * 0.82))
            y2 = min(height, int(height * 0.975))
            slot = frame[y1:y2, x1:x2]
            if slot.size == 0:
                slot_scores.append(0.0)
                continue
            slot_hsv = cv2.cvtColor(slot, cv2.COLOR_RGB2HSV)
            slot_variance = float(slot.astype(np.float32).std() / 82.0)
            slot_saturation = float(slot_hsv[:, :, 1].mean() / 255.0)
            slot_scores.append(max(0.0, min(1.0, 0.62 * slot_variance + 0.38 * slot_saturation)))
        card_bar_confidence = sum(slot_scores) / max(1, len(slot_scores))

        river_band = arena[int(arena.shape[0] * 0.42):int(arena.shape[0] * 0.53)]
        if river_band.size:
            rr = river_band[:, :, 0].astype(np.int16)
            rg = river_band[:, :, 1].astype(np.int16)
            rb = river_band[:, :, 2].astype(np.int16)
            blue_water = (rb > rr * 1.08) & (rb > rg * 0.92) & (rb > 90)
            river_confidence = min(1.0, float(blue_water.mean()) * 4.5)
        else:
            river_confidence = 0.0

        zone_h, zone_w = red_mask.shape
        upper_y1, upper_y2 = int(zone_h * 0.08), int(zone_h * 0.46)
        lower_y1, lower_y2 = int(zone_h * 0.54), int(zone_h * 0.94)
        left_x1, left_x2 = int(zone_w * 0.08), int(zone_w * 0.46)
        right_x1, right_x2 = int(zone_w * 0.54), int(zone_w * 0.92)

        def zone_score(mask: Any, y1: int, y2: int, x1: int, x2: int) -> float:
            zone = mask[y1:y2, x1:x2]
            return min(1.0, float(zone.mean()) * 32.0) if zone.size else 0.0

        red_left_tower = zone_score(red_mask, upper_y1, upper_y2, left_x1, left_x2)
        red_right_tower = zone_score(red_mask, upper_y1, upper_y2, right_x1, right_x2)
        blue_left_tower = zone_score(blue_mask, lower_y1, lower_y2, left_x1, left_x2)
        blue_right_tower = zone_score(blue_mask, lower_y1, lower_y2, right_x1, right_x2)
        red_symmetry = math.sqrt(max(0.0, red_left_tower * red_right_tower))
        blue_symmetry = math.sqrt(max(0.0, blue_left_tower * blue_right_tower))
        tower_confidence = math.sqrt(max(0.0, red_symmetry * blue_symmetry))

        arena_confidence = max(0.0, min(1.0, saturation * 1.05 + brightness * 0.20 - 0.22))
        battle_confidence = max(
            0.0,
            min(
                1.0,
                0.42 * card_bar_confidence
                + 0.18 * river_confidence
                + 0.25 * tower_confidence
                + 0.15 * arena_confidence,
            ),
        )
        if tower_confidence < 0.055 or card_bar_confidence < 0.22:
            battle_confidence *= 0.35
        red_density = float(red_mask.mean())
        phase = float((time.monotonic() % 180.0) / 180.0)

        features = [
            1.0,
            elixir / 10.0,
            left_threat,
            right_threat,
            center_threat,
            deepest,
            min(1.0, red_density * 35.0),
            max(-1.0, min(1.0, left_threat - right_threat)),
            battle_confidence,
            phase,
        ]
        return {
            "features": features,
            "elixir": elixir,
            "left_threat": left_threat,
            "right_threat": right_threat,
            "center_threat": center_threat,
            "deepest": deepest,
            "battle_confidence": battle_confidence,
            "blob_count": len(blobs),
        }


class Policy:
    TARGETS = (
        (0.36, 0.72),
        (0.64, 0.72),
        (0.36, 0.60),
        (0.64, 0.60),
        (0.40, 0.46),
        (0.60, 0.46),
    )
    CARD_X = (0.285, 0.43, 0.57, 0.715)
    CARD_Y = 0.91

    def __init__(self, model_store: ModelStore, config: dict[str, Any]) -> None:
        self.model_store = model_store
        self.config = config
        self.model = model_store.load()
        self.rng = random.Random()

    @staticmethod
    def score(weights: list[float], features: list[float]) -> float:
        return sum(float(weight) * float(feature) for weight, feature in zip(weights, features))

    def choose(self, analysis: dict[str, Any]) -> int:
        features = analysis["features"]
        elixir = float(analysis["elixir"])
        costs = [int(value) for value in self.config.get("card_costs", [3, 4, 4, 5])]
        available_cards = [index for index, cost in enumerate(costs[:4]) if elixir + 0.35 >= cost]
        if not available_cards:
            return WAIT_ACTION

        left = float(analysis["left_threat"])
        right = float(analysis["right_threat"])
        center = float(analysis["center_threat"])
        threat = max(left, right, center)
        if threat > 0.20:
            preferred_card = min(available_cards, key=lambda index: costs[index])
            target = 2 if left >= right else 3
            if center > max(left, right):
                target = 2 if self.rng.random() < 0.5 else 3
            return preferred_card * 6 + target

        candidates = [card * 6 + target for card in available_cards for target in range(6)] + [WAIT_ACTION]
        exploration = max(0.0, min(0.35, float(self.config.get("exploration", 0.04))))
        if self.rng.random() < exploration:
            return self.rng.choice(candidates)
        weights = self.model["weights"]
        scored = [(self.score(weights[action], features), action) for action in candidates]
        scored.sort(reverse=True)
        return scored[0][1]

    def execute(self, control: WindowsControl, hwnd: int, action: int) -> None:
        if action == WAIT_ACTION:
            return
        card = action // 6
        target = action % 6
        control.click_normalized(hwnd, self.CARD_X[card], self.CARD_Y)
        time.sleep(0.08)
        tx, ty = self.TARGETS[target]
        control.click_normalized(hwnd, tx, ty)


class Trainer:
    def __init__(self, model_store: ModelStore, pool: ExperiencePool, config: dict[str, Any]) -> None:
        self.model_store = model_store
        self.pool = pool
        self.config = config

    def run(self, progress: Callable[[str], None], stop: threading.Event) -> dict[str, Any]:
        max_experiences = int(self.config.get("max_experiences", 50000))
        samples = self.pool.load(max_experiences)
        model = self.model_store.load()
        if not samples:
            progress("经验池为空：保留当前模型")
            model["updated_utc"] = utc_now()
            model["benchmark"]["status"] = "unverified"
            self.model_store.save(model)
            return {"samples": 0, "generation": model.get("generation", 0)}

        rng = random.Random(2026 + int(model.get("generation", 0)))
        weights = [[float(value) for value in row] for row in model["weights"]]
        epochs = max(1, min(50, int(self.config.get("training_epochs", 5))))
        learning_rate = max(0.0001, min(0.2, float(self.config.get("training_learning_rate", 0.012))))

        for epoch in range(epochs):
            if stop.is_set():
                break
            rng.shuffle(samples)
            total_abs_reward = 0.0
            for item in samples:
                features = [max(-2.0, min(2.0, float(value))) for value in item["features"]]
                action = int(item["action"])
                reward = max(-1.0, min(1.0, float(item["reward"])))
                total_abs_reward += abs(reward)
                prediction = math.tanh(sum(weights[action][i] * features[i] for i in range(FEATURE_COUNT)))
                error = reward - prediction
                for index in range(FEATURE_COUNT):
                    weights[action][index] += learning_rate * error * features[index]
                    weights[action][index] *= 0.99998
                    weights[action][index] = max(-8.0, min(8.0, weights[action][index]))
            progress(f"模型训练 {epoch + 1}/{epochs}，有效奖励 {total_abs_reward:.2f}")

        model["weights"] = weights
        model["generation"] = int(model.get("generation", 0)) + 1
        model["updated_utc"] = utc_now()
        model["training_samples"] = int(model.get("training_samples", 0)) + len(samples)
        model["benchmark"] = {
            "status": "unverified",
            "note": "Training completed, but professional-level superiority requires external controlled benchmarking.",
        }
        self.model_store.save(model)
        self.pool.compact(max_experiences)
        return {"samples": len(samples), "generation": model["generation"]}


class GameAI:
    def __init__(self, paths: AppPaths, config: dict[str, Any]) -> None:
        self.paths = paths
        self.config = config
        self.control = WindowsControl(config)
        self.vision = VisionEngine()
        self.model_store = ModelStore(paths.model)
        self.pool = ExperiencePool(paths.experiences)
        self.policy = Policy(self.model_store, config)

    @staticmethod
    def reward(previous: dict[str, Any], current: dict[str, Any], action: int) -> float:
        previous_threat = previous["left_threat"] + previous["right_threat"] + previous["center_threat"]
        current_threat = current["left_threat"] + current["right_threat"] + current["center_threat"]
        defensive_gain = previous_threat - current_threat
        activity = 0.02 if action != WAIT_ACTION else -0.01
        confidence_penalty = -0.08 if current["battle_confidence"] < 0.30 else 0.0
        return max(-1.0, min(1.0, defensive_gain + activity + confidence_penalty))

    def run(self, progress: Callable[[str], None], stop: threading.Event) -> None:
        hwnd = self.control.launch_or_find(progress, stop)
        if hwnd is None:
            raise RuntimeError("未找到 Clash Royale 窗口；请先安装并登录可在 Windows 运行的游戏环境")
        self.control.focus(hwnd)
        progress("AI 已接管游戏窗口；按 ESC 立即停止")
        interval = max(0.55, min(3.0, float(self.config.get("decision_interval_seconds", 1.25))))
        previous_analysis: Optional[dict[str, Any]] = None
        previous_action = WAIT_ACTION
        previous_features: Optional[list[float]] = None
        last_status = 0.0

        while not stop.is_set():
            started = time.monotonic()
            current_hwnd = self.control.find_game_window()
            if current_hwnd is None:
                raise RuntimeError("游戏窗口已关闭")
            hwnd = current_hwnd
            rect = self.control.rect(hwnd)
            frame = self.vision.capture(rect)
            analysis = self.vision.analyze(frame)

            if previous_analysis is not None and previous_features is not None:
                reward = self.reward(previous_analysis, analysis, previous_action)
                self.pool.append(
                    previous_features,
                    previous_action,
                    reward,
                    {
                        "battle_confidence": round(float(analysis["battle_confidence"]), 4),
                        "blob_count": int(analysis["blob_count"]),
                    },
                )

            if analysis["battle_confidence"] < 0.52:
                action = WAIT_ACTION
            else:
                action = self.policy.choose(analysis)
                self.policy.execute(self.control, hwnd, action)

            previous_analysis = analysis
            previous_features = list(analysis["features"])
            previous_action = action

            now = time.monotonic()
            if now - last_status > 1.8:
                last_status = now
                action_text = "等待" if action == WAIT_ACTION else f"卡位 {action // 6 + 1} / 目标 {action % 6 + 1}"
                progress(
                    f"AI运行中｜圣水≈{analysis['elixir']:.1f}｜威胁 L {analysis['left_threat']:.2f} "
                    f"R {analysis['right_threat']:.2f}｜{action_text}"
                )
            elapsed = time.monotonic() - started
            stop.wait(max(0.03, interval - elapsed))


class GlobalEscapeWatcher:
    VK_ESCAPE = 0x1B

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if os.name != "nt" or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="GlobalEscape", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        was_down = False
        while not self.stop_event.wait(0.045):
            down = bool(user32.GetAsyncKeyState(self.VK_ESCAPE) & 0x8000)
            if down and not was_down:
                self.callback()
            was_down = down

    def stop(self) -> None:
        self.stop_event.set()


class ClashRoyaleApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import messagebox

        self.tk = tk
        self.messagebox = messagebox
        self.paths = PATHS
        self.paths.ensure_dirs()
        self.integrity = IntegrityService(self.paths)
        self.config_store = ConfigStore(self.paths.config)
        self.mode = "home"
        self.mode_token = 0
        self.mode_stop = threading.Event()
        self.events: queue.Queue[tuple[str, int, Any]] = queue.Queue()

        self.root = tk.Tk()
        self.root.title(f"Clash Royale 控制台 {APP_VERSION}")
        self.root.geometry("900x560")
        self.root.minsize(760, 480)
        self.root.configure(bg="#0b1220")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.exit_mode())

        self.title_var = tk.StringVar(value="Clash Royale")
        self.status_var = tk.StringVar(value="选择模式")
        self.detail_var = tk.StringVar(value="ESC 可随时退出当前模式")

        title = tk.Label(
            self.root,
            textvariable=self.title_var,
            bg="#0b1220",
            fg="#f8fafc",
            font=("Microsoft YaHei UI", 28, "bold"),
        )
        title.pack(pady=(38, 6))
        subtitle = tk.Label(
            self.root,
            text="Windows 11 x64 · Python 3.12.10",
            bg="#0b1220",
            fg="#94a3b8",
            font=("Microsoft YaHei UI", 11),
        )
        subtitle.pack()

        self.button_frame = tk.Frame(self.root, bg="#0b1220")
        self.button_frame.pack(expand=True, fill="both", padx=70, pady=34)
        for column in range(4):
            self.button_frame.grid_columnconfigure(column, weight=1)
        self.button_frame.grid_rowconfigure(0, weight=1)

        specs = [
            ("文件", "校验 / 下载 / 修复", self.start_file_mode),
            ("人", "打开游戏供玩家操作", self.start_human_mode),
            ("升级", "训练经验池与模型", self.start_upgrade_mode),
            ("AI", "视觉识别并自动操作", self.start_ai_mode),
        ]
        self.buttons: list[Any] = []
        for index, (name, caption, command) in enumerate(specs):
            panel = tk.Frame(self.button_frame, bg="#111c30", highlightthickness=1, highlightbackground="#24324a")
            panel.grid(row=0, column=index, sticky="nsew", padx=9)
            button = tk.Button(
                panel,
                text=name,
                command=command,
                bg="#1d4ed8" if name != "AI" else "#7c3aed",
                activebackground="#2563eb" if name != "AI" else "#8b5cf6",
                fg="white",
                activeforeground="white",
                relief="flat",
                cursor="hand2",
                font=("Microsoft YaHei UI", 24, "bold"),
                padx=12,
                pady=22,
            )
            button.pack(fill="x", padx=14, pady=(34, 17))
            label = tk.Label(
                panel,
                text=caption,
                wraplength=145,
                justify="center",
                bg="#111c30",
                fg="#cbd5e1",
                font=("Microsoft YaHei UI", 10),
            )
            label.pack(padx=10, pady=(0, 24))
            self.buttons.append(button)

        status_panel = tk.Frame(self.root, bg="#111827", height=86)
        status_panel.pack(fill="x", side="bottom")
        status_panel.pack_propagate(False)
        tk.Label(
            status_panel,
            textvariable=self.status_var,
            bg="#111827",
            fg="#e5e7eb",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(pady=(15, 3))
        tk.Label(
            status_panel,
            textvariable=self.detail_var,
            bg="#111827",
            fg="#93a4ba",
            font=("Microsoft YaHei UI", 9),
        ).pack()

        self.escape_watcher = GlobalEscapeWatcher(lambda: self.events.put(("escape", self.mode_token, None)))
        self.escape_watcher.start()
        self.root.after(80, self.process_events)

    def process_events(self) -> None:
        try:
            while True:
                event, token, payload = self.events.get_nowait()
                if event == "escape":
                    if self.mode != "home":
                        self.exit_mode()
                    continue
                if token != self.mode_token:
                    continue
                if event == "status":
                    self.detail_var.set(str(payload))
                elif event == "done":
                    self.status_var.set(str(payload))
                    self.root.after(900, lambda t=token: self._auto_home(t))
                elif event == "error":
                    self.status_var.set("发生错误")
                    self.detail_var.set(str(payload))
                    LOG.write("ERROR", str(payload))
        except queue.Empty:
            pass
        self.root.after(80, self.process_events)

    def _auto_home(self, token: int) -> None:
        if token == self.mode_token and self.mode in {"file", "upgrade"}:
            self.exit_mode()

    def set_mode(self, mode: str, title: str, status: str) -> int:
        if self.mode != "home":
            self.exit_mode()
        self.mode_token += 1
        self.mode = mode
        self.mode_stop = threading.Event()
        self.title_var.set(title)
        self.status_var.set(status)
        self.detail_var.set("按 ESC 退出当前模式")
        for button in self.buttons:
            button.configure(state="disabled")
        return self.mode_token

    def post(self, event: str, token: int, payload: Any) -> None:
        self.events.put((event, token, payload))

    def worker(self, token: int, function: Callable[[], None]) -> None:
        def run() -> None:
            try:
                function()
            except Exception as exc:
                LOG.exception(str(exc))
                self.post("error", token, str(exc))
        threading.Thread(target=run, name=f"Mode-{token}", daemon=True).start()

    def start_file_mode(self) -> None:
        token = self.set_mode("file", "文件模式", "正在检查完整性")

        def task() -> None:
            result = self.integrity.run(lambda text: self.post("status", token, text), self.mode_stop)
            repaired = result.get("repaired", [])
            summary = "文件完整" if not repaired else "已修复：" + "、".join(repaired)
            self.post("done", token, summary)

        self.worker(token, task)

    def start_human_mode(self) -> None:
        token = self.set_mode("human", "人模式", "正在打开游戏")

        def task() -> None:
            config = self.config_store.load()
            control = WindowsControl(config)
            hwnd = control.launch_or_find(lambda text: self.post("status", token, text), self.mode_stop)
            if hwnd is None:
                raise RuntimeError("未找到 Clash Royale 窗口或启动入口")
            control.focus(hwnd)
            self.post("status", token, "游戏窗口已激活；由玩家操作，按 ESC 返回控制台")
            while not self.mode_stop.wait(0.5):
                if control.find_game_window() is None:
                    raise RuntimeError("游戏窗口已关闭")

        self.worker(token, task)

    def start_upgrade_mode(self) -> None:
        token = self.set_mode("upgrade", "升级模式", "准备升级经验池与 AI 模型")

        def task() -> None:
            config = self.integrity.quick_ready(lambda text: self.post("status", token, text), self.mode_stop)
            if self.mode_stop.is_set():
                return
            trainer = Trainer(ModelStore(self.paths.model), ExperiencePool(self.paths.experiences), config)
            result = trainer.run(lambda text: self.post("status", token, text), self.mode_stop)
            self.post(
                "done",
                token,
                f"升级完成：第 {result['generation']} 代模型，使用 {result['samples']} 条经验",
            )

        self.worker(token, task)

    def start_ai_mode(self) -> None:
        token = self.set_mode("ai", "AI 模式", "准备视觉与模型运行环境")

        def task() -> None:
            config = self.integrity.quick_ready(lambda text: self.post("status", token, text), self.mode_stop)
            if self.mode_stop.is_set():
                return
            ai = GameAI(self.paths, config)
            ai.run(lambda text: self.post("status", token, text), self.mode_stop)

        self.worker(token, task)

    def exit_mode(self) -> None:
        if self.mode == "home":
            return
        self.mode_stop.set()
        self.mode_token += 1
        self.mode = "home"
        self.title_var.set("Clash Royale")
        self.status_var.set("选择模式")
        self.detail_var.set("ESC 可随时退出当前模式")
        for button in self.buttons:
            button.configure(state="normal")
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def close(self) -> None:
        self.mode_stop.set()
        self.escape_watcher.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    relocate_to_desktop()
    PATHS.ensure_dirs()
    add_vendor_path()
    try:
        ClashRoyaleApp().run()
    except Exception as exc:
        LOG.exception(str(exc))
        if os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"ClashRoyale 启动失败：\n{exc}\n\n日志：{PATHS.logs / 'ClashRoyale.log'}",
                    "ClashRoyale",
                    0x10,
                )
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
