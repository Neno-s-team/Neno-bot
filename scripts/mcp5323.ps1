# MCP streamable-HTTP client for the zcr521 softap bridge (http://127.0.0.1:5323/mcp)
$script:McpUrl   = 'http://127.0.0.1:5323/mcp'
$script:McpState = Join-Path $PSScriptRoot 'mcp5323.session.json'

function Get-McpSessionId {
    if (Test-Path $script:McpState) {
        $s = Get-Content $script:McpState -Raw | ConvertFrom-Json
        return $s.sessionId
    }
    return $null
}

function Save-McpSessionId([string]$id) {
    @{ sessionId = $id } | ConvertTo-Json | Set-Content $script:McpState -Encoding UTF8
}

function Invoke-McpRaw([hashtable]$body, [string]$sessionId) {
    $headers = @{
        'Accept'       = 'application/json, text/event-stream'
        'Content-Type' = 'application/json'
    }
    if ($sessionId) { $headers['Mcp-Session-Id'] = $sessionId }
    $resp = Invoke-WebRequest -Uri $script:McpUrl -Method Post `
        -Headers $headers -Body ($body | ConvertTo-Json -Depth 20 -Compress) -TimeoutSec 900
    $newSession = $resp.Headers['Mcp-Session-Id']
    if ($newSession) { Save-McpSessionId ([string]$newSession) }
    $raw = $resp.Content
    # Response may be SSE (event: message\ndata: {...}) or plain JSON
    $json = $null
    if ($raw -match '^\s*\{' ) {
        $json = $raw | ConvertFrom-Json
    } else {
        foreach ($line in ($raw -split "`n")) {
            $line = $line.Trim()
            if ($line.StartsWith('data:')) {
                $payload = $line.Substring(5).Trim()
                if ($payload) { $json = $payload | ConvertFrom-Json; break }
            }
        }
    }
    return $json
}

function Initialize-Mcp {
    $sid = Get-McpSessionId
    if ($sid) {
        # verify existing session with a ping; fall back to a fresh handshake
        try {
            $r = Invoke-McpRaw @{ jsonrpc = '2.0'; id = 1; method = 'ping' } $sid
            if ($r -and $r.result) { return $sid }
        } catch { }
    }
    $r = Invoke-McpRaw @{
        jsonrpc = '2.0'; id = 0
        method  = 'initialize'
        params  = @{
            protocolVersion   = '2025-06-18'
            capabilities      = @{}
            clientInfo        = @{ name = 'dsh-pwsh'; version = '1.0' }
        }
    } $null
    $sid = Get-McpSessionId
    if (-not $sid) { throw "initialize did not return Mcp-Session-Id" }
    Invoke-McpRaw @{ jsonrpc = '2.0'; method = 'notifications/initialized' } $sid | Out-Null
    return $sid
}

function Invoke-Mcp([string]$method, $params) {
    $sid = Initialize-Mcp
    $body = @{ jsonrpc = '2.0'; id = (Get-Random -Maximum 999999); method = $method }
    if ($params) { $body['params'] = $params }
    $r = Invoke-McpRaw $body $sid
    if ($r.error) { throw ("MCP error: " + ($r.error | ConvertTo-Json -Compress)) }
    return $r.result
}
