#!/usr/bin/env bash
# 在服务器上执行(先用 rsync 把整个项目目录传到 /opt/auto_ex 再跑这个脚本)。
# 适用于 Ubuntu/Debian 系统;跑之前确认代码(含 .env)已经在 /opt/auto_ex 里。
set -euo pipefail

APP_DIR="/opt/auto_ex"
SERVICE_USER="autoex"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "错误: $APP_DIR/.env 不存在,先把本地的 .env 传上来再跑这个脚本" >&2
  exit 1
fi

echo "== 更新系统 & 安装基础依赖 =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip ufw

echo "== 创建专用运行用户(不用 root 跑交易机器人,降低风险) =="
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" || true
fi

echo "== 目录权限 =="
sudo mkdir -p "$APP_DIR/data/logs"
sudo chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
sudo chmod 600 "$APP_DIR/.env"

echo "== 创建虚拟环境并安装依赖 =="
sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "== 安装并启动 systemd 服务 =="
sudo cp "$APP_DIR/deploy/auto_ex.service" /etc/systemd/system/auto_ex.service
sudo systemctl daemon-reload
sudo systemctl enable auto_ex
sudo systemctl restart auto_ex

echo "== 防火墙:只放行 SSH,其他端口一律拒绝(这个服务不需要对外开端口) =="
sudo ufw allow OpenSSH
sudo ufw --force enable

sleep 2
echo "== 当前服务状态 =="
sudo systemctl --no-pager status auto_ex || true

cat <<EOF

部署完成。常用命令:
  查看实时状态: sudo systemctl status auto_ex
  查看实时日志: sudo journalctl -u auto_ex -f
  查看交易日志: tail -f $APP_DIR/data/logs/trader.log
  重启服务:     sudo systemctl restart auto_ex
  停止服务:     sudo systemctl stop auto_ex
EOF
