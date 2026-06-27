# Project 5 — Mixtape Bug Hunt: Submission

**Branch:** `bugfix/mixtape`
**Bugs fixed:** 5 of 5 (3 required + 2 stretch) plus a regression test (stretch).

---

## AI Usage

I used an AI coding assistant (Claude) throughout this project. How it was actually used:

- **Codebase orientation.** I had the assistant summarize each `services/*.py` file's responsibility and trace the route → service call chains (e.g. `POST /songs/<id>/rate` → `routes/songs.py` → `notification_service.rate_song`). This is what the codebase map below is built from. I verified every claim by reading the files myself — the routes are thin and delegate immediately to services, which the AI summary matched.
- **Reproduction over guessing.** Rather than trusting an AI diagnosis, I ran the existing test suite and wrote small throwaway scripts against the seeded DB to *observe* each bug before changing code. This is where AI was least needed and most error-prone — it's good at explaining code I point it at, unreliable at guessing the defect blind.
- **Where AI's first instinct was wrong / incomplete — Issue #3.** The obvious AI explanation for "duplicate songs in search" is "the `outerjoin` on `song_tags` produces one row per tag." That's the *intended* bug and the right root cause, **but it does not actually reproduce in this environment.** I verified by running `search_songs("Crown")` directly: a 3-tag song returned exactly **one** result, and the provided `test_search_no_duplicates_multi_tag_song` test **passes unmodified**. The reason is that SQLAlchemy 2.0's legacy `Query`, when selecting full entities, de-duplicates rows by primary-key identity — so the fan-out rows collapse back to one `Song`. I only trusted this after reading the SQLAlchemy behavior and confirming it empirically, not because the AI asserted it. The fix is still correct (remove the needless join), but I documented the nuance honestly rather than claiming a reproduction I couldn't produce.
- **Targeted factual checks.** I used AI to confirm Python's `datetime.weekday()` returns 0=Monday…6=Sunday (relevant to Issue #1) and then verified against the provided `test_streak_increments_on_sunday` test, whose comments already assert `weekday() == 6` for Sunday.

Net: AI accelerated *navigation and explanation*; reproduction and root-cause confirmation were done by running code.

---

## Codebase Map

Mixtape is a Flask app using the **app-factory + blueprints + service-layer** pattern. Every route does only input parsing and JSON formatting; all business logic lives in `services/`.

### Main files and their roles

- **`app.py`** — Flask application factory (`create_app`). Owns the single `db = SQLAlchemy()` instance, registers four blueprints under URL prefixes (`/songs`, `/playlists`, `/users`, `/feed`), and calls `db.create_all()`. Note: the app *must* be started with `FLASK_APP=app:create_app flask run` — running `python app.py` double-imports and breaks SQLAlchemy.
- **`models.py`** — Defines all entities. **Models:** `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`. **Association tables:** `friendships` (symmetric many-to-many on `User`, stored as two directed rows per friendship), `song_tags` (many-to-many `Song`↔`Tag`), and `playlist_entries` (the join between `Playlist` and `Song`, carrying an explicit `position` integer, `added_by`, and `added_at`). Key design points: a song's playlist position is explicit (not insertion order); `Rating` has a `UniqueConstraint(user_id, song_id)` so a user has at most one rating per song; `Song.tags` uses `lazy="subquery"`, meaning tags are loaded by a *separate* query, not by joining in the main query.
- **`routes/`** — Thin HTTP layer.
  - `songs.py`: `/songs/search`, `/songs/<id>`, `/songs/<id>/rate` (POST), `/songs/<id>/listen` (POST).
  - `playlists.py`: create, get metadata, `GET /playlists/<id>/songs`, `POST /playlists/<id>/songs`.
  - `users.py`: user profile, `/users/<id>/streak`, `/users/<id>/notifications`, mark-read.
  - `feed.py`: `/feed/<id>/listening-now` and `/feed/<id>/activity`.
- **`services/`** — All logic: `streak_service`, `feed_service`, `search_service`, `notification_service`, `playlist_service`.
- **`seed_data.py`** — Drops and rebuilds the DB with 5 users, 13 songs (deliberately mixing 0-tag, 1-tag, and 3-tag songs to exercise search), 3 playlists, recent + old listening events, streak state, and one pre-existing playlist-add notification. The seed comments double as a spec for the intended (post-fix) behavior.
- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py` ship with the repo and already encode *correct* behavior, so several of them fail before the fixes (built-in reproductions). I added `test_notifications.py`.

### Data flow trace — rating a song (Issue #4's feature)

1. Client sends `POST /songs/<song_id>/rate` with JSON `{user_id, score}`.
2. `routes/songs.py::rate()` parses the body, validates presence, and calls `notification_service.rate_song(user_id, song_id, int(score))`.
3. `rate_song()` validates the 1–5 range, loads the `Song` and the rating `User`, then either updates the existing `Rating` (one-per-user-per-song) or inserts a new one, and commits.
4. **Intended:** it should then notify the song's original sharer (`song.shared_by`) that their song was rated — exactly as `add_to_playlist()` notifies the sharer when a song is added to a playlist. This notification step was the missing piece (Issue #4).
5. The sharer later reads notifications via `GET /users/<id>/notifications` → `get_notifications()`.

### Data flow trace — "Friends Listening Now" (Issue #2's feature)

`GET /feed/<id>/listening-now` → `feed_service.get_friends_listening_now()`: resolves the user's friend IDs, computes `cutoff = now - RECENT_THRESHOLD`, queries `ListeningEvent`s for those friends newer than the cutoff ordered by most-recent, then de-duplicates to one (the latest) event per friend. The window width (`RECENT_THRESHOLD`) is what decides whether "listening now" really means *now* — see Issue #2.

### Patterns I noticed

- **Routes never touch the DB directly for logic** (one small exception: `users.py` reads a `User` for the profile endpoint). Everything else delegates to a service.
- **`db.session.get(Model, id)` + `raise ValueError`** is the universal "load or fail" idiom; routes catch `ValueError` and map it to 404/400.
- **Notifications are a side effect of an action**, created via the shared `create_notification()` helper. Comparing the two action services (`add_to_playlist` vs `rate_song`) side by side is what exposed Issue #4.
- **Seed comments are a behavioral spec** — they explicitly say which events should/shouldn't appear after a fix.

---

## Root Cause Analyses

### Issue #1 — My listening streak keeps resetting

- **How I reproduced it.** Ran `pytest tests/test_streaks.py`. The shipped test `test_streak_increments_on_sunday` failed with `assert 1 == 2`: listening Saturday then Sunday produced a streak of 1 instead of 2. The Saturday→Sunday dates in the test (`2024-06-15` → `2024-06-16`) are the trigger condition.
- **How I found the root cause.** The README pointed to `streak_service.py`. I read `update_listening_streak` top-down. The increment branch was `elif days_since_last == 1 and today.weekday() != 6:`. I confirmed with a quick check that Python's `datetime.weekday()` returns **6 for Sunday** (0=Monday). The moment of certainty: the test's own comment annotates `sunday = ... # weekday() == 6`, so the guard `weekday() != 6` is exactly the Sunday case.
- **The root cause.** The consecutive-day increment was gated on `today.weekday() != 6`. Since `weekday()` returns 6 on Sunday, *any* listen recorded on a Sunday — even one day after the previous listen — skipped the increment branch and fell through to `else`, which resets the streak to 1. So every Sunday silently reset every user's streak. There is no business reason for a weekday to affect a consecutive-day streak; the condition was simply wrong.
- **Fix and side-effect check.** Removed `and today.weekday() != 6`, leaving `elif days_since_last == 1:`. Re-ran the full streak suite: all 5 pass, including same-day no-double-count, skipped-day reset, and new-user start-at-1. The boundary cases on both sides of "1 day" (0 days = no change, ≥2 days = reset) are untouched.

### Issue #2 — Friends Listening Now shows people from yesterday

- **How I reproduced it.** Seeded the DB and called `get_friends_listening_now()` for user **darius** (friends: nova, simone). nova's most recent listening event is **~2 hours old**, yet nova appeared in "listening now." With the threshold at 24h, a 2-hour-old (or up-to-yesterday) listen counts as "now."
- **How I found the root cause.** Read `feed_service.py`. The module constant `RECENT_THRESHOLD = timedelta(hours=24)` feeds `cutoff = now - RECENT_THRESHOLD`. The seed file's comments are explicit: events "within the past 30 minutes … should appear" and events "1–14 days ago … should NOT appear in 'listening now' after fix." A 24-hour window contradicts that spec directly.
- **The root cause.** "Listening now" is meant to be a live presence indicator, but the recency window was a full day. Any friend whose latest listen was anywhere in the last 24 hours (i.e. earlier today or yesterday) was reported as currently listening. (Note: nova's *own* feed didn't expose it because all of nova's friends happened to also have a fresh event, and the per-friend dedup hid the stale ones — darius's feed is where it surfaces.)
- **Fix and side-effect check.** Changed `RECENT_THRESHOLD` to `timedelta(minutes=30)`, matching the seed's "past 30 minutes" spec. After the fix, darius's feed shows only simone (15 min ago) and drops nova (120 min ago). I checked `get_activity_feed`, which shares the same module — it intentionally has no recency filter (its docstring says so) and still returns the full history (5 events), so it's unaffected.

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

- **How I reproduced it.** Seeded the DB; recorded nova's notification count (1 — the pre-seeded playlist-add). Had **darius** rate one of **nova's** shared songs via `rate_song()`, then re-read nova's notifications: still 1, and the only type was `song_added_to_playlist`. No rating notification was ever created.
- **How I found the root cause.** Both behaviors live in `notification_service.py`, so I diffed the two action functions line by line. `add_to_playlist()` ends with a guarded `create_notification(..., "song_added_to_playlist", ...)` for `song.shared_by`. `rate_song()` saved/updated the `Rating`, committed, and `return rating` — with **no `create_notification` call at all.**
- **The root cause.** This is architectural, not a typo: the notification step that the playlist path implements was simply never written into the rating path. The data write (the `Rating`) succeeded, so the action "worked," but the side effect that informs the sharer was absent.
- **Fix and side-effect check.** After the commit, added a guarded notification mirroring `add_to_playlist`: `if song.shared_by != user_id: create_notification(song.shared_by, "song_rated", "<rater> rated your song '<title>' <score>/5.")`. The `shared_by != user_id` guard means rating your own song produces nothing (matches the playlist path's self-action guard). Verified: darius rating nova's song now yields a `song_rated` notification; nova rating her own song adds none; the re-rate/update path still works. I placed the call *after* `db.session.commit()` so the rating is persisted before the notification, consistent with the surrounding code.

### Issue #5 — The last song in a playlist never shows up (stretch)

- **How I reproduced it.** Ran `pytest tests/test_playlists.py`. `test_playlist_returns_all_songs` failed (got 4, expected 5) and `test_playlist_returns_songs_in_order` failed (the list ended at "Track 4"). A 5-song playlist returned 4 songs, always missing the last by position.
- **How I found the root cause.** Read `get_playlist_songs` in `playlist_service.py`. The query is correct — it joins `playlist_entries` and orders by `position` ascending. The defect is the very last line: `return [song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice drops the final element.
- **The root cause.** An off-by-one: the result list was sliced with `[:-1]`, discarding the last (highest-position) song on every non-empty playlist. The query and ordering were fine; only the return slicing was wrong. (The function's docstring even claims "returns all songs," contradicting the code.)
- **Fix and side-effect check.** Changed `songs[:-1]` to `songs`. Re-ran the playlist suite: all 3 pass, including `test_empty_playlist_returns_empty_list` — an empty playlist already returned `[]` (the slice wasn't what produced the empty case), so the boundary at zero songs is still correct.

### Issue #3 — The same song keeps showing up twice in search (stretch; reproduction caveat below)

- **How I attempted to reproduce it.** Seeded the DB (it deliberately includes 3-tag songs "to expose Issue #3") and called `search_songs("Crown")`, `("Harlem")`, etc. for multi-tag songs. **Each returned exactly one result — the bug did not reproduce.** The shipped `test_search_no_duplicates_multi_tag_song` also **passes unmodified**, even though its comment says "bug causes it to be 3."
- **How I found the root cause (and why it's masked here).** I read `search_service.py`: the query does `.outerjoin(song_tags, Song.id == song_tags.c.song_id)` and then filters only on `Song.title`/`Song.artist`. A song with N tags produces N joined rows — the textbook cause of duplicate search results. **Why it doesn't surface in this environment:** SQLAlchemy 2.0's legacy `Query`, when selecting whole entities, de-duplicates result rows by primary-key identity, so the fan-out rows collapse back to a single `Song`. The intended defect is real and the root cause is the unnecessary join; the specific runtime just happens to hide its symptom. I confirmed this empirically (one row returned) rather than assuming.
- **The root cause.** The `outerjoin` on `song_tags` is gratuitous: tags are not referenced in the filter and are loaded separately via the `Song.tags` relationship (`lazy="subquery"`). Its only effect is to multiply result rows by tag count, which *would* duplicate songs under any code path that selects rows/columns instead of de-duplicated entities (e.g. selecting specific columns, `.distinct()`-less raw rows, or a different SQLAlchemy configuration).
- **Fix and side-effect check.** Removed the join entirely so the query returns one row per matching song *by construction*, independent of ORM dedup behavior, and dropped the now-unused `Tag`/`song_tags` imports. Verified search still returns the correct songs **with their tags intact** (tags load via the relationship), no duplicates, and all 5 search tests pass. This is the smallest fix that addresses the actual root cause rather than papering over it with `.distinct()`.

---

## Stretch — Regression Test

`tests/test_notifications.py` adds `test_rating_notifies_song_sharer`, which would have caught Issue #4: it rates a sharer's song as another user and asserts exactly one `song_rated` notification is created. I verified it **fails against the pre-fix `rate_song`** (`len([]) == 0`) and **passes after the fix**. A companion `test_self_rating_does_not_notify` locks in the self-rating guard.

---

## Commit History (`git log --oneline main..bugfix/mixtape`)

```
aafbc74 test: add regression test for rating notification (Issue #4)
feddf51 fix: remove unnecessary song_tags join that duplicated search results
aa173f5 fix: include the last song when listing playlist songs
47fd8e4 fix: notify song sharer when a friend rates their song
229a488 fix: narrow 'Friends Listening Now' window to 30 minutes
20b26e1 fix: increment listening streak on Sunday instead of resetting
```

<img width="791" height="113" alt="git log" src="https://github.com/user-attachments/assets/8d63e09c-b259-4d02-ae10-41472a2b39f3" />


**Full suite after all fixes:** `15 passed`.
