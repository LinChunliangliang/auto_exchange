#!/usr/bin/env bash
# 一键部署脚本。在全新的 Ubuntu/Debian 服务器上,用 root 权限执行:
#
#   curl -fsSL https://raw.githubusercontent.com/LinChunliangliang/auto_exchange/main/deploy/bootstrap.sh | sudo bash
#
# 重复执行是安全的(拉最新代码、重装依赖、重启服务),不会覆盖已有的 .env 和 data/ 目录,
# 可以用来做后续更新部署。
set -euo pipefail

REPO_URL="https://github.com/LinChunliangliang/auto_exchange.git"
APP_DIR="/opt/auto_ex"
SERVICE_USER="autoex"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 权限运行,例如: curl -fsSL <url> | sudo bash" >&2
  exit 1
fi

echo "== 安装基础依赖 =="
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip ufw

echo "== 创建专用运行用户(不用 root 直接跑交易机器人) =="
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "== 拉取/更新代码 =="
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  # 目录存在但不是 git 仓库(比如之前用旧的手动部署方式留下的):
  # 先把 .env 和 data/ 备份好,重新 clone 之后再放回去,避免把密钥/历史数据冲掉
  TMP_ENV=""
  TMP_DATA=""
  if [ -f "$APP_DIR/.env" ]; then
    TMP_ENV="$(mktemp)"
    cp "$APP_DIR/.env" "$TMP_ENV"
  fi
  if [ -d "$APP_DIR/data" ]; then
    TMP_DATA="$(mktemp -d)"
    cp -a "$APP_DIR/data/." "$TMP_DATA/"
  fi

  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"

  if [ -n "$TMP_ENV" ]; then
    cp "$TMP_ENV" "$APP_DIR/.env"
    rm -f "$TMP_ENV"
  fi
  if [ -n "$TMP_DATA" ]; then
    mkdir -p "$APP_DIR/data"
    cp -a "$TMP_DATA/." "$APP_DIR/data/"
    rm -rf "$TMP_DATA"
  fi
fi

echo "== 准备 .env =="
FIRST_TIME_ENV=false
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  FIRST_TIME_ENV=true
fi

mkdir -p "$APP_DIR/data/logs"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "== 创建虚拟环境并安装依赖 =="
sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "== 安装 systemd 服务 =="
cp "$APP_DIR/deploy/auto_ex.service" /etc/systemd/system/auto_ex.service
systemctl daemon-reload
systemctl enable auto_ex

echo "== 防火墙:只放行 SSH,其他一律拒绝 =="
# 之前这里直接用 `ufw allow OpenSSH`,这个规则硬编码放行 22 端口,不会读 sshd 实际配置。
# 如果服务器 SSH 用的是非 22 端口,ufw enable 之后真实端口没被放行,会直接把自己锁在外面。
# 改成实际探测 sshd 正在监听的端口:优先看运行时真实监听(ss),配置文件解析做兜底。
detect_ssh_ports() {
  local ports=""
  if command -v ss >/dev/null 2>&1; then
    ports="$(ss -tlnp 2>/dev/null | grep -i sshd | grep -oE ':[0-9]+' | tr -d ':' | sort -u)"
  fi
  if [ -z "$ports" ]; then
    ports="$(grep -rhE '^[[:space:]]*Port[[:space:]]+[0-9]+' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
      | awk '{print $2}' | sort -u)"
  fi
  if [ -z "$ports" ]; then
    ports="22"
  fi
  echo "$ports"
}

SSH_PORTS="$(detect_ssh_ports)"
PORTS_VALID=true
for p in $SSH_PORTS; do
  if ! [[ "$p" =~ ^[0-9]+$ ]]; then
    PORTS_VALID=false
  fi
done

if [ -z "$SSH_PORTS" ] || [ "$PORTS_VALID" = false ]; then
  echo "!! 无法可靠探测到 SSH 监听端口,为避免把你锁在外面,这次跳过防火墙配置 !!" >&2
  echo "!! 请手动确认 SSH 端口后自己执行: ufw allow <你的端口>/tcp && ufw enable !!" >&2
else
  echo "探测到 SSH 监听端口: $SSH_PORTS(将全部放行)"
  for p in $SSH_PORTS; do
    ufw allow "$p"/tcp
  done
  ufw --force enable || true
fi

# .env 是全新拷贝的占位内容(或者还没填 session cookie),先别启动,
# 避免拿着空密钥/占位符跑起来在日志里刷一堆失败
NEEDS_CONFIG=false
if [ "$FIRST_TIME_ENV" = true ]; then
  NEEDS_CONFIG=true
elif grep -q "换成你自己的" "$APP_DIR/.env" || ! grep -qE '^YBRADAR_SESSION_COOKIE=.+' "$APP_DIR/.env"; then
  NEEDS_CONFIG=true
fi

if [ "$NEEDS_CONFIG" = true ]; then
  cat <<EOF

======================================================================
.env 还没有配置真实的 YBRADAR_SESSION_COOKIE / 币安 API Key,服务先不启动。

编辑配置:
  sudo nano $APP_DIR/.env

填好之后启动:
  sudo systemctl start auto_ex
======================================================================
EOF
else
  systemctl restart auto_ex
  sleep 2
  echo "== 服务状态 =="
  systemctl --no-pager status auto_ex || true
fi

cat <<EOF

常用命令:
  查看状态:         sudo systemctl status auto_ex
  查看实时日志:     sudo journalctl -u auto_ex -f
  查看交易日志:     tail -f $APP_DIR/data/logs/trader.log
  改配置后重启:     sudo systemctl restart auto_ex
  更新代码并重新部署: 重新执行这条 curl 命令即可(不会覆盖 .env 和 data/)
EOF
