#!/bin/sh
# 在 HA 主机上执行：把 sicn_heater 集成部署到 /config 并重启 HA。
# 用法：先把整个 sicn_ha 目录 scp 到 HA 的 /tmp/sicn_ha，再在 HA 终端跑此脚本。
set -e

SRC=/tmp/sicn_ha/custom_components/sicn_heater
DST=/config/custom_components/sicn_heater

mkdir -p /config/custom_components
rm -rf "$DST"
cp -r "$SRC" "$DST"

echo "已部署到 $DST"
if command -v ha >/dev/null 2>&1; then
  ha core restart
  echo "已重启 HA。去 设置 -> 设备与服务 添加 SICN 加热器"
else
  echo "未找到 ha 命令，请手动重启 Home Assistant"
fi
