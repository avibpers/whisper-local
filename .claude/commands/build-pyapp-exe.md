---
description: Build pyapp executable
argument-hint: "[-Clean (clean Rust build)] [-Test (run exe after build)]"
allowed-tools: Bash(powershell.exe:*), Bash(koli.exe:*)
---
[FLAGS]=$ARGUMENTS

1. Build: `powershell.exe -ExecutionPolicy Bypass -File pyapp-build/build-pyapp.ps1` (pass through flags from [FLAGS])
   - `-Clean`: Clean Rust target directory before building (full rebuild)
2. Watch for "Build successful!" message
3. Note the executable path from output

If -Test in [FLAGS]:
4. Convert exe path to bash-compatible format and run: `"<path>/koli.exe"`
   - C:\Users\... → `"/mnt/c/Users/.../koli.exe"`
5. Watch for "Application ready!" message (success) or errors
6. KillShell to terminate
7. Ignore exit code 137 (expected from KillShell)
