"""``forge campaign``: approved creatives into paused app-install campaigns on Meta, TikTok and Google Ads.

Safety is the product, so the order of things is fixed:

1. ``plan``: a campaign YAML, a ``forge publish`` folder and the network adapters give the exact list of objects that
   would be created (every payload, nothing sent). Store URLs that are placeholders or not a store listing, exports
   that did not pass QA and a person's review, and budgets above the configured caps are refused here.
2. ``create``: the plan is executed in the configured mode (``mock``, ``sandbox`` or ``live``). Every object is created
   PAUSED, every call goes into an append-only, hash-chained audit log, and every object carries an idempotency key
   so a retry or a re-run never creates it twice.
3. ``activate``: a separate step that needs the typed confirmation phrase with the daily budget in it, checks the
   per-campaign daily and total caps again, and re-reads the budget from the network before switching anything on.
4. ``sync`` / ``status``: delivery and spend come back into the performance store; the test cell of ``perf.cells``
   turns them into pause / scale RECOMMENDATIONS. Ads are paused automatically only when ``automation.auto_pause`` is
   true in the config; scaling is never automatic.

AppLovin, Unity Ads and Mintegral document campaign APIs, but none documents that a campaign can be created paused,
so they get a manual upload pack instead of API calls (see ``networks/manual.py``).
"""
