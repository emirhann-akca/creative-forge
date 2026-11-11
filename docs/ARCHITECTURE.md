# Architecture

## Module map

| Module | Responsibility |
|---|---|
| `spec.py` | Pydantic schema for an ad batch. Unknown keys are rejected. Defaults are the production values. `timeline_for()` computes the planned timeline. |
| `pricing.py`, `data/pricing.yaml` | Price table (`as_of: 2026-10`) and `estimate()`, an upper-bound plan that assumes every generation misses the cache. |
| `cache.py` | Content-addressed store with atomic entries (written to a temp dir, then `os.replace`) and `pending.json` for long-running operations. |
| `ledger.py` | `CostLedger` (every call, cached or not) and `BudgetGuard` (reserve before the call, release after). |
| `generator.py` | The only path to a provider: per-key lock, then cache lookup, budget reservation, call, cache write and ledger entry. |
| `providers/` | `base.py` interfaces and request types. `vertex.py` is REST over `google-auth` ADC. `comfyui.py` runs the hook image edit on a local ComfyUI server (SDXL workflows in `data/comfyui/`). `fake.py` is offline media built with ffmpeg and Pillow. |
| `imaging.py` | Text stripping, programmatic effects (dust, mud, frost, darken), headline and button rendering, end-card frames, disclaimer overlay. |
| `stages/video.py` | Gameplay segment, hook clip (trim, patches, mci interpolation), end card, crossfaded assembly. |
| `stages/audio.py` | Mix (padded FX bus, sidechain ducking), master loop, mux. |
| `stages/export.py` | 9:16 and 4:5 encodes with a size-aware maxrate. |
| `qa/` | Checks that return `CheckResult(status, value, threshold, message, details)`. `runner.py` reads the `.forge.json` sidecar. |
| `lint.py` | Plan-time rules learned in production, checked before any spend. Issues carry a severity; `error` (unknown tags) blocks `forge plan` and `forge run`. |
| `naming.py` | `CreativeIds`: the seven names of one export, its deterministic `creative_id` (SHA-256 prefix) and the `cf1.` ad name; `parse()` checks the ID, `find()` locates a name inside a longer string. |
| `tags.py`, `data/tag_taxonomy.yaml` | The tag vocabulary, lint of hand-set tags, and the derived tags (`format`, `length_bucket`, `ai_generated`) added per export. |
| `licenses.py`, `data/model_licenses.yaml` | Licence registry (code and weights licence, commercial status, conditions, source, date checked) and the guard: `models_in_use(spec)` lists every model a real run would call, `check()` gives each a verdict against `targets:` (`refuse`, `warn`, `ok`, `service`), and `--license-override MODEL=REASON` turns one refusal into an `overridden` entry that the ledger records. `Pipeline.run()` checks again before any generation, so library callers get the same guard as the CLI. |
| `pipeline.py` | One job per concept in a private temp dir, a thread pool across concepts, then the blind judge. |
| `report.py` | Self-contained HTML (inline CSS, base64 contact sheets) and a JSON twin. |
| `review.py` | Human decisions per export (`review.json`, keyed to file SHA-256) and the publish gate with its manifest. |
| `ffmpeg.py` | The one place that decides which ffmpeg/ffprobe run (`CREATIVE_FORGE_FFMPEG_DIR`, the managed build, `PATH`), probes a build's features, picks the best AAC encoder that actually opens (`aac_at`, `aac_mf`, `aac`), and decodes every tool output as UTF-8. |
| `setup_ffmpeg.py` | `forge setup ffmpeg`: the pinned 9.0.2 builds per platform (URL, size, SHA-256), verified download, extraction of named members only, feature probe before activation, one-rename swap. |
| `doctor.py` | `forge doctor`: ffmpeg, OCR, folders, optional ComfyUI and ADC presence as `ok/warn/fail/info` items; text or JSON. |
| `paths.py`, `fsutil.py` | platformdirs locations (`CREATIVE_FORGE_HOME` overrides) and renames/deletes that retry while Windows reports a sharing violation. |
| `ui/` | `forge ui`: FastAPI + Jinja2 + vendored htmx. Runs list, report, range-request players, approve/reject through `review.decide`, background demo, doctor. Loopback `Host` check and a per-process token on every POST. |
| `perf/` | `store.py`: SQLite (`perf_rows`, `creatives`, `imports`) with upserts on (network, account, level, date, date_end, ad, country). `http.py`: GET with retries, `Retry-After`, a `Redactor` and transport errors re-raised without their URL. `meta.py`, `applovin.py`, `csvimport.py`: the importers. `commands.py`: `forge perf`. |
| `ideate/` | `library.py`: lesson library, hashing or Vertex embedder, JSON vector index. `tools.py`: function declarations and their local implementations. `agent.py`: the function-calling loop, the Vertex chat model and the scripted one. |

## One concept, step by step

1. **Gameplay segment.** The clip is cut `[in_s, out_s)`, optionally cropped (for example to drop a status bar), cover-scaled to the render size, retimed by `speed` and resampled to the output fps. The **real frame** is the first frame of this output. That makes it the exact pixels the viewer will see after the cut: same crop, same scaling, no second colour pass.
2. **First frame.** `text_regions` are blurred out, then `effects` are painted on the real frame. If `image_prompt` is set, the image model adds only the hand or tool. If `edit_region` is set, every pixel outside it is copied back from the prepared frame after the edit (ComfyUI also uses it as the inpaint mask).
3. **Hook video.** Veo is called with `image` = first frame, `lastFrame` = real frame, `durationSeconds`, `generateAudio`, `sampleCount` and the negative prompt. The operation name is saved on submit. Polling runs every 10 s for up to 120 polls.
4. **Hook clip.** The pipeline cuts at `cut_s`, the default being duration − 1 s. It overlays `patches` (regions of the real frame, feathered, faded in at `from_s`), then runs `minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1` and `fps=<output>`. The hook's own audio goes through `loudnorm=I=-16:TP=-3:LRA=11` with a 0.25 s fade-out.
5. **Assembly.** `xfade` the hook into the gameplay, then the gameplay into the end card, each over `crossfade_s`. The disclaimer overlay is enabled only before the first crossfade.
6. **Mix.** Hook audio sits at 0 and gameplay audio at the gameplay start, with optional SFX (delay, gain, pitch). These are summed with `amix=normalize=0` and padded with `apad=whole_dur=<total>`. Music is faded in (0.4 s) and out (0.6 s), padded and trimmed, then ducked under the bus with `sidechaincompress=threshold=0.05:ratio=4:attack=5:release=250`. A final limiter at 0.95.
7. **Master.** See below.
8. **Mux and export.** The pipeline muxes with an explicit `-t` (never `-shortest`), then encodes 9:16 and 4:5 with `crf 20`, `maxrate = min(3600k, size budget)`, `bufsize = 2 x maxrate`. Each export is named after its ad name and gets a `.forge.json` sidecar with its creative ID, ad name, tags, the models that made it (with licences), format, size, timeline and overlay regions.
9. **QA.** Every check runs on every export. The 4:5 variant skips seam, cadence and OCR, which were already measured on the 9:16 master it is derived from.

After all concepts finish, the 9:16 exports are shuffled, code-named `V01..Vn`, and judged `judge_runs` times each.

## Timeline

With hook length `H` (after the cut), gameplay length `G`, end card `E` and crossfade `X`:

```
hook_end = gameplay_start = H - X
end_card_start = gameplay_end = H - X + G - X
total = H + G + E - 2X
```

The hook-vs-gameplay loudness windows are `[0, hook_end)` and `[gameplay_start + 0.6, gameplay_end)`. The 0.6 s skip keeps the crossfade and the hook's fade-out out of the gameplay measurement.

## Master loop

This ports the production script, plus two changes.

```
offset = gameplay_lufs - hook_lufs          # dB added to the hook, ramped over 0.4 s around hook_end
gain   = 0
repeat up to 6 times:
    envelope(offset) -> volume(gain) -> resample 192 kHz -> alimiter(limit) -> resample 48 kHz -> AAC encode
    measure the ENCODED file: integrated, true peak, hook, gameplay
    stop if |I - target| < 0.3 and |hook - gameplay| < 0.8 and true peak <= ceiling
    gain   += (target - I) / slope          # change 1: secant slope, clamped to [0.25, 1]
    offset -= hook - gameplay
    if true peak > ceiling: limit *= 10^((ceiling - TP - 0.1) / 20)   # change 2: encoder overshoot
```

Why it loops at all: the limiter squashes transient gameplay SFX harder than the smoother AI hook audio. A correction computed before the limiter is not the correction you get after it. Why the secant step: when the limiter is working hard, 1 dB of gain buys less than 1 dB of loudness, so the original "add the error" step needed every round it had. Limiting at 192 kHz approximates a true-peak limiter. Measuring after encoding catches encoder overshoot.

## Caching and money

- Cache key: `sha256(kind:provider, canonical JSON of the request)`. Every `Path` in the request is replaced by the SHA-256 of the file's bytes, and provider-side settings that change the output (`Provider.cache_params()`, e.g. ComfyUI's sampler, seed and workflow hashes) are folded in. Fake and real providers never share entries.
- The `Generator` holds one lock per key. Two jobs asking for the same generation at once pay once: the second waits, then hits the cache.
- `BudgetGuard.reserve()` runs before every billable call, and the guard counts spent plus reserved. An unpriced call (the judge, by default) is refused while a budget is set.
- `VertexClient.post(idempotent=False)` is used to submit long-running jobs. It retries only on 429, 503 and connections that never reached the server (refused, unresolvable, connect timeout). It never retries on a read timeout, a dropped connection or a 500, where the job may exist and a retry could pay twice. Polling is idempotent and retries on all of them.

## Adding a provider

Implement one of the interfaces in `providers/base.py` and set `name` (it is part of the cache key), `billable`, and `local` for a provider that runs on this machine (no list price). Return anything that changes the output but is not in the request from `cache_params()`. Then return a `ProviderResult(files, info)` and wire it into `providers/__init__.py:build_providers`. Caching, budget and ledger come from the `Generator` for free.

## The ideation loop

```
contents = [user: brief + N]
repeat up to --max-steps:
    response = Gemini generateContent(systemInstruction, contents, functionDeclarations, mode ANY)
    append the model turn verbatim                      # keeps thought signatures
    for each functionCall: run the local tool, append its functionResponse
    stop when submit_concepts accepted the set          # AdSpec validation + lint, same code as forge run
```

Tool errors go back to the model as `{"error": ...}`; they do not end the run. `strip-text` cannot be satisfied from text (it needs pixel boxes of the real frame), so it is written into the spec as a TODO instead of blocking acceptance.
