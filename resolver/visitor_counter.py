import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class VisitorCounter:
    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 90,
        timezone_name: str = "Asia/Tokyo",
    ) -> None:
        self.path = path
        self.retention_days = max(7, min(730, retention_days))
        self.timezone = ZoneInfo(timezone_name)
        self._lock = threading.Lock()

    @staticmethod
    def _empty() -> dict[str, object]:
        return {"total": 0, "days": {}}

    def _load(self) -> dict[str, object]:
        if not self.path.is_file():
            return self._empty()

        data = json.loads(self.path.read_text(encoding="utf-8"))
        total = data.get("total")
        days = data.get("days")
        if not isinstance(total, int) or total < 0 or not isinstance(days, dict):
            raise ValueError("Visitor count file is invalid")

        clean_days: dict[str, int] = {}
        for day, count in days.items():
            if isinstance(day, str) and isinstance(count, int) and count >= 0:
                clean_days[day] = count
        return {"total": total, "days": clean_days}

    def _summary(self, data: dict[str, object]) -> dict[str, int]:
        today = datetime.now(self.timezone).date()
        yesterday = today - timedelta(days=1)
        days = data["days"]
        assert isinstance(days, dict)
        return {
            "total": int(data["total"]),
            "today": int(days.get(today.isoformat(), 0)),
            "yesterday": int(days.get(yesterday.isoformat(), 0)),
        }

    def _save(self, data: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary_file.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary_file, self.path)

    def read(self) -> dict[str, int]:
        with self._lock:
            return self._summary(self._load())

    def increment(self) -> dict[str, int]:
        with self._lock:
            data = self._load()
            today = datetime.now(self.timezone).date()
            days = data["days"]
            assert isinstance(days, dict)
            data["total"] = int(data["total"]) + 1
            day_key = today.isoformat()
            days[day_key] = int(days.get(day_key, 0)) + 1

            cutoff = today - timedelta(days=self.retention_days)
            data["days"] = {
                day: count
                for day, count in days.items()
                if day >= cutoff.isoformat()
            }
            self._save(data)
            return self._summary(data)
