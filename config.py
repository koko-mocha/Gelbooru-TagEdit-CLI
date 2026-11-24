import csv
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path

VALID_VERIFICATION_MODES = ("strict", "balanced", "fast")


@dataclass
class Config:
    base_url: str = "https://gelbooru.com"
    api_key: str = ""
    user_id: str = ""
    cookie_header: str = ""
    max_rps: int = 10
    page_limit: int = 100
    search_concurrency: int = 4
    auto_yes: bool = False
    verification_mode: str = "balanced"
    verify_sample_every: int = 5
    log_flush_every: int = 16
    settings_file: Path = Path("gelbooru_settings.json")
    debug_csv: Path = Path("logs/gelbooru_debug.csv")

    def normalized_verification_mode(self) -> str:
        mode = (self.verification_mode or "balanced").strip().lower()
        return mode if mode in VALID_VERIFICATION_MODES else "balanced"

    def has_api_creds(self) -> bool:
        return bool(self.api_key.strip() and self.user_id.strip())

    def has_cookies(self) -> bool:
        return bool(self.cookie_header.strip())

    def auth_summary(self) -> str:
        api_state = "SET" if self.has_api_creds() else "MISSING"
        cookie_state = "SET" if self.has_cookies() else "MISSING"
        auto_state = "ON" if self.auto_yes else "OFF"
        v_mode = self.normalized_verification_mode()
        v_detail = f"{v_mode} (1/{self.verify_sample_every})" if v_mode == "balanced" else v_mode
        return (
            f"API Creds: {api_state} (uid={self.user_id or '-'}) | "
            f"Cookies: {cookie_state} | Auto-Yes: {auto_state} | Verify: {v_detail}"
        )

    def load_saved(self) -> None:
        if self.settings_file.exists():
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            self.base_url = str(data.get("base_url", self.base_url)).rstrip("/")
            self.api_key = str(data.get("api_key", self.api_key)).strip()
            self.user_id = str(data.get("user_id", self.user_id)).strip()
            self.cookie_header = str(data.get("cookie_header", self.cookie_header)).strip()
            self.max_rps = max(1, int(data.get("max_rps", self.max_rps)))
            self.search_concurrency = max(1, int(data.get("search_concurrency", self.search_concurrency)))
            self.auto_yes = bool(data.get("auto_yes", self.auto_yes))
            mode = str(data.get("verification_mode", self.verification_mode)).strip().lower()
            self.verification_mode = mode if mode in VALID_VERIFICATION_MODES else "balanced"
            self.verify_sample_every = max(1, int(data.get("verify_sample_every", self.verify_sample_every)))
            self.log_flush_every = max(1, int(data.get("log_flush_every", self.log_flush_every)))

    def save(self) -> None:
        payload = {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "user_id": self.user_id,
            "cookie_header": self.cookie_header,
            "max_rps": self.max_rps,
            "search_concurrency": self.search_concurrency,
            "auto_yes": self.auto_yes,
            "verification_mode": self.normalized_verification_mode(),
            "verify_sample_every": max(1, int(self.verify_sample_every)),
            "log_flush_every": max(1, int(self.log_flush_every)),
        }
        self.settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class CsvDebugHandler(logging.Handler):
    FIELDNAMES = ["timestamp", "level", "source", "post_id", "action", "status", "detail"]

    def __init__(self, csv_path: Path, flush_every: int = 16) -> None:
        super().__init__(level=logging.DEBUG)
        self.csv_path = csv_path
        self.flush_every = max(1, flush_every)
        self._pending = 0
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        self._file = self.csv_path.open("a", encoding="utf-8", newline="", buffering=65536)
        self._writer = csv.writer(self._file)
        if write_header:
            self._writer.writerow(self.FIELDNAMES)
            self._file.flush()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra = record.__dict__
            ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            row = [
                ts,
                record.levelname,
                f"{record.filename}:{record.lineno}",
                extra.get("post_id", ""),
                extra.get("action", ""),
                extra.get("status", ""),
                self.format(record),
            ]
            self._writer.writerow(row)
            self._pending += 1
            if record.levelno >= logging.WARNING or self._pending >= self.flush_every:
                self._file.flush()
                self._pending = 0
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        try:
            if not self._file.closed:
                self._file.flush()
                self._pending = 0
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.flush()
            if not self._file.closed:
                self._file.close()
        finally:
            super().close()


def setup_csv_logger(csv_path: Path, flush_every: int = 16) -> logging.Logger:
    logger = logging.getLogger("gelbooru_csv")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for existing in logger.handlers:
        existing.close()
    logger.handlers.clear()
    handler = CsvDebugHandler(csv_path, flush_every=flush_every)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger