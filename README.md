# creative-forge

[![ci](https://github.com/emirhann-akca/creative-forge/actions/workflows/ci.yml/badge.svg)](https://github.com/emirhann-akca/creative-forge/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Spec-driven pipeline for mobile game video ads with an AI-generated hook and real gameplay. A YAML spec goes in. Mastered 9:16 and 4:5 videos come out, each with a QA report that records what was measured and the threshold it was judged against. A person approves what ships.

```
forge demo                                  # whole pipeline offline: fake providers, synthetic gameplay
forge ui                                    # local web page: runs, QA reports, players, approve / reject
forge ideate --game "..." --clips a.mp4     # draft concepts with a tool-calling LLM + lesson retrieval
forge plan   examples/ads.yaml              # cost estimate, spec lint and model licences, no calls
forge run    examples/ads.yaml --budget-usd 15
forge review out/puzzle-ads                 # approve / reject each export, with a note
forge publish out/puzzle-ads --to deliver/  # only approved files; refuses on failed QA unless overridden
forge setup ffmpeg                          # pinned ffmpeg 9.0.2, SHA-256 verified, no admin rights
forge doctor                                # ffmpeg, OCR, folders, ComfyUI, credentials; exit 1 on a blocker
```

![QA report from forge demo](docs/img/qa-report.png)

*The HTML report from `forge demo`: contact sheet across the cut, generation checks, delivery checks with measured value and threshold, and a judge that warns when its two runs disagree.*

**What is in it**

- **The production recipe**: an image model edits a real gameplay frame, Veo 3.1 animates it with `lastFrame` set to the real first gameplay frame, a 0.3 s crossfade into the same level's gameplay, an end card, and a loudness-matched master. 9:16 and a 4:5 variant that never crops gameplay.
- **14 production defects, each prevented in the pipeline, measured by a check, or both**, with a test on synthetic media for each ([table](#qa-checks-and-the-defects-behind-them)).
- **Paid calls treated like money**: content-hash cache, the list price reserved against `--budget-usd` before each call, and a Veo job's operation name saved before polling so a crash resumes instead of paying twice.
- **A local open-model path**: SDXL through a local ComfyUI server for the hook image edit, with the workflow shipped as JSON.
- **Agentic ideation**: Gemini function calling over five local tools, with retrieval over the lesson library. It stops when its concepts pass the pipeline's own validation and lint.
- **Human in the loop**: `forge review` records decisions against file hashes; `forge publish` copies only approved, QA-clean exports with a manifest.
- **Runs on Windows 10/11, macOS (Apple Silicon) and Linux**: one-line installers that need no admin rights, a pinned ffmpeg, RapidOCR where Apple Vision is missing, and CI that installs the built wheel the way a user would on each OS, then runs the demo in a folder named `Çalışma Ş/İğ`.
- 190 tests (about 40 s), run on Windows, macOS and Ubuntu with Python 3.11, 3.12 and 3.13; any text I/O that depends on the locale's encoding fails them. `forge demo` takes about 10 s on an M4 Mac.

![Demo export frames](docs/img/demo-frames.png)

*`forge demo` output, synthetic content: the fake hook, the hook landing on the real frame, real gameplay, end card. Each pair shows 9:16 and the 4:5 variant.*

## Why it exists

I used these scripts in October 2026 to produce my studio's UA video ads for *Acorn Mart: Goods Sorting*, our goods-sort puzzle game [live on Google Play](https://play.google.com/store/apps/details?id=com.emirhan.cartherounity). The final round was 7 ads, 25–30 s each, delivered in 9:16 and 4:5, mastered to about −14 LUFS with true peak at or below −2.6 dBTP. Gemini 3 Pro Image edited the frames, Veo 3.1 animated them and Lyria 3 Pro made the music.

The scripts worked, but every QA round found a defect a quick watch had missed. They were one-off, single-machine scripts. This repo turns them into a tool someone else can run: each defect became a requirement that the pipeline prevents, a check measures, or both.

## Design decisions

- **The AI shot has to end on real pixels.** I set Veo's `lastFrame` to the first frame of the gameplay segment *after* it has been cropped, scaled and retimed, so the hook lands on exactly what follows the cut. The cut is where a viewer notices an ad is fake; a 0.3 s crossfade covers the small remaining difference, and `hook_landing` and `seam` measure it.
- **The image model is asked for the smallest edit, and the code enforces the rest.** Gemini redrew the products when I asked it to cover the board in dust, so dust and mud are painted on the real frame in numpy and the model only adds the hand. With `hook.edit_region`, every pixel outside the box is copied back from the real frame after the edit. A prompt can only ask; the composite guarantees.
- **Loudness and true peak are measured after encoding.** ffmpeg's built-in AAC encoder pushed a limited master 6.7 dB over its ceiling while the pre-encode measurement looked fine. The master loop encodes every round and measures the file that ships. It uses a secant step, because behind a working limiter 1 dB of gain buys less than 1 dB of loudness.
- **The budget is reserved before a call, not checked after.** With concepts rendering in parallel, "spent < budget" lets two jobs both pass the check and overspend. Each call reserves its list price under a lock first; a call without a known price is refused while a budget is set.
- **A paid submit is never retried on an ambiguous error.** Only 429, 503 and connections that never reached the server are retried. A read timeout or a 500 may mean the Veo job exists and is billed, so the run stops and says so instead of submitting again. Once a submit is accepted, its operation name is saved before the first poll, so a crash or a polling timeout after that resumes the same job.
- **Fakes over mocks for media.** The fake providers make real files with ffmpeg: a 24 fps clip with audio that dissolves onto `lastFrame`. Tests and the demo therefore run the real interpolation, loudness loop and QA on real media. Mocks appear only at the HTTP boundary.
- **A check never passes silently.** Every result carries the measured value and the threshold. When a check cannot run (no OCR backend, no timeline), it reports `skip` with the reason.
- **A person approves bytes, not names.** A review decision stores the file's SHA-256, so a re-render voids it, and a failed check blocks publishing unless someone writes an override reason into the manifest.
- **The agent can only act through the pipeline's own validators.** `forge ideate`'s tools call the same `AdSpec`, `lint` and `estimate` code as `forge run`, the loop has a hard step cap, and anything a model cannot know (pixel boxes of text) is left as a TODO for a person. Retrieval is a JSON file of vectors searched with numpy: at 19 entries a vector database would add a moving part and no recall.

## Architecture

```mermaid
flowchart LR
  idea[forge ideate:<br/>Gemini tools + retrieval] -.-> spec
  spec[ads.yaml] --> gp[gameplay segment<br/>+ real first frame]
  gp --> ff[first frame:<br/>strip text, paint effects]
  ff --> img[image edit: Gemini 3 Pro Image<br/>or local SDXL via ComfyUI]
  img --> veo[Veo 3.1 image-to-video<br/>lastFrame = real frame]
  gp --> veo
  veo --> hook[trim tail, patch invented UI,<br/>24 to 60 fps mci]
  hook --> asm[0.3 s crossfade, gameplay,<br/>end card]
  gp --> asm
  lyria[Lyria music] --> mix[mix: padded FX bus,<br/>sidechain-ducked music]
  asm --> mix
  mix --> master[master loop on encoded AAC:<br/>loudness, true peak, hook/gameplay match]
  master --> exp[export 9:16 + 4:5 blurred sides<br/>under the size cap]
  exp --> qa[QA checks +<br/>blind judge x N]
  qa --> rep[HTML + JSON report]
  rep --> rev[forge review:<br/>person approves / rejects]
  rev --> pub[forge publish:<br/>approved + QA-clean + manifest]
  cache[(content-hash cache)] -.-> img & veo & lyria
  ledger[(cost ledger +<br/>budget guard)] -.-> img & veo & lyria
```

Providers sit behind small interfaces (`ImageEditProvider`, `VideoProvider`, `MusicProvider`, `JudgeProvider`). The Vertex AI implementations call REST directly with `google-auth` Application Default Credentials and `requests`. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the module map, the timeline arithmetic and the master loop.

## Install

**macOS (Apple Silicon) and Linux (x86_64)**, in a terminal:

```sh
curl -LsSf https://github.com/emirhann-akca/creative-forge/releases/latest/download/install.sh | sh
```

**Windows 10/11 (x64)**, in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://github.com/emirhann-akca/creative-forge/releases/latest/download/install.ps1 | iex"
```

Each installer works inside your user profile and needs no admin rights:

1. downloads [uv](https://github.com/astral-sh/uv) 0.13.7 and checks its SHA-256 against the value written in the script;
2. `uv tool install "creative-forge[ocr]"` with Python 3.13, which uv downloads if you do not have it. `forge` lands in `~/.local/bin` (Windows: `%USERPROFILE%\.local\bin`), and that folder is added to your PATH for new terminals;
3. runs `forge setup ffmpeg` and `forge doctor`.

Open a new terminal afterwards. `-ExecutionPolicy Bypass` applies to that one PowerShell process only: Windows clients default to the `Restricted` policy, under which no script runs. To read a script before running it, open the same URL in a browser.

The one-liners fetch the installers attached to the latest GitHub Release, and each installer installs the wheel of its own version. Until a release has been tagged (`release.yml` builds and attaches everything from a `v*` tag), install from a clone:

```sh
# macOS / Linux
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,ocr]"
forge setup ffmpeg && forge doctor
```

```powershell
# Windows PowerShell: no activation needed (Activate.ps1 is blocked by the default policy)
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,ocr]"
.venv\Scripts\forge setup ffmpeg
.venv\Scripts\forge doctor
```

## Quickstart (no keys, no cost)

```bash
forge demo            # writes out/demo/: 2 concepts x 2 formats + report.html
forge ui              # opens http://127.0.0.1:8765: the demo run, its report, players, approve / reject
forge ideate --game "goods-sort puzzle" --clips out/demo/inputs/gameplay.mp4 --n 2   # scripted model, offline
pytest                # from a clone
```

The demo builds synthetic "gameplay" from ffmpeg's `testsrc2` and runs every stage with fake providers, including real 24 → 60 fps motion interpolation, the loudness loop, both exports, OCR and a two-run blind judge. The report shows $0 charged next to the list price the same calls would cost on Vertex AI.

## ffmpeg, doctor and the local UI

**`forge setup ffmpeg`** keeps a usable ffmpeg if one resolves, otherwise downloads FFmpeg 9.0.2 into the user data folder (`%LOCALAPPDATA%\creative-forge`, `~/Library/Application Support/creative-forge`, `~/.local/share/creative-forge`). `--force` downloads even when one resolves. Each archive is pinned by URL, size and SHA-256; a mismatch deletes the download and installs nothing. The binaries are then probed for libx264, an AAC encoder, `minterpolate`, `ebur128`, `loudnorm`, `freezedetect`, `silencedetect` and a few more filters the pipeline uses; a build that lacks any is refused with the list.

| Platform | Build | SHA-256 checked against |
|---|---|---|
| Windows x64 | Gyan Doshi "essentials", GyanD/codexffmpeg 9.0.2 release (the publisher winget's `Gyan.FFmpeg` installs from) | the checksum gyan.dev publishes |
| macOS arm64 | Martin Riedl, signed with his Developer ID | the `.sha256` file next to each archive |
| Linux x64 | Martin Riedl | the `.sha256` file next to each archive |

Which ffmpeg runs: `CREATIVE_FORGE_FFMPEG_DIR` (a folder holding ffmpeg and ffprobe; if it is wrong that is an error, not a fallback), then the managed build, then `PATH`.

**`forge doctor`** reports ffmpeg and ffprobe (path, where they came from, version, missing features, which AAC encoders actually open), the OCR backend `--ocr auto` will use, Python, the data and cache folders and whether they and the working folder are writable, ComfyUI's `/system_stats` when `CREATIVE_FORGE_COMFYUI_URL` or `--comfyui URL` is given, and whether Application Default Credentials exist (only the file's `type` is shown, never its contents). It exits 1 on a blocking problem; `--json` prints the same as JSON.

**`forge ui`** serves a local page on `127.0.0.1` (port 8765, or a free one): the runs under `--root` (default `out`), each run's QA report, the exports in a player (range requests, so seeking works), approve / reject with a note, `forge demo` in the background with its log, and the doctor checks. Approve and reject call the same `review.decide` as `forge review`, so a decision is tied to the file's SHA-256 and `forge publish` gates exactly as before; publishing stays on the command line. Requests with a non-loopback `Host` header are refused, and every change needs a per-process token that only the served pages carry. `--host 0.0.0.0` exposes it to your network, and anyone who can open the page can then approve exports. FastAPI, Jinja2 and [htmx](https://htmx.org) 2.0.11 (vendored with its 0BSD license); no Node build.

## Windows notes

- The tool's own files live in `%LOCALAPPDATA%\creative-forge` (uv, ffmpeg) and `%LOCALAPPDATA%\creative-forge\Cache` (nothing roams). `CREATIVE_FORGE_HOME` moves both.
- Keep run folders short and outside OneDrive-synced Desktop/Documents: Windows paths stop at 260 characters unless long paths are enabled, the generation cache adds about 90, and OneDrive locks files while it uploads them.
- Renames that a virus scanner or the search indexer briefly blocks are retried for up to about 3.5 s.
- AAC: `aac_mf` (Media Foundation) is preferred over ffmpeg's built-in `aac`. Windows "N" editions without the Media Feature Pack lack Media Foundation; the encoder is tried before use and the pipeline falls back to `aac`. Either way the master loop measures true peak on the encoded file.
- OCR uses RapidOCR (PP-OCR models on onnxruntime, shipped in the wheel; no download at run time). macOS keeps Apple Vision as its first choice; `--ocr rapidocr` selects RapidOCR there too.
- Output is UTF-8 when it goes to a pipe or a file, so a path with "Ş" cannot crash a command; Windows PowerShell may still display such characters wrongly in redirected output.

## Real run (Vertex AI)

```bash
gcloud auth application-default login
export CREATIVE_FORGE_PROJECT=<your-project-id>   # never stored in the repo
# optional: CREATIVE_FORGE_VIDEO_LOCATION (default us-central1), CREATIVE_FORGE_GENAI_LOCATION (default global)

forge plan examples/ads.yaml                 # upper-bound cost, lint findings
forge run examples/ads.yaml --dry-run        # + input files, ffmpeg, project variable, ComfyUI if used
forge run examples/ads.yaml --budget-usd 15
```

Model IDs live in the spec (`models:`) and prices in [`creative_forge/data/pricing.yaml`](creative_forge/data/pricing.yaml) (`as_of: 2026-10`). Check both against Google Cloud before a paid run, and override prices with `--pricing pricing.local.yaml`. The judge has no recorded price, so under a budget it is refused until you add one.

## Model licence guard

Several models people reach for are not licensed for paid ads: FLUX.1 Kontext [dev] and FLUX.2 [dev] exclude "revenue-generating activity", MusicGen and F5-TTS weights are CC-BY-NC, XTTS-v2 is under the Coqui Public Model License, the Turkish Piper voice's dataset is CC BY-NC-SA, rembg's default model (BRIA RMBG-2.0) needs a paid agreement, and HunyuanVideo's licence does not apply in the EU, the UK or South Korea. [`data/model_licenses.yaml`](creative_forge/data/model_licenses.yaml) lists every model the tool can call (Vertex AI models, Wan 2.1 FLF2V/I2V, Wan 2.2 TI2V-5B/A14B, Qwen-Image-Edit, SDXL, LTX, FLUX, HunyuanVideo, CogVideoX, MusicGen, ACE-Step, XTTS-v2, F5-TTS, Piper, Kokoro, Chatterbox, rembg models, RapidOCR, faster-whisper) with its code licence, weights licence, commercial status, conditions, source URL and the date the source was read.

`forge plan` and `forge run` resolve every model the spec would call (image edit, video, music, judge; ComfyUI file names included) and print a licence table:

| Verdict | When | Effect |
|---|---|---|
| `refuse` | non-commercial weights; a territory clause and `targets.regions` is empty or includes an excluded territory; a revenue cap at or below `targets.annual_revenue_usd` | `forge plan` exits 1, `forge run` exits 4 before building a provider, both naming the licence and its source |
| `warn` | a conditional licence (LTX's $10M revenue cap, CogVideoX registration, Hunyuan with regions outside its exclusions), or a model the registry does not know | printed, not blocking |
| `ok` / `service` | open weights that allow it; a hosted API whose service terms apply (not reviewed here) | printed |

```yaml
targets:
  regions: [TR, US]               # ISO codes or EU / EEA / UK; empty means "may serve anywhere"
  annual_revenue_usd: 250000      # checked against revenue caps; undeclared caps warn
```

A refusal can only be overridden per model and with a reason: `forge run spec.yaml --license-override flux2-dev.safetensors="licensed from BFL, contract 42"`. The reason, the refused licence and its source go into `cost_ledger.json` (`license_overrides`) and `report.json`. `forge licenses` lists the registry; `forge licenses <model>` looks one up. The registry records what each licence said when it was read; it is not legal advice, and licences change.

## Local SDXL through ComfyUI

```yaml
image_provider: comfyui            # the other steps still use `providers`
comfyui: {checkpoint: sd_xl_base_1.0.safetensors, steps: 28, seed: 7}
concepts:
  - hook: {image_prompt: "...", edit_region: [600, 1100, 1080, 1700]}   # inpaint mask; outside is restored
```

The provider uploads the frame (resized to a ~1 MP SDXL canvas) and the mask to ComfyUI's temp folder, queues [`sdxl_inpaint.json`](creative_forge/data/comfyui/sdxl_inpaint.json) or [`sdxl_img2img.json`](creative_forge/data/comfyui/sdxl_img2img.json) with `POST /prompt`, polls `/history/{id}` and fetches the `PreviewImage` result with `/view`, so nothing lands in ComfyUI's `input/` or `output/`. Set `CREATIVE_FORGE_COMFYUI_URL` (default `http://127.0.0.1:8188`); `--dry-run` checks that the server answers and has the checkpoint. Local calls are free and have no list price.

Verified on 10 Nov 2026 on an Apple Silicon Mac (M4, 24 GB) with `sd_xl_base_1.0.safetensors`: a masked edit of a 360×640 demo frame on a 768×1344 canvas, 28 steps, returned an image in 176 s from a cold start (167 s for a second run). The plumbing works; the base checkpoint did not draw a recognizable hand on the abstract test frame, so real edits want an inpainting checkpoint.

## Human review gate

```bash
forge review out/puzzle-ads --reviewer ayse                  # interactive: a / r / s and a note per export
forge review out/puzzle-ads --approve puzzle-ads_dust-blower_9x16.mp4 --note "hook reads well"
forge publish out/puzzle-ads --to deliver/                   # refuses if an approved export failed QA
forge publish out/puzzle-ads --to deliver/ --override "network re-encodes audio anyway"
```

Decisions go to `review.json` next to the run, each with reviewer, time, QA status and the file's SHA-256. Publishing copies nothing when nothing is approved, when an approved file changed after review, or when an approved export (or its concept's generation checks) has a failed check and no override reason. `manifest.json` lists what was published, by whose approval, what was overridden and why, and what was left out.

## `forge ideate`: concepts from a tool-calling model

Gemini drafts N hook concepts and can only act through tools: `search_creative_library` (retrieval over [the 14 production lessons and five synthetic past concepts](creative_forge/data/library.yaml)), `list_gameplay_clips`, `lint_concept`, `estimate_cost` and `submit_concepts`. A submission is accepted only when every concept passes `AdSpec` validation and the lint rules; then it is written as a spec that `forge plan` and `forge run` load. Embeddings come from an offline hashing embedder by default or Vertex `text-embedding-005` (`--embedder vertex`), cached in a small JSON index file, not a vector database. `--llm fake` plays back a scripted session in Gemini's response format, which is also what the tests use.

Verified once on 10 Nov 2026 with Gemini 2.5 Flash on Vertex AI and `text-embedding-005`, on two synthetic clips: 5 model requests, 17,464 tokens, 20 embedding inputs, 23 s. The model fixed one lint finding (`tail-ui`) and submitted 3 concepts that load as a spec. The output is kept verbatim in [`examples/ideas.gemini-2.5-flash.yaml`](examples/ideas.gemini-2.5-flash.yaml), with what it got wrong: one draft passed the edge-entry rule with "wiping dust from the top-right corner" (the rule is now stricter), and one asks the image model to rearrange products, which no lexical rule catches.

## QA checks and the defects behind them

Each check returns `pass` / `warn` / `fail` / `skip` with the measured value and its threshold. Thresholds are set in the spec's `qa:` and `audio:` sections.

| # | Production defect | Prevention in the pipeline | Check (default threshold) |
|---|---|---|---|
| 1 | The engine's audio track ended 1.6 s before the video, because the sidechain FX track was shorter | FX bus padded to the full length (`apad=whole_dur`) | `av_duration`: \|video − audio\| ≤ 0.1 s, fail > 0.5 s |
| 2 | ffmpeg's built-in `aac` encoder overshot true peak by +6.7 dB on limited signals | `aac_at` (AudioToolbox) preferred on macOS; true peak measured after encoding, limiter pulled down if needed | `true_peak` on the delivered file: ≤ −2.0 dBTP, fail > −1.0 |
| 3 | `-shortest` cut the video to the shorter audio | never used; every mux and encode has an explicit duration | `av_duration` + test |
| 4 | Parallel jobs sharing temp files corrupted outputs | private temp dir per job, per-call temp files, per-key generation lock | concurrency tests |
| 5 | Veo copies text from the first frame as garbled text ("MILK" → "MIILAK") | `hook.text_regions` blurred out of the first frame | `first_frame_text`, `hook_text`: OCR, near-miss of a known word = fail |
| 6 | Veo drifts in the last ~1 s (stylized mascot turns photoreal) and invents fake UI in the last ~3 s | tail trim (`cut_s`, default duration − 1 s), `hook.patches` cover invented UI with real pixels | lint `tail-drift` / `tail-ui`; `hook_landing` (Veo frame at the cut vs the real frame) |
| 7 | Hands or tools covering UI made Veo invent fake UI behind them | prompt rule: the hand enters from an edge | lint `hand-enters-from-edge` |
| 8 | Gemini redraws products when asked to add dust or mud to the whole frame | `hook.effects` painted on the real frame in code; Gemini only adds the hand/tool; `hook.edit_region` restores everything outside the box | lint `effects-programmatic` |
| 9 | Veo outputs 24 fps; plain `fps=60` duplicated frames and juddered | `minterpolate=mi_mode=mci` to 60 fps, then an exact 2:1 drop to 30 | `cadence`: isolated repeated frames ≤ 2 %, fail > 8 % |
| 10 | One ad opened on a frozen, silent first 0.5 s | (caught, then trimmed) | `intro_freeze` (`freezedetect`), `intro_silence` (`silencedetect`) |
| 11 | Hook vs gameplay loudness differed by +5 to −9 LU | loop measures *after* the limiter and corrects again; one pass is not enough because the limiter squashes gameplay SFX more | `hook_gameplay_loudness`: ≤ 0.8 LU, fail > 2 |
| 12 | The 4:5 variant must not crop gameplay | full 9:16 frame centred, blurred and darkened sides | `format` (exact size and aspect) + pixel test |
| 13 | Single-run LLM blind scores are noisy (same video scored 8, then 4) | judge runs N ≥ 2 times on shuffled, code-named videos | `judge`: mean and stdev per criterion; warn if a criterion's stdev > 1 or only one run |
| 14 | Files must stay under 30 MB | crf 20 + maxrate 3.6M (lowered automatically for longer ads) | `file_size`: ≤ 30 MB |

The suite also checks `duration` (spec window), `loudness` (−14.3 LUFS ± 1 LU) and `seam` (last hook frame vs first clean gameplay frame, mean absolute difference on a 0–255 scale).

## Limitations

- Built around one studio's goods-sort puzzle ads. The defaults (end card, layout, judge rubric) reflect that.
- There is no ad-performance data yet. QA measures technical correctness and an LLM's opinion, not CPI or IPM.
- In this repo the Veo, Gemini Image, Lyria and judge providers are tested against mocked HTTP only. Their request shapes come from the production scripts, except Lyria, which follows Vertex AI's documented `:predict` shape because the October script for it was not kept. Gemini function calling, `text-embedding-005` and the ComfyUI provider were each run for real, as described above.
- Lint rules are regular expressions. They catch the phrasings behind real defects, not every semantic mistake.
- OCR: Apple Vision on macOS (compiled on first use with `swiftc`), RapidOCR on Windows and Linux with the `[ocr]` extra; the tesseract fallback is weaker on stylized game fonts. With no backend, the OCR checks report `skip`.
- Seam, landing and cadence thresholds are heuristic defaults, not values tuned on a large set.
- The original engine's game-specific motion-graphics layer (camera keyframes, a pointer synced to logged taps, text cards) is not ported.
- Developed on macOS. Windows and Linux support is what CI exercises (tests, the installers, the demo, review, publish and a UI smoke test) on GitHub's hosted Windows Server 2025, macOS and Ubuntu runners; it has not been tried on a Windows 10/11 desktop, an ARM64 Windows machine or an Intel Mac (the installers refuse the last two).

## How this was built

The production scripts, the model choices, the failure modes and the QA rules come from my own ad work in October 2026. Packaging them into this repository (the refactor, tests and docs) was done with AI coding agents working under my direction; I reviewed the changes, and the commits carry a `Co-Authored-By` line.

## Layout

```
creative_forge/
  spec.py, pricing.py, ledger.py, cache.py, retry.py, config.py, generator.py
  providers/   base.py (interfaces), vertex.py (REST), comfyui.py (local SDXL), fake.py (ffmpeg)
  stages/      video.py (gameplay, hook, end card, assembly), audio.py (mix, master, mux), export.py
  qa/          media_checks.py, audio_checks.py, seam.py, ocr.py (+ ocr_vision.swift), judge.py, runner.py
  ideate/      library.py (embedders, index), tools.py (function declarations), agent.py (loop)
  data/        pricing.yaml, library.yaml, comfyui/*.json (workflows)
  ui/          app.py (FastAPI), templates/ (Jinja2), static/ (htmx 2.0.11 + its license)
  pipeline.py, review.py, report.py, lint.py, imaging.py, demo.py, cli.py
  setup_ffmpeg.py (pinned builds), doctor.py, paths.py (platformdirs), fsutil.py (Windows-safe renames)
install.sh, install.ps1   examples/   tests/   docs/ARCHITECTURE.md   .github/workflows/ (ci, release)
```

MIT licensed.
