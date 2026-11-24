import asyncio
from collections import deque
import html
import json
import logging
import math
import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
import aiohttp

try:
    import orjson as _orjson  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _orjson = None

from config import Config

# Precompiled once instead of being re-resolved on every post / every <input>.
_RE_INPUT = re.compile(r"<input\b([^>]*)/?>", re.I)
_RE_SELECT = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.S | re.I)
_RE_TEXTAREA = re.compile(r"<textarea\b([^>]*)>(.*?)</textarea>", re.S | re.I)
_RE_ATTR_NAME = re.compile(r'\bname=["\']([^"\']+)["\']', re.I)
_RE_ATTR_TYPE = re.compile(r'\btype=["\']([^"\']+)["\']', re.I)
_RE_ATTR_VALUE = re.compile(r'\bvalue=["\']([^"\']*)["\']', re.I)
_RE_CHECKED = re.compile(r"\bchecked\b", re.I)
_RE_OPT_SELECTED = re.compile(r'<option\b[^>]*\bselected\b[^>]*value=["\']([^"\']*)["\']', re.I)
_RE_OPT_FIRST = re.compile(r'<option\b[^>]*value=["\']([^"\']*)["\']', re.I)
_RE_TAGS_TEXTAREA = re.compile(
    r'<textarea[^>]*(?:id=["\']tags["\']|name=["\']tags["\'])[^>]*>(.*?)</textarea>', re.S | re.I
)
_RE_EDIT_FORM = re.compile(
    r'<form[^>]*(?:id=["\']edit_form["\']|name=["\']edit_form["\'])[^>]*>(.*?)</form>', re.S | re.I
)
_RE_WS = re.compile(r"\s+")


def _decode_json_bytes(raw: bytes) -> Any:
    if not raw or not raw.strip():
        return []
    if _orjson is not None:
        return _orjson.loads(raw)
    return json.loads(raw.decode("utf-8", errors="replace"))


def parse_tags_input(raw: str) -> str:
    text = raw.strip()
    if text.startswith(("http://", "https://")):
        qs = parse_qs(urlparse(text).query)
        if "tags" in qs and qs["tags"]:
            return unquote(qs["tags"][0]).strip()
    return unquote(text).strip()


def _redact_text(text: str, secret: str) -> str:
    if secret and len(secret) >= 4:
        return text.replace(secret, "[REDACTED]")
    return text


class RateLimiter:
    def __init__(self, max_rps: int = 10) -> None:
        self.max_rps = max(1, max_rps)
        self._hits: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._hits and now - self._hits[0] >= 1.0:
                self._hits.popleft()
            if len(self._hits) >= self.max_rps:
                sleep_s = 1.0 - (now - self._hits[0]) + 0.01
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= 1.0:
                    self._hits.popleft()
            self._hits.append(time.monotonic())


class GelbooruAPI:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger
        self.limiter = RateLimiter(cfg.max_rps)
        self._session: aiohttp.ClientSession | None = None
        self._success_streak: int = 0

    def _log(
        self,
        level: int,
        msg: str,
        *,
        post_id: int | str = "",
        action: str = "",
        status: str = "",
    ) -> None:
        if not self.logger.isEnabledFor(level):
            return
        safe_msg = _redact_text(msg, self.cfg.api_key)
        safe_msg = _redact_text(safe_msg, self.cfg.cookie_header)
        self.logger.log(
            level,
            safe_msg,
            extra={"post_id": post_id, "action": action, "status": status},
        )

    async def __aenter__(self) -> "GelbooruAPI":
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
            ),
            "Origin": self.cfg.base_url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        cookie_val = self.cfg.cookie_header.strip()
        if cookie_val.lower().startswith("cookie:"):
            cookie_val = cookie_val.split(":", 1)[1].strip()
        if cookie_val:
            headers["Cookie"] = cookie_val

        connector = aiohttp.TCPConnector(limit=max(8, self.cfg.search_concurrency * 2), ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers=headers,
            connector=connector,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self._log(
                logging.ERROR,
                f"Session exited with {exc_type.__name__ if exc_type else 'err'}: {exc}",
                action="session",
                status="error",
            )
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None
        return self._session

    def _auth(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.cfg.api_key:
            out["api_key"] = self.cfg.api_key
        if self.cfg.user_id:
            out["user_id"] = self.cfg.user_id
        return out

    async def _get_json(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
        """Returns (rows, total_count). total_count is None when the API doesn't report it."""
        url = f"{self.cfg.base_url}/index.php"
        query = {"page": "dapi", "s": "post", "q": "index", "json": "1", **self._auth(), **params}
        debug_on = self.logger.isEnabledFor(logging.DEBUG)

        for attempt in range(3):
            await self.limiter.wait()
            if debug_on:
                safe_q = {k: ("[REDACTED]" if k == "api_key" else v) for k, v in query.items()}
                self._log(logging.DEBUG, f"GET {url} params={safe_q} attempt={attempt + 1}", action="dapi_search")
            async with self.session.get(url, params=query) as resp:
                raw = await resp.read()
                if debug_on:
                    safe_url = str(resp.url).replace(self.cfg.api_key, "[REDACTED]") if self.cfg.api_key else str(resp.url)
                    self._log(
                        logging.DEBUG,
                        f"DAPI response url={safe_url} status={resp.status} bytes={len(raw)}",
                        action="dapi_search",
                        status=str(resp.status),
                    )
                if resp.status in (401, 403):
                    snippet = raw[:300].decode("utf-8", errors="replace")
                    self._log(
                        logging.ERROR,
                        f"Auth rejected ({resp.status}): {snippet}",
                        action="dapi_search",
                        status=str(resp.status),
                    )
                    raise PermissionError(f"HTTP {resp.status} Unauthorized. Check API credentials in Menu -> 5.")
                if resp.status >= 500 or resp.status == 429:
                    if attempt < 2:
                        await asyncio.sleep(0.8 * (2**attempt))
                        continue
                resp.raise_for_status()
                data = _decode_json_bytes(raw)
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict)], None
                if isinstance(data, dict):
                    total: int | None = None
                    attrs = data.get("@attributes")
                    if isinstance(attrs, dict):
                        try:
                            total = int(attrs.get("count"))
                        except (TypeError, ValueError):
                            total = None
                    posts = data.get("post")
                    if isinstance(posts, list):
                        return [x for x in posts if isinstance(x, dict)], total
                    if isinstance(posts, dict):
                        return [posts], total
                    if "id" in data:
                        return [data], total
                    return [], total
                return [], None
        return [], None

    async def search_posts(self, tags_query: str, max_posts: int | None = None) -> list[dict[str, Any]]:
        limit = max(1, int(self.cfg.page_limit))
        first_limit = min(limit, max_posts) if max_posts else limit
        first_rows, total = await self._get_json({"tags": tags_query, "limit": first_limit, "pid": 0})
        if not first_rows:
            return []

        all_posts: list[dict[str, Any]] = []
        seen: set[int] = set()

        def ingest(rows: list[dict[str, Any]]) -> int:
            added = 0
            for r in rows:
                try:
                    post_id = int(r.get("id", 0))
                except (TypeError, ValueError):
                    continue
                if post_id and post_id not in seen:
                    seen.add(post_id)
                    all_posts.append(r)
                    added += 1
                    if max_posts and len(all_posts) >= max_posts:
                        break
            return added

        ingest(first_rows)
        if max_posts and len(all_posts) >= max_posts:
            return all_posts
        if len(first_rows) < first_limit:
            return all_posts

        # Last page index worth requesting, derived from the reported total and/or max_posts.
        # Avoids firing whole speculative waves past the end of the result set.
        target = total
        if max_posts:
            target = min(target, max_posts) if target is not None else max_posts
        last_pid = math.ceil(target / limit) - 1 if target is not None else None

        # Fetch subsequent pages in bounded parallel waves while respecting RateLimiter
        sem = asyncio.Semaphore(max(1, int(self.cfg.search_concurrency)))
        next_pid = 1
        wave_size = max(2, int(self.cfg.search_concurrency) * 2)

        async def fetch_page(page_idx: int) -> list[dict[str, Any]]:
            async with sem:
                rows, _ = await self._get_json({"tags": tags_query, "limit": limit, "pid": page_idx})
                return rows

        while last_pid is None or next_pid <= last_pid:
            end_pid = next_pid + wave_size
            if last_pid is not None:
                end_pid = min(end_pid, last_pid + 1)
            pids = range(next_pid, end_pid)
            next_pid = end_pid
            # gather preserves input order, so no sort is needed
            results = await asyncio.gather(*(fetch_page(p) for p in pids))

            hit_short_page = False
            for rows in results:
                if not rows:
                    hit_short_page = True
                    break
                added = ingest(rows)
                if max_posts and len(all_posts) >= max_posts:
                    hit_short_page = True
                    break
                if len(rows) < limit or added == 0:
                    hit_short_page = True
                    break

            if hit_short_page:
                break

        return all_posts

    def _extract_form_payload(self, form_body: str) -> dict[str, str]:
        payload: dict[str, str] = {}

        for m in _RE_INPUT.finditer(form_body):
            attrs = m.group(1)
            nm = _RE_ATTR_NAME.search(attrs)
            if not nm:
                continue
            name = html.unescape(nm.group(1))
            tm = _RE_ATTR_TYPE.search(attrs)
            itype = tm.group(1).lower() if tm else "text"
            vm = _RE_ATTR_VALUE.search(attrs)
            val = html.unescape(vm.group(1)) if vm else ""
            if itype == "radio":
                if _RE_CHECKED.search(attrs):
                    payload[name] = val
            elif itype == "checkbox":
                if _RE_CHECKED.search(attrs):
                    payload[name] = val or "on"
            else:
                payload[name] = val

        for sm in _RE_SELECT.finditer(form_body):
            s_attrs, s_body = sm.group(1), sm.group(2)
            snm = _RE_ATTR_NAME.search(s_attrs)
            if not snm:
                continue
            s_name = html.unescape(snm.group(1))
            opt_sel = _RE_OPT_SELECTED.search(s_body)
            if opt_sel:
                payload[s_name] = html.unescape(opt_sel.group(1))
            else:
                opt_first = _RE_OPT_FIRST.search(s_body)
                if opt_first:
                    payload[s_name] = html.unescape(opt_first.group(1))

        for tam in _RE_TEXTAREA.finditer(form_body):
            ta_attrs, ta_val = tam.group(1), tam.group(2)
            tanm = _RE_ATTR_NAME.search(ta_attrs)
            if not tanm:
                continue
            ta_name = html.unescape(tanm.group(1))
            if ta_name != "tags":
                payload[ta_name] = html.unescape(ta_val)

        return payload

    def _should_verify_post(self, post_id: int, status: int, loc: str, attempt_num: int) -> tuple[bool, str]:
        mode = self.cfg.normalized_verification_mode()
        if mode == "fast":
            return False, "mode=fast"
        if mode == "strict":
            return True, "mode=strict"

        uncertain = attempt_num > 1 or (status == 302 and f"id={post_id}" not in loc)
        if uncertain:
            return True, "mode=balanced(uncertain)"

        sample_every = max(1, int(self.cfg.verify_sample_every))
        if (self._success_streak + 1) % sample_every == 0:
            return True, f"mode=balanced(sample_1_in_{sample_every})"

        return False, "mode=balanced(skipped)"

    async def _verify_view_page(
        self,
        view_url: str,
        post_id: int,
        clean_tags: str,
        removed_tags: set[str],
        attempt_num: int,
    ) -> tuple[bool, str]:
        await self.limiter.wait()
        async with self.session.get(view_url) as r_ver:
            ver_html = await r_ver.text()

        ta_m = _RE_TAGS_TEXTAREA.search(ver_html)
        if not ta_m:
            msg = "Missing tags textarea on verification GET"
            self._log(logging.WARNING, f"attempt={attempt_num} {msg}", post_id=post_id, action="verify", status="no_textarea")
            return False, msg

        actual_set = {html.unescape(t) for t in html.unescape(ta_m.group(1)).split() if t.strip()}
        expected_set = {html.unescape(t) for t in clean_tags.split() if t.strip()}
        missing_added = expected_set - actual_set
        still_present = ({html.unescape(t) for t in removed_tags}) & actual_set

        if missing_added:
            msg = f"Missing expected tags after save: {' '.join(sorted(missing_added))}"
            self._log(logging.WARNING, f"attempt={attempt_num} {msg}", post_id=post_id, action="verify", status="missing_tags")
            return False, msg

        if still_present:
            msg = f"Removed tags still present after save: {' '.join(sorted(still_present))}"
            self._log(logging.WARNING, f"attempt={attempt_num} {msg}", post_id=post_id, action="verify", status="still_present")
            return False, msg

        self._log(
            logging.INFO,
            f"attempt={attempt_num} Updated and verified (tags={len(actual_set)})",
            post_id=post_id,
            action="verify",
            status="ok",
        )
        return True, "Updated & verified"

    async def _submit_once(
        self,
        post_id: int,
        tags_str: str,
        rating: str | None,
        source: str | None,
        removed_tags: set[str],
        attempt_num: int,
    ) -> tuple[bool, str]:
        view_url = f"{self.cfg.base_url}/index.php?page=post&s=view&id={post_id}"
        edit_url = f"{self.cfg.base_url}/public/edit_post.php"

        await self.limiter.wait()
        async with self.session.get(view_url) as r_get:
            html_text = await r_get.text()
            r_get.raise_for_status()
            self._log(
                logging.DEBUG,
                f"attempt={attempt_num} GET view status={r_get.status} final_url={r_get.url} bytes={len(html_text)}",
                post_id=post_id,
                action="fetch_view",
                status=str(r_get.status),
            )

        form_m = _RE_EDIT_FORM.search(html_text)
        if not form_m:
            notice_m = re.search(r'<div[^>]*(?:class|id)=["\'][^"\']*(?:notice|error|alert)[^"\']*["\'][^>]*>(.*?)</div>', html_text, re.S | re.I)
            notice_txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", notice_m.group(1)))).strip()[:200] if notice_m else ""
            msg = f"edit_form missing{': ' + notice_txt if notice_txt else ' (check cookie/session)'}"
            self._log(logging.WARNING, f"attempt={attempt_num} {msg}", post_id=post_id, action="parse_form", status="no_form")
            return False, msg

        form_body = form_m.group(1)
        payload = self._extract_form_payload(form_body)

        clean_tags = html.unescape(tags_str).strip()
        ta_orig = _RE_TAGS_TEXTAREA.search(form_body)
        if ta_orig:
            page_tags = [html.unescape(t) for t in html.unescape(ta_orig.group(1)).split()]
            rem_set = {html.unescape(t) for t in removed_tags}
            # Set membership instead of `t not in merged` (list scan): O(n + m) instead of O(n * m)
            seen = set(page_tags)
            merged = [t for t in page_tags if t not in rem_set]
            for t in clean_tags.split():
                t_clean = html.unescape(t)
                if t_clean not in seen:
                    seen.add(t_clean)
                    merged.append(t_clean)
            clean_tags = " ".join(merged)

        payload["id"] = str(post_id)
        payload["tags"] = clean_tags
        if rating:
            payload["rating"] = rating
        if source is not None:
            payload["source"] = html.unescape(source)
        payload.setdefault("tagsSearched", "")
        payload.setdefault("g-recaptcha-response", "")
        payload.setdefault("submit", "Save changes")

        self._log(
            logging.DEBUG,
            (
                f"attempt={attempt_num} prepared payload keys={list(payload.keys())} "
                f"uid={payload.get('uid', '')} uname={payload.get('uname', '')} "
                f"lupdated={payload.get('lupdated', '')} has_csrf={bool(payload.get('csrf-token'))} "
                f"rating={payload.get('rating', '')} tags_count={len(clean_tags.split())}"
            ),
            post_id=post_id,
            action="prepare_form",
            status="ready",
        )

        await asyncio.sleep(1.6)
        await self.limiter.wait()

        async with self.session.post(
            edit_url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": view_url,
            },
            allow_redirects=False,
        ) as r_post:
            loc = (r_post.headers.get("Location") or "").strip()
            set_cookies = [v.split("=", 1)[0].strip() for v in r_post.headers.getall("Set-Cookie", [])]
            post_body = await r_post.text()
            body_snip = _RE_WS.sub(" ", post_body[:2000]).strip()[:220]
            self._log(
                logging.DEBUG,
                f"attempt={attempt_num} POST edit_post.php status={r_post.status} location='{loc}' set_cookies={set_cookies} body='{body_snip}'",
                post_id=post_id,
                action="submit_edit",
                status=str(r_post.status),
            )
            if r_post.status not in (200, 302):
                msg = f"HTTP {r_post.status}: {body_snip}"
                self._log(logging.ERROR, f"attempt={attempt_num} {msg}", post_id=post_id, action="submit_edit", status="http_err")
                return False, msg
            if loc and (loc.endswith("id=") or f"id={post_id}" not in loc):
                msg = f"Redirected to '{loc}' (edit not applied to post {post_id})"
                self._log(logging.WARNING, f"attempt={attempt_num} {msg}", post_id=post_id, action="submit_edit", status="redirect_mismatch")
                return False, msg

        do_verify, reason = self._should_verify_post(post_id, r_post.status, loc, attempt_num)
        if not do_verify:
            self._success_streak += 1
            self._log(
                logging.INFO,
                f"attempt={attempt_num} Submitted ({reason}, status={r_post.status})",
                post_id=post_id,
                action="verify",
                status="skipped",
            )
            return True, f"Submitted ({reason})"

        ok, msg = await self._verify_view_page(view_url, post_id, clean_tags, removed_tags, attempt_num)
        if ok:
            self._success_streak += 1
        return ok, msg

    async def update_post(
        self,
        post_id: int,
        tags_str: str,
        rating: str | None = None,
        source: str | None = None,
        removed_tags: set[str] | None = None,
    ) -> tuple[bool, str]:
        if not self.cfg.has_cookies():
            msg = "No browser Cookie header stored (set in Menu -> 5)"
            self._log(logging.WARNING, msg, post_id=post_id, action="update_post", status="no_cookie")
            return False, msg

        rem = removed_tags or set()
        last_msg = ""
        for attempt in range(1, 4):
            if attempt > 1:
                wait_s = 3.0 * (attempt - 1)
                await asyncio.sleep(wait_s)
                self._log(
                    logging.INFO,
                    f"Waiting {wait_s}s before retry attempt={attempt}",
                    post_id=post_id,
                    action="retry",
                    status="retrying",
                )
            ok, msg = await self._submit_once(post_id, tags_str, rating, source, rem, attempt)
            if ok:
                return True, msg
            last_msg = msg
        return False, last_msg