from __future__ import annotations

import ast
import copy
import ctypes
import hashlib
import importlib
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
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

APP_NAME = "ClashRoyale"
APP_VERSION = "1.2.0"
REQUIRED_PYTHON = (3, 12, 10)
DEPENDENCIES = (
    "Pillow>=10.2,<13",
    "numpy>=1.26,<3",
    "opencv-python-headless>=4.9,<5",
)
MAX_UPDATE_FILE_BYTES = 512 * 1024 * 1024
MAX_LAUNCHER_FILE_BYTES = 8 * 1024 * 1024
MAX_JSON_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPERIENCE_LINE_BYTES = 1024 * 1024
MAX_EXPERIENCE_ITEMS = 500000
VENDOR_MARKER_SCHEMA = 2
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


def strict_json_loads(value: str | bytes) -> Any:
    def reject_constant(constant: str) -> Any:
        raise ValueError(f"JSON 包含无效常量：{constant}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"JSON 包含重复字段：{key}")
            result[key] = item
        return result

    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=reject_duplicates)


def read_json_file(path: Path, max_bytes: int = MAX_JSON_FILE_BYTES) -> Any:
    size = path.stat().st_size
    if size < 0 or size > max_bytes:
        raise ValueError(f"JSON 文件大小无效：{path.name}")
    return strict_json_loads(path.read_text(encoding="utf-8"))


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
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


def atomic_copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".copy", dir=str(target.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        temp_path.chmod(0o600)
        with temp_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        if target.exists():
            try:
                target.chmod(0o600)
            except OSError:
                pass
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.chmod(0o600)
            temp_path.unlink()
        except FileNotFoundError:
            pass


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def path_is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


def remove_tree(path: Path, strict: bool = False) -> None:
    if not path.exists():
        return
    for child in path.rglob("*"):
        try:
            child.chmod(0o700)
        except OSError:
            pass
    try:
        shutil.rmtree(path)
    except OSError:
        if strict:
            raise


def quarantine_non_file(path: Path, temp_root: Path) -> Optional[Path]:
    exists = path.exists() or path_is_link(path)
    if not exists or (path.is_file() and not path_is_link(path)):
        return None
    temp_root.mkdir(parents=True, exist_ok=True)
    target = temp_root / f"quarantine-{path.name}-{time.time_ns()}"
    os.replace(path, target)
    return target


def safe_update_path(value: Any) -> tuple[str, Path]:
    if not isinstance(value, str):
        raise ValueError("更新路径必须是字符串")
    raw = str(value).replace("\\", "/")
    relative = raw.strip()
    pure = PurePosixPath(relative)
    invalid_characters = set('<>:"|?*\0')
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if raw != relative or not relative or relative.startswith("/") or pure.is_absolute():
        raise ValueError(f"拒绝不安全的更新路径：{relative}")
    if not pure.parts or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError(f"拒绝不安全的更新路径：{relative}")
    for part in pure.parts:
        if part != part.rstrip(" .") or any(character in invalid_characters for character in part):
            raise ValueError(f"拒绝 Windows 不支持的更新路径：{relative}")
        if part.split(".", 1)[0].casefold() in reserved:
            raise ValueError(f"拒绝 Windows 保留路径：{relative}")
    normalized = pure.as_posix()
    return normalized, Path(*pure.parts)


def launcher_source_valid(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > MAX_LAUNCHER_FILE_BYTES:
            return False
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except Exception:
        return False
    top_level_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    return {"main", "ClashRoyaleApp", "IntegrityService", "GameAI"}.issubset(top_level_names)


def launcher_version(path: Path) -> tuple[int, ...]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_LAUNCHER_FILE_BYTES:
            return (0,)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "APP_VERSION":
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    parts = tuple(int(part) for part in value.split("."))
                    if parts:
                        return parts
    except Exception:
        pass
    return (0,)


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


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass(frozen=True)
class AppPaths:
    root: Path
    launcher: Path
    launcher_backup: Path
    config: Path
    model: Path
    experiences: Path
    checksums: Path
    vendor: Path
    dependency_marker: Path
    logs: Path
    temp: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        root = desktop_path() / APP_NAME
        return cls(
            root=root,
            launcher=root / "ClashRoyale.py",
            launcher_backup=root / "backup" / "ClashRoyale.py",
            config=root / "config.json",
            model=root / "model" / "policy.json",
            experiences=root / "data" / "experiences.jsonl",
            checksums=root / "checksums.json",
            vendor=root / "vendor",
            dependency_marker=root / "vendor" / ".installed.json",
            logs=root / "logs",
            temp=root / "temp",
        )

    def ensure_dirs(self) -> None:
        for path in (
            self.root,
            self.launcher_backup.parent,
            self.model.parent,
            self.experiences.parent,
            self.vendor,
            self.logs,
            self.temp,
        ):
            path.mkdir(parents=True, exist_ok=True)


PATHS = AppPaths.discover()


def relocate_to_desktop() -> None:
    if os.name != "nt":
        return
    try:
        PATHS.ensure_dirs()
        quarantine_non_file(PATHS.launcher, PATHS.temp)
        current = Path(__file__).resolve()
        target = PATHS.launcher.resolve()
        if current == target:
            return
        use_existing = (
            target.exists()
            and launcher_source_valid(target)
            and launcher_version(target) >= launcher_version(current)
        )
        if not use_existing:
            fd, temp_name = tempfile.mkstemp(prefix="ClashRoyale.", suffix=".py.tmp", dir=str(PATHS.root))
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                shutil.copy2(current, temp_path)
                if not launcher_source_valid(temp_path):
                    raise RuntimeError("复制后的启动文件未通过结构验证")
                if target.exists():
                    try:
                        target.chmod(0o600)
                    except OSError:
                        pass
                os.replace(temp_path, target)
            finally:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
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
    "auto_queue_battle": True,
    "lobby_click_cooldown_seconds": 8.0,
    "training_epochs": 5,
    "training_learning_rate": 0.012,
    "max_experiences": 50000,
}


def new_default_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


class AppLog:
    def __init__(self, paths: AppPaths) -> None:
        self.path = paths.logs / "ClashRoyale.log"
        self._lock = threading.Lock()

    def write(self, level: str, message: str) -> None:
        line = f"{utc_now()} [{level}] {message}\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
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

    @staticmethod
    def _normalize(value: Any) -> dict[str, Any]:
        defaults = new_default_config()
        if not isinstance(value, dict):
            return defaults

        result = new_default_config()
        result["schema"] = int(defaults["schema"])
        result["app_version"] = APP_VERSION

        manifest_url = value.get("update_manifest_url", defaults["update_manifest_url"])
        if isinstance(manifest_url, str):
            manifest_url = manifest_url.strip()
            if not manifest_url or manifest_url.lower().startswith("https://"):
                result["update_manifest_url"] = manifest_url

        for key in ("window_title_keywords", "launcher_name_keywords"):
            raw = value.get(key)
            if isinstance(raw, list):
                cleaned = [str(item).strip() for item in raw if isinstance(item, str) and item.strip()]
                if cleaned:
                    result[key] = cleaned[:32]

        costs = value.get("card_costs")
        if isinstance(costs, list) and len(costs) == 4:
            normalized_costs: list[int] = []
            for item in costs:
                if isinstance(item, bool) or not isinstance(item, int):
                    normalized_costs = []
                    break
                normalized_costs.append(max(1, min(10, item)))
            if len(normalized_costs) == 4:
                result["card_costs"] = normalized_costs

        numeric_limits: dict[str, tuple[float, float, type]] = {
            "decision_interval_seconds": (0.55, 3.0, float),
            "exploration": (0.0, 0.35, float),
            "lobby_click_cooldown_seconds": (4.0, 30.0, float),
            "training_epochs": (1.0, 50.0, int),
            "training_learning_rate": (0.0001, 0.2, float),
            "max_experiences": (100.0, 500000.0, int),
        }
        for key, (low, high, output_type) in numeric_limits.items():
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                continue
            clamped = max(low, min(high, float(raw)))
            result[key] = output_type(clamped)

        for key in ("auto_launch_game", "auto_queue_battle"):
            raw = value.get(key)
            if isinstance(raw, bool):
                result[key] = raw
        return result

    def _read_raw(self) -> Any:
        try:
            return read_json_file(self.path)
        except Exception:
            return None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return new_default_config()
        return self._normalize(self._read_raw())

    def repair(self) -> tuple[dict[str, Any], bool]:
        raw = self._read_raw() if self.path.exists() else None
        current = self._normalize(raw)
        changed = raw != current
        if changed:
            atomic_write_json(self.path, current)
        return current, changed


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
            if not isinstance(weights, list) or len(weights) != ACTION_COUNT:
                return False
            for row in weights:
                if not isinstance(row, list) or len(row) != FEATURE_COUNT:
                    return False
                if not all(
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(float(value))
                    for value in row
                ):
                    return False
            return True
        except Exception:
            return False

    @classmethod
    def normalize(cls, model: Any) -> Optional[dict[str, Any]]:
        if not cls.valid(model):
            return None
        result = copy.deepcopy(model)
        result["schema"] = 1
        result["app_version"] = APP_VERSION
        generation = result.get("generation", 0)
        result["generation"] = (
            generation if not isinstance(generation, bool) and isinstance(generation, int) and generation >= 0 else 0
        )
        samples = result.get("training_samples", 0)
        result["training_samples"] = (
            samples if not isinstance(samples, bool) and isinstance(samples, int) and samples >= 0 else 0
        )
        result["weights"] = [
            [max(-8.0, min(8.0, float(value))) for value in row]
            for row in result["weights"]
        ]
        for key in ("created_utc", "updated_utc"):
            value = result.get(key)
            result[key] = value if isinstance(value, str) and value.strip() else utc_now()
        benchmark = result.get("benchmark")
        if not isinstance(benchmark, dict):
            benchmark = {}
        status = benchmark.get("status")
        if status not in {"unverified", "verified"}:
            status = "unverified"
        if status == "verified" and not isinstance(benchmark.get("evidence"), dict):
            status = "unverified"
        result["benchmark"] = {
            **benchmark,
            "status": status,
            "note": str(benchmark.get("note", "No validated professional-level benchmark is bundled.")),
        }
        return result

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = read_json_file(self.path)
                normalized = self.normalize(value)
                if normalized is not None:
                    if normalized != value:
                        atomic_write_json(self.path, normalized)
                    return normalized
            except Exception:
                pass
            value = self.default_model()
            atomic_write_json(self.path, value)
            return value

    def repair(self) -> bool:
        try:
            if self.path.exists():
                value = read_json_file(self.path)
                normalized = self.normalize(value)
                if normalized is not None:
                    if normalized != value:
                        atomic_write_json(self.path, normalized)
                        return True
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
        self._lock = threading.RLock()

    def _lines(self) -> Any:
        with self.path.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_EXPERIENCE_LINE_BYTES + 1)
                if not raw:
                    return
                if len(raw) > MAX_EXPERIENCE_LINE_BYTES and not raw.endswith(b"\n"):
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(MAX_EXPERIENCE_LINE_BYTES + 1)
                    yield None
                    continue
                try:
                    yield raw.decode("utf-8")
                except UnicodeDecodeError:
                    yield None

    def ensure(self, max_items: int = MAX_EXPERIENCE_ITEMS) -> bool:
        max_items = max(1, min(MAX_EXPERIENCE_ITEMS, int(max_items)))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.touch()
                return True
            good_lines: deque[str] = deque(maxlen=max_items)
            damaged = False
            valid_count = 0
            try:
                for line in self._lines():
                    if line is None:
                        damaged = True
                        continue
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        item = strict_json_loads(stripped)
                        if self.valid_item(item):
                            valid_count += 1
                            good_lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                        else:
                            damaged = True
                    except Exception:
                        damaged = True
            except OSError:
                damaged = True
            if valid_count > max_items:
                damaged = True
            if damaged:
                atomic_write_text(self.path, "".join(line + "\n" for line in good_lines))
            return damaged

    @staticmethod
    def valid_item(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        features = item.get("features")
        action = item.get("action")
        reward = item.get("reward")
        return (
            isinstance(features, list)
            and len(features) == FEATURE_COUNT
            and all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                for value in features
            )
            and not isinstance(action, bool)
            and isinstance(action, int)
            and 0 <= action < ACTION_COUNT
            and not isinstance(reward, bool)
            and isinstance(reward, (int, float))
            and math.isfinite(float(reward))
        )

    def append(self, features: list[float], action: int, reward: float, meta: Optional[dict[str, Any]] = None) -> None:
        candidate = {"features": features, "action": action, "reward": reward}
        if not self.valid_item(candidate):
            raise ValueError("拒绝写入无效经验")
        item = {
            "ts": utc_now(),
            "features": [round(float(v), 6) for v in features],
            "action": int(action),
            "reward": round(float(max(-2.0, min(2.0, reward))), 6),
            "meta": meta or {},
        }
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def load(self, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        if limit <= 0:
            return []
        result: deque[dict[str, Any]] = deque(maxlen=limit)
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = strict_json_loads(line)
                        except Exception:
                            continue
                        if self.valid_item(item):
                            result.append(item)
            except OSError:
                return []
        return list(result)

    def compact(self, max_items: int) -> bool:
        max_items = max(1, min(MAX_EXPERIENCE_ITEMS, int(max_items)))
        with self._lock:
            items = self.load(max_items + 1)
            if len(items) <= max_items:
                return False
            kept = items[-max_items:]
            atomic_write_text(
                self.path,
                "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in kept),
            )
            return True


class DependencyManager:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @staticmethod
    def _vendor_digest(root: Path) -> dict[str, Any]:
        digest = hashlib.sha256()
        file_count = 0
        total_bytes = 0
        marker = root / ".installed.json"
        for path in root.rglob("*"):
            if path_is_link(path):
                raise RuntimeError(f"依赖目录包含不安全链接：{path.name}")
        candidates = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file()
                and path != marker
                and "__pycache__" not in path.parts
            ),
            key=lambda path: path.relative_to(root).as_posix().casefold(),
        )
        for path in candidates:
            if not path_is_within(path, root):
                raise RuntimeError(f"依赖目录包含不安全链接：{path.name}")
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
            file_count += 1
            total_bytes += size
        return {
            "sha256": digest.hexdigest(),
            "file_count": file_count,
            "total_bytes": total_bytes,
        }

    def _read_marker(self) -> Optional[dict[str, Any]]:
        try:
            marker = read_json_file(self.paths.dependency_marker, 4 * 1024 * 1024)
            if not isinstance(marker, dict):
                return None
            valid = (
                marker.get("schema") == VENDOR_MARKER_SCHEMA
                and marker.get("python") == ".".join(map(str, REQUIRED_PYTHON))
                and marker.get("requirements") == list(DEPENDENCIES)
                and isinstance(marker.get("versions"), dict)
                and isinstance(marker.get("inventory"), dict)
            )
            return marker if valid else None
        except Exception:
            return None

    def available(self, deep_check: bool = False) -> bool:
        marker = self._read_marker()
        if marker is None or not self.paths.vendor.is_dir():
            return False
        try:
            versions = self._validate_staging(self.paths.vendor)
            expected_versions = {str(key): str(value) for key, value in marker["versions"].items()}
            if versions != expected_versions:
                return False
            if deep_check and self._vendor_digest(self.paths.vendor) != marker["inventory"]:
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _validate_staging(staging: Path) -> dict[str, str]:
        code = r"""
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import PIL, numpy, cv2
mods = {"PIL": PIL, "numpy": numpy, "cv2": cv2}
for name, module in mods.items():
    origin = pathlib.Path(module.__file__).resolve()
    try:
        origin.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{name} loaded outside vendor: {origin}") from exc
print(json.dumps({name: str(getattr(module, "__version__", "unknown")) for name, module in mods.items()}))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", code, str(staging)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError("依赖导入验证失败：" + (result.stderr or result.stdout)[-800:])
        try:
            versions = strict_json_loads(result.stdout.strip().splitlines()[-1])
        except Exception as exc:
            raise RuntimeError("依赖版本信息无效") from exc
        if not isinstance(versions, dict):
            raise RuntimeError("依赖版本信息无效")
        return {str(key): str(value) for key, value in versions.items()}

    def install(
        self,
        progress: Callable[[str], None],
        stop: threading.Event,
        deep_check: bool = False,
    ) -> None:
        if self.available(deep_check=deep_check):
            progress("本地依赖完整")
            return
        if stop.is_set():
            return
        progress("准备下载图像与数值计算依赖…")
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

        staging = self.paths.temp / f"vendor-install-{os.getpid()}-{time.time_ns()}"
        old_vendor = self.paths.temp / f"vendor-old-{os.getpid()}-{time.time_ns()}"
        remove_tree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--isolated",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--upgrade",
            "--target",
            str(staging),
            *DEPENDENCIES,
        ]
        install_env = os.environ.copy()
        install_env["PIP_CACHE_DIR"] = str(self.paths.temp / "pip-cache")
        install_env["TMP"] = str(self.paths.temp)
        install_env["TEMP"] = str(self.paths.temp)
        output_path = self.paths.temp / f"pip-install-{os.getpid()}-{time.time_ns()}.log"
        output_handle = output_path.open("w+b")
        process: Any = None
        try:
            process = subprocess.Popen(
                command,
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=install_env,
            )
            last_notice = 0.0
            while process.poll() is None:
                if stop.wait(0.20):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise RuntimeError("依赖安装已取消")
                if time.monotonic() - last_notice >= 4.0:
                    last_notice = time.monotonic()
                    progress("正在下载并安装依赖…")
            output_handle.flush()
            output_handle.seek(0)
            output = output_handle.read().decode("utf-8", errors="replace")
            if process.returncode != 0:
                raise RuntimeError("依赖下载失败：" + (output[-1200:] if output else "pip exited with error"))
            if stop.is_set():
                raise RuntimeError("依赖安装已取消")

            progress("验证依赖完整性…")
            versions = self._validate_staging(staging)
            inventory = self._vendor_digest(staging)
            atomic_write_json(
                staging / ".installed.json",
                {
                    "schema": VENDOR_MARKER_SCHEMA,
                    "installed_utc": utc_now(),
                    "python": ".".join(map(str, REQUIRED_PYTHON)),
                    "requirements": list(DEPENDENCIES),
                    "versions": versions,
                    "inventory": inventory,
                },
            )

            old_moved = False
            if self.paths.vendor.exists():
                remove_tree(old_vendor, strict=True)
                os.replace(self.paths.vendor, old_vendor)
                old_moved = True
            try:
                os.replace(staging, self.paths.vendor)
                importlib.invalidate_caches()
                if not self.available(deep_check=True):
                    raise RuntimeError("依赖已安装，但完整性复检失败")
            except Exception:
                if self.paths.vendor.exists():
                    remove_tree(self.paths.vendor, strict=True)
                if old_moved and old_vendor.exists() and not self.paths.vendor.exists():
                    os.replace(old_vendor, self.paths.vendor)
                    importlib.invalidate_caches()
                raise
            remove_tree(old_vendor)
            progress("依赖安装完成")
        finally:
            if process is not None and process.poll() is None:
                process.kill()
            output_handle.close()
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
            remove_tree(staging)


class RemoteUpdater:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @staticmethod
    def _open_https(url: str, timeout: float) -> Any:
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        response = urllib.request.urlopen(request, timeout=timeout)
        final_url = str(response.geturl())
        if not final_url.lower().startswith("https://"):
            response.close()
            raise ValueError("更新地址重定向到了非 HTTPS 地址")
        return response

    def apply(self, manifest_url: str, progress: Callable[[str], None], stop: threading.Event) -> int:
        if not manifest_url:
            return 0
        if not manifest_url.lower().startswith("https://"):
            raise ValueError("update_manifest_url 必须使用 HTTPS")
        progress("检查远程更新清单…")
        with self._open_https(manifest_url, 20) as response:
            raw_manifest = response.read(2 * 1024 * 1024 + 1)
        if len(raw_manifest) > 2 * 1024 * 1024:
            raise ValueError("更新清单过大")
        manifest = strict_json_loads(raw_manifest.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("更新清单格式错误")
        files = manifest.get("files", [])
        if not isinstance(files, list) or len(files) > 1000:
            raise ValueError("更新清单格式错误")

        seen: set[str] = set()
        pending: list[dict[str, Any]] = []
        for index, item in enumerate(files, 1):
            if stop.is_set():
                raise RuntimeError("更新已取消")
            if not isinstance(item, dict):
                raise ValueError("更新项格式错误")
            relative, relative_path = safe_update_path(item.get("path", ""))
            expected = str(item.get("sha256", "")).lower().strip()
            url = str(item.get("url", "")).strip()
            declared_size = item.get("size")
            seen_key = relative.casefold()
            if seen_key in seen:
                raise ValueError(f"拒绝重复的更新路径：{relative}")
            seen.add(seen_key)
            if relative_path.parts and relative_path.parts[0].casefold() in {"backup", "logs", "temp"}:
                raise ValueError(f"拒绝更新受保护目录：{relative}")
            if relative.casefold() == "checksums.json":
                raise ValueError("拒绝由远程清单覆盖 checksums.json")
            if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
                raise ValueError(f"更新项 SHA-256 无效：{relative}")
            if not url.lower().startswith("https://"):
                raise ValueError(f"更新项必须使用 HTTPS：{relative}")
            if declared_size is not None:
                if isinstance(declared_size, bool) or not isinstance(declared_size, int):
                    raise ValueError(f"更新项大小无效：{relative}")
                if declared_size < 0 or declared_size > MAX_UPDATE_FILE_BYTES:
                    raise ValueError(f"更新项过大：{relative}")

            unresolved_target = self.paths.root / relative_path
            cursor = self.paths.root
            for part in relative_path.parts:
                cursor /= part
                if cursor.exists() and path_is_link(cursor):
                    raise ValueError(f"拒绝通过链接更新文件：{relative}")
            target = unresolved_target.resolve()
            if not path_is_within(target, self.paths.root) or target == self.paths.root.resolve():
                raise ValueError(f"更新目标越界：{relative}")
            if target.exists() and not target.is_file():
                raise ValueError(f"更新目标不是普通文件：{relative}")
            file_limit = MAX_LAUNCHER_FILE_BYTES if target == self.paths.launcher else MAX_UPDATE_FILE_BYTES
            if declared_size is not None and declared_size > file_limit:
                raise ValueError(f"更新项过大：{relative}")
            if target.exists() and target.is_file() and sha256_file(target) == expected:
                continue
            pending.append(
                {
                    "manifest_index": index,
                    "relative": relative,
                    "expected": expected,
                    "url": url,
                    "declared_size": declared_size,
                    "file_limit": file_limit,
                    "target": target,
                }
            )

        if not pending:
            return 0

        staging = self.paths.temp / f"remote-update-{os.getpid()}-{time.time_ns()}"
        downloads = staging / "downloads"
        rollback = staging / "rollback"
        remove_tree(staging, strict=True)
        downloads.mkdir(parents=True, exist_ok=True)
        committed: list[tuple[Path, Path, bool]] = []
        try:
            for position, plan in enumerate(pending):
                if stop.is_set():
                    raise RuntimeError("更新已取消")
                relative = str(plan["relative"])
                progress(f"下载更新 {plan['manifest_index']}/{len(files)}：{relative}")
                temp_path = downloads / f"{position:04d}.download"
                file_limit = int(plan["file_limit"])
                total = 0
                with self._open_https(str(plan["url"]), 45) as response, temp_path.open("wb") as handle:
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            remote_size = int(content_length)
                        except ValueError:
                            remote_size = -1
                        if remote_size > file_limit:
                            raise RuntimeError(f"更新文件过大：{relative}")
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        if stop.is_set():
                            raise RuntimeError("更新已取消")
                        total += len(chunk)
                        if total > file_limit:
                            raise RuntimeError(f"更新文件过大：{relative}")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                declared_size = plan["declared_size"]
                if declared_size is not None and total != declared_size:
                    raise RuntimeError(f"更新文件大小校验失败：{relative}")
                if sha256_file(temp_path) != plan["expected"]:
                    raise RuntimeError(f"更新文件校验失败：{relative}")
                target = plan["target"]
                if target == self.paths.launcher and not launcher_source_valid(temp_path):
                    raise RuntimeError("远程启动文件未通过结构验证")

            if stop.is_set():
                raise RuntimeError("更新已取消")
            progress("原子应用已验证的更新…")
            rollback.mkdir(parents=True, exist_ok=True)
            for position, plan in enumerate(pending):
                target = plan["target"]
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = rollback / f"{position:04d}.backup"
                had_original = target.exists()
                if had_original:
                    try:
                        target.chmod(0o600)
                    except OSError:
                        pass
                    os.replace(target, backup)
                committed.append((target, backup, had_original))
                os.replace(downloads / f"{position:04d}.download", target)
            return len(pending)
        except Exception as exc:
            rollback_errors: list[str] = []
            for target, backup, had_original in reversed(committed):
                try:
                    if target.exists():
                        target.unlink()
                    if had_original and backup.exists():
                        os.replace(backup, target)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{target.name}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError("更新失败且回滚不完整：" + "；".join(rollback_errors)) from exc
            raise
        finally:
            remove_tree(staging)


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

    def _previous_hash(self, relative: str) -> Optional[str]:
        try:
            records = read_json_file(self.paths.checksums, 4 * 1024 * 1024)
            value = records["files"][relative]["sha256"]
            if isinstance(value, str) and len(value) == 64:
                return value.lower()
        except Exception:
            pass
        return None

    def repair_launcher_backup(self, accept_current: bool = False) -> Optional[str]:
        launcher = self.paths.launcher
        backup = self.paths.launcher_backup
        backup.parent.mkdir(parents=True, exist_ok=True)
        quarantined_launcher = quarantine_non_file(launcher, self.paths.temp)
        quarantine_non_file(backup, self.paths.temp)
        if not launcher.exists():
            if backup.is_file() and launcher_source_valid(backup):
                atomic_copy_file(backup, launcher)
                if quarantined_launcher is not None:
                    return "已隔离异常启动路径，并从可信备份补全 ClashRoyale.py"
                return "已从可信备份补全 ClashRoyale.py"
            source = Path(__file__).resolve()
            if launcher_source_valid(source):
                atomic_copy_file(source, launcher)
                atomic_copy_file(source, backup)
                return "已补全 ClashRoyale.py 及其备份"
            raise RuntimeError("ClashRoyale.py 缺失，且没有可用备份")

        current_valid = launcher_source_valid(launcher)
        backup_valid = backup.exists() and launcher_source_valid(backup)
        expected = self._previous_hash("ClashRoyale.py")
        current_hash = sha256_file(launcher) if launcher.is_file() else ""
        backup_hash = sha256_file(backup) if backup_valid else ""

        if accept_current and current_valid:
            if not backup_valid or backup_hash != current_hash:
                atomic_copy_file(launcher, backup)
                return "已刷新启动文件备份"
            return None
        if current_valid and backup_valid and launcher_version(launcher) > launcher_version(backup):
            atomic_copy_file(launcher, backup)
            return "已接受较新版本并刷新启动文件备份"
        if expected and current_hash != expected and backup_valid and backup_hash == expected:
            atomic_copy_file(backup, launcher)
            return "已从可信备份恢复 ClashRoyale.py"
        if expected and current_hash != expected:
            raise RuntimeError("ClashRoyale.py 与完整性记录不一致，且没有匹配的可信备份")
        if not current_valid and backup_valid:
            atomic_copy_file(backup, launcher)
            return "已替换损坏的 ClashRoyale.py"
        if not current_valid:
            raise RuntimeError("ClashRoyale.py 结构损坏，且没有可用备份")
        if not backup_valid or backup_hash != current_hash:
            atomic_copy_file(launcher, backup)
            return "已刷新启动文件备份"
        return None

    def _write_integrity_records(self) -> None:
        records: dict[str, Any] = {
            "schema": 1,
            "generated_utc": utc_now(),
            "app_version": APP_VERSION,
            "files": {},
        }
        tracked = (
            self.paths.launcher,
            self.paths.launcher_backup,
            self.paths.config,
            self.paths.model,
            self.paths.experiences,
            self.paths.dependency_marker,
        )
        for path in tracked:
            if path.exists() and path.is_file() and path_is_within(path, self.paths.root):
                records["files"][str(path.relative_to(self.paths.root)).replace("\\", "/")] = {
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
        atomic_write_json(self.paths.checksums, records)

    def run(self, progress: Callable[[str], None], stop: threading.Event) -> dict[str, Any]:
        self.paths.ensure_dirs()
        progress("检查运行环境…")
        errors = self.platform_errors()
        if errors:
            raise RuntimeError("；".join(errors))
        repaired: list[str] = []

        progress("检查启动文件与可信备份…")
        backup_result = self.repair_launcher_backup()
        if backup_result:
            repaired.append(backup_result)

        progress("检查配置文件…")
        config, config_changed = self.config_store.repair()
        if config_changed:
            repaired.append("config.json")

        progress("检查 AI 模型…")
        if self.model_store.repair():
            repaired.append("model/policy.json")

        progress("检查经验池…")
        max_experiences = int(config.get("max_experiences", 50000))
        if self.pool.ensure(max_experiences):
            repaired.append("data/experiences.jsonl")

        self.dependencies.install(progress, stop, deep_check=True)
        if stop.is_set():
            return {"repaired": repaired, "updated": 0}

        updated = self.remote.apply(str(config.get("update_manifest_url", "")).strip(), progress, stop)
        if updated:
            repaired.append(f"远程更新 {updated} 个文件")
            progress("复检远程更新后的文件…")
            config, config_changed = self.config_store.repair()
            if config_changed:
                repaired.append("远程配置已规范化")
            if self.model_store.repair():
                repaired.append("远程模型无效，已重建")
            max_experiences = int(config.get("max_experiences", 50000))
            if self.pool.ensure(max_experiences):
                repaired.append("远程经验池无效，已修复")
            if not launcher_source_valid(self.paths.launcher):
                raise RuntimeError("远程更新后的 ClashRoyale.py 未通过结构验证")
            self.dependencies.install(progress, stop, deep_check=True)

        backup_result = self.repair_launcher_backup(accept_current=bool(updated))
        if backup_result and backup_result not in repaired:
            repaired.append(backup_result)

        progress("生成完整性记录…")
        self._write_integrity_records()
        progress("文件完整性检查完成")
        return {"repaired": repaired, "updated": updated}

    def quick_ready(self, progress: Callable[[str], None], stop: threading.Event) -> dict[str, Any]:
        self.paths.ensure_dirs()
        errors = self.platform_errors()
        if errors:
            raise RuntimeError("；".join(errors))
        config, _ = self.config_store.repair()
        self.model_store.repair()
        self.pool.ensure(int(config.get("max_experiences", 50000)))
        self.dependencies.install(progress, stop, deep_check=False)
        return config


class WindowsControl:
    user32 = ctypes.windll.user32 if os.name == "nt" else None
    enum_callback_type: Any = None
    api_ready = False

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._configure_api()

    @classmethod
    def _configure_api(cls) -> None:
        if os.name != "nt" or cls.api_ready:
            return
        user32 = cls.user32
        assert user32 is not None
        cls.enum_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.mouse_event.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        user32.mouse_event.restype = None
        user32.EnumWindows.argtypes = [cls.enum_callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        cls.api_ready = True

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
        callback_type = self.enum_callback_type
        assert callback_type is not None

        def enum_callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == os.getpid():
                return True
            title = self._text(hwnd).strip()
            folded = title.casefold()
            if not title or not any(keyword in folded for keyword in keywords):
                return True
            rect = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                width = max(0, rect.right - rect.left)
                height = max(0, rect.bottom - rect.top)
                if width >= 360 and height >= 540:
                    area = width * height
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
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / r"LDPlayer\LDPlayer9\dnplayer.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / r"ldplayer9box\dnplayer.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / r"Netease\MuMuPlayerGlobal-12.0\shell\MuMuPlayer.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / r"Nemu\EmulatorShell\NemuPlayer.exe",
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
            if stop.wait(0.5):
                return None
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
        client = wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(client)):
            raise RuntimeError("无法读取游戏客户区范围")
        top_left = wintypes.POINT(client.left, client.top)
        bottom_right = wintypes.POINT(client.right, client.bottom)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            raise RuntimeError("无法转换游戏客户区坐标")
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            raise RuntimeError("无法转换游戏客户区坐标")
        width = bottom_right.x - top_left.x
        height = bottom_right.y - top_left.y
        if width < 360 or height < 540:
            raise RuntimeError("游戏窗口尺寸过小")
        return top_left.x, top_left.y, bottom_right.x, bottom_right.y

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

    def find_lobby_action(self, frame: Any) -> Optional[tuple[float, float, float]]:
        """Locate a prominent central action button without relying on OCR or fixed pixels."""
        np = self.np
        cv2 = self.cv2
        height, width = frame.shape[:2]
        if height < 200 or width < 200:
            return None
        roi_y1 = int(height * 0.50)
        roi_y2 = int(height * 0.96)
        roi = frame[roi_y1:roi_y2]
        red = roi[:, :, 0].astype(np.int16)
        green = roi[:, :, 1].astype(np.int16)
        blue = roi[:, :, 2].astype(np.int16)
        yellow = (red > 165) & (green > 105) & (blue < 155) & (red > blue * 1.30) & (green > blue * 1.08)
        cyan_blue = (blue > 145) & (green > 95) & (blue > red * 1.08) & ((blue + green) > 285)
        mask = (yellow | cyan_blue).astype(np.uint8) * 255
        kernel = np.ones((5, 9), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best: Optional[tuple[float, float, float]] = None
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            wr = w / max(1, width)
            hr = h / max(1, height)
            cx = (x + w / 2.0) / max(1, width)
            cy = (roi_y1 + y + h / 2.0) / max(1, height)
            fill = float(cv2.contourArea(contour)) / max(1.0, float(w * h))
            if not (0.18 <= wr <= 0.78 and 0.03 <= hr <= 0.20):
                continue
            if not (0.24 <= cx <= 0.76 and 0.56 <= cy <= 0.94):
                continue
            if fill < 0.38:
                continue
            centrality = max(0.0, 1.0 - abs(cx - 0.5) / 0.26)
            lower_preference = max(0.0, 1.0 - abs(cy - 0.79) / 0.25)
            size_score = min(1.0, wr / 0.45) * min(1.0, hr / 0.09)
            confidence = 0.42 * fill + 0.25 * centrality + 0.18 * lower_preference + 0.15 * size_score
            if best is None or confidence > best[2]:
                best = (cx, cy, confidence)
        return best

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

    def execute(
        self,
        control: WindowsControl,
        hwnd: int,
        action: int,
        stop: Optional[threading.Event] = None,
    ) -> None:
        if action == WAIT_ACTION or (stop is not None and stop.is_set()):
            return
        card = action // 6
        target = action % 6
        control.click_normalized(hwnd, self.CARD_X[card], self.CARD_Y)
        if stop is not None:
            if stop.wait(0.08):
                return
        else:
            time.sleep(0.08)
        tx, ty = self.TARGETS[target]
        if stop is not None and stop.is_set():
            return
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
            if stop.is_set():
                return {"samples": 0, "generation": model.get("generation", 0), "cancelled": True}
            progress("经验池为空：保留当前模型")
            model["updated_utc"] = utc_now()
            model["benchmark"]["status"] = "unverified"
            self.model_store.save(model)
            return {"samples": 0, "generation": model.get("generation", 0), "cancelled": False}

        rng = random.Random(2026 + int(model.get("generation", 0)))
        weights = [[float(value) for value in row] for row in model["weights"]]
        epochs = max(1, min(50, int(self.config.get("training_epochs", 5))))
        learning_rate = max(0.0001, min(0.2, float(self.config.get("training_learning_rate", 0.012))))

        cancelled = False
        for epoch in range(epochs):
            if stop.is_set():
                cancelled = True
                break
            rng.shuffle(samples)
            total_abs_reward = 0.0
            for sample_index, item in enumerate(samples):
                if sample_index % 128 == 0 and stop.is_set():
                    cancelled = True
                    break
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
            if cancelled:
                break
            progress(f"模型训练 {epoch + 1}/{epochs}，有效奖励 {total_abs_reward:.2f}")

        if cancelled or stop.is_set():
            progress("升级已取消，模型保持不变")
            return {
                "samples": 0,
                "generation": model.get("generation", 0),
                "cancelled": True,
            }

        model["weights"] = weights
        model["generation"] = int(model.get("generation", 0)) + 1
        model["updated_utc"] = utc_now()
        model["training_samples"] = int(model.get("training_samples", 0)) + len(samples)
        model["benchmark"] = {
            "status": "unverified",
            "note": (
                "Training completed, but professional-level superiority requires "
                "external controlled benchmarking."
            ),
        }
        self.model_store.save(model)
        self.pool.compact(max_experiences)
        return {"samples": len(samples), "generation": model["generation"], "cancelled": False}


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
        last_lobby_click = 0.0
        low_confidence_frames = 0
        lobby_cooldown = max(4.0, min(30.0, float(self.config.get("lobby_click_cooldown_seconds", 8.0))))

        while not stop.is_set():
            started = time.monotonic()
            current_hwnd = self.control.find_game_window()
            if current_hwnd is None:
                raise RuntimeError("游戏窗口已关闭")
            hwnd = current_hwnd
            rect = self.control.rect(hwnd)
            frame = self.vision.capture(rect)
            analysis = self.vision.analyze(frame)

            if (
                previous_analysis is not None
                and previous_features is not None
                and previous_analysis["battle_confidence"] >= 0.52
                and analysis["battle_confidence"] >= 0.40
            ):
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

            queued = False
            if analysis["battle_confidence"] < 0.52:
                action = WAIT_ACTION
                low_confidence_frames += 1
                now = time.monotonic()
                if (
                    bool(self.config.get("auto_queue_battle", True))
                    and low_confidence_frames >= 3
                    and now - last_lobby_click >= lobby_cooldown
                    and not stop.is_set()
                ):
                    lobby_action = self.vision.find_lobby_action(frame)
                    if lobby_action is not None and lobby_action[2] >= 0.68:
                        self.control.focus(hwnd)
                        self.control.click_normalized(hwnd, lobby_action[0], lobby_action[1])
                        last_lobby_click = now
                        low_confidence_frames = 0
                        queued = True
                        previous_analysis = None
                        previous_features = None
            else:
                low_confidence_frames = 0
                action = self.policy.choose(analysis)
                if not stop.is_set():
                    self.policy.execute(self.control, hwnd, action, stop)

            if not queued:
                previous_analysis = analysis
                previous_features = list(analysis["features"])
                previous_action = action

            now = time.monotonic()
            if now - last_status > 1.8:
                last_status = now
                if queued:
                    action_text = "尝试进入下一场对战"
                else:
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

        self.tk = tk
        self.paths = PATHS
        self.paths.ensure_dirs()
        self.integrity = IntegrityService(self.paths)
        self.config_store = ConfigStore(self.paths.config)
        self.mode = "home"
        self.mode_token = 0
        self.mode_stop = threading.Event()
        self.events: queue.Queue[tuple[str, int, Any]] = queue.Queue()
        self.active_workers = 0

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
            wraplength=840,
            justify="center",
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
                if event == "worker_finished":
                    self.active_workers = max(0, self.active_workers - 1)
                    if self.mode == "home" and self.active_workers == 0:
                        for button in self.buttons:
                            button.configure(state="normal")
                        self.status_var.set("选择模式")
                        self.detail_var.set("ESC 可随时退出当前模式")
                    continue
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
                    self.root.after(1400, lambda t=token: self._auto_home(t))
        except queue.Empty:
            pass
        self.root.after(80, self.process_events)

    def _auto_home(self, token: int) -> None:
        if token == self.mode_token and self.mode != "home":
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
        self.active_workers += 1

        def run() -> None:
            try:
                function()
            except Exception as exc:
                LOG.exception(str(exc))
                self.post("error", token, str(exc))
            finally:
                self.post("worker_finished", token, None)

        try:
            threading.Thread(target=run, name=f"Mode-{token}", daemon=True).start()
        except Exception:
            self.active_workers = max(0, self.active_workers - 1)
            raise

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
            if self.mode_stop.is_set() or result.get("cancelled"):
                return
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
        self.detail_var.set(
            "后台任务正在安全停止…" if self.active_workers else "ESC 可随时退出当前模式"
        )
        for button in self.buttons:
            button.configure(state="disabled" if self.active_workers else "normal")
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
    enable_dpi_awareness()
    try:
        errors = IntegrityService.platform_errors()
        if errors:
            raise RuntimeError("；".join(errors))
        relocate_to_desktop()
        PATHS.ensure_dirs()
        add_vendor_path()
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
