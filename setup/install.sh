#!/bin/bash
# install.sh — Set up Splitflap OS on a Raspberry Pi (Bookworm / Trixie)
# Run as root from the repo directory: sudo bash setup/install.sh
# Use --skip-network to skip hotspot/NetworkManager setup

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$REPO_DIR/venv"
SKIP_NETWORK=false

for arg in "$@"; do
    case "$arg" in
        --skip-network) SKIP_NETWORK=true ;;
    esac
done

echo "=== Splitflap OS Installer ==="
echo "  Installing from: $REPO_DIR"
if $SKIP_NETWORK; then
    echo "  Network/hotspot: SKIPPED (--skip-network)"
fi
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo bash setup/install.sh)"
    exit 1
fi

# Install system packages
echo "[1/4] Installing system packages..."
apt-get update -qq
if $SKIP_NETWORK; then
    apt-get install -y python3-pip python3-venv libopenblas0
else
    apt-get install -y python3-pip python3-venv network-manager libopenblas0
fi

# Ensure NetworkManager manages WiFi
if $SKIP_NETWORK; then
    echo "[2/4] Skipping NetworkManager (--skip-network)..."
else
    echo "[2/4] Configuring NetworkManager..."
    if ! systemctl is-active --quiet NetworkManager; then
        systemctl enable NetworkManager
        systemctl start NetworkManager
    fi
fi

# Create venv and install Python dependencies
# Using a venv avoids PEP 668 conflicts on Bookworm/Trixie and keeps
# dependencies isolated from the system Python.
# --prefer-binary uses pre-built wheels — much faster on Pi Zero W (ARMv6).
echo "[3/4] Installing Python dependencies..."
if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi
echo "  Installing packages (this may take a while on Pi Zero W)..."
"$VENV_DIR/bin/pip" install --prefer-binary -r "$REPO_DIR/server/requirements.txt"

# Make scripts executable
chmod +x "$REPO_DIR/setup/network-check.sh"

# Install systemd services (preserve existing Environment= variables)
echo "[4/4] Setting up systemd services..."

# Install a unit file, carrying over the Environment= settings of the copy
# already installed.
#
# Each setting is keyed by its variable name. Keying on the literal
# "Environment" made every variable collide: the substitution rewrote every
# Environment= line in the template, so a unit with two of them came out of an
# update holding only whichever was read last — silently dropping, say,
# SPLITFLAP_CONFIG and pointing the server at a fresh settings.json. A variable
# the template does not mention is appended to [Service] rather than dropped,
# so one added by hand survives too.
install_service() {
    local src="$1"
    local dest="$2"
    local tmp
    tmp=$(mktemp)
    sed "s|/opt/splitflap-os|$REPO_DIR|g" "$src" > "$tmp"

    if [ -f "$dest" ]; then
        local -a names=() lines=() taken=()
        local line name

        while IFS= read -r line || [ -n "$line" ]; do
            if [[ "$line" == Environment=*=* ]]; then
                name="${line#Environment=}"
                name="${name%%=*}"
                if [ -n "$name" ]; then
                    names+=("$name")
                    lines+=("$line")
                    taken+=(0)
                fi
            fi
        done < "$dest"

        if [ ${#names[@]} -gt 0 ]; then
            local out section="" tname replaced i
            out=$(mktemp)

            while IFS= read -r line || [ -n "$line" ]; do
                # Leaving [Service]: emit anything preserved that the template
                # has no line for. Environment= is only meaningful here, so it
                # cannot be appended at the end of the file.
                if [[ "$line" == \[*\] ]]; then
                    if [ "$section" = "Service" ]; then
                        for i in "${!names[@]}"; do
                            if [ "${taken[$i]}" = 0 ]; then
                                printf '%s\n' "${lines[$i]}"
                            fi
                        done
                    fi
                    section="${line#[}"
                    section="${section%]}"
                    printf '%s\n' "$line"
                    continue
                fi

                if [[ "$line" == Environment=*=* ]]; then
                    tname="${line#Environment=}"
                    tname="${tname%%=*}"
                    replaced=0
                    for i in "${!names[@]}"; do
                        if [ "${names[$i]}" = "$tname" ]; then
                            printf '%s\n' "${lines[$i]}"
                            taken[$i]=1
                            replaced=1
                            break
                        fi
                    done
                    if [ "$replaced" = 1 ]; then
                        continue
                    fi
                fi

                printf '%s\n' "$line"
            done < "$tmp" > "$out"

            # A unit whose last section is [Service] never hits another header.
            if [ "$section" = "Service" ]; then
                for i in "${!names[@]}"; do
                    if [ "${taken[$i]}" = 0 ]; then
                        printf '%s\n' "${lines[$i]}" >> "$out"
                    fi
                done
            fi

            mv "$out" "$tmp"
        fi
    fi

    mv "$tmp" "$dest"
}

if ! $SKIP_NETWORK; then
    install_service "$REPO_DIR/setup/splitflap-network.service" /etc/systemd/system/splitflap-network.service
fi
install_service "$REPO_DIR/setup/splitflap.service" /etc/systemd/system/splitflap.service
systemctl daemon-reload
if ! $SKIP_NETWORK; then
    systemctl enable splitflap-network.service
    systemctl restart splitflap-network.service
fi
systemctl enable splitflap.service
systemctl restart splitflap.service

# Create settings.json if it doesn't exist
if [ ! -f "$REPO_DIR/server/settings.json" ]; then
    echo "{}" > "$REPO_DIR/server/settings.json"
fi

echo ""
echo "=== Splitflap OS installed and running ==="
echo ""
echo "  Access UI:     http://$(hostname -I | awk '{print $1}')"
echo "  View logs:     journalctl -u splitflap -f"
if ! $SKIP_NETWORK; then
    echo "  Network logs:  journalctl -u splitflap-network -f"
fi
echo ""
echo "  To update:     cd $REPO_DIR && git pull && sudo bash setup/install.sh"
echo ""
if ! $SKIP_NETWORK; then
    echo "  WiFi hotspot fallback is enabled."
    echo "  If no WiFi is found on boot, the Pi will create:"
    echo "    SSID: SplitflapOS"
    echo "    Password: splitflap"
    echo ""
    echo "  To change hotspot credentials, edit:"
    echo "    /etc/systemd/system/splitflap-network.service"
    echo ""
fi
