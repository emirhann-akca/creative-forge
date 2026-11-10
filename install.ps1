# creative-forge installer for Windows 10/11 x64 (Windows PowerShell 5.1 or PowerShell 7). No admin rights.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://github.com/emirhann-akca/creative-forge/releases/latest/download/install.ps1 | iex"
#
# What it does, all inside your user profile:
#   1. downloads uv (pinned version, SHA-256 checked against the value in this file) into %LOCALAPPDATA%\creative-forge;
#   2. `uv tool install creative-forge[ocr]` (uv downloads Python if needed) -> forge.exe in %USERPROFILE%\.local\bin;
#   3. `forge setup ffmpeg` (pinned ffmpeg 9.0.2, SHA-256 checked) and `forge doctor`.
#
# Settings (environment variables):
#   FORGE_SOURCE          what to install instead of the release wheel: a wheel path or URL, or a git+https URL
#   FORGE_PYTHON          Python version for the tool environment (default 3.13)
#   FORGE_NO_MODIFY_PATH  1 = do not add the tool folder to your user PATH
#
# Written for `irm | iex`: ASCII only, everything inside one script block (nothing leaks into your session), and no
# `exit` (it would close your window). A failure throws; the error is printed and the window stays open.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # the progress bar makes Invoke-WebRequest many times slower on 5.1

    $ForgeVersion = '0.2.0'
    $UvVersion = '0.13.7'
    $UvSha256 = '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'  # uv-x86_64-pc-windows-msvc.zip
    $Repo = 'emirhann-akca/creative-forge'
    $Python = if ($env:FORGE_PYTHON) { $env:FORGE_PYTHON } else { '3.13' }
    $Source = if ($env:FORGE_SOURCE) { $env:FORGE_SOURCE } else {
        "https://github.com/$Repo/releases/download/v$ForgeVersion/creative_forge-$ForgeVersion-py3-none-any.whl"
    }

    function Say([string]$Message) { Write-Host "forge-install: $Message" }

    # Runs a program and returns its exit code. uv reports progress on stderr; when this session's output is
    # redirected (CI, a log file), Windows PowerShell 5.1 turns each stderr line into an error record, which
    # 'Stop' would make fatal. So stderr is merged and printed as plain text, and only the exit code decides.
    function Invoke-Exe([string]$Exe, [string[]]$Arguments) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
            return $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $saved
        }
    }

    function Invoke-Native([string]$Exe, [string[]]$Arguments) {
        $code = Invoke-Exe $Exe $Arguments
        if ($code -ne 0) {
            throw "forge-install: '$([IO.Path]::GetFileName($Exe)) $($Arguments -join ' ')' failed with exit code $code"
        }
    }

    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'forge-install: PowerShell 5.1 or newer is required' }
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($arch -ne 'AMD64') { throw "forge-install: only x64 Windows is supported (this is $arch)" }
    # Windows PowerShell 5.1 on older .NET defaults may not offer TLS 1.2, which GitHub requires.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $dataDir = if ($env:CREATIVE_FORGE_HOME) { Join-Path $env:CREATIVE_FORGE_HOME 'data' } else {
        Join-Path $env:LOCALAPPDATA 'creative-forge'
    }
    $uvDir = Join-Path $dataDir "uv\$UvVersion"
    $uv = Join-Path $uvDir 'uv.exe'
    $haveUv = $false
    if (Test-Path -LiteralPath $uv) {
        $v = & $uv --version
        $haveUv = ($LASTEXITCODE -eq 0) -and ("$v" -like "uv $UvVersion*")
    }
    if ($haveUv) {
        Say "using uv $UvVersion from $uvDir"
    } else {
        Say "downloading uv $UvVersion"
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("forge-install-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            $zip = Join-Path $tmp 'uv.zip'
            $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
            $got = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($got -ne $UvSha256) {
                throw "forge-install: uv download SHA-256 mismatch (got $got, expected $UvSha256); nothing was installed"
            }
            Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $tmp 'x') -Force
            New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
            Copy-Item -LiteralPath (Join-Path $tmp 'x\uv.exe') -Destination $uv -Force
        } finally {
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if (Test-Path -LiteralPath $Source) {
        $Source = ([Uri](Resolve-Path -LiteralPath $Source).ProviderPath).AbsoluteUri  # file:///C:/... (percent-encoded)
    }
    Say "installing creative-forge from $Source (Python $Python; uv downloads it if missing)"
    Invoke-Native $uv @('tool', 'install', '--force', '--compile-bytecode', '--python', $Python, "creative-forge[ocr] @ $Source")

    $bin = (& $uv tool dir --bin | Select-Object -First 1).Trim()
    if ($env:FORGE_NO_MODIFY_PATH -ne '1' -and $env:UV_NO_MODIFY_PATH -ne '1') {
        # adds $bin to the user PATH (HKCU\Environment) for new terminals; not fatal if it cannot
        if ((Invoke-Exe $uv @('tool', 'update-shell')) -ne 0) { Say "could not update PATH; add $bin to it yourself" }
    }
    $env:Path = "$bin;$env:Path"  # this session only
    $forge = Join-Path $bin 'forge.exe'
    if (-not (Test-Path -LiteralPath $forge)) { throw "forge-install: forge.exe was not installed into $bin" }

    Say 'setting up ffmpeg'
    Invoke-Native $forge @('setup', 'ffmpeg')
    Say 'checking the installation'
    Invoke-Native $forge @('doctor')
    Say 'done. Open a NEW terminal and run:  forge demo   (or: forge ui)'
}
