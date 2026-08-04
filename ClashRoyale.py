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
APP_VERSION = "3.0.0"
REQUIRED_PYTHON = (3, 12)
DEPENDENCIES = (
    "Pillow>=10.2,<13",
    "numpy>=1.26,<3",
    "opencv-python-headless>=4.9,<5",
    "onnxruntime>=1.18,<2",
)
MAX_UPDATE_FILE_BYTES = 512 * 1024 * 1024
MAX_LAUNCHER_FILE_BYTES = 8 * 1024 * 1024
MAX_JSON_FILE_BYTES = 64 * 1024 * 1024
MAX_EXPERIENCE_LINE_BYTES = 4 * 1024 * 1024
MAX_EXPERIENCE_ITEMS = 500000
MAX_VISION_MODEL_BYTES = 64 * 1024 * 1024
VENDOR_MARKER_SCHEMA = 3
GRID_COLUMNS = 8
GRID_ROWS = 12
PLACEMENTS_PER_CARD = GRID_COLUMNS * GRID_ROWS
ACTION_COUNT = 4 * PLACEMENTS_PER_CARD + 1
WAIT_ACTION = ACTION_COUNT - 1
STATE_SIZE = 192
HIDDEN_1 = 128
HIDDEN_2 = 64
MAX_UNITS = 20
EXPERIENCE_SCHEMA = 2
MODEL_SCHEMA = 2
VISION_LABEL_SCHEMA = 1
USER_BENCHMARK_SCHEMA = 1
USER_BENCHMARK_MIN_MATCHES = 100
ARENA_MIN_MATCHES_PER_MODEL = 30
MIN_HUMAN_DEMO_ACTIONS = 3000
MIN_HUMAN_DEMO_EPISODES = 20
VALID_RESULTS = {"pending", "win", "loss", "draw", "unknown"}
VALID_SOURCES = {"human_demo", "ai_transition"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_deck_signature(value: str) -> tuple[str, ...]:
    normalized = value.replace(",", "|").replace(";", "|").replace("，", "|").replace("；", "|")
    cards = [part.strip().casefold() for part in normalized.split("|") if part.strip()]
    return tuple(sorted(set(cards)))


def canonical_deck_signature(cards: Any) -> str:
    return "|".join(sorted({str(card).strip().casefold() for card in cards if str(card).strip()}))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_weights_digest(model: dict[str, Any]) -> str:
    network = model.get("network", {})
    payload = json.dumps(network, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def wilson_lower_bound(wins: int, matches: int, z: float = 1.959963984540054) -> float:
    if matches <= 0 or wins < 0 or wins > matches:
        return 0.0
    proportion = wins / matches
    denominator = 1.0 + (z * z) / matches
    centre = proportion + (z * z) / (2.0 * matches)
    margin = z * math.sqrt((proportion * (1.0 - proportion) + (z * z) / (4.0 * matches)) / matches)
    return max(0.0, min(1.0, (centre - margin) / denominator))


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
    exists = path.exists() or path_is_link(path)
    if not exists:
        return
    try:
        if path_is_link(path) or path.is_file():
            path.unlink()
            return
        for child in path.rglob("*"):
            if path_is_link(child):
                continue
            try:
                child.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(path)
    except OSError:
        if strict:
            raise


def ensure_real_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() or path_is_link(path)
    if exists and (path_is_link(path) or not path.is_dir()):
        quarantine = path.parent / f"{path.name}.quarantine-{time.time_ns()}"
        os.replace(path, quarantine)
    path.mkdir(parents=True, exist_ok=True)
    if path_is_link(path) or not path.is_dir():
        raise RuntimeError(f"无法创建安全目录：{path}")


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
    candidate_model: Path
    model_archive: Path
    experiences: Path
    benchmarks: Path
    vision_model: Path
    vision_labels: Path
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
            candidate_model=root / "model" / "candidate.json",
            model_archive=root / "model" / "archive",
            experiences=root / "data" / "experiences.jsonl",
            benchmarks=root / "data" / "user_benchmark.jsonl",
            vision_model=root / "vision" / "detector.onnx",
            vision_labels=root / "vision" / "labels.json",
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
            self.model_archive,
            self.experiences.parent,
            self.vision_model.parent,
            self.vendor,
            self.logs,
            self.temp,
        ):
            ensure_real_directory(path)


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
    "schema": 2,
    "app_version": APP_VERSION,
    "update_manifest_url": "",
    "vision_model_url": "",
    "vision_model_sha256": "",
    "vision_labels_url": "",
    "vision_labels_sha256": "",
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
    "decision_interval_seconds": 0.85,
    "exploration": 0.0,
    "auto_launch_game": True,
    "auto_queue_battle": True,
    "lobby_click_cooldown_seconds": 8.0,
    "training_epochs_behavior_clone": 8,
    "training_epochs_reinforcement": 4,
    "training_learning_rate": 0.0008,
    "training_batch_size": 64,
    "training_validation_fraction": 0.20,
    "min_human_demo_actions": MIN_HUMAN_DEMO_ACTIONS,
    "min_human_demo_episodes": MIN_HUMAN_DEMO_EPISODES,
    "max_experiences": 50000,
    "benchmark_deck_signature": "",
    "benchmark_cup_band": "",
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

        for key in (
            "update_manifest_url",
            "vision_model_url",
            "vision_labels_url",
        ):
            raw = value.get(key, defaults[key])
            if isinstance(raw, str):
                raw = raw.strip()
                if not raw or raw.lower().startswith("https://"):
                    result[key] = raw
        for key in ("vision_model_sha256", "vision_labels_sha256"):
            raw = value.get(key, defaults[key])
            if isinstance(raw, str):
                raw = raw.strip().lower()
                if not raw or (len(raw) == 64 and all(ch in "0123456789abcdef" for ch in raw)):
                    result[key] = raw
        for key in ("benchmark_deck_signature", "benchmark_cup_band"):
            raw = value.get(key, defaults[key])
            if isinstance(raw, str):
                result[key] = raw.strip()[:256]

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
            "decision_interval_seconds": (0.35, 3.0, float),
            "exploration": (0.0, 0.20, float),
            "lobby_click_cooldown_seconds": (4.0, 30.0, float),
            "training_epochs_behavior_clone": (1.0, 50.0, int),
            "training_epochs_reinforcement": (0.0, 50.0, int),
            "training_learning_rate": (0.00001, 0.02, float),
            "training_batch_size": (16.0, 512.0, int),
            "training_validation_fraction": (0.10, 0.35, float),
            "min_human_demo_actions": (1000.0, 100000.0, int),
            "min_human_demo_episodes": (5.0, 1000.0, int),
            "max_experiences": (5000.0, 500000.0, int),
        }
        for key, (low, high, output_type) in numeric_limits.items():
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                continue
            result[key] = output_type(max(low, min(high, float(raw))))

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
    def __init__(self, path: Path, candidate_path: Optional[Path] = None, archive_dir: Optional[Path] = None) -> None:
        self.path = path
        self.candidate_path = candidate_path or path.with_name("candidate.json")
        self.archive_dir = archive_dir or path.parent / "archive"
        self._lock = threading.RLock()

    @staticmethod
    def _matrix(rows: int, columns: int, rng: random.Random, scale: float) -> list[list[float]]:
        return [[rng.uniform(-scale, scale) for _ in range(columns)] for _ in range(rows)]

    @classmethod
    def default_model(cls) -> dict[str, Any]:
        rng = random.Random(20260803)
        scale1 = math.sqrt(2.0 / STATE_SIZE)
        scale2 = math.sqrt(2.0 / HIDDEN_1)
        scale_policy = math.sqrt(2.0 / HIDDEN_2)
        return {
            "schema": MODEL_SCHEMA,
            "app_version": APP_VERSION,
            "generation": 0,
            "created_utc": utc_now(),
            "updated_utc": utc_now(),
            "state_size": STATE_SIZE,
            "action_count": ACTION_COUNT,
            "architecture": [STATE_SIZE, HIDDEN_1, HIDDEN_2, ACTION_COUNT, 1],
            "network": {
                "w1": cls._matrix(HIDDEN_1, STATE_SIZE, rng, scale1),
                "b1": [0.0] * HIDDEN_1,
                "w2": cls._matrix(HIDDEN_2, HIDDEN_1, rng, scale2),
                "b2": [0.0] * HIDDEN_2,
                "policy_w": cls._matrix(ACTION_COUNT, HIDDEN_2, rng, scale_policy),
                "policy_b": [0.0] * ACTION_COUNT,
                "value_w": [rng.uniform(-scale_policy, scale_policy) for _ in range(HIDDEN_2)],
                "value_b": 0.0,
            },
            "training": {
                "human_demo_samples": 0,
                "ai_transition_samples": 0,
                "train_episode_count": 0,
                "validation_episode_count": 0,
            },
            "evaluation": {
                "status": "official",
                "note": "初始策略价值网络；尚未完成用户基准评测。",
            },
        }

    @staticmethod
    def _finite_vector(value: Any, length: int) -> bool:
        return (
            isinstance(value, list)
            and len(value) == length
            and all(not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(float(item)) for item in value)
        )

    @classmethod
    def _finite_matrix(cls, value: Any, rows: int, columns: int) -> bool:
        return isinstance(value, list) and len(value) == rows and all(cls._finite_vector(row, columns) for row in value)

    @classmethod
    def valid(cls, model: Any) -> bool:
        try:
            if not isinstance(model, dict):
                return False
            if model.get("schema") != MODEL_SCHEMA or model.get("state_size") != STATE_SIZE or model.get("action_count") != ACTION_COUNT:
                return False
            if model.get("architecture") != [STATE_SIZE, HIDDEN_1, HIDDEN_2, ACTION_COUNT, 1]:
                return False
            network = model.get("network")
            return (
                isinstance(network, dict)
                and cls._finite_matrix(network.get("w1"), HIDDEN_1, STATE_SIZE)
                and cls._finite_vector(network.get("b1"), HIDDEN_1)
                and cls._finite_matrix(network.get("w2"), HIDDEN_2, HIDDEN_1)
                and cls._finite_vector(network.get("b2"), HIDDEN_2)
                and cls._finite_matrix(network.get("policy_w"), ACTION_COUNT, HIDDEN_2)
                and cls._finite_vector(network.get("policy_b"), ACTION_COUNT)
                and cls._finite_vector(network.get("value_w"), HIDDEN_2)
                and not isinstance(network.get("value_b"), bool)
                and isinstance(network.get("value_b"), (int, float))
                and math.isfinite(float(network.get("value_b")))
            )
        except Exception:
            return False

    @classmethod
    def normalize(cls, model: Any) -> Optional[dict[str, Any]]:
        if not cls.valid(model):
            return None
        result = copy.deepcopy(model)
        result["app_version"] = APP_VERSION
        result["generation"] = max(0, int(result.get("generation", 0)))
        for key in ("created_utc", "updated_utc"):
            if not isinstance(result.get(key), str) or not result[key].strip():
                result[key] = utc_now()
        for key, value in result["network"].items():
            if isinstance(value, list) and value and isinstance(value[0], list):
                result["network"][key] = [[max(-12.0, min(12.0, float(x))) for x in row] for row in value]
            elif isinstance(value, list):
                result["network"][key] = [max(-12.0, min(12.0, float(x))) for x in value]
            else:
                result["network"][key] = max(-12.0, min(12.0, float(value)))
        if not isinstance(result.get("training"), dict):
            result["training"] = {}
        if not isinstance(result.get("evaluation"), dict):
            result["evaluation"] = {"status": "unverified"}
        return result

    def _load_path(self, path: Path, create_default: bool = False) -> Optional[dict[str, Any]]:
        try:
            value = read_json_file(path)
            normalized = self.normalize(value)
            if normalized is not None:
                if normalized != value:
                    atomic_write_json(path, normalized)
                return normalized
        except Exception:
            pass
        if create_default:
            value = self.default_model()
            atomic_write_json(path, value)
            return value
        return None

    def load(self) -> dict[str, Any]:
        with self._lock:
            result = self._load_path(self.path, create_default=True)
            assert result is not None
            return result

    def load_candidate(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._load_path(self.candidate_path, create_default=False) if self.candidate_path.exists() else None

    def repair(self) -> bool:
        with self._lock:
            if self._load_path(self.path, create_default=False) is None:
                atomic_write_json(self.path, self.default_model())
                return True
            if self.candidate_path.exists() and self._load_path(self.candidate_path, create_default=False) is None:
                quarantine = self.candidate_path.with_name(f"candidate.invalid-{time.time_ns()}.json")
                os.replace(self.candidate_path, quarantine)
                return True
            return False

    def save(self, model: dict[str, Any], candidate: bool = False) -> None:
        normalized = self.normalize(model)
        if normalized is None:
            raise ValueError("拒绝保存无效的策略价值网络")
        with self._lock:
            atomic_write_json(self.candidate_path if candidate else self.path, normalized)

    def promote_candidate(self, benchmark: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            candidate = self.load_candidate()
            if candidate is None:
                raise RuntimeError("没有可晋升的候选模型")
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            active = self.load()
            archive = self.archive_dir / f"policy-g{int(active.get('generation', 0)):06d}-{time.time_ns()}.json"
            atomic_write_json(archive, active)
            candidate["evaluation"] = {
                "status": "official",
                "promoted_utc": utc_now(),
                "user_benchmark": benchmark,
            }
            candidate["updated_utc"] = utc_now()
            self.save(candidate, candidate=False)
            try:
                self.candidate_path.unlink()
            except FileNotFoundError:
                pass
            return candidate


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

    @staticmethod
    def _finite_state(value: Any) -> bool:
        return (
            isinstance(value, list)
            and len(value) == STATE_SIZE
            and all(not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(float(x)) for x in value)
        )

    @classmethod
    def valid_item(cls, item: Any) -> bool:
        if not isinstance(item, dict) or item.get("schema") != EXPERIENCE_SCHEMA:
            return False
        episode_id = item.get("episode_id")
        legal_actions = item.get("legal_actions")
        action = item.get("action")
        reward = item.get("reward")
        result = item.get("result")
        source = item.get("source")
        return (
            isinstance(episode_id, str) and 1 <= len(episode_id.strip()) <= 160
            and cls._finite_state(item.get("state"))
            and isinstance(legal_actions, list) and bool(legal_actions)
            and len(legal_actions) <= ACTION_COUNT
            and all(not isinstance(x, bool) and isinstance(x, int) and 0 <= x < ACTION_COUNT for x in legal_actions)
            and len(set(legal_actions)) == len(legal_actions)
            and not isinstance(action, bool) and isinstance(action, int) and action in legal_actions
            and not isinstance(reward, bool) and isinstance(reward, (int, float)) and math.isfinite(float(reward))
            and cls._finite_state(item.get("next_state"))
            and isinstance(item.get("done"), bool)
            and result in VALID_RESULTS
            and source in VALID_SOURCES
        )

    @staticmethod
    def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": EXPERIENCE_SCHEMA,
            "ts": str(item.get("ts") or utc_now()),
            "episode_id": str(item["episode_id"]).strip(),
            "step_id": max(0, int(item.get("step_id", 0))),
            "state": [round(max(-8.0, min(8.0, float(x))), 6) for x in item["state"]],
            "legal_actions": sorted(set(int(x) for x in item["legal_actions"])),
            "action": int(item["action"]),
            "reward": round(max(-2.0, min(2.0, float(item["reward"]))), 6),
            "next_state": [round(max(-8.0, min(8.0, float(x))), 6) for x in item["next_state"]],
            "done": bool(item["done"]),
            "result": str(item["result"]),
            "source": str(item["source"]),
            "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
        }

    def ensure(self, max_items: int = MAX_EXPERIENCE_ITEMS) -> bool:
        max_items = max(1, min(MAX_EXPERIENCE_ITEMS, int(max_items)))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.touch()
                return True
            good: deque[str] = deque(maxlen=max_items)
            rejected: list[str] = []
            damaged = False
            for line in self._lines():
                if line is None or not line.strip():
                    if line is None:
                        damaged = True
                    continue
                try:
                    item = strict_json_loads(line)
                    if self.valid_item(item):
                        normalized = self.normalize_item(item)
                        good.append(json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
                    else:
                        rejected.append(line.rstrip("\n"))
                        damaged = True
                except Exception:
                    rejected.append(line.rstrip("\n"))
                    damaged = True
            if damaged:
                if rejected:
                    reject_path = self.path.with_name(f"experiences.rejected-{time.time_ns()}.jsonl")
                    atomic_write_text(reject_path, "\n".join(rejected) + "\n")
                atomic_write_text(self.path, "".join(line + "\n" for line in good))
            return damaged

    def append_transition(self, item: dict[str, Any]) -> None:
        if not self.valid_item(item):
            raise ValueError("拒绝写入不完整或无效的状态转换")
        normalized = self.normalize_item(item)
        line = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()

    def append_many(self, items: list[dict[str, Any]]) -> int:
        lines: list[str] = []
        for item in items:
            if not self.valid_item(item):
                raise ValueError("拒绝批量写入无效状态转换")
            lines.append(json.dumps(self.normalize_item(item), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
        if not lines:
            return 0
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.writelines(lines)
                handle.flush()
                os.fsync(handle.fileno())
        return len(lines)

    def load(self, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists() or limit <= 0:
            return []
        result: deque[dict[str, Any]] = deque(maxlen=limit)
        with self._lock:
            try:
                for line in self._lines():
                    if line is None or not line.strip():
                        continue
                    try:
                        item = strict_json_loads(line)
                    except Exception:
                        continue
                    if self.valid_item(item):
                        result.append(self.normalize_item(item))
            except OSError:
                return []
        return list(result)

    def count(self, source: Optional[str] = None, limit: int = MAX_EXPERIENCE_ITEMS + 1) -> int:
        count = 0
        for item in self.load(limit):
            if source is None or item.get("source") == source:
                count += 1
        return count

    def episode_counts(self, source: Optional[str] = None, limit: int = MAX_EXPERIENCE_ITEMS) -> tuple[int, int]:
        items = self.load(limit)
        filtered = [item for item in items if source is None or item.get("source") == source]
        return len(filtered), len({item["episode_id"] for item in filtered})

    def split_by_episode(self, validation_fraction: float, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        items = self.load(limit)
        episodes: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            episodes.setdefault(item["episode_id"], []).append(item)
        ordered = sorted(episodes.items(), key=lambda pair: min(str(x.get("ts", "")) for x in pair[1]))
        if len(ordered) < 2:
            return items, []
        validation_count = max(1, min(len(ordered) - 1, int(math.ceil(len(ordered) * validation_fraction))))
        validation_ids = {episode_id for episode_id, _ in ordered[-validation_count:]}
        train = [item for item in items if item["episode_id"] not in validation_ids]
        validation = [item for item in items if item["episode_id"] in validation_ids]
        assert not ({item["episode_id"] for item in train} & {item["episode_id"] for item in validation})
        return train, validation

    def compact(self, max_items: int) -> bool:
        max_items = max(1, min(MAX_EXPERIENCE_ITEMS, int(max_items)))
        items = self.load(max_items + 1)
        if len(items) <= max_items:
            return False
        kept = items[-max_items:]
        if kept and int(kept[0].get("step_id", 0)) != 0:
            first_episode = kept[0]["episode_id"]
            kept = [item for item in kept if item["episode_id"] != first_episode]
        atomic_write_text(
            self.path,
            "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for item in kept),
        )
        return True


class BenchmarkStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    @staticmethod
    def valid_record(record: Any) -> bool:
        if not isinstance(record, dict) or record.get("schema") != USER_BENCHMARK_SCHEMA:
            return False
        return (
            isinstance(record.get("episode_id"), str)
            and record.get("actor") in {"human", "ai"}
            and isinstance(record.get("model_sha256"), str)
            and isinstance(record.get("deck_signature"), str)
            and isinstance(record.get("cup_band"), str)
            and isinstance(record.get("valid"), bool)
            and record.get("result") in {"win", "loss", "draw", "unknown"}
            and all(not isinstance(record.get(key), bool) and isinstance(record.get(key), (int, float)) and math.isfinite(float(record.get(key))) for key in ("crown_diff", "tower_hp_diff"))
        )

    def ensure(self) -> bool:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.touch()
                return True
            valid_lines: list[str] = []
            rejected: list[str] = []
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            item = strict_json_loads(line)
                        except Exception:
                            rejected.append(line.rstrip("\n"))
                            continue
                        if self.valid_record(item):
                            valid_lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
                        else:
                            rejected.append(line.rstrip("\n"))
            except OSError:
                rejected.append("无法读取原基准文件")
            if not rejected:
                return False
            reject_path = self.path.with_name(f"user_benchmark.rejected-{time.time_ns()}.jsonl")
            atomic_write_text(reject_path, "\n".join(rejected) + "\n")
            atomic_write_text(self.path, "".join(line + "\n" for line in valid_lines))
            return True

    def append(self, record: dict[str, Any]) -> None:
        candidate = {
            "schema": USER_BENCHMARK_SCHEMA,
            "ts": str(record.get("ts") or utc_now()),
            "episode_id": str(record.get("episode_id", "")),
            "actor": str(record.get("actor", "")),
            "model_sha256": str(record.get("model_sha256", "")),
            "model_role": str(record.get("model_role", "none")),
            "deck_signature": str(record.get("deck_signature", "")),
            "cup_band": str(record.get("cup_band", "")),
            "valid": bool(record.get("valid", False)),
            "exclusion_reason": str(record.get("exclusion_reason", "")),
            "result": str(record.get("result", "unknown")),
            "crown_diff": float(record.get("crown_diff", 0.0)),
            "tower_hp_diff": float(record.get("tower_hp_diff", 0.0)),
            "vision_failure": bool(record.get("vision_failure", False)),
            "disconnected": bool(record.get("disconnected", False)),
        }
        if not self.valid_record(candidate):
            raise ValueError("拒绝写入无效用户基准记录")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result: list[dict[str, Any]] = []
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = strict_json_loads(line)
                        except Exception:
                            continue
                        if self.valid_record(item):
                            result.append(item)
            except OSError:
                pass
        return result

    def evaluate_user_benchmark(self, candidate_digest: str, deck_signature: str, cup_band: str) -> dict[str, Any]:
        records = [
            item for item in self.load()
            if item["valid"] and not item["vision_failure"] and not item["disconnected"]
            and item["deck_signature"] == deck_signature and item["cup_band"] == cup_band
        ]
        human = [item for item in records if item["actor"] == "human"]
        ai = [item for item in records if item["actor"] == "ai" and item["model_sha256"] == candidate_digest]
        human_wins = sum(item["result"] == "win" for item in human)
        ai_wins = sum(item["result"] == "win" for item in ai)
        user_win_rate = human_wins / len(human) if human else 0.0
        lower = wilson_lower_bound(ai_wins, len(ai))
        matches = len(ai)
        candidate_passed = (
            matches >= USER_BENCHMARK_MIN_MATCHES
            and wilson_lower_bound(ai_wins, matches) > user_win_rate
        )
        passed = len(human) >= USER_BENCHMARK_MIN_MATCHES and candidate_passed
        def average(rows: list[dict[str, Any]], key: str) -> float:
            return sum(float(row[key]) for row in rows) / len(rows) if rows else 0.0
        return {
            "schema": USER_BENCHMARK_SCHEMA,
            "deck_signature": deck_signature,
            "cup_band": cup_band,
            "human_matches": len(human),
            "human_wins": human_wins,
            "user_win_rate": round(user_win_rate, 6),
            "ai_matches": len(ai),
            "ai_wins": ai_wins,
            "ai_win_rate": round(ai_wins / len(ai), 6) if ai else 0.0,
            "ai_wilson_lower_bound": round(lower, 6),
            "human_average_crown_diff": round(average(human, "crown_diff"), 6),
            "ai_average_crown_diff": round(average(ai, "crown_diff"), 6),
            "human_average_tower_hp_diff": round(average(human, "tower_hp_diff"), 6),
            "ai_average_tower_hp_diff": round(average(ai, "tower_hp_diff"), 6),
            "candidate_passed": passed,
        }

    def evaluate_candidate_arena(self, candidate_digest: str, official_digest: str, deck_signature: str, cup_band: str) -> dict[str, Any]:
        records = [
            item for item in self.load()
            if item["valid"] and item["actor"] == "ai" and item["deck_signature"] == deck_signature and item["cup_band"] == cup_band
        ]
        candidate = [item for item in records if item["model_sha256"] == candidate_digest]
        official = [item for item in records if item["model_sha256"] == official_digest]
        candidate_wins = sum(item["result"] == "win" for item in candidate)
        official_wins = sum(item["result"] == "win" for item in official)
        official_rate = official_wins / len(official) if official else 1.0
        candidate_lower = wilson_lower_bound(candidate_wins, len(candidate))
        return {
            "candidate_matches": len(candidate),
            "official_matches": len(official),
            "candidate_win_rate": round(candidate_wins / len(candidate), 6) if candidate else 0.0,
            "official_win_rate": round(official_rate, 6),
            "candidate_wilson_lower_bound": round(candidate_lower, 6),
            "candidate_passed": (
                len(candidate) >= ARENA_MIN_MATCHES_PER_MODEL
                and len(official) >= ARENA_MIN_MATCHES_PER_MODEL
                and candidate_lower > official_rate
            ),
        }


class UserBenchmark:
    """Evaluate a candidate against the user's real-match baseline."""

    @staticmethod
    def evaluate(store: BenchmarkStore, candidate_digest: str, deck_signature: str, cup_band: str) -> dict[str, Any]:
        return store.evaluate_user_benchmark(candidate_digest, deck_signature, cup_band)


class CandidateArena:
    """Compare candidate and official models on alternating real matchmaking episodes."""

    @staticmethod
    def evaluate(
        store: BenchmarkStore,
        candidate_digest: str,
        official_digest: str,
        deck_signature: str,
        cup_band: str,
    ) -> dict[str, Any]:
        return store.evaluate_candidate_arena(candidate_digest, official_digest, deck_signature, cup_band)


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
import PIL, numpy, cv2, onnxruntime
mods = {"PIL": PIL, "numpy": numpy, "cv2": cv2, "onnxruntime": onnxruntime}
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


class VisionAssetManager:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @staticmethod
    def _valid_sha(value: str) -> bool:
        return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)

    @staticmethod
    def _download(url: str, target: Path, expected_sha: str, max_bytes: int, progress: Callable[[str], None], stop: threading.Event) -> None:
        if not url.lower().startswith("https://"):
            raise RuntimeError(f"下载地址必须使用 HTTPS：{target.name}")
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".download", dir=str(target.parent))
        os.close(fd)
        temp = Path(temp_name)
        try:
            total = 0
            with urllib.request.urlopen(request, timeout=45) as response, temp.open("wb") as handle:
                final_url = str(response.geturl())
                if not final_url.lower().startswith("https://"):
                    raise RuntimeError("视觉文件下载被重定向到非 HTTPS 地址")
                while True:
                    if stop.is_set():
                        raise RuntimeError("视觉文件下载已取消")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(f"视觉文件过大：{target.name}")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if sha256_file(temp) != expected_sha:
                raise RuntimeError(f"视觉文件 SHA-256 校验失败：{target.name}")
            os.replace(temp, target)
            progress(f"已下载并校验 {target.name}")
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _labels_valid(path: Path) -> bool:
        try:
            data = read_json_file(path, 4 * 1024 * 1024)
            if not isinstance(data, dict) or data.get("schema") != VISION_LABEL_SCHEMA:
                return False
            classes = data.get("classes")
            if not isinstance(classes, dict) or not classes:
                return False
            allowed = {"card", "unit", "tower", "timer", "status", "screen", "elixir"}
            kinds: list[str] = []
            teams: set[str] = set()
            towers: set[str] = set()
            statuses: set[str] = set()
            card_count = 0
            for key, item in classes.items():
                int(key)
                if not isinstance(item, dict) or item.get("kind") not in allowed:
                    return False
                if not isinstance(item.get("name"), str) or not item["name"].strip():
                    return False
                kind = str(item["kind"]).casefold()
                kinds.append(kind)
                if kind == "card":
                    card_count += 1
                elif kind == "unit":
                    teams.add(str(item.get("team", "")).casefold())
                elif kind == "tower":
                    towers.add(str(item.get("tower", "")).casefold())
                elif kind in {"status", "screen"}:
                    statuses.add(str(item.get("status", item.get("name", ""))).casefold())
            required_towers = {"friendly_left", "friendly_right", "friendly_king", "enemy_left", "enemy_right", "enemy_king"}
            friendly_team = bool(teams & {"friendly", "ally", "blue", "self"})
            enemy_team = bool(teams & {"enemy", "opponent", "red"})
            return (
                card_count >= 4
                and friendly_team and enemy_team
                and required_towers.issubset(towers)
                and "timer" in kinds
                and {"double_elixir", "overtime", "win", "loss", "settlement"}.issubset(statuses)
            )
        except Exception:
            return False

    def _model_valid(self, path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_VISION_MODEL_BYTES:
            return False
        try:
            add_vendor_path()
            import numpy as np
            import onnxruntime as ort
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            inputs = session.get_inputs()
            outputs = session.get_outputs()
            if len(inputs) != 1 or not outputs or "float" not in str(inputs[0].type).casefold():
                return False
            shape = list(inputs[0].shape)
            if len(shape) != 4:
                return False
            concrete = [1, 3, 384, 640]
            for index, value in enumerate(shape):
                if isinstance(value, int) and value > 0:
                    concrete[index] = value
            if concrete[0] != 1 or concrete[1] != 3:
                return False
            result = session.run([outputs[0].name], {inputs[0].name: np.zeros(concrete, dtype=np.float32)})[0]
            array = np.asarray(result)
            while array.ndim > 2 and array.shape[0] == 1:
                array = array[0]
            return array.ndim == 2 and 6 <= array.shape[1] <= 16
        except Exception:
            return False

    def ensure(self, config: dict[str, Any], progress: Callable[[str], None], stop: threading.Event) -> list[str]:
        repaired: list[str] = []
        self.paths.vision_model.parent.mkdir(parents=True, exist_ok=True)
        specs = (
            ("vision_model_url", "vision_model_sha256", self.paths.vision_model, MAX_VISION_MODEL_BYTES),
            ("vision_labels_url", "vision_labels_sha256", self.paths.vision_labels, 4 * 1024 * 1024),
        )
        for url_key, sha_key, target, limit in specs:
            expected = str(config.get(sha_key, "")).strip().lower()
            current_ok = target.is_file() and (not expected or sha256_file(target) == expected)
            if target == self.paths.vision_model:
                current_ok = current_ok and self._model_valid(target)
            else:
                current_ok = current_ok and self._labels_valid(target)
            if current_ok:
                continue
            url = str(config.get(url_key, "")).strip()
            if not url or not self._valid_sha(expected):
                raise RuntimeError(
                    f"缺少或损坏 {target.name}；请通过 update_manifest_url 下发该文件，"
                    f"或在 config.json 配置 {url_key} 与 {sha_key}"
                )
            progress(f"下载视觉文件：{target.name}")
            self._download(url, target, expected, limit, progress, stop)
            repaired.append(str(target.relative_to(self.paths.root)).replace("\\", "/"))
        if not self._model_valid(self.paths.vision_model):
            raise RuntimeError("ONNX 视觉模型无法由 onnxruntime 加载")
        if not self._labels_valid(self.paths.vision_labels):
            raise RuntimeError("视觉标签文件格式无效")
        return repaired


class IntegrityService:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.config_store = ConfigStore(paths.config)
        self.model_store = ModelStore(paths.model, paths.candidate_model, paths.model_archive)
        self.pool = ExperiencePool(paths.experiences)
        self.benchmarks = BenchmarkStore(paths.benchmarks)
        self.dependencies = DependencyManager(paths)
        self.remote = RemoteUpdater(paths)
        self.vision_assets = VisionAssetManager(paths)

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
        if sys.version_info[:2] != REQUIRED_PYTHON:
            errors.append(
                "需要 Python 3.12.x；当前为 "
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
            self.paths.candidate_model,
            self.paths.experiences,
            self.paths.benchmarks,
            self.paths.vision_model,
            self.paths.vision_labels,
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
        progress("检查 UserBenchmark 记录…")
        if self.benchmarks.ensure():
            repaired.append("data/user_benchmark.jsonl")

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
            if self.benchmarks.ensure():
                repaired.append("远程 UserBenchmark 记录无效，已修复")
            if not launcher_source_valid(self.paths.launcher):
                raise RuntimeError("远程更新后的 ClashRoyale.py 未通过结构验证")
            self.dependencies.install(progress, stop, deep_check=True)

        progress("检查 ONNX 视觉模型与标签…")
        for repaired_path in self.vision_assets.ensure(config, progress, stop):
            repaired.append(repaired_path)

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
        self.benchmarks.ensure()
        self.dependencies.install(progress, stop, deep_check=False)
        self.vision_assets.ensure(config, progress, stop)
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
    """ONNX-based semantic detector with a fixed state-vector contract."""

    CARD_X = (0.285, 0.43, 0.57, 0.715)
    CARD_Y = 0.91
    ARENA_LEFT = 0.06
    ARENA_RIGHT = 0.94
    ARENA_TOP = 0.10
    ARENA_BOTTOM = 0.82

    def __init__(self, config: Optional[dict[str, Any]] = None, paths: AppPaths = PATHS) -> None:
        add_vendor_path()
        from PIL import ImageGrab
        import cv2
        import numpy as np
        import onnxruntime as ort

        self.config = config or new_default_config()
        self.paths = paths
        self.ImageGrab = ImageGrab
        self.cv2 = cv2
        self.np = np
        self.ort = ort
        labels = read_json_file(paths.vision_labels, 4 * 1024 * 1024)
        if not isinstance(labels, dict) or labels.get("schema") != VISION_LABEL_SCHEMA or not isinstance(labels.get("classes"), dict):
            raise RuntimeError("视觉标签文件格式无效")
        self.labels = {int(key): value for key, value in labels["classes"].items() if isinstance(value, dict)}
        self.session = ort.InferenceSession(str(paths.vision_model), providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        if not inputs:
            raise RuntimeError("ONNX 视觉模型没有输入")
        self.input_name = inputs[0].name
        shape = list(inputs[0].shape)
        self.input_height = int(shape[-2]) if len(shape) >= 4 and isinstance(shape[-2], int) and shape[-2] > 0 else 384
        self.input_width = int(shape[-1]) if len(shape) >= 4 and isinstance(shape[-1], int) and shape[-1] > 0 else 640
        self.output_names = [item.name for item in self.session.get_outputs()]
        self._active_episode_id: Optional[str] = None
        self._episode_counter = 0
        self._last_battle_seen = 0.0
        self._reset_after_result = False

    def capture(self, rect: tuple[int, int, int, int]) -> Any:
        image = self.ImageGrab.grab(bbox=rect, all_screens=True)
        return self.np.asarray(image.convert("RGB"))

    def find_lobby_action(self, frame: Any) -> Optional[tuple[float, float, float]]:
        np = self.np
        cv2 = self.cv2
        height, width = frame.shape[:2]
        if height < 200 or width < 200:
            return None
        roi_y1, roi_y2 = int(height * 0.50), int(height * 0.96)
        roi = frame[roi_y1:roi_y2]
        red, green, blue = [roi[:, :, index].astype(np.int16) for index in range(3)]
        yellow = (red > 165) & (green > 105) & (blue < 155) & (red > blue * 1.30) & (green > blue * 1.08)
        cyan_blue = (blue > 145) & (green > 95) & (blue > red * 1.08) & ((blue + green) > 285)
        mask = (yellow | cyan_blue).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 9), np.uint8), iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best: Optional[tuple[float, float, float]] = None
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            wr, hr = w / max(1, width), h / max(1, height)
            cx = (x + w / 2.0) / max(1, width)
            cy = (roi_y1 + y + h / 2.0) / max(1, height)
            fill = float(cv2.contourArea(contour)) / max(1.0, float(w * h))
            if not (0.18 <= wr <= 0.78 and 0.03 <= hr <= 0.20 and 0.24 <= cx <= 0.76 and 0.56 <= cy <= 0.94 and fill >= 0.38):
                continue
            confidence = 0.45 * fill + 0.30 * max(0.0, 1.0 - abs(cx - 0.5) / 0.26) + 0.25 * max(0.0, 1.0 - abs(cy - 0.79) / 0.25)
            if best is None or confidence > best[2]:
                best = (cx, cy, confidence)
        return best

    def _infer(self, frame: Any) -> list[dict[str, Any]]:
        np = self.np
        cv2 = self.cv2
        resized = cv2.resize(frame, (self.input_width, self.input_height), interpolation=cv2.INTER_AREA)
        tensor = resized.astype(np.float32) / 255.0
        tensor = np.transpose(tensor, (2, 0, 1))[None, ...]
        outputs = self.session.run(self.output_names, {self.input_name: tensor})
        if not outputs:
            return []
        rows = np.asarray(outputs[0])
        while rows.ndim > 2 and rows.shape[0] == 1:
            rows = rows[0]
        if rows.ndim != 2 or not (6 <= rows.shape[1] <= 16):
            raise RuntimeError("ONNX 检测输出必须为后处理后的 N×6 至 N×16，字段为 x1,y1,x2,y2,score,class_id[,health,dx,dy,value]")
        detections: list[dict[str, Any]] = []
        for row in rows:
            score = float(row[4])
            if not math.isfinite(score) or score < 0.35:
                continue
            class_id = int(round(float(row[5])))
            label = self.labels.get(class_id)
            if label is None:
                continue
            x1, y1, x2, y2 = [float(row[index]) for index in range(4)]
            if max(abs(x1), abs(x2)) > 2.0:
                x1, x2 = x1 / self.input_width, x2 / self.input_width
            if max(abs(y1), abs(y2)) > 2.0:
                y1, y2 = y1 / self.input_height, y2 / self.input_height
            x1, x2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
            y1, y2 = sorted((max(0.0, min(1.0, y1)), max(0.0, min(1.0, y2))))
            detections.append({
                "class_id": class_id,
                "label": label,
                "score": score,
                "x": (x1 + x2) / 2.0,
                "y": (y1 + y2) / 2.0,
                "w": max(0.0, x2 - x1),
                "h": max(0.0, y2 - y1),
                "health": max(0.0, min(1.0, float(row[6]))) if len(row) > 6 and math.isfinite(float(row[6])) else 1.0,
                "dx": max(-1.0, min(1.0, float(row[7]))) if len(row) > 7 and math.isfinite(float(row[7])) else 0.0,
                "dy": max(-1.0, min(1.0, float(row[8]))) if len(row) > 8 and math.isfinite(float(row[8])) else 0.0,
                "value": float(row[9]) if len(row) > 9 and math.isfinite(float(row[9])) else 0.0,
            })
        return detections

    @staticmethod
    def _stable_id(name: str, explicit: Any) -> int:
        if not isinstance(explicit, bool) and isinstance(explicit, int) and explicit >= 0:
            return explicit
        return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16) % 10000

    def _heuristic_elixir(self, frame: Any) -> float:
        np = self.np
        height, width = frame.shape[:2]
        bar = frame[int(height * 0.86):int(height * 0.93), int(width * 0.10):int(width * 0.90)]
        if not bar.size:
            return 0.0
        r, g, b = [bar[:, :, index].astype(np.int16) for index in range(3)]
        purple = (r > 110) & (b > 125) & (r > g * 1.18) & (b > g * 1.20)
        columns = purple.mean(axis=0) > 0.10
        return max(0.0, min(10.0, float(columns.mean()) * 12.0 if columns.size else 0.0))

    @staticmethod
    def encode_action(card_slot: int, column: int, row: int) -> int:
        if not (0 <= card_slot < 4 and 0 <= column < GRID_COLUMNS and 0 <= row < GRID_ROWS):
            raise ValueError("动作网格坐标越界")
        return card_slot * PLACEMENTS_PER_CARD + row * GRID_COLUMNS + column

    @staticmethod
    def decode_action(action: int) -> tuple[int, int, int]:
        if action == WAIT_ACTION:
            return -1, -1, -1
        card, cell = divmod(action, PLACEMENTS_PER_CARD)
        row, column = divmod(cell, GRID_COLUMNS)
        return card, column, row

    @classmethod
    def grid_point(cls, column: int, row: int) -> tuple[float, float]:
        x = cls.ARENA_LEFT + (column + 0.5) / GRID_COLUMNS * (cls.ARENA_RIGHT - cls.ARENA_LEFT)
        y = cls.ARENA_TOP + (row + 0.5) / GRID_ROWS * (cls.ARENA_BOTTOM - cls.ARENA_TOP)
        return x, y

    @classmethod
    def point_to_cell(cls, x: float, y: float) -> Optional[tuple[int, int]]:
        if not (cls.ARENA_LEFT <= x <= cls.ARENA_RIGHT and cls.ARENA_TOP <= y <= cls.ARENA_BOTTOM):
            return None
        column = min(GRID_COLUMNS - 1, max(0, int((x - cls.ARENA_LEFT) / (cls.ARENA_RIGHT - cls.ARENA_LEFT) * GRID_COLUMNS)))
        row = min(GRID_ROWS - 1, max(0, int((y - cls.ARENA_TOP) / (cls.ARENA_BOTTOM - cls.ARENA_TOP) * GRID_ROWS)))
        return column, row

    def _legal_actions(self, cards: list[Optional[dict[str, Any]]], elixir: float) -> list[int]:
        legal = [WAIT_ACTION]
        fallback_costs = [int(value) for value in self.config.get("card_costs", [3, 4, 4, 5])]
        for slot, card in enumerate(cards):
            if card is None:
                continue
            label = card["label"]
            cost = label.get("cost", fallback_costs[slot] if slot < len(fallback_costs) else 10)
            try:
                cost_value = max(1, min(10, int(cost)))
            except Exception:
                cost_value = fallback_costs[slot]
            if elixir + 0.35 < cost_value:
                continue
            placement = str(label.get("placement", "own")).casefold()
            for row in range(GRID_ROWS):
                if placement in {"own", "building"} and row < GRID_ROWS // 2:
                    continue
                if placement == "bridge" and row not in {GRID_ROWS // 2 - 1, GRID_ROWS // 2}:
                    continue
                for column in range(GRID_COLUMNS):
                    legal.append(self.encode_action(slot, column, row))
        return sorted(set(legal))

    def _state_vector(
        self,
        cards: list[Optional[dict[str, Any]]],
        units: list[dict[str, Any]],
        tower_hp: dict[str, float],
        elixir: float,
        battle_confidence: float,
        match_time: float,
        double_elixir: bool,
        overtime: bool,
        result: str,
        done: bool,
    ) -> list[float]:
        state = [0.0] * STATE_SIZE
        state[0] = 1.0
        state[1] = elixir / 10.0
        state[2] = battle_confidence
        state[3] = max(0.0, min(1.0, match_time / 300.0))
        state[4] = 1.0 if double_elixir else 0.0
        state[5] = 1.0 if overtime else 0.0
        state[6] = 1.0 if done else 0.0
        state[7] = 1.0 if result == "win" else 0.0
        state[8] = 1.0 if result == "loss" else 0.0
        state[9] = 1.0 if result == "draw" else 0.0
        fallback_costs = [int(value) for value in self.config.get("card_costs", [3, 4, 4, 5])]
        for slot in range(4):
            card = cards[slot]
            if card is not None:
                label = card["label"]
                card_id = self._stable_id(str(label.get("name", "card")), label.get("card_id"))
                state[10 + slot] = card_id / 10000.0
                state[14 + slot] = float(card["score"])
                try:
                    cost = int(label.get("cost", fallback_costs[slot]))
                except Exception:
                    cost = fallback_costs[slot]
                state[18 + slot] = max(0.0, min(1.0, cost / 10.0))
        tower_order = ("friendly_left", "friendly_right", "friendly_king", "enemy_left", "enemy_right", "enemy_king")
        for index, key in enumerate(tower_order):
            state[22 + index] = max(0.0, min(1.0, float(tower_hp.get(key, 0.0))))
        friendly_units = sum(unit["team"] > 0 for unit in units)
        enemy_units = sum(unit["team"] < 0 for unit in units)
        state[28] = min(1.0, friendly_units / MAX_UNITS)
        state[29] = min(1.0, enemy_units / MAX_UNITS)
        state[30] = min(1.0, sum(unit["health"] for unit in units if unit["team"] > 0) / MAX_UNITS)
        state[31] = min(1.0, sum(unit["health"] for unit in units if unit["team"] < 0) / MAX_UNITS)
        ranked = sorted(units, key=lambda unit: (unit["score"], unit["health"]), reverse=True)[:MAX_UNITS]
        for index, unit in enumerate(ranked):
            start = 32 + index * 8
            state[start:start + 8] = [
                float(unit["team"]),
                float(unit["unit_id"]) / 10000.0,
                float(unit["x"]),
                float(unit["y"]),
                float(unit["health"]),
                float(unit["dx"]),
                float(unit["dy"]),
                float(unit["score"]),
            ]
        return state

    def analyze(self, frame: Any) -> dict[str, Any]:
        detections = self._infer(frame)
        cards: list[Optional[dict[str, Any]]] = [None, None, None, None]
        units: list[dict[str, Any]] = []
        tower_hp: dict[str, float] = {}
        match_time = 0.0
        double_elixir = False
        overtime = False
        result = "pending"
        elixir = self._heuristic_elixir(frame)

        for det in detections:
            label = det["label"]
            kind = str(label.get("kind", "")).casefold()
            name = str(label.get("name", "unknown"))
            if kind == "card" and det["y"] >= 0.76:
                slot = min(range(4), key=lambda index: abs(self.CARD_X[index] - det["x"]))
                if cards[slot] is None or det["score"] > cards[slot]["score"]:
                    cards[slot] = det
            elif kind == "unit":
                team_text = str(label.get("team", "enemy")).casefold()
                team = 1 if team_text in {"friendly", "ally", "blue", "self"} else -1
                units.append({
                    "name": name,
                    "unit_id": self._stable_id(name, label.get("unit_id")),
                    "team": team,
                    "x": det["x"], "y": det["y"], "health": det["health"],
                    "dx": det["dx"], "dy": det["dy"], "score": det["score"],
                })
            elif kind == "tower":
                tower_key = str(label.get("tower", "")).casefold()
                if not tower_key:
                    team = "friendly" if det["y"] > 0.5 else "enemy"
                    lane = "king" if 0.42 <= det["x"] <= 0.58 else ("left" if det["x"] < 0.5 else "right")
                    tower_key = f"{team}_{lane}"
                tower_hp[tower_key] = max(tower_hp.get(tower_key, 0.0), det["health"])
            elif kind == "timer":
                match_time = max(match_time, max(0.0, det["value"] or det["health"] * float(label.get("seconds_scale", 300.0))))
            elif kind == "elixir":
                elixir = max(0.0, min(10.0, det["value"] if det["value"] else det["health"] * 10.0))
            elif kind in {"status", "screen"}:
                status = str(label.get("status", name)).casefold()
                if status in {"double_elixir", "double", "2x"}:
                    double_elixir = True
                elif status in {"overtime", "extra_time"}:
                    overtime = True
                elif status in {"win", "victory"}:
                    result = "win"
                elif status in {"loss", "defeat"}:
                    result = "loss"
                elif status in {"draw", "tie"}:
                    result = "draw"
                elif status in {"settlement", "results", "result_screen"} and result == "pending":
                    result = "unknown"

        semantic_score = (
            0.34 * min(1.0, sum(card is not None for card in cards) / 4.0)
            + 0.26 * min(1.0, len(tower_hp) / 6.0)
            + 0.22 * min(1.0, len(units) / 4.0)
            + 0.18 * min(1.0, len(detections) / 12.0)
        )
        battle_confidence = max(0.0, min(1.0, semantic_score))
        now = time.monotonic()
        if self._reset_after_result:
            self._active_episode_id = None
            self._reset_after_result = False
        if battle_confidence >= 0.48:
            self._last_battle_seen = now
            if self._active_episode_id is None:
                self._episode_counter += 1
                self._active_episode_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{self._episode_counter:06d}"
        elif self._active_episode_id is not None and now - self._last_battle_seen > 12.0:
            result = "unknown"
        done = result in {"win", "loss", "draw"} or (result == "unknown" and self._active_episode_id is not None)
        episode_id = self._active_episode_id
        if done:
            self._reset_after_result = True
        legal_actions = self._legal_actions(cards, elixir)
        state = self._state_vector(cards, units, tower_hp, elixir, battle_confidence, match_time, double_elixir, overtime, result, done)
        card_names = [str(card["label"].get("name", "")) if card is not None else "" for card in cards]
        return {
            "state": state,
            "legal_actions": legal_actions,
            "cards": cards,
            "card_names": card_names,
            "hand_signature": "|".join(card_names) if all(card_names) else "",
            "units": units,
            "tower_hp": tower_hp,
            "elixir": elixir,
            "match_time": match_time,
            "double_elixir": double_elixir,
            "overtime": overtime,
            "battle_confidence": battle_confidence,
            "vision_ok": battle_confidence >= 0.48,
            "episode_id": episode_id,
            "done": done,
            "result": result,
            "detections": len(detections),
        }


class PolicyValueNetwork:
    def __init__(self, model: dict[str, Any]) -> None:
        if not ModelStore.valid(model):
            raise ValueError("策略价值模型无效")
        add_vendor_path()
        import numpy as np
        self.np = np
        self.model = model
        network = model["network"]
        self.w1 = np.asarray(network["w1"], dtype=np.float32)
        self.b1 = np.asarray(network["b1"], dtype=np.float32)
        self.w2 = np.asarray(network["w2"], dtype=np.float32)
        self.b2 = np.asarray(network["b2"], dtype=np.float32)
        self.policy_w = np.asarray(network["policy_w"], dtype=np.float32)
        self.policy_b = np.asarray(network["policy_b"], dtype=np.float32)
        self.value_w = np.asarray(network["value_w"], dtype=np.float32)
        self.value_b = np.float32(network["value_b"])

    def forward(self, state: list[float]) -> tuple[Any, float]:
        np = self.np
        x = np.asarray(state, dtype=np.float32)
        if x.shape != (STATE_SIZE,):
            raise ValueError("状态向量长度错误")
        h1 = np.maximum(0.0, self.w1 @ x + self.b1)
        h2 = np.maximum(0.0, self.w2 @ h1 + self.b2)
        logits = self.policy_w @ h2 + self.policy_b
        value = float(np.tanh(self.value_w @ h2 + self.value_b))
        return logits, value

    def choose(self, state: list[float], legal_actions: list[int], exploration: float = 0.0, rng: Optional[random.Random] = None) -> tuple[int, float]:
        np = self.np
        scores, value = self.forward(state)
        legal_mask = np.zeros(ACTION_COUNT, dtype=bool)
        legal_mask[np.asarray(legal_actions, dtype=np.int64)] = True
        scores[~legal_mask] = -1e9
        if exploration > 0.0 and (rng or random).random() < exploration:
            ranked = np.argsort(scores)[-min(8, len(legal_actions)):]
            candidates = [int(action) for action in ranked if legal_mask[action]]
            if candidates:
                return int((rng or random).choice(candidates)), value
        return int(np.argmax(scores)), value


class Policy:
    CARD_X = VisionEngine.CARD_X
    CARD_Y = VisionEngine.CARD_Y

    def __init__(self, model: dict[str, Any], config: dict[str, Any]) -> None:
        self.config = config
        self.rng = random.Random()
        self.set_model(model)

    def set_model(self, model: dict[str, Any]) -> None:
        self.model = model
        self.network = PolicyValueNetwork(model)
        self.digest = model_weights_digest(model)

    def choose(self, analysis: dict[str, Any]) -> tuple[int, float]:
        exploration = max(0.0, min(0.20, float(self.config.get("exploration", 0.0))))
        return self.network.choose(analysis["state"], analysis["legal_actions"], exploration, self.rng)

    def execute(self, control: WindowsControl, hwnd: int, action: int, stop: Optional[threading.Event] = None) -> None:
        if action == WAIT_ACTION or (stop is not None and stop.is_set()):
            return
        card, column, row = VisionEngine.decode_action(action)
        control.click_normalized(hwnd, self.CARD_X[card], self.CARD_Y)
        if stop is not None:
            if stop.wait(0.07):
                return
        else:
            time.sleep(0.07)
        x, y = VisionEngine.grid_point(column, row)
        if stop is None or not stop.is_set():
            control.click_normalized(hwnd, x, y)


class NetworkTrainer:
    def __init__(self, model: dict[str, Any], learning_rate: float) -> None:
        add_vendor_path()
        import numpy as np
        self.np = np
        self.model = copy.deepcopy(model)
        self.lr = float(learning_rate)
        network = self.model["network"]
        self.params = {
            "w1": np.asarray(network["w1"], dtype=np.float32),
            "b1": np.asarray(network["b1"], dtype=np.float32),
            "w2": np.asarray(network["w2"], dtype=np.float32),
            "b2": np.asarray(network["b2"], dtype=np.float32),
            "policy_w": np.asarray(network["policy_w"], dtype=np.float32),
            "policy_b": np.asarray(network["policy_b"], dtype=np.float32),
            "value_w": np.asarray(network["value_w"], dtype=np.float32),
            "value_b": np.asarray(network["value_b"], dtype=np.float32),
        }

    @staticmethod
    def _target(item: dict[str, Any]) -> float:
        if item["result"] == "win":
            return 1.0
        if item["result"] == "loss":
            return -1.0
        if item["result"] == "draw":
            return 0.0
        return max(-1.0, min(1.0, float(item["reward"])))

    def train_epoch(self, samples: list[dict[str, Any]], batch_size: int, behavior_clone_only: bool, rng: random.Random, stop: threading.Event) -> dict[str, float]:
        np = self.np
        order = list(range(len(samples)))
        rng.shuffle(order)
        policy_loss_total = 0.0
        value_loss_total = 0.0
        updates = 0
        for offset in range(0, len(order), batch_size):
            if stop.is_set():
                break
            batch = [samples[index] for index in order[offset:offset + batch_size]]
            if behavior_clone_only:
                batch = [item for item in batch if item["source"] == "human_demo"]
            if not batch:
                continue
            x = np.asarray([item["state"] for item in batch], dtype=np.float32)
            actions = np.asarray([item["action"] for item in batch], dtype=np.int64)
            targets = np.asarray([self._target(item) for item in batch], dtype=np.float32)
            legal = np.zeros((len(batch), ACTION_COUNT), dtype=bool)
            for index, item in enumerate(batch):
                legal[index, np.asarray(item["legal_actions"], dtype=np.int64)] = True

            p = self.params
            z1 = x @ p["w1"].T + p["b1"]
            h1 = np.maximum(0.0, z1)
            z2 = h1 @ p["w2"].T + p["b2"]
            h2 = np.maximum(0.0, z2)
            logits = h2 @ p["policy_w"].T + p["policy_b"]
            logits[~legal] = -1e9
            shifted = logits - logits.max(axis=1, keepdims=True)
            exp = np.exp(shifted)
            exp[~legal] = 0.0
            probs = exp / np.maximum(1e-12, exp.sum(axis=1, keepdims=True))
            raw_value = h2 @ p["value_w"] + p["value_b"]
            values = np.tanh(raw_value)

            selected = np.maximum(1e-12, probs[np.arange(len(batch)), actions])
            sources = [item["source"] for item in batch]
            coeff = np.asarray([
                2.0 if source == "human_demo" else float(max(-1.0, min(1.0, targets[index] - values[index])))
                for index, source in enumerate(sources)
            ], dtype=np.float32)
            policy_loss_total += float((-coeff * np.log(selected)).sum())
            value_loss_total += float(((values - targets) ** 2).sum())

            dlogits = probs.copy()
            dlogits[np.arange(len(batch)), actions] -= 1.0
            dlogits *= coeff[:, None] / len(batch)
            dvalue = (values - targets) * (1.0 - values * values) / len(batch)

            grads: dict[str, Any] = {}
            grads["policy_w"] = dlogits.T @ h2
            grads["policy_b"] = dlogits.sum(axis=0)
            grads["value_w"] = h2.T @ dvalue
            grads["value_b"] = dvalue.sum()
            dh2 = dlogits @ p["policy_w"] + dvalue[:, None] * p["value_w"][None, :]
            dz2 = dh2 * (z2 > 0.0)
            grads["w2"] = dz2.T @ h1
            grads["b2"] = dz2.sum(axis=0)
            dh1 = dz2 @ p["w2"]
            dz1 = dh1 * (z1 > 0.0)
            grads["w1"] = dz1.T @ x
            grads["b1"] = dz1.sum(axis=0)

            norm = math.sqrt(sum(float((gradient * gradient).sum()) for gradient in grads.values()))
            scale = min(1.0, 5.0 / max(1e-9, norm))
            for key, gradient in grads.items():
                p[key] -= self.lr * scale * gradient
                p[key] = np.clip(p[key], -12.0, 12.0)
            updates += 1
        return {
            "updates": float(updates),
            "policy_loss": policy_loss_total / max(1, len(samples)),
            "value_loss": value_loss_total / max(1, len(samples)),
        }

    def export(self) -> dict[str, Any]:
        for key, value in self.params.items():
            self.model["network"][key] = value.tolist() if hasattr(value, "tolist") else float(value)
        return self.model


class PolicyEvaluator:
    @staticmethod
    def evaluate(model: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
        network = PolicyValueNetwork(model)
        human = [item for item in samples if item["source"] == "human_demo"]
        correct = 0
        value_error = 0.0
        for item in samples:
            action, value = network.choose(item["state"], item["legal_actions"])
            if item["source"] == "human_demo" and action == item["action"]:
                correct += 1
            target = NetworkTrainer._target(item)
            value_error += abs(value - target)
        return {
            "samples": len(samples),
            "human_samples": len(human),
            "human_action_accuracy": round(correct / len(human), 6) if human else 0.0,
            "value_mae": round(value_error / len(samples), 6) if samples else 1.0,
        }


class PromotionGate:
    @staticmethod
    def try_promote(model_store: ModelStore, benchmark_store: BenchmarkStore, config: dict[str, Any]) -> Optional[dict[str, Any]]:
        candidate = model_store.load_candidate()
        if candidate is None:
            return None
        deck = canonical_deck_signature(parse_deck_signature(str(config.get("benchmark_deck_signature", ""))))
        cup = str(config.get("benchmark_cup_band", "")).strip()
        if not deck or not cup:
            return None
        official = model_store.load()
        candidate_digest = model_weights_digest(candidate)
        official_digest = model_weights_digest(official)
        arena = CandidateArena.evaluate(benchmark_store, candidate_digest, official_digest, deck, cup)
        user = UserBenchmark.evaluate(benchmark_store, candidate_digest, deck, cup)
        if arena["candidate_passed"] and user["candidate_passed"]:
            evidence = {"arena": arena, "user": user, "evaluated_utc": utc_now()}
            model_store.promote_candidate(evidence)
            return evidence
        return {"arena": arena, "user": user, "promoted": False}


class Trainer:
    def __init__(self, model_store: ModelStore, pool: ExperiencePool, config: dict[str, Any]) -> None:
        self.model_store = model_store
        self.pool = pool
        self.config = config

    def run(self, progress: Callable[[str], None], stop: threading.Event) -> dict[str, Any]:
        max_experiences = int(self.config.get("max_experiences", 50000))
        human_actions, human_episodes = self.pool.episode_counts("human_demo", max_experiences)
        min_actions = int(self.config.get("min_human_demo_actions", MIN_HUMAN_DEMO_ACTIONS))
        min_episodes = int(self.config.get("min_human_demo_episodes", MIN_HUMAN_DEMO_EPISODES))
        official = self.model_store.load()
        generation = int(official.get("generation", 0))
        if human_actions < min_actions or human_episodes < min_episodes:
            progress(f"用户示范不足：需要至少 {min_actions} 次有效出牌且覆盖 {min_episodes} 局；当前 {human_actions} 次 / {human_episodes} 局")
            return {
                "samples": 0, "added": 0, "generation": generation, "cancelled": False,
                "insufficient_demo": True, "human_actions": human_actions, "human_episodes": human_episodes,
            }
        validation_fraction = float(self.config.get("training_validation_fraction", 0.20))
        train, validation = self.pool.split_by_episode(validation_fraction, max_experiences)
        if not validation:
            raise RuntimeError("至少需要两局完整经验，才能按对局划分训练集和验证集")
        train_ids = {item["episode_id"] for item in train}
        validation_ids = {item["episode_id"] for item in validation}
        if train_ids & validation_ids:
            raise RuntimeError("检测到训练集与验证集对局泄漏")
        progress(f"按整局切分：训练 {len(train)} 条/{len(train_ids)} 局，验证 {len(validation)} 条/{len(validation_ids)} 局")

        learning_rate = float(self.config.get("training_learning_rate", 0.0008))
        batch_size = int(self.config.get("training_batch_size", 64))
        trainer = NetworkTrainer(official, learning_rate)
        rng = random.Random(20260803 + generation)
        bc_epochs = int(self.config.get("training_epochs_behavior_clone", 8))
        rl_epochs = int(self.config.get("training_epochs_reinforcement", 4))
        for epoch in range(bc_epochs):
            if stop.is_set():
                return {"samples": 0, "added": 0, "generation": generation, "cancelled": True}
            metrics = trainer.train_epoch(train, batch_size, True, rng, stop)
            progress(f"行为克隆 {epoch + 1}/{bc_epochs}｜批次 {int(metrics['updates'])}｜策略损失 {metrics['policy_loss']:.4f}")
        ai_samples = [item for item in train if item["source"] == "ai_transition"]
        for epoch in range(rl_epochs):
            if stop.is_set():
                return {"samples": 0, "added": 0, "generation": generation, "cancelled": True}
            if not ai_samples:
                break
            metrics = trainer.train_epoch(ai_samples, batch_size, False, rng, stop)
            progress(f"实战经验训练 {epoch + 1}/{rl_epochs}｜批次 {int(metrics['updates'])}｜价值损失 {metrics['value_loss']:.4f}")

        candidate = trainer.export()
        candidate["generation"] = generation + 1
        candidate["updated_utc"] = utc_now()
        candidate["training"] = {
            "human_demo_samples": sum(item["source"] == "human_demo" for item in train),
            "ai_transition_samples": sum(item["source"] == "ai_transition" for item in train),
            "train_episode_count": len(train_ids),
            "validation_episode_count": len(validation_ids),
            "split": "chronological_episode_id",
        }
        official_eval = PolicyEvaluator.evaluate(official, validation)
        candidate_eval = PolicyEvaluator.evaluate(candidate, validation)
        offline_passed = (
            candidate_eval["human_action_accuracy"] >= official_eval["human_action_accuracy"]
            and candidate_eval["value_mae"] <= official_eval["value_mae"] + 0.05
        )
        candidate["evaluation"] = {
            "status": "candidate" if offline_passed else "rejected_offline",
            "official_validation": official_eval,
            "candidate_validation": candidate_eval,
            "note": "候选模型不会覆盖正式模型；还需真实 AI A/B 对局与 UserBenchmark。",
        }
        if offline_passed:
            self.model_store.save(candidate, candidate=True)
            progress("候选模型已保存；正式模型保持不变，等待真实对局评测")
        else:
            progress("候选模型未通过留出对局评测，正式模型保持不变")
        self.pool.compact(max_experiences)
        return {
            "samples": len(train), "validation_samples": len(validation), "added": 0,
            "generation": generation + (1 if offline_passed else 0), "cancelled": False,
            "offline_accuracy": candidate_eval["human_action_accuracy"], "candidate_saved": offline_passed,
            "official_accuracy": official_eval["human_action_accuracy"],
        }


class GameAI:
    def __init__(self, paths: AppPaths, config: dict[str, Any]) -> None:
        self.paths = paths
        self.config = config
        self.control = WindowsControl(config)
        self.vision = VisionEngine(config, paths)
        self.model_store = ModelStore(paths.model, paths.candidate_model, paths.model_archive)
        self.pool = ExperiencePool(paths.experiences)
        self.benchmarks = BenchmarkStore(paths.benchmarks)
        self.official = self.model_store.load()
        self.candidate = self.model_store.load_candidate()
        self.policy = Policy(self.official, config)
        self.model_role = "official"
        self.current_episode: Optional[str] = None
        self.step_id = 0
        self.episode_first: Optional[dict[str, Any]] = None
        self.vision_failed = False
        self.observed_cards: set[str] = set()
        self._benchmarked: set[str] = set()

    @staticmethod
    def transition_reward(previous: dict[str, Any], current: dict[str, Any], action: int) -> float:
        previous_enemy = sum(previous["state"][25:28])
        current_enemy = sum(current["state"][25:28])
        previous_friendly = sum(previous["state"][22:25])
        current_friendly = sum(current["state"][22:25])
        reward = (previous_enemy - current_enemy) - (previous_friendly - current_friendly)
        if action != WAIT_ACTION:
            reward += 0.005
        if current["result"] == "win":
            reward += 1.0
        elif current["result"] == "loss":
            reward -= 1.0
        return max(-2.0, min(2.0, reward))

    def _select_model(self, episode_id: str) -> None:
        self.official = self.model_store.load()
        self.candidate = self.model_store.load_candidate()
        use_candidate = False
        if self.candidate is not None:
            deck = canonical_deck_signature(parse_deck_signature(str(self.config.get("benchmark_deck_signature", ""))))
            cup = str(self.config.get("benchmark_cup_band", "")).strip()
            records = [item for item in self.benchmarks.load() if item.get("actor") == "ai" and item.get("deck_signature") == deck and item.get("cup_band") == cup]
            candidate_digest = model_weights_digest(self.candidate)
            official_digest = model_weights_digest(self.official)
            candidate_count = sum(item.get("model_sha256") == candidate_digest for item in records)
            official_count = sum(item.get("model_sha256") == official_digest for item in records)
            use_candidate = candidate_count <= official_count
        model = self.candidate if use_candidate else self.official
        assert model is not None
        self.model_role = "candidate" if use_candidate else "official"
        self.policy.set_model(model)

    def _benchmark_episode(self, analysis: dict[str, Any], disconnected: bool = False) -> None:
        episode_id = str(analysis.get("episode_id") or self.current_episode or "")
        if not episode_id or episode_id in self._benchmarked:
            return
        self._benchmarked.add(episode_id)
        towers = analysis.get("tower_hp") if isinstance(analysis.get("tower_hp"), dict) else {}
        friendly = [float(towers.get(key, 0.0)) for key in ("friendly_left", "friendly_right", "friendly_king")]
        enemy = [float(towers.get(key, 0.0)) for key in ("enemy_left", "enemy_right", "enemy_king")]
        crown_diff = sum(value <= 0.02 for value in enemy) - sum(value <= 0.02 for value in friendly)
        expected_deck = parse_deck_signature(str(self.config.get("benchmark_deck_signature", "")))
        deck = canonical_deck_signature(expected_deck)
        observed_deck = canonical_deck_signature(self.observed_cards)
        deck_verified = len(expected_deck) == 8 and observed_deck == deck
        cup = str(self.config.get("benchmark_cup_band", "")).strip()
        result = str(analysis.get("result", "unknown"))
        valid = bool(deck_verified and cup and result in {"win", "loss", "draw"} and not disconnected and not self.vision_failed)
        if valid:
            reason = ""
        elif not deck or not cup:
            reason = "未配置固定八卡卡组/杯段"
        elif not deck_verified:
            reason = "检测到的八卡卡组与基准配置不一致或不完整"
        else:
            reason = "掉线、识别失败或缺少结算结果"
        self.benchmarks.append({
            "episode_id": episode_id,
            "actor": "ai",
            "model_sha256": self.policy.digest,
            "model_role": self.model_role,
            "deck_signature": deck,
            "cup_band": cup,
            "valid": valid,
            "exclusion_reason": reason,
            "result": result if result in {"win", "loss", "draw"} else "unknown",
            "crown_diff": crown_diff,
            "tower_hp_diff": sum(friendly) - sum(enemy),
            "vision_failure": self.vision_failed,
            "disconnected": disconnected,
        })
        PromotionGate.try_promote(self.model_store, self.benchmarks, self.config)

    def run(self, progress: Callable[[str], None], stop: threading.Event) -> None:
        hwnd = self.control.launch_or_find(progress, stop)
        if hwnd is None:
            raise RuntimeError("未找到 Clash Royale 窗口；请先安装并登录游戏环境")
        self.control.focus(hwnd)
        candidate_text = "存在候选模型，将与正式模型交替进行真实 A/B 对局" if self.candidate is not None else "当前仅运行正式模型"
        progress(f"AI 已接管游戏窗口；{candidate_text}；按 ESC 立即停止")
        interval = max(0.35, min(3.0, float(self.config.get("decision_interval_seconds", 0.85))))
        previous: Optional[dict[str, Any]] = None
        previous_action = WAIT_ACTION
        last_status = 0.0
        last_lobby_click = 0.0
        low_confidence_frames = 0
        lobby_cooldown = max(4.0, min(30.0, float(self.config.get("lobby_click_cooldown_seconds", 8.0))))
        try:
            while not stop.is_set():
                started = time.monotonic()
                current_hwnd = self.control.find_game_window()
                if current_hwnd is None:
                    if self.current_episode:
                        self._benchmark_episode(previous or {"episode_id": self.current_episode, "result": "unknown", "tower_hp": {}}, disconnected=True)
                    raise RuntimeError("游戏窗口已关闭")
                hwnd = current_hwnd
                frame = self.vision.capture(self.control.rect(hwnd))
                analysis = self.vision.analyze(frame)
                episode_id = analysis.get("episode_id")
                if episode_id and episode_id != self.current_episode:
                    self.current_episode = str(episode_id)
                    self.step_id = 0
                    self.episode_first = analysis
                    self.vision_failed = False
                    self.observed_cards = set()
                    previous = None
                    self._select_model(self.current_episode)
                if self.current_episode and not analysis.get("vision_ok", False):
                    self.vision_failed = True
                if self.current_episode:
                    self.observed_cards.update(str(name).strip().casefold() for name in analysis.get("card_names", []) if str(name).strip())

                if previous is not None and previous.get("episode_id") == analysis.get("episode_id"):
                    self.pool.append_transition({
                        "schema": EXPERIENCE_SCHEMA,
                        "episode_id": self.current_episode,
                        "step_id": self.step_id,
                        "state": previous["state"],
                        "legal_actions": previous["legal_actions"],
                        "action": previous_action,
                        "reward": self.transition_reward(previous, analysis, previous_action),
                        "next_state": analysis["state"],
                        "done": bool(analysis["done"]),
                        "result": analysis["result"] if analysis["done"] else "pending",
                        "source": "ai_transition",
                        "meta": {"model_sha256": self.policy.digest, "model_role": self.model_role, "battle_confidence": analysis["battle_confidence"]},
                    })
                    self.step_id += 1

                queued = False
                value = 0.0
                if analysis["done"]:
                    action = WAIT_ACTION
                    self._benchmark_episode(analysis)
                    previous = None
                    self.current_episode = None
                elif analysis["battle_confidence"] < 0.48 or not episode_id:
                    action = WAIT_ACTION
                    low_confidence_frames += 1
                    now = time.monotonic()
                    if bool(self.config.get("auto_queue_battle", True)) and low_confidence_frames >= 3 and now - last_lobby_click >= lobby_cooldown:
                        lobby = self.vision.find_lobby_action(frame)
                        if lobby is not None and lobby[2] >= 0.68 and not stop.is_set():
                            self.control.focus(hwnd)
                            self.control.click_normalized(hwnd, lobby[0], lobby[1])
                            last_lobby_click = now
                            low_confidence_frames = 0
                            queued = True
                else:
                    low_confidence_frames = 0
                    action, value = self.policy.choose(analysis)
                    self.policy.execute(self.control, hwnd, action, stop)

                if not queued and episode_id and not analysis["done"]:
                    previous = analysis
                    previous_action = action
                now = time.monotonic()
                if now - last_status >= 1.8:
                    last_status = now
                    if action == WAIT_ACTION:
                        action_text = "等待"
                    else:
                        card, column, row = VisionEngine.decode_action(action)
                        action_text = f"卡槽 {card + 1} / 网格 ({column + 1},{row + 1})"
                    progress(f"AI运行中｜{self.model_role}｜圣水≈{analysis['elixir']:.1f}｜价值 {value:+.2f}｜{action_text}")
                stop.wait(max(0.03, interval - (time.monotonic() - started)))
        finally:
            if self.current_episode and previous is not None:
                self._benchmark_episode(previous, disconnected=True)


class HumanDemonstrationRecorder:
    VK_LBUTTON = 0x01

    def __init__(self, control: WindowsControl, vision: VisionEngine, pool: ExperiencePool, benchmarks: BenchmarkStore, config: dict[str, Any]) -> None:
        self.control = control
        self.vision = vision
        self.pool = pool
        self.benchmarks = benchmarks
        self.config = config
        self._was_down = False
        self._selected_card: Optional[int] = None
        self._selected_state: Optional[list[float]] = None
        self._selected_legal: Optional[list[int]] = None
        self._selected_episode: Optional[str] = None
        self._selected_at = 0.0
        self._pending: Optional[dict[str, Any]] = None
        self._step_id = 0
        self._benchmarked: set[str] = set()
        self._vision_failed = False
        self._observed_cards: set[str] = set()
        self._active_episode: Optional[str] = None
        self.recorded = 0

    @staticmethod
    def _cursor_normalized(rect: tuple[int, int, int, int]) -> Optional[tuple[float, float]]:
        if os.name != "nt":
            return None
        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return None
        x, y = (point.x - left) / (right - left), (point.y - top) / (bottom - top)
        return (float(x), float(y)) if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 else None

    @staticmethod
    def _nearest(values: tuple[float, ...], value: float) -> tuple[int, float]:
        index = min(range(len(values)), key=lambda item: abs(values[item] - value))
        return index, abs(values[index] - value)

    def _flush_pending(self, analysis: dict[str, Any]) -> None:
        if self._pending is None:
            return
        if self._pending["episode_id"] != analysis.get("episode_id") and not analysis.get("done"):
            self._pending = None
            return
        if time.monotonic() - self._pending["created"] < 0.12 and not analysis.get("done"):
            return
        result = analysis["result"] if analysis.get("done") else "pending"
        reward = 1.0 if result == "win" else (-1.0 if result == "loss" else 0.0)
        self.pool.append_transition({
            "schema": EXPERIENCE_SCHEMA,
            "episode_id": self._pending["episode_id"],
            "step_id": self._step_id,
            "state": self._pending["state"],
            "legal_actions": self._pending["legal_actions"],
            "action": self._pending["action"],
            "reward": reward,
            "next_state": analysis["state"],
            "done": bool(analysis.get("done")),
            "result": result,
            "source": "human_demo",
            "meta": {"battle_confidence": analysis["battle_confidence"], "hand_signature": analysis.get("hand_signature", "")},
        })
        self.recorded += 1
        self._step_id += 1
        self._pending = None

    def _benchmark_episode(self, analysis: dict[str, Any], disconnected: bool = False) -> None:
        episode_id = str(analysis.get("episode_id") or "")
        if not episode_id or episode_id in self._benchmarked:
            return
        self._benchmarked.add(episode_id)
        towers = analysis.get("tower_hp") if isinstance(analysis.get("tower_hp"), dict) else {}
        friendly = [float(towers.get(key, 0.0)) for key in ("friendly_left", "friendly_right", "friendly_king")]
        enemy = [float(towers.get(key, 0.0)) for key in ("enemy_left", "enemy_right", "enemy_king")]
        expected_deck = parse_deck_signature(str(self.config.get("benchmark_deck_signature", "")))
        deck = canonical_deck_signature(expected_deck)
        observed_deck = canonical_deck_signature(self._observed_cards)
        deck_verified = len(expected_deck) == 8 and observed_deck == deck
        cup = str(self.config.get("benchmark_cup_band", "")).strip()
        result = str(analysis.get("result", "unknown"))
        valid = bool(deck_verified and cup and result in {"win", "loss", "draw"} and not self._vision_failed and not disconnected)
        self.benchmarks.append({
            "episode_id": episode_id, "actor": "human", "model_sha256": "", "model_role": "human",
            "deck_signature": deck, "cup_band": cup, "valid": valid,
            "exclusion_reason": "" if valid else "固定八卡卡组/杯段不匹配、识别失败、掉线或缺少结算结果",
            "result": result if result in {"win", "loss", "draw"} else "unknown",
            "crown_diff": sum(value <= 0.02 for value in enemy) - sum(value <= 0.02 for value in friendly),
            "tower_hp_diff": sum(friendly) - sum(enemy),
            "vision_failure": self._vision_failed, "disconnected": disconnected,
        })
        self._vision_failed = False
        self._observed_cards = set()
        self._active_episode = None
        self._step_id = 0

    def finalize_interrupted(self, analysis: Optional[dict[str, Any]]) -> None:
        if analysis is None:
            return
        if self._pending is not None:
            terminal = dict(analysis)
            terminal["done"] = True
            terminal["result"] = "unknown"
            self._flush_pending(terminal)
        if analysis.get("episode_id"):
            interrupted = dict(analysis)
            interrupted["result"] = "unknown"
            self._benchmark_episode(interrupted, disconnected=True)

    def poll(self, hwnd: int, analysis: dict[str, Any]) -> bool:
        episode_id = str(analysis.get("episode_id") or "")
        if episode_id and episode_id != self._active_episode:
            self._active_episode = episode_id
            self._observed_cards = set()
            self._vision_failed = False
            self._step_id = 0
        if episode_id:
            self._observed_cards.update(str(name).strip().casefold() for name in analysis.get("card_names", []) if str(name).strip())
        self._flush_pending(analysis)
        if analysis.get("done"):
            self._benchmark_episode(analysis)
            self._selected_card = None
            return False
        if os.name != "nt" or analysis.get("battle_confidence", 0.0) < 0.48 or not analysis.get("episode_id"):
            if analysis.get("episode_id"):
                self._vision_failed = True
            self._selected_card = None
            return False
        down = bool(ctypes.windll.user32.GetAsyncKeyState(self.VK_LBUTTON) & 0x8000)
        clicked = down and not self._was_down
        self._was_down = down
        if not clicked:
            return False
        cursor = self._cursor_normalized(self.control.rect(hwnd))
        if cursor is None:
            return False
        x, y = cursor
        now = time.monotonic()
        card, distance = self._nearest(Policy.CARD_X, x)
        if y >= 0.82 and distance <= 0.075:
            self._selected_card = card
            self._selected_state = list(analysis["state"])
            self._selected_legal = list(analysis["legal_actions"])
            self._selected_episode = str(analysis["episode_id"])
            self._selected_at = now
            return False
        if self._selected_card is None or self._selected_state is None or self._selected_legal is None or now - self._selected_at > 2.5:
            return False
        cell = VisionEngine.point_to_cell(x, y)
        if cell is None:
            return False
        column, row = cell
        action = VisionEngine.encode_action(self._selected_card, column, row)
        if action not in self._selected_legal:
            self._selected_card = None
            return False
        self._pending = {
            "episode_id": self._selected_episode,
            "state": self._selected_state,
            "legal_actions": self._selected_legal,
            "action": action,
            "created": now,
        }
        self._selected_card = None
        self._selected_state = None
        self._selected_legal = None
        return True


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
            text="Windows 11 x64 · Python 3.12.x",
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
            config = self.integrity.quick_ready(lambda text: self.post("status", token, text), self.mode_stop)
            if self.mode_stop.is_set():
                return
            control = WindowsControl(config)
            hwnd = control.launch_or_find(lambda text: self.post("status", token, text), self.mode_stop)
            if hwnd is None:
                raise RuntimeError("未找到 Clash Royale 窗口或启动入口")
            control.focus(hwnd)
            vision = VisionEngine(config, self.paths)
            recorder = HumanDemonstrationRecorder(
                control,
                vision,
                ExperiencePool(self.paths.experiences),
                BenchmarkStore(self.paths.benchmarks),
                config,
            )
            self.post("status", token, "游戏窗口已激活；玩家操作会被记录为示范经验，按 ESC 返回控制台")
            last_status = 0.0
            last_analysis: Optional[dict[str, Any]] = None
            try:
                while not self.mode_stop.is_set():
                    current_hwnd = control.find_game_window()
                    if current_hwnd is None:
                        raise RuntimeError("游戏窗口已关闭")
                    frame = vision.capture(control.rect(current_hwnd))
                    analysis = vision.analyze(frame)
                    last_analysis = analysis
                    recorder.poll(current_hwnd, analysis)
                    now = time.monotonic()
                    if now - last_status >= 2.0:
                        last_status = now
                        self.post("status", token, f"玩家示范记录中｜已记录 {recorder.recorded} 次有效出牌")
                    self.mode_stop.wait(0.05)
            finally:
                recorder.finalize_interrupted(last_analysis)

        self.worker(token, task)

    def start_upgrade_mode(self) -> None:
        token = self.set_mode("upgrade", "升级模式", "准备升级经验池与 AI 模型")

        def task() -> None:
            config = self.integrity.quick_ready(lambda text: self.post("status", token, text), self.mode_stop)
            if self.mode_stop.is_set():
                return
            trainer = Trainer(
                ModelStore(self.paths.model, self.paths.candidate_model, self.paths.model_archive),
                ExperiencePool(self.paths.experiences),
                config,
            )
            result = trainer.run(lambda text: self.post("status", token, text), self.mode_stop)
            if self.mode_stop.is_set() or result.get("cancelled"):
                return
            if result.get("insufficient_demo"):
                self.post(
                    "done",
                    token,
                    f"示范不足：当前 {result.get('human_actions', 0)} 次有效出牌 / {result.get('human_episodes', 0)} 局；继续使用“人”模式采集",
                )
                return
            accuracy = float(result.get("offline_accuracy", 0.0)) * 100.0
            status = "候选模型已生成，等待真实评测" if result.get("candidate_saved") else "候选模型未通过留出对局评测"
            self.post(
                "done",
                token,
                f"升级完成：训练 {result['samples']} 条，验证 {result.get('validation_samples', 0)} 条，动作准确率 {accuracy:.1f}%｜{status}",
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
