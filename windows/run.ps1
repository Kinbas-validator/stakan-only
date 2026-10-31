# Stakan-only для Windows: установка при первом запуске и запуск.
# Запускается из Start.bat. Python ставится портативно в папку python\ рядом с проектом:
# в систему ничего не устанавливается, права администратора не нужны, удалить — стереть папку.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # без полосы прогресса Invoke-WebRequest качает в разы быстрее
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$PyVer = "3.12.10"
$Root = Split-Path -Parent $PSScriptRoot
$PyDir = Join-Path $Root "python"
$Py = Join-Path $PyDir "python.exe"
$Ready = Join-Path $PyDir ".installed"        # Python и pip поставлены полностью
$ReqStamp = Join-Path $PyDir ".requirements"  # с какими зависимостями
Set-Location $Root

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }

function Get-File($url, $out) {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest $url -OutFile $out -UseBasicParsing; return }
        catch { if ($i -eq 3) { throw "не удалось скачать $url — проверьте интернет. $($_.Exception.Message)" }; Start-Sleep 3 }
    }
}

function Install-Python {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
    Step "Скачиваю Python $PyVer ($arch), около 11 МБ…"
    $zip = Join-Path $env:TEMP "stakan-python-$PyVer-$arch.zip"
    Get-File "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-$arch.zip" $zip
    if (Test-Path $PyDir) { Remove-Item $PyDir -Recurse -Force }
    Expand-Archive $zip $PyDir -Force
    Remove-Item $zip -Force
    # встроенный Python видит только пути из файла ._pth: включаем site-packages и добавляем папку проекта
    $pth = Get-ChildItem $PyDir -Filter "python*._pth" | Select-Object -First 1
    $lines = @(Get-Content $pth.FullName | ForEach-Object { $_ -replace '^#\s*import site', 'import site' }) + ".."
    Set-Content $pth.FullName $lines -Encoding ASCII
    Step "Ставлю pip…"
    $getPip = Join-Path $PyDir "get-pip.py"
    Get-File "https://bootstrap.pypa.io/get-pip.py" $getPip
    & $Py $getPip --no-warn-script-location --disable-pip-version-check -q
    if ($LASTEXITCODE) { throw "pip не установился (код $LASTEXITCODE)" }
    Remove-Item $getPip -Force
    Set-Content $Ready $PyVer
}

function Install-Requirements {
    $want = (Get-Content (Join-Path $Root "requirements.txt") -Raw).Trim()
    $have = if (Test-Path $ReqStamp) { (Get-Content $ReqStamp -Raw).Trim() } else { "" }
    if ($want -eq $have) { return }
    Step "Ставлю зависимости…"
    & $Py -m pip install --no-warn-script-location --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE) { throw "зависимости не установились (код $LASTEXITCODE)" }
    Set-Content $ReqStamp $want
}

function Update-Shortcut {
    # ярлык «Stakan» на рабочем столе; обновляется при каждом запуске, если папку перенесли
    try {
        $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Stakan.lnk"
        $bat = Join-Path $Root "Start.bat"
        $shell = New-Object -ComObject WScript.Shell
        $new = -not (Test-Path $lnk)
        $s = $shell.CreateShortcut($lnk)
        if ($s.TargetPath -ne $bat) {
            $s.TargetPath = $bat
            $s.WorkingDirectory = $Root
            $s.IconLocation = "$Py,0"
            $s.Description = "Стакан Lighter RH с пометкой premium-ордеров"
            $s.Save()
            if ($new) { Step "На рабочем столе появился ярлык «Stakan» — дальше запускайте им." }
        }
    } catch { }   # без ярлыка можно жить: Start.bat работает и так
}

try {
    if (-not ((Test-Path $Py) -and (Test-Path $Ready))) {
        Write-Host "Первый запуск: ставлю всё нужное в папку $PyDir (1–2 минуты)." -ForegroundColor Yellow
        Install-Python
    }
    Install-Requirements
    Update-Shortcut
} catch {
    Write-Host ""
    Write-Host "Установка не удалась: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Проверьте интернет и запустите Start.bat ещё раз."
    exit 1
}

$env:PYTHONUTF8 = "1"
Write-Host ""
Write-Host "Стакан запускается и сейчас откроется в браузере." -ForegroundColor Green
Write-Host "Не закрывайте это окно: пока оно открыто, стакан работает. Остановить — закрыть окно или Ctrl+C."
Write-Host ""
& $Py -u (Join-Path $Root "server.py") @args
exit $LASTEXITCODE
