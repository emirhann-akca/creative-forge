# creative-forge installer for Windows 10/11 x64 (Windows PowerShell 5.1 or PowerShell 7). No admin rights, no git.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/emirhann-akca/creative-forge/main/install.ps1 | iex"
#
# What it does, all inside your user profile:
#   1. downloads uv (pinned version, SHA-256 checked against the value in this file) into %LOCALAPPDATA%\creative-forge;
#   2. installs creative-forge[ocr] with `uv tool install` (uv downloads Python if needed) -> forge.exe in
#      %USERPROFILE%\.local\bin. Source: the release wheel of this installer's version; if that release does not
#      exist (HTTP 404), the source archive of the main branch, which uv builds locally. It prints which one it used;
#   3. `forge setup ffmpeg` (pinned ffmpeg 9.0.2, SHA-256 checked) and `forge doctor`.
#
# Settings (environment variables):
#   FORGE_SOURCE          install this instead: a wheel path or URL, a source archive URL (.zip / .tar.gz)
#   FORGE_RELEASE_URL     the release wheel to try first (default: this version's wheel on GitHub Releases)
#   FORGE_ARCHIVE_URL     the source archive used when the release wheel is missing (default: the main branch)
#   FORGE_PYTHON          Python version for the tool environment (default 3.13)
#   FORGE_NO_MODIFY_PATH  1 = do not add the tool folder to your user PATH
#   FORGE_NO_SHORTCUT     1 = do not add the "Creative Forge" Desktop and Start menu shortcuts
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
    $Wheel = "creative_forge-$ForgeVersion-py3-none-any.whl"
    $ReleaseUrl = if ($env:FORGE_RELEASE_URL) { $env:FORGE_RELEASE_URL } else {
        "https://github.com/$Repo/releases/download/v$ForgeVersion/$Wheel"
    }
    $ArchiveUrl = if ($env:FORGE_ARCHIVE_URL) { $env:FORGE_ARCHIVE_URL } else {
        "https://github.com/$Repo/archive/refs/heads/main.zip"
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

    $wheelDir = $null
    if ($env:FORGE_SOURCE) {
        $Source = $env:FORGE_SOURCE
        Say "source: FORGE_SOURCE=$Source"
    } else {
        $wheelDir = Join-Path ([IO.Path]::GetTempPath()) ("forge-wheel-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $wheelDir | Out-Null
        # Windows PowerShell 5.1 sometimes sees GitHub close the connection on a 404 instead of reading the
        # response: no keep-alive, and up to three tries until the server's status code is known.
        $status = 0
        $lastError = ''
        for ($attempt = 1; $attempt -le 3 -and $status -eq 0; $attempt++) {
            try {
                Invoke-WebRequest -Uri $ReleaseUrl -OutFile (Join-Path $wheelDir $Wheel) -UseBasicParsing -DisableKeepAlive
                $status = 200
            } catch {
                $lastError = $_.Exception.Message
                $response = $_.Exception.Response
                if ($response) { $status = [int]$response.StatusCode }
                if ($status -eq 0) { Start-Sleep -Seconds (2 * $attempt) }
            }
        }
        $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($status -eq 0 -and $curl) {
            # curl.exe ships with Windows 10 1803+ and reads the status code where .NET could not
            $code = & $curl.Source -s -L --retry 2 -o (Join-Path $wheelDir $Wheel) -w '%{http_code}' $ReleaseUrl
            if ("$code" -match '^\d{3}$') { $status = [int]"$code" }
        }
        if ($status -ne 200 -and $status -ne 404) {
            throw "forge-install: could not download $ReleaseUrl (HTTP $status; $lastError)"
        }
        if ($status -eq 200) {
            $Source = Join-Path $wheelDir $Wheel
            Say "source: release wheel v$ForgeVersion ($ReleaseUrl)"
        } else {
            $Source = $ArchiveUrl
            Say "no release wheel for v$ForgeVersion (HTTP 404 at $ReleaseUrl)"
            Say "source: main branch source archive ($Source); uv builds it, no git needed"
        }
    }
    if (Test-Path -LiteralPath $Source) {
        $Source = ([Uri](Resolve-Path -LiteralPath $Source).ProviderPath).AbsoluteUri  # file:///C:/... (percent-encoded)
    }
    Say "installing creative-forge (Python $Python; uv downloads it if missing)"
    # --refresh-package: a branch archive keeps its URL while its contents change
    try {
        Invoke-Native $uv @('tool', 'install', '--force', '--refresh-package', 'creative-forge', '--compile-bytecode',
            '--python', $Python, "creative-forge[ocr] @ $Source")
    } finally {
        if ($wheelDir) { Remove-Item -LiteralPath $wheelDir -Recurse -Force -ErrorAction SilentlyContinue }
    }

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
    $launcher = $false
    if ($env:FORGE_NO_SHORTCUT -ne '1') {
        if ((Invoke-Exe $forge @('app', '--install-shortcut')) -eq 0) { $launcher = $true }
        else { Say "could not add the Desktop / Start menu shortcut; 'forge app' opens the same window" }
    }
    if ($launcher) {
        Say "done. Open 'Creative Forge' from the Start menu or the Desktop (or, in a NEW terminal: forge app)"
    } else {
        Say 'done. Open a NEW terminal and run:  forge demo   (or: forge app)'
    }
}
