# Neno TDLib sidecar

This is the native Telegram client boundary for Neno. It loads TDLib's
`tdjson.dll` at runtime and proxies newline-delimited JSON between a parent
process and TDLib:

```text
parent process <-> stdin/stdout JSONL <-> neno-tdlib-sidecar <-> tdjson.dll
```

The sidecar deliberately does not contain Neno chat logic, Telegram policy,
LLM calls, ASR, TTS, or media business rules. Those belong in the future
Telegram gateway process. No Telegram credential is accepted on the command
line or printed by this binary.

## Build

From a Visual Studio Developer PowerShell or a normal PowerShell with Visual
Studio Build Tools installed:

```powershell
pwsh -NoProfile -File scripts\build-telegram-sidecar.ps1
```

The script builds `Release\neno-tdlib-sidecar.exe`. The executable does not
link against TDLib at build time; it loads a pinned `tdjson.dll` at runtime.

To build TDLib itself from the pinned upstream tag and copy the runtime DLLs
to `runtime/tdlib`, run:

```powershell
pwsh -NoProfile -File scripts\build-tdlib.ps1
```

The source and build trees are ignored by Git. The default tag is `v1.8.0`;
change it explicitly when upgrading and keep the resulting runtime isolated
from the Python virtual environment.

## Run

```powershell
.\telegram_sidecar\build\Release\neno-tdlib-sidecar.exe --tdjson C:\path\to\tdjson.dll
```

The path can also be supplied as `TDLIB_JSON_LIBRARY`. Every non-empty stdin
line is forwarded as one TDLib JSON request. Every TDLib response/update is
written as one stdout line. The only sidecar control message currently
recognized is:

```json
{"@type":"sidecar.shutdown"}
```

The parent should send `setTdlibParameters` as the first TDLib request. The
database and file directories must be dedicated to TDLib, for example:

```text
data/telegram/tdlib/database
data/telegram/tdlib/files
```

The parent process owns the authorization state machine and must keep API
credentials and the database encryption key outside source control and logs.

For the one-time interactive login, after filling the Telegram variables in
`.env`, run:

```powershell
python -m telegram_gateway --login
```

The normal long-running gateway refuses to prompt for credentials and exits
with an instruction to run this command when the TDLib database is not yet
authorized.

## Boundary

The sidecar is a transport boundary, not a Telegram Bot API server. The
future gateway will translate TDLib `updateNewMessage` objects into the
platform-neutral Neno chat/media contract and translate outbound directives
back into TDLib `sendMessage`/media requests.
