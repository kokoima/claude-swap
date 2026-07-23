# claude-swap

Rotate between multiple Claude Max accounts in Claude Code. One command, zero dependencies.

## The problem

Claude Max subscribers hit rate limits on 5-hour and 7-day windows. If you have multiple accounts, switching between them requires running `claude login` each time — slow and disruptive.

## The solution

`claude-swap` manages your setup tokens and swaps credentials instantly. Check which accounts are rate-limited, pick the best one, and keep coding.

```
$ claude-swap check

claude-swap v1.2.0

Active: 2 (Work / me@work.com)

 #   Cuenta        Plan    5h           reset   7d           Fable       │  J23    V24    S25    D26    L27    M28    X29    J30
─────────────────────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────
  1  Personal      MAX20X  ░░░░░░   0%   20:30  ██████ 100%  ██████ 100% │   ·      ·    22:00    ·      ·      ·      ·      ·
 *2  Work          MAX20X  █▊░░░░  30%   21:10  ██▋░░░  43%  ███▊░░  62% │   ·      ·      ·      ·      ·      ·    08:00    ·
  3  Side Project  MAX20X  ░░░░░░   0%  +01:10  ███▊░░  63%  ██████ 100% │   ·      ·      ·      ·      ·    17:00    ·      ·
  4  Old Account   FREE   — desactivada —

  calendario: hora del reset 7d · Fable si cae en dia distinto   │   barras: <50% verde · 50–80% amarillo · ≥80% rojo

  Estrategia Fable — primero la que antes renueva (excluye rate-limited y Fable 100%)
 *2  Work          MAX20X  █▊░░░░  30%   21:10  ██▋░░░  43%  ███▊░░  62% │   ·      ·      ·      ·      ·      ·    08:00    ·

  Estrategia General 7d — primero la que antes renueva (excluye rate-limited)
  3  Side Project  MAX20X  ░░░░░░   0%  +01:10  ███▊░░  63%  ██████ 100% │   ·      ·      ·      ·      ·    17:00    ·      ·
 *2  Work          MAX20X  █▊░░░░  30%   21:10  ██▋░░░  43%  ███▊░░  62% │   ·      ·      ·      ·      ·      ·    08:00    ·

$ claude-swap 1

Rotating to account 1 (Personal)

  [1/4] Keychain cleared
  [2/4] .credentials.json updated
  [3/4] oauthAccount cleared
  [4/4] Store updated

Done. New Claude Code sessions will use account 1.
```

Color bars for the 5h / 7d / Fable windows, subscription tier, an 8-day
calendar with the exact time each weekly limit renews, and two swap
strategies (soonest renewal first — what you spend there comes back first).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/kokoima/claude-swap/main/install.sh | sh
```

Or clone and install:

```bash
git clone https://github.com/kokoima/claude-swap
cd claude-swap && make install
```

## Quick start

### 1. Set up your accounts

```bash
claude-swap init
```

This will guide you through adding your first account. For each account:

1. Open a new terminal
2. Log in: `claude login`
3. Generate a token: `claude setup-token`
4. Paste the token when prompted

Setup tokens last **1 year** — no need to re-login constantly.

### 2. Add more accounts

```bash
claude-swap add
```

### 3. Switch accounts

```bash
# Quick switch
claude-swap 2

# Auto-pick the best account (lowest rate limit usage)
claude-swap auto

# Interactive: see status, check limits, then choose
claude-swap
```

## Commands

| Command | Description |
|---------|-------------|
| `claude-swap` | Interactive: status + rate check + select |
| `claude-swap <n>` | Switch to account N |
| `claude-swap status` | Show current account state |
| `claude-swap check` | Status + API rate limit check |
| `claude-swap auto` | Auto-switch to best available account |
| `claude-swap add` | Add a new account |
| `claude-swap remove <n>` | Remove an account |
| `claude-swap key <n>` | Set claude.ai sessionKey (enables Fable % in check) |
| `claude-swap keys-sync` | Auto-import sessionKeys from Chrome profiles (by email) |
| `claude-swap disable <n>` | Mark account as disabled (skipped by check/auto/keys-sync) |
| `claude-swap enable <n>` | Re-enable a disabled account |
| `claude-swap init` | First-time setup |

Short aliases: `s` (status), `c` (check), `a` (auto), `ks` (keys-sync).

## Fable % and the weekly calendar

The Anthropic API only exposes the unified 5h/7d windows. The per-model
Fable weekly counter (and your subscription tier) comes from claude.ai's
own API, which needs the `sessionKey` cookie of a logged-in claude.ai
session:

- `claude-swap key <n>` — paste a sessionKey manually (DevTools >
  Application > Cookies > `sessionKey`, starts with `sk-ant-sid`).
- `claude-swap keys-sync` — scans all Chrome profiles on the machine,
  validates each session against claude.ai and matches accounts by email.
  Requires `pip3 install --user browser_cookie3`. Re-run it whenever the
  Fable column shows `-` (cookies expire periodically; re-login claude.ai
  in a Chrome profile and sync again).

`check` then renders, per account: subscription tier (MAX20X / MAX5X /
PRO / FREE, cached in the store), color bars for 5h / 7d / Fable, the
exact 5h reset time, and an 8-day calendar with the time each weekly
limit renews. Below the table, two orderings of the same rows suggest
swaps: **Estrategia Fable** and **Estrategia General 7d**, both sorted by
soonest weekly renewal (capacity you burn there is regained first);
rate-limited and disabled accounts are excluded.

## How it works

Claude Code stores credentials in two places:

1. **macOS Keychain** (`Claude Code-credentials`) — has priority
2. **`~/.claude/.credentials.json`** — fallback

When you run `claude-swap <n>`, it:

1. Deletes the Keychain entry (macOS only) so it doesn't override the file
2. Writes the selected setup token to `~/.claude/.credentials.json`
3. Clears `oauthAccount` from `~/.claude.json` (Claude Code repopulates it)
4. Updates the active account in `~/.claude/claude-swap.json`

New Claude Code sessions pick up the change automatically. Existing sessions keep the old token in memory — close and reopen them to switch.

## `auto` mode

`claude-swap auto` checks all your accounts against the Anthropic API and picks the one with the lowest 5-hour utilization. If all accounts are rate-limited, it tells you which one unblocks soonest.

The check uses a single minimal Haiku request per account — cheap and fast.

## Requirements

- **macOS** or **Linux**
- **Python 3** (pre-installed on macOS)
- **Claude Code** installed
- **Claude Max** subscription (one or more accounts)
- Optional: **browser_cookie3** (`pip3 install --user browser_cookie3`) — only for `keys-sync`

## FAQ

### Is this safe?

Yes. Your tokens (and claude.ai sessionKeys, if you use the Fable
feature) are stored locally in `~/.claude/claude-swap.json` with `600`
permissions (owner-only read/write). Tokens are only sent to
`api.anthropic.com` and sessionKeys only to `claude.ai` during health
checks — never to any third party.

### Do I need to restart Claude Code?

New sessions automatically use the swapped account. Sessions already running keep their token in memory — close and reopen them.

### What about Linux?

Works the same, minus the Keychain step. Claude Code on Linux only reads `.credentials.json`, so rotation is simpler.

### What's a setup token?

A long-lived OAuth token generated by `claude setup-token`. It lasts 1 year and works the same as logging in with `claude login`, but without needing to re-authenticate.

### Can I use this with the Claude desktop app?

No — `claude-swap` only manages Claude Code (the CLI) credentials.

## Uninstall

```bash
make uninstall
# or
rm ~/.local/bin/claude-swap
```

Your accounts are preserved in `~/.claude/claude-swap.json`. Delete it manually if you want a clean removal.

## License

MIT
