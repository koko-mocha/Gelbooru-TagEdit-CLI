import asyncio
import sys
import traceback
from urllib.parse import parse_qs

from api import GelbooruAPI, parse_tags_input
from config import Config, setup_csv_logger


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def print_progress_bar(done: int, total: int, ok_cnt: int, fail_cnt: int, current_id: int) -> None:
    width = 24
    pct = (done / total) if total else 1.0
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    print(
        f"\r  [{bar}] {pct * 100:5.1f}% ({done}/{total}) | ok:{ok_cnt} fail:{fail_cnt} | #{current_id}",
        end="",
        flush=True,
    )


def format_compact_diff(
    post_id: int,
    old_tags: list[str],
    new_tags: list[str],
    old_r: str,
    new_r: str,
    old_s: str,
    new_s: str,
) -> str:
    added = [t for t in new_tags if t not in set(old_tags)]
    removed = [t for t in old_tags if t not in set(new_tags)]
    parts: list[str] = []
    if added:
        parts.append("+ " + " ".join(added))
    if removed:
        parts.append("- " + " ".join(removed))
    if new_r != old_r:
        parts.append(f"rating:{old_r or '-'}->{new_r}")
    if new_s != old_s:
        parts.append(f"source:{old_s or '-'}->{new_s}")
    return f"  #{post_id}: " + " | ".join(parts)


async def run_tag_mutation(cfg: Config, logger, mode: str) -> None:
    if not cfg.has_api_creds():
        print("API credentials are MISSING. Open Menu -> 4 first.")
        return

    raw_query = ask("Search tags or Gelbooru list URL")
    query = parse_tags_input(raw_query)
    if not query:
        print("Empty search query.")
        return

    target_tags = [t.strip() for t in ask(f"Tags to {mode.upper()} (space-separated)").split() if t.strip()]
    if not target_tags:
        print("No tags specified.")
        return

    cap_raw = ask("Max posts (blank = all)", "")
    max_posts = int(cap_raw) if cap_raw.isdigit() and int(cap_raw) > 0 else None

    async with GelbooruAPI(cfg, logger) as api:
        posts = await api.search_posts(query, max_posts=max_posts)
        if not posts:
            print("No posts found.")
            return

        plan: list[tuple[int, list[str], list[str], str, str]] = []
        for p in posts:
            pid = int(p["id"])
            old_tags = [t for t in str(p.get("tags", "")).split() if t]
            rating = str(p.get("rating", ""))
            source = str(p.get("source", ""))

            if mode == "remove":
                rem = set(target_tags)
                new_tags = [t for t in old_tags if t not in rem]
            else:
                seen = set(old_tags)
                new_tags = list(old_tags)
                for t in target_tags:
                    if t not in seen:
                        seen.add(t)
                        new_tags.append(t)

            if new_tags != old_tags:
                plan.append((pid, old_tags, new_tags, rating, source))

        if not plan:
            print(f"No posts require '{mode}' changes.")
            return

        print(f"\nPreview ({len(plan)} post(s)):")
        for pid, old_t, new_t, r, s in plan[:15]:
            print(format_compact_diff(pid, old_t, new_t, r, r, s, s))
        if len(plan) > 15:
            print(f"  ... +{len(plan) - 15} more")

        if not cfg.auto_yes:
            if ask("Apply changes? (y/N)", "n").lower() != "y":
                print("Cancelled.")
                return
        else:
            print("Auto-Yes enabled -> applying changes...")

        removed_set = set(target_tags) if mode == "remove" else set()
        ok_cnt = fail_cnt = 0
        failures: list[str] = []
        total = len(plan)

        for idx, (pid, _, new_t, r, s) in enumerate(plan, start=1):
            ok, msg = await api.update_post(
                pid,
                " ".join(new_t),
                rating=r or None,
                source=s,
                removed_tags=removed_set,
            )
            if ok:
                ok_cnt += 1
            else:
                fail_cnt += 1
                failures.append(f"#{pid}: {msg}")
            print_progress_bar(idx, total, ok_cnt, fail_cnt, pid)

        print()
        for f_msg in failures:
            print(f"  FAILED {f_msg}")
        print(f"Done. Updated: {ok_cnt}, Failed: {fail_cnt} (CSV log: {cfg.debug_csv})")


async def run_metadata_edit(cfg: Config, logger) -> None:
    if not cfg.has_api_creds():
        print("API credentials are MISSING. Open Menu -> 4 first.")
        return

    raw_query = ask("Search tags or Gelbooru list URL")
    query = parse_tags_input(raw_query)
    if not query:
        print("Empty search query.")
        return

    new_rating = ask("New rating (general/sensitive/questionable/explicit, blank = keep)", "")
    new_source = ask("New source (blank = keep)", "")
    if not new_rating and not new_source:
        print("Nothing to update.")
        return

    cap_raw = ask("Max posts (blank = all)", "")
    max_posts = int(cap_raw) if cap_raw.isdigit() and int(cap_raw) > 0 else None

    async with GelbooruAPI(cfg, logger) as api:
        posts = await api.search_posts(query, max_posts=max_posts)
        plan = []
        for p in posts:
            pid = int(p["id"])
            tags_str = str(p.get("tags", ""))
            old_r = str(p.get("rating", ""))
            old_s = str(p.get("source", ""))
            tgt_r = new_rating or old_r
            tgt_s = new_source if new_source != "" else old_s
            if tgt_r != old_r or tgt_s != old_s:
                plan.append((pid, tags_str, old_r, tgt_r, old_s, tgt_s))

        if not plan:
            print("No posts need metadata changes.")
            return

        print(f"\nMetadata Preview ({len(plan)} post(s)):")
        for pid, t_str, old_r, tgt_r, old_s, tgt_s in plan[:15]:
            t_list = t_str.split()
            print(format_compact_diff(pid, t_list, t_list, old_r, tgt_r, old_s, tgt_s))
        if len(plan) > 15:
            print(f"  ... +{len(plan) - 15} more")

        if not cfg.auto_yes:
            if ask("Apply metadata changes? (y/N)", "n").lower() != "y":
                print("Cancelled.")
                return
        else:
            print("Auto-Yes enabled -> applying metadata changes...")

        ok_cnt = fail_cnt = 0
        failures: list[str] = []
        total = len(plan)

        for idx, (pid, t_str, _, tgt_r, _, tgt_s) in enumerate(plan, start=1):
            ok, msg = await api.update_post(pid, t_str, rating=tgt_r or None, source=tgt_s)
            if ok:
                ok_cnt += 1
            else:
                fail_cnt += 1
                failures.append(f"#{pid}: {msg}")
            print_progress_bar(idx, total, ok_cnt, fail_cnt, pid)

        print()
        for f_msg in failures:
            print(f"  FAILED {f_msg}")
        print(f"Done. Updated: {ok_cnt}, Failed: {fail_cnt} (CSV log: {cfg.debug_csv})")


def configure_auth(cfg: Config, logger) -> None:
    print(f"Current -> {cfg.auth_summary()}")
    quick_str = ask("Paste '&api_key=...&user_id=...' or press Enter to keep/enter separately", "")
    if "api_key=" in quick_str or "user_id=" in quick_str:
        parsed = parse_qs(quick_str.lstrip("&?"))
        if "api_key" in parsed and parsed["api_key"]:
            cfg.api_key = parsed["api_key"][0].strip()
        if "user_id" in parsed and parsed["user_id"]:
            cfg.user_id = parsed["user_id"][0].strip()
    else:
        cfg.api_key = ask("Gelbooru api_key", cfg.api_key)
        cfg.user_id = ask("Gelbooru user_id", cfg.user_id)

    cfg.cookie_header = ask("Browser Cookie header", cfg.cookie_header)
    rps = ask("Max requests/sec", str(cfg.max_rps))
    if rps.isdigit() and int(rps) > 0:
        cfg.max_rps = int(rps)
    auto_in = ask("Auto-confirm 'y' without prompting? (y/n)", "y" if cfg.auto_yes else "n").lower()
    if auto_in in ("y", "yes", "true", "1"):
        cfg.auto_yes = True
    elif auto_in in ("n", "no", "false", "0"):
        cfg.auto_yes = False

    cfg.save()
    logger.info(
        "Saved settings to %s",
        cfg.settings_file,
        extra={"action": "configure_auth", "status": "saved"},
    )
    print(f"Saved -> {cfg.auth_summary()}")


async def menu_loop() -> None:
    cfg = Config()
    cfg.load_saved()
    logger = setup_csv_logger(cfg.debug_csv)
    logger.info("CLI started", extra={"action": "startup", "status": "ok"})

    while True:
        print("\n=== Gelbooru CLI ===")
        print(f"[{cfg.auth_summary()}]")
        print("1. Remove tags from posts")
        print("2. Add tags to posts")
        print("3. Modify metadata (rating / source)")
        print("4. Credentials, Cookies & Auto-Yes")
        print("5. Exit")

        choice = input("Choose: ").strip()
        try:
            if choice == "1":
                await run_tag_mutation(cfg, logger, "remove")
            elif choice == "2":
                await run_tag_mutation(cfg, logger, "add")
            elif choice == "3":
                await run_metadata_edit(cfg, logger)
            elif choice == "4":
                configure_auth(cfg, logger)
            elif choice == "5":
                break
            else:
                print("Invalid choice.")
        except Exception as exc:
            logger.error(
                f"Unhandled error: {exc}\n{traceback.format_exc()}",
                extra={"action": "menu_loop", "status": "error"},
            )
            print(f"Error: {exc} (details in {cfg.debug_csv})")


if __name__ == "__main__":
    try:
        asyncio.run(menu_loop())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)