# install_libreoffice.ps1
# Download and silently install LibreOffice from China mirrors (TUNA -> Aliyun fallback).
# Called by setup.bat. Exit code 0 = installed, 1 = failed.

$ErrorActionPreference = 'Continue'

$Mirrors = @(
    'https://mirrors.tuna.tsinghua.edu.cn/libreoffice/libreoffice/stable/',
    'https://mirrors.aliyun.com/libreoffice/stable/'
)

foreach ($base in $Mirrors) {
    try {
        Write-Host "[..] Resolving latest version from $base"

        # 1. Latest stable version directory
        $html = (Invoke-WebRequest -UseBasicParsing $base -TimeoutSec 15).Content
        $versions = [regex]::Matches($html, 'href="([0-9]+\.[0-9]+\.[0-9]+)/"') |
            ForEach-Object { $_.Groups[1].Value } |
            Sort-Object { [version]$_ } -Descending -Unique
        if (-not $versions) { throw "no version directories found" }
        $ver = $versions[0]

        # 2. Windows 64-bit MSI filename
        $dir = "$base$ver/win/x86_64/"
        $html2 = (Invoke-WebRequest -UseBasicParsing $dir -TimeoutSec 15).Content
        $msi = [regex]::Matches($html2, 'href="(LibreOffice_[^"]+_Win_x86-64\.msi)"') |
            ForEach-Object { $_.Groups[1].Value } |
            Select-Object -First 1
        if (-not $msi) { throw "no Win x86_64 msi found under $dir" }

        # 3. Download (curl.exe ships with Windows 10+)
        $url = "$dir$msi"
        $out = Join-Path $env:TEMP $msi
        Write-Host "[..] Downloading $url"
        & curl.exe -L --fail --connect-timeout 15 -o $out $url
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) { throw "download failed" }

        # 4. Silent install
        Write-Host "[..] Installing $msi (silent) ..."
        $proc = Start-Process msiexec.exe -ArgumentList '/i', "`"$out`"", '/qn', '/norestart' -Wait -PassThru
        if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) { throw "msiexec exit code $($proc.ExitCode)" }

        Remove-Item $out -Force -ErrorAction SilentlyContinue
        Write-Host "[OK] LibreOffice $ver installed."
        exit 0
    }
    catch {
        Write-Host "[WARN] Mirror $base failed: $($_.Exception.Message)"
    }
}

Write-Host "[ERROR] All mirrors failed."
exit 1
