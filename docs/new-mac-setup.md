# Moving the Loop projects to this Mac

Written 2026-10-16 for user `mini` (home `/Volumes/Crucial/Users/mini`, arm64, macOS 27).

## What happened

The three loops were last driven from another Mac (`/Users/super/...`). Their runners died with
that machine. `STATUS.md` still says RUNNING because the runner never got to record its exit.
`loop start` checks the pid *and* its process identity, so it treats these as stale and starts
cleanly. No `.loop/` file needs a hand edit. The `/Users/super` strings in `state.json` are agent
summaries, not paths the controller uses.

| Project | Goal | Where it stopped | Needs |
|---|---|---|---|
| `ai/agent-fabric` | `agent-fabric-g1-f06-runtime` | turn 41, test_design (after plan_review round 4 fail), 14/70 tasks | Python, Node **22.22.3**, pnpm **10.18.0**, codex |
| `maxint/maxint-safehold` | `m1-android-core` | turn 53, develop, m1-07 check job, 7/15 tasks | Python **3.11+**, Temurin **17.0.17+10**, Android SDK, codex |
| `ai/macare` | `macare-observatory-r1` | turn 474, Edge Dock / Core history measurements, 10/21 tasks | Python, **full Xcode** (XCTest), codex |

The projects' checks pin exact versions. For example, `g1-identity/scripts/check.mjs` rejects
anything but `v22.22.3`, and `fixtures_preflight.py` rejects any JDK except 17.0.17. Generic
`brew install node` / `openjdk` will fail those checks, so the guide installs exact tarballs.

## Keeping the main disk free

These already live on Crucial, so anything installed there costs the internal disk nothing:

- `$HOME` = `/Volumes/Crucial/Users/mini` (so `~/Library`, `~/.codex`, `~/.cache`, Xcode
  DerivedData, Gradle and npm caches)
- Homebrew: `/opt/homebrew` → `/Volumes/Crucial/homebrew`
- `/Volumes/Crucial/Applications`

These are the traps that **would** write to the main disk. The guide avoids each one:

| Trap | Instead |
|---|---|
| Xcode from the App Store → `/Applications` (~15–40 GB) | `.xip` expanded into `/Volumes/Crucial/Applications` |
| Temurin `.pkg` → `/Library/Java/JavaVirtualMachines` | tarball in `/Volumes/Crucial/Users/mini/toolchains`, plus `JAVA_HOME` |
| Safehold build home defaults to `/private/tmp/safehold-m1-build-home` | `M1_BUILD_USER_HOME` on Crucial |
| `TMPDIR` (`/private/var/folders/...`) used by Gradle, swift and the emulator | `TMPDIR` on Crucial, set only for the runners |
| iOS simulator runtimes → `/Library/Developer/CoreSimulator` | don't install them; macare is a macOS app |

### What still lands on the internal disk

These can't be moved without editing project code or the OS. All are small or temporary:

| What | Where | Size |
|---|---|---|
| macare validation results and the `.app` package build. Its validator *requires* `/private/tmp/...` result paths (`Sources/ObservatoryValidation/main.swift`), and the build check uses `/private/tmp/macare-r1-loop-build` | `/private/tmp` | tens to hundreds of MB, cleared at reboot |
| agent-fabric g1-storage recovery journal (`/private/tmp/agent-fabric-owned-recovery-*`) | `/private/tmp` | small |
| macOS swap and sleep image, which grow when three emulators plus macare scans run at once | `/private/var/vm` | several GB at peak, released afterward |
| Xcode first-launch system packages, `xcode-select` link, Command Line Tools (already installed) | `/Library` | a few hundred MB |
| Per-user caches some tools write to `getconf DARWIN_USER_CACHE_DIR` (not moved by `TMPDIR`) | `/private/var/folders/.../C` | under ~1 GB (926 MB of `/var/folders` today) |

Don't symlink `/private/tmp` or `/var/folders` to Crucial. macOS manages them, and macare checks
that its result path really is `/private/tmp`. Running one runner at a time keeps swap low.

Safehold's emulator harness keeps its AVDs and system images inside the project
(`ANDROID_AVD_HOME` = the project's state dir), so they're already on Crucial.

## 1. Folders and environment

```bash
mkdir -p ~/toolchains ~/tmp/loop ~/tmp/safehold-m1-build-home
```

The Crucial root is owned by root, so toolchains live in the home folder, which is also on Crucial.

Append this to `~/.zprofile`, then open a new terminal:

```bash
# --- Loop toolchains (all on Crucial) ---
export TOOLCHAINS_DIR=/Volumes/Crucial/Users/mini/toolchains
export PATH="/opt/homebrew/bin:$PATH"
export NVM_DIR="$TOOLCHAINS_DIR/nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export JAVA_HOME="$TOOLCHAINS_DIR/jdk-17.0.17+10/Contents/Home"
export ANDROID_HOME="$HOME/Library/Android/sdk"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
export M1_BUILD_USER_HOME=/Volumes/Crucial/Users/mini/tmp/safehold-m1-build-home
export PATH="/opt/homebrew/opt/python@3.12/libexec/bin:$JAVA_HOME/bin:$ANDROID_HOME/emulator:$PATH"
```

## 2. Python for the framework (3.12)

```bash
brew install python@3.12
```

```bash
cd /Volumes/Crucial/Users/mini/Documents/ai/loop-engineering-code && /opt/homebrew/bin/python3.12 -m venv .venv
```

```bash
cd /Volumes/Crucial/Users/mini/Documents/ai/loop-engineering-code && .venv/bin/python -m unittest discover -s tests -t .
```

The framework is stdlib-only, so there is nothing to pip install. `bin/loop` picks up `.venv`
automatically. Safehold's own scripts call `python3` and need 3.11+ (`tomllib`). Brew's `python@3.12`
has no unversioned `python3`, so step 1 puts its `libexec/bin` ahead of `/usr/bin/python3` (3.9).

## 3. Node 22.22.3 and pnpm 10.18.0 (agent-fabric, codex)

Node comes from nvm, and nvm lives on Crucial. Step 1 sets `NVM_DIR=/Volumes/Crucial/Users/mini/toolchains/nvm`,
so nvm, every Node version and every global npm package (codex included) are stored there.
`PROFILE=/dev/null` stops the installer from editing your shell files, because step 1 already
loads nvm.

```bash
mkdir -p /Volumes/Crucial/Users/mini/toolchains/nvm && curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | NVM_DIR=/Volumes/Crucial/Users/mini/toolchains/nvm PROFILE=/dev/null bash
```

Open a new terminal so step 1 loads nvm, then:

```bash
nvm install 22.22.3 && nvm alias default 22.22.3
```

```bash
npm install -g corepack && corepack enable && corepack prepare pnpm@10.18.0 --activate && node -v && pnpm -v && which node
```

This nvm build of 22.22.3 ships without `corepack`, so it comes from npm first.

Expect `v22.22.3`, `10.18.0`, and a path under `/Volumes/Crucial/Users/mini/toolchains/nvm/versions/node/`.
The checks only accept 22.22.3, so keep it as the nvm default. The runners use whatever `node`
was on PATH in the terminal that started them.

## 4. Codex CLI (the adapter all three runners use)

```bash
npm install -g @openai/codex
```

```bash
codex login
```

You do the sign-in yourself. The global install goes into nvm's `22.22.3` folder on Crucial. If you
later `nvm install` another Node version, codex isn't there. Reinstall it, or use
`nvm install <ver> --reinstall-packages-from=22.22.3`. The goals
are configured for `codex` with the model and effort they already store (safehold's audit expects
`gpt-6.1-sol` / `high`), so nothing changes there.

## 5. Temurin JDK 17.0.17+10 (safehold)

```bash
cd /Volumes/Crucial/Users/mini/toolchains && curl -fL -o temurin17.tar.gz "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.17%2B10/OpenJDK17U-jdk_aarch64_mac_hotspot_17.0.17_10.tar.gz" && tar xzf temurin17.tar.gz
```

```bash
"$JAVA_HOME/bin/java" -version
```

Expect `17.0.17`. Check the SHA-256 against the `.sha256.txt` on the same release page. Without
`JAVA_HOME`, safehold's scripts fall back to `/Library/Java/JavaVirtualMachines/temurin-17.jdk`,
which doesn't exist here. Always launch the runner from a shell where step 1 is loaded.

## 6. Android SDK (safehold)

Use the arm64 "command line tools only" zip (no Android Studio needed). On 2026-10-16 Google's
index (`repository2-3.xml`) listed `commandlinetools-mac_arm64-16111833_latest.zip`, sha1
`ad03dc49bfacfd52c110b14104ea548b8a07e830`. Download it from
`https://dl.google.com/android/repository/`, check the sha1, then:

```bash
mkdir -p "$ANDROID_HOME/cmdline-tools" && cd "$ANDROID_HOME/cmdline-tools" && unzip ~/Downloads/commandlinetools-mac_arm64-*_latest.zip && mv cmdline-tools latest
```

```bash
yes | sdkmanager --licenses
```

```bash
sdkmanager "platform-tools" "emulator" "platforms;android-36" "build-tools;35.0.0"
```

The harness downloads and verifies its own API 29/33/36 `arm64-v8a` images and creates its own
AVDs, so don't create them by hand. `scripts/android/emulators/registry.json` pins emulator
**36.3.10**, and `sdkmanager` installs 37.x, so swap it with Google's manual procedure
(<https://developer.android.com/studio/emulator_archive>): download
`emulator-darwin_aarch64-14472402.zip` (SHA-256
`f285a4974c3352a1c0026ae76fe53b06a02d71d0e13ebbab79a928af1f86d3a2`), unzip, clear quarantine
(`xattr -dr com.apple.quarantine emulator`), copy the old `emulator/package.xml` into it, set
its `<revision>` to 36.3.10, and replace `$ANDROID_HOME/emulator`. `emulator -version` must
print `36.3.10.0`. **Never run `sdkmanager --update`**; it would put 37.x back. Recent
`sdkmanager` is a wrapper over the new Android CLI, and `--licenses` is no longer needed.

## 7. Xcode on Crucial (macare)

1. Download the current Xcode `.xip` for macOS 27 from <https://developer.apple.com/download/all/>
   (Apple ID sign-in, which you do). Save it on Crucial, e.g. `~/Downloads`.
2. Expand it on Crucial, then delete the `.xip`:

```bash
cd /Volumes/Crucial/Applications && xip -x ~/Downloads/Xcode_*.xip
```

```bash
sudo xcode-select -s /Volumes/Crucial/Applications/Xcode.app/Contents/Developer && sudo xcodebuild -license accept && xcodebuild -runFirstLaunch
```

If the first-launch dialog offers platforms, skip iOS/watchOS/visionOS. They install to the main
disk and macare doesn't need them. Crucial must be mounted whenever macare runs.

## 8. Point each project at this machine

`loop install` rewrites `.loop/bin/loop`, `.mcp.json`, `.codex/config.toml` and the skill files
with this machine's paths. Repeat for each project:

```bash
/Volumes/Crucial/Users/mini/Documents/ai/loop-engineering-code/bin/loop --project /Volumes/Crucial/Users/mini/Documents/ai/agent-fabric install --host claude,codex
```

```bash
/Volumes/Crucial/Users/mini/Documents/ai/loop-engineering-code/bin/loop --project /Volumes/Crucial/Users/mini/Documents/maxint/maxint-safehold install --host claude,codex
```

```bash
/Volumes/Crucial/Users/mini/Documents/ai/loop-engineering-code/bin/loop --project /Volumes/Crucial/Users/mini/Documents/ai/macare install --host claude,codex
```

Then trust each project once in Codex (the unattended runner doesn't need this, interactive
Codex does), and restart Claude Code / Codex sessions so they load the new MCP config.

## 9. Restart the runners

From a terminal with step 1 loaded, in each project:

```bash
.loop/bin/loop status
```

```bash
TMPDIR=/Volumes/Crucial/Users/mini/tmp/loop .loop/bin/loop start
```

Watch with `.loop/bin/loop status --watch` or `.loop/bin/loop dashboard`.

Start one runner and confirm it gets a turn through before starting the next. All three run
heavy checks (pnpm builds, three emulators, 1M-file scans), so running them at once on one Mac
will distort timings.

## What carries over and what doesn't

- **Completed tasks stay done.** Controller-run results are recorded in `state.json`.
- **macare:** the Edge Dock, Core history, scan and cancellation budgets are performance
  measurements of the *current candidate on this hardware*. Expect the in-flight job
  `job-9fb020092b` to be abandoned and rerun. Earlier numbers came from the other Mac and
  shouldn't be compared directly.
- **safehold:** the AVD state and `android/.gradle-user-home/` cache didn't move, or will be
  rebuilt. The first m1-07 check rebuilds images (~2 GB, on Crucial) and the Gradle cache.
  Expect the in-flight job `job-e9018ba9d8` to be rerun.
- **agent-fabric:** resumes at test_design. `node_modules` came from the old Mac, so if
  anything native fails, run `pnpm install --frozen-lockfile` in the affected package.
  Postgres-backed tasks (`f06-08*`) were source-only under the earlier no-Docker restriction.
  Docker is installed here, but enabling it changes the goal's constraints and needs a
  re-approved goal.
