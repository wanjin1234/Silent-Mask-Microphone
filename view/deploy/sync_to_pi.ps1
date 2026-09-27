# ============================================================
#  Silent-Mask-Microphone - code sync script (run on Windows dev machine)
#  Uses scp to sync all code to the Raspberry Pi.
#
#  Usage (PowerShell):
#      .\deploy\sync_to_pi.ps1
#
#  IP note:
#    $PI_HOST = "auto"  -> auto-detect the Pi (recommended)
#    or set a fixed IP, e.g. "172.20.10.12"
# ============================================================

# ---------------- config ----------------
$PI_USER = "wanghy25"
$PI_HOST = "auto"                    # "auto" or a fixed IP like "172.20.10.12"
$PI_MDNS = "device259.local"         # Pi mDNS hostname (used first when auto)
$PI_DIR  = "arDisplay"               # project dir under the Pi home dir
$LOCAL   = "C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone"

# ============================================================
# Auto-detect the Pi IP (only used when $PI_HOST = "auto")
# ============================================================
function Find-PiHost {
    # 1) try mDNS hostname first
    try {
        $r = Resolve-DnsName -Name $PI_MDNS -ErrorAction Stop
        $ip = ($r | Where-Object { $_.Type -eq 'A' -or $_.Type -eq 'AAAA' } | Select-Object -First 1).IPAddress
        if ($ip) {
            Write-Host "Found Pi via mDNS: $PI_MDNS ($ip)" -ForegroundColor Green
            return $PI_MDNS
        }
    } catch {}

    # 2) scan this machine's subnet for port 22 (SSH)
    $local = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
        Sort-Object PrefixLength | Select-Object -First 1).IPAddress
    if ($local) {
        $prefix = ($local -split '\.')[0..2] -join '.'
        Write-Host "Scanning $prefix.0/24 for SSH (22)..." -ForegroundColor Cyan

        $clients = @()
        foreach ($i in 1..254) {
            $ip = "$prefix.$i"
            $client = New-Object System.Net.Sockets.TcpClient
            try {
                $async = $client.BeginConnect($ip, 22, $null, $null)
                $clients += [pscustomobject]@{ IP = $ip; Client = $client; Async = $async }
            } catch {
                $client.Dispose()
            }
        }

        $found = @()
        foreach ($c in $clients) {
            if ($c.Async.AsyncWaitHandle.WaitOne(300) -and $c.Client.Connected) {
                $found += $c.IP
            }
            $c.Client.Close()
        }

        if ($found.Count -eq 1) {
            Write-Host "Found Pi: $($found[0])" -ForegroundColor Green
            return $found[0]
        }
        elseif ($found.Count -gt 1) {
            Write-Host "Multiple SSH hosts found: $($found -join ', '). Using the first one." -ForegroundColor Yellow
            return $found[0]
        }
    }

    Write-Warning "Could not auto-detect the Pi. Set `$PI_HOST in the script to the actual IP."
    return $null
}

# ============================================================
# Resolve target host
# ============================================================
if ($PI_HOST -eq "auto") {
    $PI_HOST = Find-PiHost
    if (-not $PI_HOST) { exit 1 }
}

$DST = "${PI_USER}@${PI_HOST}:${PI_DIR}/"
Write-Host "Target: $DST" -ForegroundColor Cyan

# 1) ensure target dirs exist (idempotent)
ssh "${PI_USER}@${PI_HOST}" "mkdir -p ${PI_DIR}/src ${PI_DIR}/deploy ${PI_DIR}/models ${PI_DIR}/logs"

# 2) clean local __pycache__ to avoid cross-version .pyc issues
Get-ChildItem -Recurse -Directory -Filter __pycache__ "$LOCAL\src" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 3) all source code (src, including test_ai)
scp -r "$LOCAL\src" $DST

# 4) launch scripts (multiple files use space, not comma)
scp "$LOCAL\run.sh" "$LOCAL\run_ai.sh" $DST

# 5) deploy dir (setup_pi.sh / ar-display.service / this script)
scp -r "$LOCAL\deploy" $DST

# 6) root README (optional)
scp "$LOCAL\README.md" $DST

Write-Host ""
Write-Host "Sync done -> $DST" -ForegroundColor Green
Write-Host ""
Write-Host "Next, log in to the Pi:" -ForegroundColor Cyan
Write-Host "  ssh ${PI_USER}@${PI_HOST}"
Write-Host "  cd ~/${PI_DIR} && bash deploy/setup_pi.sh"
Write-Host "Then run: bash run.sh"
