import csv
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path


@dataclass
class Config:
    base_url: str = "https://gelbooru.com"
    api_key: str = ""
    user_id: str = ""
    cookie_header: str = ""
    max_rps: int = 10
    page_limit: int = 100
    settings_file: Path = Path("gelbooru_settings.json")
    debug_csv: Path = Path("logs/gelbooru_debug.csv")

    def has_api_creds(self) -> bool:
        return bool(self.api_key.strip() and self.user_id.strip())

    def has_cookies(self) -> bool:
        return bool(self.cookie_header.strip())

    def auth_summary(self) -> str:
        api_state = "SET" if self.has_api_creds() else "MISSING"
        cookie_state = "SET" if self.has_cookies() else "MISSING"
        return f"API Creds: {api_state} (uid={self.user_id or '-'}) | Cookies: {cookie_state}"

    def load_saved(self) -> None:
        if self.settings_file.exists():
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            self.base_url = str(data.get("base_url", self.base_url)).rstrip("/")
            self.api_key = str(data.get("api_key", self.api_key)).strip()
            self.user_id = str(data.get("user_id", self.user_id)).strip()
            self.cookie_header = str(data.get("cookie_header", self.cookie_header)).strip()
            self.max_rps = max(1, int(data.get("max_rps", self.max_rps)))

    def save(self) -> None:
        payload = {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "user_id": self.user_id,
            "cookie_header": self.cookie_header,
            "max_rps": self.max_rps,
        }
        self.settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class CsvDebugHandler(logging.Handler):
    FIELDNAMES = ["timestamp", "level", "source", "post_id", "action", "status", "detail"]

    def __init__(self, csv_path: Path) -> None:
        super().__init__(level=logging.DEBUG)
        self.csv_path = csv_path
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        with self.csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            if write_header:
                writer.writeheader()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra = record.__dict__
            row = {
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "level": record.levelname,
                "source": f"{record.filename}:{record.lineno}",
                "post_id": extra.get("post_id", ""),
                "action": extra.get("action", ""),
                "status": extra.get("status", ""),
                "detail": self.format(record),
            }
            with self.csv_path.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
                writer.writerow(row)
        except Exception:
            self.handleError(record)


def setup_csv_logger(csv_path: Path) -> logging.Logger:
    logger = logging.getLogger("gelbooru_csv")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()
    handler = CsvDebugHandler(csv_path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger