# Changelog

## 0.2.6 — 2026-08-19

- Add explicit per-session close controls to the top tab bar and session menu.
- Stop and clean the exact managed worker/tmux window after confirmation.
- Allow verified imported `tmux gen` windows to be closed explicitly.
- Preserve chat history as a closed, non-resumable session while removing it from active and history UI.

## 0.2.5 — 2026-08-19

- Allow messages while agents are running.
- Use native Codex/tcodex `turn/steer` for same-turn supplements.
- Persist Claude/tclaude follow-ups and automatically run them as the next turn.
- Support best-effort running input for imported `tmux gen` sessions while continuing to reject blocked interactive states.
- Preserve Plan, commentary, and collapsible tool-call visibility in Chat.
- Fix responsive shrinking, retained horizontal offsets, and session-tab resizing.
- Keep session-to-session handoff, history recovery, Markdown rendering, and persistent tmux execution.

## 0.2.0 — 2026-08-18

- Unify managed and imported sessions in the VS Code Chat interface.
- Add stable message scrolling, session handoff, Markdown rendering, and graphical `tmux gen` relay.
