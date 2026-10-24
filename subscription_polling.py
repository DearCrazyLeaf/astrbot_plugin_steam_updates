"""Fetch once per source, then select and acknowledge delivery per session."""

import json
import time
from datetime import datetime


class SubscriptionPollingMixin:
    def _subscription_state_key(self, profile):
        return "subscriptions:" + self._target_ref(profile.target.umo)

    def _subscription_snapshot(self, profile):
        return {
            "games": list(profile.games),
            "workshop": list(profile.workshop),
            "free_games": profile.free_games,
            "enabled": profile.enabled,
            "baseline_token": profile.baseline_token,
        }

    def _profile_still_current(self, profile):
        return (
            not self._stop_event.is_set()
            and self._is_current_poll_instance()
            and bool(self._cfg("enable_push", True))
            and self._resolve_subscriptions().get(profile.target.umo) == profile
        )

    async def _poll_once(self):
        if (
            self._stop_event.is_set()
            or not self._is_current_poll_instance()
            or not bool(self._cfg("enable_push", True))
        ):
            return
        try:
            profiles = list(self._resolve_subscriptions().values())
        except ValueError as exc:
            self._log_warn(
                "subscription", "invalid configuration; poll skipped", error=str(exc)
            )
            return
        if not profiles:
            return
        await self._ensure_http_client()
        enabled = [p for p in profiles if p.enabled]
        appids = list(dict.fromkeys(a for p in enabled for a in p.games))
        workshop_ids = list(dict.fromkeys(a for p in enabled for a in p.workshop))
        count = max(self._get_max_days(), 50)
        fetched, failures = await self._fetch_news_for_appids(
            appids, count, only_today=True
        )
        workshop_items = (
            await self._fetch_workshop_news_items(workshop_ids) if workshop_ids else []
        )
        free_items = (
            await self._fetch_free_game_items()
            if any(p.free_games for p in enabled)
            else None
        )
        recent = {
            a: self._filter_recent_days(items, self._get_max_days())
            for a, items in fetched.items()
        }
        state = self._load_state()
        if "subscriptions_schema" not in state:
            # Preserve a recoverable pre-migration snapshot, never overwrite it.
            path = self._state_path.with_name("state.before-subscriptions-v1.json")
            try:
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(state, stream, ensure_ascii=False, indent=2)
            except FileExistsError:
                pass
            state["subscriptions_schema"] = "1"

        query_text = datetime.now().strftime("%Y/%m/%d %H:%M")
        deadline = time.monotonic() + self._llm_round_timeout_sec()
        section_cache, batches = {}, {}
        global_games = set(self._normalize_appids(self._cfg("steam_appids", [])))
        global_workshop = set(
            self._normalize_workshop_item_ids(self._cfg("workshop_item_ids", []))
        )

        def add_batch(profile, kind, sections, acknowledgements):
            if not sections:
                return
            # All presentation inputs participate; different game selections,
            # error notices, free-game choices and display modes cannot leak.
            key = (kind, profile.mode, repr(sections))
            group = batches.setdefault(
                key, {"sections": sections, "entries": [], "kind": kind}
            )
            group["entries"].append((profile, acknowledgements))

        for profile in profiles:
            if not self._profile_still_current(profile):
                continue
            snapshot_key = self._subscription_state_key(profile)
            try:
                previous = json.loads(state.get(snapshot_key, "null"))
            except (ValueError, TypeError):
                previous = None
            if not isinstance(previous, dict):
                previous = None
            paused = previous is not None and (
                not previous.get("enabled", True)
                or previous.get("baseline_token", "") != profile.baseline_token
            )
            if not profile.enabled:
                state[snapshot_key] = json.dumps(self._subscription_snapshot(profile))
                continue
            target = profile.target
            pending, acknowledgements = {}, {}

            def needs_baseline(source, source_id, migrated):
                ready = self._target_delivery_state_key(
                    target, "subscription_ready", source + ":" + source_id
                )
                was_selected = (
                    (
                        source_id in previous.get(source, [])
                        if source != "free_games"
                        else previous.get("free_games", False)
                    )
                    if previous
                    else False
                )
                changed = previous is not None and (paused or not was_selected)
                initial = profile.explicit and not migrated
                return ready, changed or (ready not in state and initial)

            for appid in profile.games:
                if appid in failures or appid not in fetched:
                    continue  # A failed initial fetch must not create a baseline.
                items = recent.get(appid, [])
                key = self._target_delivery_state_key(target, "news", appid)
                ready, baseline = needs_baseline(
                    "games", appid, profile.legacy and appid in global_games
                )
                if baseline:
                    known = self._decode_news_seen_state(state.get(key, ""))
                    state[key] = self._encode_seen_gids([i.gid for i in items] + known)
                seen = self._target_news_seen_gids(state, target, appid, items)
                state[ready] = "1"
                new = [i for i in items if i.gid not in seen]
                if new:
                    pending[appid] = new
                    acknowledgements[key] = self._encode_seen_gids(
                        [i.gid for i in new] + list(seen)
                    )

            free_ack = {}
            new_free, active_free = [], []
            if profile.free_games and free_items is not None:
                key = self._target_delivery_state_key(target, "free_games", "steam")
                ready, baseline = needs_baseline("free_games", "steam", profile.legacy)
                if key not in state:
                    state[key] = (
                        state.get(self._free_games_state_key(), "[]")
                        if profile.legacy
                        else "[]"
                    )
                if baseline:
                    state[key] = self._encode_seen_gids([i.gid for i in free_items])
                new_free, _ = self._split_new_free_game_items(
                    free_items, self._decode_news_seen_state(state[key])
                )
                state[ready] = "1"
                active_free = list(free_items)
                free_ack[key] = self._encode_seen_gids(
                    [i.gid for i in free_items]
                    + self._decode_news_seen_state(state[key])
                )
            if pending:
                signature = tuple(
                    (a, tuple(i.gid for i in items)) for a, items in pending.items()
                )
                if signature not in section_cache:
                    section_cache[signature] = await self._build_sections(
                        list(pending), pending, llm_deadline=deadline
                    )
                sections = self._merge_game_sections_with_free_games(
                    section_cache[signature], active_free, free_only_when_no_news=True
                )
                add_batch(profile, "game", sections, {**acknowledgements, **free_ack})
            elif new_free:
                sections = self._merge_game_sections_with_free_games(
                    [], new_free, free_only_when_no_news=True
                )
                add_batch(profile, "game", sections, free_ack)

            workshop_pending, workshop_ack = [], {}
            for item in workshop_items:
                item_id = item.gid.split(":", 1)[0]
                if item_id not in profile.workshop:
                    continue
                key = self._target_delivery_state_key(target, "workshop", item_id)
                migrated = profile.legacy and item_id in global_workshop
                ready, baseline = needs_baseline("workshop", item_id, migrated)
                if key not in state and migrated and "workshop:" + item_id in state:
                    state[key] = state["workshop:" + item_id]
                first = key not in state
                if baseline or (first and not self._workshop_push_on_first_seen()):
                    state[key] = str(item.date)
                try:
                    last = int(state.get(key, "0"))
                except (TypeError, ValueError):
                    last = 0
                state[ready] = "1"
                if item.date > last:
                    workshop_pending.append(item)
                    workshop_ack[key] = str(item.date)
            if workshop_pending:
                signature = ("workshop", tuple(i.gid for i in workshop_pending))
                if signature not in section_cache:
                    section_cache[signature] = await self._build_workshop_sections(
                        workshop_pending
                    )
                add_batch(profile, "workshop", section_cache[signature], workshop_ack)

            errors = [
                a
                for a in profile.games
                if a in failures and self._query_error_notice_due(state, target, a)
            ]
            if errors:
                signature = ("errors", tuple(errors))
                if signature not in section_cache:
                    section_cache[signature] = await self._build_query_error_sections(
                        errors
                    )
                add_batch(
                    profile,
                    "game",
                    section_cache[signature],
                    {
                        self._target_delivery_state_key(target, "query_error", a): str(
                            int(time.time())
                        )
                        for a in errors
                    },
                )
            state[snapshot_key] = json.dumps(self._subscription_snapshot(profile))

        if not self._is_current_poll_instance():
            return
        self._save_state(state)  # Persist migrations/baselines before any send.
        for batch in batches.values():
            entries = [
                (p, ack)
                for p, ack in batch["entries"]
                if self._profile_still_current(p)
            ]
            if not entries:
                continue
            sections = batch["sections"]
            latest = max(
                (i.date for sec in sections for i in sec.updates if i.date),
                default=int(time.time()),
            )
            result = await self._deliver_poll_payload(
                [p.target for p, _ in entries],
                sections,
                datetime.fromtimestamp(latest).strftime("%Y/%m/%d %H:%M"),
                query_text,
                batch["kind"],
                entries[0][0].mode,
            )
            accepted = set(result.succeeded) | set(result.uncertain)
            if result.failed:
                self._log_warn(
                    "subscription",
                    "delivery failed; targets will retry",
                    failed_count=len(result.failed),
                    card_kind=batch["kind"],
                )
            for profile, ack in entries:
                if profile.target not in accepted:
                    continue
                state.update(ack)
                if profile.target in result.uncertain:
                    prefix = self._target_delivery_state_key(profile.target, "news", "")
                    for key, value in ack.items():
                        if key.startswith(prefix):
                            state[
                                self._target_delivery_state_key(
                                    profile.target, "news_uncertain", key[len(prefix) :]
                                )
                            ] = value
            if not self._is_current_poll_instance():
                return
            self._save_state(state)
