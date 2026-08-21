# Changelog

## 0.2.8 — 2026-08-21

- Repair stale `tmux gen` chat bindings when a tmux window ID is reused by a
  different Agent runtime, preventing live windows from pointing at a closed
  database session.
- Add model and reasoning-effort selectors when creating a session while
  preserving the existing default-model behavior.
- Show the actual current model and reasoning effort below the composer and in
  the conversation header.
- Preserve selected model settings when a managed tmux worker is relaunched.
- Preserve an active text selection while transcript polling continues, so
  selecting part of a reply no longer expands or resets to the whole message.

## 0.2.7 — 2026-08-20

- Send imported `tmux gen` messages as real bracketed paste and verify that the
  exact Agent TUI accepted the submission before beginning the long reply wait.
- Match native replies only to the current relay turn, so an old
  failed/interrupted placeholder cannot consume a later reply.
- Follow the live rollout path while monitoring and fail immediately if the
  source tmux window exits or changes runtime identity.

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
