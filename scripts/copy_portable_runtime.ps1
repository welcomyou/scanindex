param(
    [Parameter(Mandatory = $true)]
    [string]$DistDir,
    [string]$IncludeCorrection = "1",
    [string]$IncludeLegacyChrome = "0"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistRoot = [System.IO.Path]::GetFullPath($DistDir)

function Is-Enabled([string]$Value) {
    $Value -in @("1", "true", "True", "TRUE", "yes", "YES", "on", "ON")
}

function Repo-Path([string]$RelativePath) {
    Join-Path $RepoRoot $RelativePath
}

function Dist-Path([string]$RelativePath) {
    Join-Path $DistRoot $RelativePath
}

function Ensure-Parent([string]$Path) {
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
}

function Copy-FileIfExists([string]$SourceRel, [string]$DestRel, [string]$Label) {
    $src = Repo-Path $SourceRel
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
        return
    }
    Write-Host "  [+] $Label"
    $dst = Dist-Path $DestRel
    Ensure-Parent $dst
    Copy-Item -LiteralPath $src -Destination $dst -Force
}

function Copy-FileQuiet([string]$SourceRel, [string]$DestRel) {
    $src = Repo-Path $SourceRel
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
        return
    }
    $dst = Dist-Path $DestRel
    Ensure-Parent $dst
    Copy-Item -LiteralPath $src -Destination $dst -Force
}

function Copy-DirIfExists(
    [string]$SourceRel,
    [string]$DestRel,
    [string]$Label,
    [string[]]$ExcludeFiles = @()
) {
    $src = Repo-Path $SourceRel
    if (-not (Test-Path -LiteralPath $src -PathType Container)) {
        return
    }
    Write-Host "  [+] $Label"
    $dst = Dist-Path $DestRel
    New-Item -ItemType Directory -Path $dst -Force | Out-Null

    $args = @($src, $dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    if ($ExcludeFiles.Count -gt 0) {
        $args += "/XF"
        $args += $ExcludeFiles
    }
    & robocopy @args | Out-Null
    $rc = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    if ($rc -ge 8) {
        throw "robocopy failed for $SourceRel -> $DestRel with exit code $rc"
    }
}

function Remove-IfExists([string]$RelativePath) {
    $path = Dist-Path $RelativePath
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
        return $true
    }
    return $false
}

function Prune-ExcludedPythonRuntime {
    $internal = Dist-Path "_internal"
    if (-not (Test-Path -LiteralPath $internal -PathType Container)) {
        return
    }

    $removed = 0
    foreach ($name in @(
        "torch",
        "torchvision",
        "torchaudio",
        "scipy",
        "sklearn",
        "sentence_transformers",
        "torchvision.libs",
        "scipy.libs"
    )) {
        if (Remove-IfExists "_internal\$name") {
            $removed += 1
        }
    }

    $distInfoPatterns = @(
        "torch-*.dist-info",
        "torchvision-*.dist-info",
        "torchaudio-*.dist-info",
        "scipy-*.dist-info",
        "scikit_learn-*.dist-info",
        "sklearn-*.dist-info",
        "sentence_transformers-*.dist-info"
    )
    foreach ($pattern in $distInfoPatterns) {
        foreach ($item in Get-ChildItem -LiteralPath $internal -Directory -Filter $pattern -ErrorAction SilentlyContinue) {
            Remove-Item -LiteralPath $item.FullName -Recurse -Force
            $removed += 1
        }
    }

    if ($removed -gt 0) {
        Write-Host "  [-] Pruned excluded Python runtime leftovers ($removed item(s))"
    }
}

function Copy-ScreenAiRuntime {
    $root = Repo-Path "models\screen_ai"
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        return
    }
    $versionDir = Get-ChildItem -LiteralPath $root -Directory |
        Sort-Object Name -Descending |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "chrome_screen_ai.dll") } |
        Select-Object -First 1
    if (-not $versionDir) {
        Write-Host "  [WARN] ScreenAI base DLL not found under models\screen_ai"
        return
    }

    $version = $versionDir.Name
    Copy-DirIfExists `
        "models\screen_ai\$version" `
        "models\screen_ai\$version" `
        "ScreenAI runtime ($version, filtered)" `
        @(
            "chrome_screen_ai_w_*.dll",
            "chrome_screen_ai_copy*.dll",
            "chrome_screen_ai_worker*.dll",
            "chrome_screen_ai_p*w*.dll"
        )
}

function Copy-LayoutTextKie {
    $srcRoot = "models\layoutlmv3_fontgray_norm_final_epoch25"
    if (-not (Test-Path -LiteralPath (Repo-Path $srcRoot) -PathType Container)) {
        return
    }
    Write-Host "  [+] LayoutLMv3 text KIE (int8 ONNX + tokenizer/config)"
    $files = @(
        "layoutlmv3_fontgray_norm_final_epoch25.int8.onnx",
        "label_list.json",
        "layoutlmv3_fontgray_config.json",
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "vocab.json",
        "merges.txt"
    )
    foreach ($file in $files) {
        Copy-FileQuiet "$srcRoot\$file" "$srcRoot\$file"
    }
}

function Copy-LightgbmSplitter {
    $root = "models\lightgbm_splitter"
    if (-not (Test-Path -LiteralPath (Repo-Path $root) -PathType Container)) {
        return
    }
    Write-Host "  [+] archive splitter LightGBM (Booster text runtime)"
    foreach ($group in @("doc_start", "signer_page")) {
        Copy-FileQuiet "$root\$group\model.txt" "$root\$group\model.txt"
        Copy-FileQuiet "$root\$group\metadata.json" "$root\$group\metadata.json"
        Copy-FileQuiet "$root\$group\report.json" "$root\$group\report.json"
    }
}

New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null

Copy-FileIfExists "settings.ini" "settings.ini" "settings.ini"
Copy-FileIfExists "settings.ini.example" "settings.ini.example" "settings.ini.example"
Copy-FileIfExists "ignored_words.txt" "ignored_words.txt" "ignored words"

Copy-DirIfExists "assets" "assets" "assets"
Copy-DirIfExists "config" "config" "sign/config files"
Copy-DirIfExists "dictionaries" "dictionaries" "dictionaries"

Copy-ScreenAiRuntime
Copy-DirIfExists "models\orientation" "models\orientation" "orientation ONNX"
Copy-DirIfExists "models\gmft_onnx" "models\gmft_onnx" "GMFT table ONNX"
Copy-DirIfExists "models\docling_tableformer_v1_stepcache_onnx" "models\docling_tableformer_v1_stepcache_onnx" "Docling TableFormer v1 step-cache ONNX"
Copy-DirIfExists "models\doclayout_yolo_onnx_dynamic" "models\doclayout_yolo_onnx_dynamic" "DocLayout-YOLO dynamic ONNX"
Copy-DirIfExists "models\doclayout_yolo_doclaynet_onnx_dynamic" "models\doclayout_yolo_doclaynet_onnx_dynamic" "DocLayout-YOLO DocLayNet auxiliary ONNX"
Copy-LightgbmSplitter
Copy-LayoutTextKie
if (Is-Enabled $IncludeCorrection) {
    Copy-DirIfExists "models\distilled_ct2" "models\distilled_ct2" "distilled Proton CT2"
} else {
    Write-Host "  [-] Skipped optional CT2 correction model (set INCLUDE_CORRECTION=1 to include)"
}

if (Is-Enabled $IncludeLegacyChrome) {
    Copy-FileIfExists "drivers\chromedriver.exe" "drivers\chromedriver.exe" "chromedriver"
    Copy-DirIfExists "bin\chrome-win64" "bin\chrome-win64" "bundled Chrome"
} else {
    Write-Host "  [-] Skipped legacy Chrome/Selenium fallback"
}

Prune-ExcludedPythonRuntime

$global:LASTEXITCODE = 0
