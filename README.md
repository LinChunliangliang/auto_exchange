# auto_ex

YBRadar 信号驱动的币安合约自动交易机器人。

## 它做什么

- 每隔 `SIGNAL_POLL_INTERVAL_SECONDS` 轮询一次 YBRadar 的 `/api/signals`,筛出同时满足以下条件的品种:
  - 强信号(🔥 `signalKey=hot`)
  - 当前处于强信号窗口(`strongState=active`)
  - 方向明确(`recDir=long/short`,排除观望)
  - 信号足够新鲜(`strongSince` 距离现在不超过 `MAX_SIGNAL_AGE_SECONDS`,避免追一个已经走完的行情)
- 命中就在币安 USDⓈ-M 合约按配置的保证金 × 杠杆开市价单
- 开仓后**不依赖交易所条件单**:实测这个账号的 `STOP_MARKET`/`TAKE_PROFIT_MARKET` 委托会被交易所拒绝(错误码 -4120),所以止盈止损改成机器人自己每隔 `POSITION_MONITOR_INTERVAL_SECONDS` 盯盘一次标记价格,触发阈值就发市价单平仓——盯仓和拉信号是两个独立的节奏,前者查币安自己的 API 可以很快,后者要顾及第三方网站的压力
- 止盈/止损阈值按**币本位涨跌幅**计算(`entry_price` 直接乘百分比),不受杠杆影响
- **没有超时强平**:只要没到止盈/止损线就一直持有——"舔一口就跑"指的是拿到正确收益才走,不是拿够时间就走

## 架构

```
src/
  main.py              交易主程序循环入口
  dashboard.py         只读监控面板(独立进程,Flask + Basic Auth)
  config.py            .env 配置加载
  signal_client.py     拉取/过滤 YBRadar 信号
  risk.py              开仓前风控检查(并发上限/冷却期/日亏损熔断)+ 仓位数量计算
  trader.py            开仓 + 自监盘止盈止损
  state_store.py       本地持久化状态(data/state.json):信号去重/币种冷却/当前持仓/当日盈亏/成交记录
  status_file.py       轻量心跳状态文件(data/status.json),给面板判断主程序是否存活
  logger.py            日志(控制台 + data/logs/trader.log,固定按北京时间显示)
  exchange/
    base.py             交易所抽象接口(含限流熔断状态查询)
    binance_futures.py  币安合约真实下单(测试网/实盘通过 .env 切换),内置限流熔断
    dry_run.py          纯模拟交易所(不需要 API Key,用真实行情价在内存里模拟开平仓)
deploy/
  bootstrap.sh                一键部署脚本(全新服务器上一条命令完成部署)
  auto_ex.service             交易主程序 systemd 服务定义(崩溃自动重启)
  auto_ex_dashboard.service   监控面板 systemd 服务定义(崩溃自动重启)
```

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env:
#   - YBRADAR_SESSION_COOKIE 必填(登录 YBRadar 网页后从浏览器 Cookie 里复制 ybr_session)
#   - 想真实下单再填 BINANCE_API_KEY / BINANCE_API_SECRET,否则保持 DRY_RUN=true 纯模拟

PYTHONPATH=src python src/main.py
```

## 配置说明(`.env`)

| 变量 | 说明 |
|---|---|
| `YBRADAR_SESSION_COOKIE` | YBRadar 登录态,过期后需要重新登录复制新值 |
| `SIGNAL_POLL_INTERVAL_SECONDS` | 拉取 YBRadar 信号的间隔(秒),默认对齐 YBRadar 自己 3 分钟的扫描周期,没必要拉更快 |
| `POSITION_MONITOR_INTERVAL_SECONDS` | 检查已开仓位是否触发止盈/止损的间隔(秒),查的是币安自己的 API,跟拉信号的频率完全独立,默认 5 秒,这些币行情变化快,不能等 3 分钟才查一次 |
| `MAX_SIGNAL_AGE_SECONDS` | 信号窗口(`strongSince`)超过这个秒数就不再当新信号处理,要比 `SIGNAL_POLL_INTERVAL_SECONDS` 明显大一截,留出轮询延迟/漏轮的容错空间 |
| `TRADE_EXCHANGE` | 目前只支持 `binance` |
| `DRY_RUN` | `true` = 纯模拟不下真实单;`false` = 真实下单 |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | 币安合约 API 密钥(建议只开交易权限,不开提现权限,并绑定服务器 IP 白名单) |
| `BINANCE_TESTNET` | `true` = 测试网假资金;`false` = 实盘真实资金 |
| `POSITION_SIZE_PCT` | 每笔保证金 = 账户可用余额 × 这个百分比(小数,0.05 = 5%),每次开仓前实时查余额计算,不是固定金额,名义仓位 = 保证金 × `LEVERAGE` |
| `DRY_RUN_BALANCE_USDT` | 仅 `DRY_RUN=true` 时用到,模拟账户没有真实余额,用这个固定值当参考余额 |
| `LEVERAGE` | 杠杆倍数 |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | 止盈/止损百分比,按币价格涨跌幅计算,不受杠杆影响 |
| `MAX_CONCURRENT_POSITIONS` | 同时最多持有几个仓位 |
| `SYMBOL_COOLDOWN_SECONDS` | 平仓后同一币种多久内不再重复进场 |
| `MAX_DAILY_LOSS_PCT` | 当日累计亏损达到 账户余额 × 这个百分比 就停止开新仓(熔断),已有仓位的止盈止损不受影响。跟 `POSITION_SIZE_PCT` 一样按余额百分比算,不是写死的美元数 |
| `DASHBOARD_PORT` | 监控面板监听端口 |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | 面板 Basic Auth 账号密码,必须改成自己的强密码,不能留空(留空面板拒绝启动) |

## 监控面板

只读的 Web 面板,跟交易主程序是完全独立的进程(`src/dashboard.py`),挂了也不影响交易逻辑。展示内容:

- 运行状态:当前模式(DRY_RUN/测试网/实盘)、心跳是否正常、交易所是否正被限流熔断
- 当前持仓:开仓价/标记价/止盈止损线/持仓时长/浮动盈亏
- 风控状态:当日盈亏 vs 熔断线、并发持仓数、冷却中的币种
- 绩效统计:胜率、平均止盈/止损、净盈亏(基于最近 200 笔结构化成交记录,不依赖解析日志文本)
- 最近成交记录、当前生效的风控参数(只读,方便确认服务器实际跑的是哪套配置)

本地单独运行:

```bash
PYTHONPATH=src python src/dashboard.py
```

浏览器/手机访问 `http://<地址>:<DASHBOARD_PORT>/`,会弹出账号密码框(HTTP Basic Auth)。

**安全提示**:Basic Auth 在没有配 HTTPS 的情况下是明文传输,不要用你在其他地方也在用的密码。面板只读,没有任何下单/改配置的操作入口,即使密码泄露也不会导致直接的资金操作,但会暴露仓位/盈亏等信息。

## 部署到服务器

全新 Ubuntu/Debian 服务器上,一条命令完成部署:

```bash
curl -fsSL https://raw.githubusercontent.com/LinChunliangliang/auto_exchange/main/deploy/bootstrap.sh | sudo bash
```

会自动完成:安装依赖、创建专用运行用户(不用 root 直接跑交易机器人)、clone 代码、创建虚拟环境并装依赖、安装交易主程序和监控面板两个 systemd 服务(都配置崩溃自动重启)、配置防火墙(探测并放行实际使用的 SSH 端口 + 面板端口,其余一律拒绝)。

`.env` 不在仓库里(含密钥,已 gitignore),首次部署脚本会从 `.env.example` 复制一份占位文件。交易主程序和面板是分开判断是否就绪的(检测到 `YBRADAR_SESSION_COOKIE` 还没填就不会启动交易主程序,检测到 `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` 还没填就不会启动面板),但**只需要一条命令就能同时启动两个**——`auto_ex.service` 配置了 `Wants=auto_ex_dashboard.service`,启动交易主程序时会顺带拉起面板:

```bash
sudo nano /opt/auto_ex/.env    # 填入真实配置(交易 + 面板账号密码都填)
sudo systemctl start auto_ex   # 一条命令,交易主程序和面板都会启动
```

**更新代码**:重新执行同一条 curl 命令即可,脚本是幂等的,不会覆盖已有的 `.env` 和 `data/`(历史日志/状态)。但要注意——`.env` 里的具体数值(比如风控参数)不会被 `git pull` 自动同步,改了默认配置后记得手动同步服务器上的 `.env` 再重启。

常用命令:

```bash
sudo systemctl status auto_ex                    # 交易主程序状态
sudo journalctl -u auto_ex -f                    # 交易主程序实时日志(含每轮心跳)
tail -f /opt/auto_ex/data/logs/trader.log        # 交易明细日志
sudo systemctl status auto_ex_dashboard          # 面板状态
sudo journalctl -u auto_ex_dashboard -f          # 面板日志
sudo systemctl restart auto_ex auto_ex_dashboard # 改配置后重启两个都生效(面板已经在跑的话,
                                                  # 单独 restart auto_ex 不会连带重启它,
                                                  # Wants= 只在面板还没启动时才会顺带拉起)
```

## 已知限制 / 重要说明

- **止盈止损靠机器人自己盯盘,不是交易所原生条件单**——这意味着机器人进程必须持续运行,如果崩溃或断网,持仓在那段时间没有止损保护。systemd 配置了崩溃自动重启,但仍建议部署在能长期稳定运行的服务器上,而不是本机电脑。
- **YBRadar 数据本身有滞后**:网站每 3 分钟扫描一次,`MAX_SIGNAL_AGE_SECONDS` 只能过滤"信号窗口开始太久"的情况,无法消除数据源本身的延迟。
- **测试网流动性和实盘不一样**:标记价格(mark price)跟实盘几乎实时同步,但测试网撮合深度更薄,实际成交可能有比实盘更明显的滑点。
- 这是自动交易程序,涉及真实资金操作,使用风险自担。建议先在测试网跑够长时间、确认逻辑符合预期后再切实盘,并从小资金开始。
