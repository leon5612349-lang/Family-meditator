# apply_reference_images.ps1
# Inserts the real illustrations into the v0.dev mockup (_reference_v0.html):
#   * welcome screen: .illustration-placeholder -> .illustration-image (couple_welcome.png)
#   * chat screen:    .chat-illustration before <h1 class="question-heading"> (couple_chat.png)
#   * bubble avatars: 32px, 2px white border, coral (outgoing) / sage (incoming)
# Idempotent: running it twice does not duplicate anything.
# Usage (from project root):  powershell -ExecutionPolicy Bypass -File .\apply_reference_images.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1) locate the mockup
$candidates = @(
    (Join-Path $root "_reference_v0.html"),
    (Join-Path $root "frontend\_reference_v0.html")
)
$target = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $target) {
    Write-Host "NOT FOUND: _reference_v0.html" -ForegroundColor Red
    Write-Host "Put the mockup into the project root (or frontend/) and run this script again."
    exit 1
}
Write-Host "Mockup: $target"

# 2) relative image paths (relative to the mockup location)
$dir = Split-Path -Parent $target
$prefix = if ($dir -eq $root) { "frontend/" } else { "" }
$welcome = "${prefix}couple_welcome.png"
$chatImg = "${prefix}couple_chat.png"
foreach ($img in @($welcome, $chatImg)) {
    $p = Join-Path $dir $img
    if (-not (Test-Path $p)) { Write-Host "MISSING IMAGE: $p" -ForegroundColor Red; exit 1 }
}

$html = Get-Content $target -Raw -Encoding UTF8

# 3) CSS: illustration rules + updated avatars
$css = @'
        /* --- Real illustrations (instead of v0.dev placeholders) ------ */
        .illustration-image {
            width: min(80vw, 300px);
            aspect-ratio: 1;
            margin: clamp(14px, 3vh, 28px) 0 16px;
            border-radius: 42px;
            overflow: hidden;
            display: grid;
            place-items: center;
        }
        .illustration-image img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }

        .chat-illustration {
            width: 100%;
            max-height: 180px;
            margin: 4px 0 12px;
            border-radius: 24px;
            overflow: hidden;
            display: grid;
            place-items: center;
        }
        .chat-illustration img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
        }

        /* --- Avatars in bubbles --------------------------------------- */
        .avatar {
            width: 32px;
            height: 32px;
            flex: 0 0 32px;
            border-radius: 50%;
            border: 2px solid #FFFFFF;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-family: var(--font-body, "Nunito", sans-serif);
            font-size: 12px;
            font-weight: 700;
            color: #FFFFFF;
        }
        .outgoing .avatar { background: #C77D68; }
        .incoming .avatar { background: #9CAF88; }
'@

if ($html -notmatch "\.illustration-image\s*\{") {
    $idx = $html.LastIndexOf("</style>")
    if ($idx -lt 0) { Write-Host "No closing style tag found" -ForegroundColor Red; exit 1 }
    $html = $html.Substring(0, $idx) + $css + "`r`n" + $html.Substring($idx)
    Write-Host "CSS: illustration + avatar rules added"
} else {
    Write-Host "CSS: rules already present - skipped"
}

# 4) welcome screen: .illustration-placeholder -> .illustration-image
if ($html -notmatch 'class="illustration-image"') {
    $m = [regex]::Match($html, '<div\s+class="illustration-placeholder"[^>]*>', 'IgnoreCase')
    if ($m.Success) {
        $start = $m.Index
        $pos = $m.Index + $m.Length
        $depth = 1
        while ($pos -lt $html.Length -and $depth -gt 0) {
            $nextOpen = $html.IndexOf("<div", $pos, [StringComparison]::OrdinalIgnoreCase)
            $nextClose = $html.IndexOf("</div>", $pos, [StringComparison]::OrdinalIgnoreCase)
            if ($nextClose -lt 0) { break }
            if ($nextOpen -ge 0 -and $nextOpen -lt $nextClose) { $depth++; $pos = $nextOpen + 4 }
            else { $depth--; $pos = $nextClose + 6 }
        }
        $end = $pos
        $block = "<div class=`"illustration-image`" role=`"img`" aria-label=`"Para smotrit na telefon vmeste`">`r`n    <img src=`"$welcome`" alt=`"Para smotrit na telefon vmeste`" />`r`n</div>`r`n"
        $html = $html.Substring(0, $start) + $block + $html.Substring($end)
        Write-Host "HTML: welcome screen -> .illustration-image ($welcome)"
    } else {
        Write-Host "NOT FOUND: .illustration-placeholder - skipped" -ForegroundColor Yellow
    }
} else {
    Write-Host "HTML: .illustration-image already present - skipped"
}

# 5) chat screen: .chat-illustration before the question heading
if ($html -notmatch 'class="chat-illustration"') {
    $idx = $html.IndexOf('<h1 class="question-heading"')
    if ($idx -ge 0) {
        $block = "<div class=`"chat-illustration`" role=`"img`" aria-label=`"Para s serdechkom`">`r`n    <img src=`"$chatImg`" alt=`"Para s serdechkom`" />`r`n</div>`r`n"
        $html = $html.Substring(0, $idx) + $block + $html.Substring($idx)
        Write-Host "HTML: chat screen -> .chat-illustration ($chatImg)"
    } else {
        Write-Host "NOT FOUND: question heading - skipped" -ForegroundColor Yellow
    }
} else {
    Write-Host "HTML: .chat-illustration already present - skipped"
}

[IO.File]::WriteAllText($target, $html, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Done: $target" -ForegroundColor Green
