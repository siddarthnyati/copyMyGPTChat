# copyMyGPTChat

Export every ChatGPT conversation from the **macOS ChatGPT desktop app** to local `.md` and `.txt` files. Built for the case where you've lost access to your account email and can't use the official export feature, but you're still logged in to `ChatGPT.app`.

Outputs go to `~/chatgpt_exports/`.

## How it works

The macOS ChatGPT app is a SwiftUI + WebKit hybrid. This script uses two macOS APIs:

1. **Accessibility API (pyobjc)** — locates the sidebar's scroll area by geometry (leftmost narrow `AXScrollArea`), then enumerates its conversation buttons by shape (width ≈ 230, height ≈ 34). Each button is pressed via `AXUIElementPerformAction`.
2. **Keyboard + clipboard (pyautogui + pyperclip)** — scrolls the message pane to force-load older messages, then `Cmd+A` / `Cmd+C` and parses the clipboard text.

**Important limitation:** ChatGPT.app does not expose conversation titles via the Accessibility API — every sidebar button has an empty AX title. So the script can't name conversations up front. Instead, it **derives a title** from the first user message after copying the content, and uses that (first 5 alphanumeric words) as the filename slug. The "bucket" (Today / Yesterday / Previous 7 Days) is also unlabeled in AX and is not recorded.

Parsing splits on `You said:` / `ChatGPT said:` markers that appear in copied ChatGPT text. Progress is checkpointed so you can resume after interruption. Row identity is the button's y-position relative to the sidebar's collection-list origin, which is stable under scrolling.

## Setup

```bash
cd /Users/siddarthnyati/copyMyGPTChat
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Grant Accessibility permission (required)

1. Open **System Settings → Privacy & Security → Accessibility**.
2. Click `+` and add the terminal you'll run the script from (Terminal.app, iTerm, VS Code, etc.).
3. Toggle it on.
4. If the script still errors about trust, also add `/usr/bin/python3` or your venv's python.

## Usage

```bash
# 1. Verify enumeration works — prints the sidebar list, writes nothing.
python export_chatgpt.py --dry-run

# 2. Smoke-test on one conversation.
python export_chatgpt.py --limit 1

# 3. Small batch — try Ctrl+C mid-run, then re-run to confirm resume works.
python export_chatgpt.py --limit 5

# 4. Full run.
python export_chatgpt.py

# 5. Re-try conversations that failed in a previous run.
python export_chatgpt.py --retry-failed

# 6. Dump the AX tree for debugging (writes ~/chatgpt_exports/.ax_dump.txt).
python export_chatgpt.py --debug --dry-run
```

## While it runs

- **Do not touch the mouse or keyboard.** pyautogui steals focus; any interaction will derail the script.
- **Failsafe**: snap the cursor into any screen corner to abort. Progress is checkpointed.
- Leave ChatGPT.app as the frontmost window. Don't minimize it.
- Expect ~5–10 minutes per 100 conversations at default delays.

## Output

Each conversation produces two files in `~/chatgpt_exports/`:

```
2026-04-23_my-chat-title.md
2026-04-23_my-chat-title.txt
```

Both start with a header:

```
Title: <first 5 alphanumeric words of the first user message>
Sidebar index: <1-based position in the sidebar at enumeration time>
Exported: <ISO timestamp>
```

Followed by `USER:` / `ASSISTANT:` blocks.

Also written:
- `.checkpoint.json` — resumable state; don't delete mid-run.
- `.log` — run log.
- `.ax_dump.txt` — only with `--debug`.

## Caveats

- **Titles are derived, not original.** ChatGPT.app does not expose sidebar titles via accessibility. The filename and in-file `Title:` header are built from the first 5 alphanumeric words of the first user message. If a conversation has no user message, you get `untitled-<N>`.
- **No per-conversation dates.** The app doesn't expose either message timestamps or the sidebar date bucket via clipboard or AX. Filenames use the export date (today).
- **Images / attachments** aren't copied — plain text only.
- **Turn labels are best-effort** — based on `You said:` / `ChatGPT said:` markers in the copied text. If a conversation has none, it's saved under a single `UNLABELED:` block and logged.
- **Sidebar enumeration is geometric.** Rows are identified by shape (width ≈ 230, height ≈ 34) inside the leftmost narrow `AXScrollArea`. If ChatGPT ships a major UI change and these numbers drift, run `--debug --dry-run` and share `.ax_dump.txt` to re-tune the constants in `list_sidebar_buttons`.
- **Do not run while using the computer for anything else.**

## Troubleshooting

- **"Accessibility permission is not granted"** — see setup steps above; the permission applies to the *parent process* (your terminal), not the Python binary directly.
- **Sidebar enumeration returns 0** — ChatGPT.app may not be frontmost, or the sidebar may be collapsed. Open the app, expand the sidebar (`Cmd+Shift+S` or the toggle button), then re-run.
- **Clipboard comes back empty** — the message pane didn't receive focus. Try increasing `INTER_CONV_DELAY` at the top of `export_chatgpt.py`, or widen the ChatGPT window so the pane is clearly distinct from the sidebar.
- **Same conversation exported twice** — titles collided. The checkpoint disambiguates by `(title, bucket, index)` so re-runs are safe, but filenames get `-2`, `-3` suffixes on collision.
