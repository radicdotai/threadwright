#!/usr/bin/env bash
set -euo pipefail

ROLE="${1:-}"
[[ "$ROLE" == listener || "$ROLE" == worker ]] || { echo "usage: bootstrap.sh listener|worker"; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/threadwright
[[ -f "$SRC/deploy/site.env" ]] && source "$SRC/deploy/site.env"
ORG="${ORG:-}"
REPOS="${REPOS:-}"
GIT_NAME="${GIT_NAME:-Threadwright}"
GIT_EMAIL="${GIT_EMAIL:-}"
DB_PROXIES="${DB_PROXIES:-}"
EXTRA_PACKAGES="${EXTRA_PACKAGES:-}"
SQL_PROXY_VERSION="${SQL_PROXY_VERSION:-v2.28.1}"
NODE_VERSION="${NODE_VERSION:-v20.20.2}"
PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION:-}"
EGRESS_ALLOW="${EGRESS_ALLOW:-}"

apt-get update -q
apt-get install -y -q git curl ca-certificates python3 python3-venv
if [[ "$ROLE" == worker ]]; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /etc/apt/keyrings/cloud.google.gpg
  echo "deb [signed-by=/etc/apt/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    > /etc/apt/sources.list.d/google-cloud-sdk.list
  apt-get update -q
  apt-get install -y -q docker.io docker-compose-v2 postgresql-client gh google-cloud-cli $EXTRA_PACKAGES
fi

id threadwright >/dev/null 2>&1 || useradd -m -s /bin/bash threadwright
install -d -o threadwright -g threadwright /srv/threadwright/state
if [[ "$ROLE" == worker ]]; then
  usermod -aG docker threadwright
  install -d -o threadwright -g threadwright /srv/threadwright/repos /srv/threadwright/work /srv/threadwright/dev-files
fi

if [[ "$SRC" != "$APP" ]]; then
  rm -rf "$APP"
  mkdir -p "$APP"
  tar -C "$SRC" --exclude=.venv --exclude=.git -cf - . | tar -C "$APP" -xf -
fi
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
(cd "$APP" && UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --python /usr/bin/python3)
chown -R root:root "$APP"
chmod -R go-w "$APP"
chmod 755 "$APP"/deploy/hooks/*.sh

ENV_SRC="$APP/deploy/$ROLE.env"
[[ -f "$ENV_SRC" ]] || ENV_SRC="$APP/deploy/$ROLE.env.example"
[[ -f "/etc/threadwright-$ROLE.env" ]] || install -m 600 -o root -g root "$ENV_SRC" "/etc/threadwright-$ROLE.env"
install -m 644 "$APP/deploy/threadwright@.service" /etc/systemd/system/threadwright@.service
systemctl daemon-reload

if [[ "$ROLE" == worker ]]; then
  sudo -iu threadwright bash -c 'command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash'
  if [[ "$(/usr/local/bin/node --version 2>/dev/null)" != "$NODE_VERSION" ]]; then
    tmp="$(mktemp -d)"
    tarball="node-$NODE_VERSION-linux-x64.tar.xz"
    curl -sSfL -o "$tmp/$tarball" "https://nodejs.org/dist/$NODE_VERSION/$tarball"
    curl -sSfL -o "$tmp/SHASUMS256.txt" "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt"
    (cd "$tmp" && grep " $tarball\$" SHASUMS256.txt | sha256sum -c -)
    tar -xJf "$tmp/$tarball" -C /usr/local --strip-components=1
    rm -rf "$tmp"
  fi
  if [[ -n "$PLAYWRIGHT_VERSION" ]]; then
    /usr/local/bin/npx -y "playwright@$PLAYWRIGHT_VERSION" install-deps chromium
    sudo -iu threadwright /usr/local/bin/npx -y "playwright@$PLAYWRIGHT_VERSION" install chromium
  fi
  if [[ -n "$EGRESS_ALLOW" ]]; then
    apt-get install -y -q tinyproxy
    : > /etc/tinyproxy/filter
    for domain in $EGRESS_ALLOW; do
      printf '(^|\\.)%s$\n' "${domain//./\\.}" >> /etc/tinyproxy/filter
    done
    cat > /etc/tinyproxy/tinyproxy.conf <<CONF
User tinyproxy
Group tinyproxy
Port 8888
Listen 127.0.0.1
Timeout 600
MaxClients 100
Filter "/etc/tinyproxy/filter"
FilterType ere
FilterDefaultDeny Yes
FilterURLs Off
ConnectPort 443
ConnectPort 80
LogLevel Connect
CONF
    systemctl enable tinyproxy
    systemctl restart tinyproxy
  fi
  sudo -iu threadwright git config --global user.name "$GIT_NAME"
  [[ -z "$GIT_EMAIL" ]] || sudo -iu threadwright git config --global user.email "$GIT_EMAIL"
  if [[ -n "$DB_PROXIES" ]]; then
    [[ -x /usr/local/bin/cloud-sql-proxy ]] || curl -sSfL -o /usr/local/bin/cloud-sql-proxy \
      "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/$SQL_PROXY_VERSION/cloud-sql-proxy.linux.amd64"
    chmod 755 /usr/local/bin/cloud-sql-proxy
    for proxy in $DB_PROXIES; do
      IFS='=' read -r name port instance <<< "$proxy"
      sed -e "s/@NAME@/$name/g" -e "s/@PORT@/$port/g" -e "s/@INSTANCE@/$instance/g" "$APP/deploy/threadwright-db.service" \
        > "/etc/systemd/system/threadwright-db-$name.service"
      systemctl daemon-reload
      systemctl enable --now "threadwright-db-$name"
    done
  fi
  if [[ -z "$ORG" || -z "$REPOS" ]]; then
    echo "Set ORG and REPOS in deploy/site.env, then re-run this script to clone the repos."
  elif sudo -iu threadwright gh auth status >/dev/null 2>&1; then
    sudo -iu threadwright gh auth setup-git
    for repo in $REPOS; do
      [[ -d "/srv/threadwright/repos/$repo" ]] || sudo -iu threadwright gh repo clone "$ORG/$repo" "/srv/threadwright/repos/$repo"
    done
    echo "Repos ready."
  else
    echo "Next: sudo -iu threadwright gh auth login, then re-run this script to clone the repos."
  fi
fi

echo "Then: fill /etc/threadwright-$ROLE.env (see README) and run: systemctl enable --now threadwright@$ROLE"
