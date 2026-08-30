#!/bin/bash
# install-launchd.sh — Install Agora launchd plists for crash recovery
# Run: bash scripts/install-launchd.sh

set -euo pipefail

AGORA_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/.agora/logs"

mkdir -p "$LOG_DIR"

# ── SSE Server (:7431, requires --sse flag) ──
cat > "$LAUNCH_DIR/com.agora.sse.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agora.sse</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/uv</string>
        <string>run</string>
        <string>--extra</string>
        <string>admission</string>
        <string>--directory</string>
        <string>__AGORA_DIR__</string>
        <string>agora-mcp</string>
        <string>--sse</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/xiamingxing/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>WORKSPACE_HOME</key>
        <string>/Users/xiamingxing/Workspace</string>
        <key>AGORA_ADMISSION_MODE</key>
        <string>degraded</string>
        <key>AGORA_DATA_DIR</key>
        <string>__LOG_DIR__</string>
        <key>AGORA_AUTH_MODE</key>
        <string>permissive</string>
        <key>AGORA_GATEWAY_OWNER</key>
        <string>1</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>__LOG_DIR__/sse-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__LOG_DIR__/sse-stderr.log</string>
    <key>Nice</key>
    <integer>-5</integer>
</dict>
</plist>
PLIST

# ── Daemon (:7432, A2A bus server, T1-12 WP-P2 prerequisite) ──
cat > "$LAUNCH_DIR/com.agora.daemon.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agora.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>src/agora/daemon.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>__AGORA_DIR__</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>__LOG_DIR__/daemon-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__LOG_DIR__/daemon-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>WORKSPACE_HOME</key>
        <string>/Users/xiamingxing/Workspace</string>
    </dict>
</dict>
</plist>
PLIST

# ── Gateway (:7422) — P2-5: SSE 进程作唯一 backend owner, gateway 独立进程已废弃 ──
# SSE 进程经 AGORA_GATEWAY_OWNER=1 主动拉起 KNOWN_BACKENDS (mcp.py _init_proxy
# Phase 1.5), 不再需要独立 gateway 进程重复拉起。plist 不再安装。
# 如需独立 gateway, 取消注释下方块。
# cat > "$LAUNCH_DIR/com.agora.gateway.plist" <<'PLIST'
# <?xml version="1.0" encoding="UTF-8"?>
# <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
# <plist version="1.0">
# <dict>
#     <key>Label</key>
#     <string>com.agora.gateway</string>
#     <key>ProgramArguments</key>
#     <array>
#         <string>/opt/homebrew/bin/uv</string>
#         <string>run</string>
#         <string>--directory</string>
#         <string>__AGORA_DIR__</string>
#         <string>python</string>
#         <string>-m</string>
#         <string>agora.auth.mcp_gateway</string>
#     </array>
#     <key>EnvironmentVariables</key>
#     <dict>
#         <key>PATH</key>
#         <string>/Users/xiamingxing/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
#         <key>WORKSPACE_HOME</key>
#         <string>/Users/xiamingxing/Workspace</string>
#         <key>AGORA_ADMISSION_MODE</key>
#         <string>degraded</string>
#         <key>AGORA_DATA_DIR</key>
#         <string>__LOG_DIR__</string>
#     </dict>
#     <key>KeepAlive</key>
#     <true/>
#     <key>ThrottleInterval</key>
#     <integer>5</integer>
#     <key>RunAtLoad</key>
#     <true/>
#     <key>StandardOutPath</key>
#     <string>__LOG_DIR__/gateway-stdout.log</string>
#     <key>StandardErrorPath</key>
#     <string>__LOG_DIR__/gateway-stderr.log</string>
# </dict>
# </plist>
# PLIST

# Substitute placeholders
sed -i '' "s|__AGORA_DIR__|$AGORA_DIR|g" "$LAUNCH_DIR/com.agora.sse.plist"
sed -i '' "s|__LOG_DIR__|$LOG_DIR|g" "$LAUNCH_DIR/com.agora.sse.plist"
sed -i '' "s|__AGORA_DIR__|$AGORA_DIR|g" "$LAUNCH_DIR/com.agora.daemon.plist"
sed -i '' "s|__LOG_DIR__|$LOG_DIR|g" "$LAUNCH_DIR/com.agora.daemon.plist"

# 移除已废弃的 gateway plist (P2-5: SSE 单一 owner)
rm -f "$LAUNCH_DIR/com.agora.gateway.plist"

# Load (unload first to avoid duplicates)
launchctl unload "$LAUNCH_DIR/com.agora.sse.plist" 2>/dev/null || true
launchctl unload "$LAUNCH_DIR/com.agora.daemon.plist" 2>/dev/null || true
launchctl unload "$LAUNCH_DIR/com.agora.gateway.plist" 2>/dev/null || true
launchctl load "$LAUNCH_DIR/com.agora.sse.plist"
launchctl load "$LAUNCH_DIR/com.agora.daemon.plist"

echo "✅ Agora launchd services installed:"
launchctl list | grep com.agora
