$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$requirementsStamp = Join-Path $venvPath ".requirements-hash"

try {
    Set-Location -LiteralPath $projectRoot
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $python = Get-Command py -ErrorAction SilentlyContinue
        if ($python) {
            & $python.Source -3 -m venv $venvPath
        }
        else {
            $python = Get-Command python -ErrorAction SilentlyContinue
            if (-not $python) {
                throw "Python 3.9 or newer was not found."
            }
            & $python.Source -m venv $venvPath
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Python could not create the replay-server environment."
        }
    }

    $requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $requirementsStamp) {
        (Get-Content -LiteralPath $requirementsStamp -Raw).Trim()
    }
    else {
        ""
    }
    if ($installedHash -ne $requirementsHash) {
        Write-Host "Preparing the replay server for first use..."
        & $venvPython -m pip install --disable-pip-version-check -r $requirementsPath
        if ($LASTEXITCODE -ne 0) {
            throw "The required packages could not be installed."
        }
        Set-Content -LiteralPath $requirementsStamp -Value $requirementsHash -Encoding ASCII
    }

    & $venvPython -m speedrun_submitter.replay_server
    exit $LASTEXITCODE
}
catch {
    Write-Error ("Replay server setup failed: " + $_.Exception.Message)
    exit 1
}
