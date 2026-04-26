# copyMyGPTChat

> Export every ChatGPT conversation — including **Project conversations** — from the macOS ChatGPT desktop app to local `.md` and `.txt` files, entirely offline with no API key required.

Built for the case where you've lost access to your account email and can't use the official data-export feature, but you're still logged in to `ChatGPT.app`.

**Output goes to `~/chatgpt_exports/`.**

---

## Features

| Feature | Details |
|---|---|
| **Full sidebar export** | Walks every conversation in your ChatGPT sidebar |
| **Project export** | Exports conversations inside named Projects |
| **Interactive TUI** | Rich terminal UI with menus, progress bar, pre-flight checks |
| **Resumable** | Checkpointed — Ctrl+C mid-run, re-run to continue |
| **Auto-retry** | Failed conversations are automatically retried once |
| **No API key** | Uses macOS Accessibility API only — nothing leaves your machine |
| **Dual output** | Each conversation saved as both `.md` and `.txt` |

---

## How it works

The macOS ChatGPT app is a SwiftUI + WebKit hybrid. This script uses the **macOS Accessibility API (pyobjc)** to read and interact with the app's UI tree directly — no clipboard, no network requests.

```
┌─────────────────────────────────────────────────────────────────┐
│                        copyMyGPTChat                            │
└─────────────────────────────────────────────────────────────────┘

  ┌──────────────┐     AX tree walk      ┌──────────────────────┐
  │  ChatGPT.app │ ◄──────────────────── │   export_chatgpt.py  │
  │              │                       │                      │
  │  ┌─────────┐ │  AXUIElementPerform   │  1. find_sidebar     │
  │  │Sidebar  │ │ ◄───────────────────  │  2. click row        │
  │  │  Rows   │ │                       │  3. find_message_pane│
  │  └─────────┘ │  AXDescription read   │  4. walk AX text     │
  │  ┌─────────┐ │ ──────────────────►   │  5. coalesce blocks  │
  │  │Message  │ │                       │  6. write .md/.txt   │
  │  │  Pane   │ │                       └──────────────────────┘
  └──────────────┘                                │
                                                  ▼
                                     ~/chatgpt_exports/
                                     ├── 2026-04-26_my-chat.md
                                     ├── 2026-04-26_my-chat.txt
                                     ├── my-project/
                                     │   ├── 2026-04-26_conv-1.md
                                     │   └── 2026-04-26_conv-2.md
                                     ├── .checkpoint.json
                                     └── .log
```

### Key technical details

- **Sidebar enumeration** — finds the leftmost narrow `AXScrollArea`, then enumerates `AXButton` children. Row identity is the button's stable y-position within the list, so resume works correctly after scrolling.
- **Message extraction** — clicks each conversation, locates the message-pane `AXScrollArea`, then pages through it with `PageDown` keystrokes, capturing every `AXStaticText` node at each scroll position. Handles virtualized lists that mount/unmount nodes as you scroll.
- **Project export** — navigates to the project via the sidebar, finds the project's `AXOpaqueProviderGroup` card list (narrower than a full chat pane at ~433px vs ~741px), and applies the same extraction pipeline per conversation.
- **ChatGPT-only guard** — all raw Quartz mouse/scroll events are gated by `NSWorkspace.frontmostApplication()` so the script never accidentally sends input to VS Code, Safari, or any other open window.
- **Pane signature** — a SHA-1 fingerprint of visible `AXStaticText` content detects when a click actually switched conversations, preventing silent duplicate exports.

---

## Setup

### Requirements

- macOS 12+
- Python 3.10+
- ChatGPT desktop app (logged in, sidebar visible)

### Install

```bash
git clone https://github.com/siddarthnyati/copyMyGPTChat.git
cd copyMyGPTChat
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Grant Accessibility permission (required)

The script drives ChatGPT.app's UI via the macOS Accessibility API. Your terminal needs permission:

1. Open **System Settings → Privacy & Security → Accessibility**
2. Click **+** and add the terminal you'll run the script from (Terminal.app, iTerm2, VS Code, etc.)
3. Toggle it **on**
4. Re-run the script — the pre-flight check will confirm it's granted

> The permission applies to the **parent process** (your terminal), not to the Python binary itself.

---

## Usage

### Interactive mode (recommended)

```bash
python export_chatgpt.py
```

You'll see a welcome screen and menu:

```
? What would you like to do?
❯ 🗂️  Export all standard conversations
  📁  Export conversations from a Project
  🔄  Retry previously failed conversations
  🔍  Dry run (list conversations only)
  ❌  Exit
```

Pick **"Export conversations from a Project"** and the script auto-detects your projects from the sidebar so you can select one by name.

### CLI flags

```bash
# Safe first-look — lists conversations, writes nothing
python export_chatgpt.py --dry-run

# Smoke-test on one conversation
python export_chatgpt.py --limit 1

# Export a specific project (skips interactive prompts)
python export_chatgpt.py --project --project-name "My Project Name"

# Retry previously failed conversations
python export_chatgpt.py --retry-failed

# Dump the full AX tree for debugging
python export_chatgpt.py --debug --dry-run
```

---

## While it runs

- **Do not touch the mouse or keyboard.** The script sends raw input events to ChatGPT.app — any mouse movement or keystroke will interfere.
- **Failsafe**: snap the cursor into any screen corner to abort safely. Progress is checkpointed.
- Keep ChatGPT.app as the **frontmost window**. Do not minimize it.
- Expect roughly **5–10 seconds per conversation** at default settings.

---

## Output

Each conversation produces two files:

```
~/chatgpt_exports/
├── 2026-04-26_my-chat-title.md       ← Markdown with USER:/ASSISTANT: blocks
├── 2026-04-26_my-chat-title.txt      ← Plain text version
├── my-project/                        ← Project conversations in a subfolder
│   └── 2026-04-26_conv-title.md
├── .checkpoint.json                   ← Resume state (do not delete mid-run)
└── .log                               ← Detailed run log
```

File header format:

```markdown
---
Title: first five alphanumeric words of first user message
Sidebar index: 1-based position at enumeration time
Exported: 2026-04-26T14:33:54
---

**USER:**
...

**ASSISTANT:**
...
```

---

## Security & Privacy

- **No network requests** — the script only reads your local ChatGPT.app UI. Nothing is sent anywhere.
- **No API keys** — the macOS Accessibility API requires no credentials.
- **Output stays local** — all `.md`/`.txt` files are written to `~/chatgpt_exports/` on your own machine.
- **`.gitignore` excludes exports** — `chatgpt_exports/` and `*.checkpoint.json` are listed in `.gitignore` so your chat history is never accidentally committed.
- **Accessibility permission is scoped** — you grant it to your terminal app, not to any remote service. Revoking it in System Settings immediately stops the script from accessing ChatGPT.app.

---

## Caveats

| Limitation | Reason |
|---|---|
| **Titles are derived, not original** | ChatGPT.app doesn't expose sidebar titles via AX. Filename = first 5 alphanumeric words of first user message. |
| **No timestamps** | The app doesn't expose per-message timestamps via AX. Filenames use today's date. |
| **Images / attachments skipped** | Only plain text `AXStaticText` nodes are captured. |
| **macOS only** | Uses pyobjc and macOS-specific Accessibility APIs. |
| **Don't use computer during export** | Raw input events go to whichever window is under the cursor. |

---

## Troubleshooting

**"Accessibility permission is not granted"**
→ Follow the Setup steps above. The permission must be granted to your terminal, not to Python directly.

**Sidebar enumeration returns 0 conversations**
→ ChatGPT.app may not be frontmost, or the sidebar is collapsed. Expand it with `Cmd+Shift+S`, then re-run.

**Project not found / "No projects detected"**
→ Scroll the ChatGPT sidebar all the way to the top so the Projects section is visible, then re-run. You can also type the project name manually when prompted.

**A conversation exported with wrong/empty content**
→ Re-run with `--retry-failed`. If it persists, run `--debug --dry-run` and inspect `.ax_dump.txt`.

**Script scrolling the wrong window**
→ Make sure ChatGPT.app is the frontmost window before the export starts. The script checks `NSWorkspace.frontmostApplication()` before every scroll/click event and re-activates ChatGPT if focus drifts.

---

## Contributing

PRs welcome. If ChatGPT.app ships a UI change that breaks enumeration, run:

```bash
python export_chatgpt.py --debug --dry-run
```

and open an issue with the `.ax_dump.txt` contents. That file shows the full AX tree and makes it straightforward to re-tune the geometry constants.

---

## License

MIT
