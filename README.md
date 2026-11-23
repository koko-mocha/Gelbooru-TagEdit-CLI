# Gelbooru Bulk Tag & Metadata Manager CLI

A fast, interactive, low-dependency **Python 3.15 (64-bit)** CLI for querying Gelbooru posts via `dapi`, computing non-destructive bulk tag and metadata (`rating`, `source`) diffs, previewing changes with a Gelbooru-inspired ANSI color theme, and executing background updates within a strict **10 requests/second** sliding-window rate limit.

---

## 1. Installation & Setup

1. Ensure you have **Python 3.15 (64-bit)** installed.
2. Install the single external dependency (`aiohttp`):
   ```bash
   pip install -r requirements.txt
   ```
