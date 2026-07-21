# auto_ex

YBRadar 信号驱动的币安合约自动交易机器人。

## 它做什么

- 每隔 `POLL_INTERVAL_SECONDS` 轮询一次 YBRadar 的 `/api/signals`,筛出同时满足以下条件的品种:
  - 强信号(🔥 `signalKey=hot`)
  - 当前处于强信号窗口(`strongState=active`)
  - 方向明确(`recDir=long/short`,排除观望)
  - 信号足够新鲜(`strongSince` 距离现在不超过 `MAX_SIGNAL_AGE_SECONDS`,避免追一个已经走完的行情)
- 命中就在币安 USDⓈ-M 合约按配置的保证金 × 杠杆开市价单
- 开仓后**不依赖交易所条件单**:实测这个账号的 `STOP_MARKET`/`TAKE_PROFIT_MARKET` 委托会被交易所拒绝(错误码 -4120),所以止盈止损改成机器人自己每轮盯盘标记价格,触发阈值就发市价单平仓
- 止盈/止损阈值按**币本位涨跌幅**计算(`entry_price` 直接乘百分比),不受杠杆影响
- **没有超时强平**:只要没到止盈/止损线就一直持有——"舔一口就跑"指的是拿到正确收益才走,不是拿够时间就走

## 架构

```
src/
  main.py              主循环入口
  config.py            .env 配置加载
  signal_client.py     拉取/过滤 YBRadar 信号
  risk.py              开仓前风控检查(并发上限/冷却期/日亏损熔断)+ 仓位数量计算
  trader.py            开仓 + 自监盘止盈止损
  state_store.py       本地持久化状态(data/state.json):信号去重/币种冷却/当前持仓/当日盈亏
  logger.py            日志(控制台 + data/logs/trader.log)
  exchange/
    base.py             交易所抽象接口
    binance_futures.py  币安合约真实下单(测试网/实盘通过 .env 切换)
    dry_run.py          纯模拟交易所(不需要 API Key,用真实行情价在内存里模拟开平仓)
deploy/
  bootstrap.sh          一键部署脚本(全新服务器上一条命令完成部署)
  auto_ex.service       systemd 服务定义(崩溃自动重启)
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
| `POLL_INTERVAL_SECONDS` | 轮询间隔(秒) |
| `MAX_SIGNAL_AGE_SECONDS` | 信号窗口(`strongSince`)超过这个秒数就不再当新信号处理,默认对齐 YBRadar 自己 3 分钟的扫描周期 |
| `TRADE_EXCHANGE` | 目前只支持 `binance` |
| `DRY_RUN` | `true` = 纯模拟不下真实单;`false` = 真实下单 |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | 币安合约 API 密钥(建议只开交易权限,不开提现权限,并绑定服务器 IP 白名单) |
| `BINANCE_TESTNET` | `true` = 测试网假资金;`false` = 实盘真实资金 |
| `POSITION_SIZE_USDT` | 每笔保证金(USDT),名义仓位 = 这个 × `LEVERAGE` |
| `LEVERAGE` | 杠杆倍数 |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | 止盈/止损百分比,按币价格涨跌幅计算,不受杠杆影响 |
| `MAX_CONCURRENT_POSITIONS` | 同时最多持有几个仓位 |
| `SYMBOL_COOLDOWN_SECONDS` | 平仓后同一币种多久内不再重复进场 |
| `MAX_DAILY_LOSS_USDT` | 当日累计亏损达到这个数就停止开新仓(熔断),已有仓位的止盈止损不受影响 |

## 部署到服务器

全新 Ubuntu/Debian 服务器上,一条命令完成部署:

```bash
curl -fsSL https://raw.githubusercontent.com/LinChunliangliang/auto_exchange/main/deploy/bootstrap.sh | sudo bash
```

会自动完成:安装依赖、创建专用运行用户(不用 root 直接跑交易机器人)、clone 代码、创建虚拟环境并装依赖、安装 systemd 服务(崩溃自动重启)、配置防火墙(只放行 SSH)。

`.env` 不在仓库里(含密钥,已 gitignore),首次部署脚本会从 `.env.example` 复制一份占位文件,检测到还没填真实配置就不会自动启动服务,需要手动:

```bash
sudo nano /opt/auto_ex/.env    # 填入真实配置
sudo systemctl start auto_ex
```

**更新代码**:重新执行同一条 curl 命令即可,脚本是幂等的,不会覆盖已有的 `.env` 和 `data/`(历史日志/状态)。但要注意——`.env` 里的具体数值(比如风控参数)不会被 `git pull` 自动同步,改了默认配置后记得手动同步服务器上的 `.env` 再重启。

常用命令:

```bash
sudo systemctl status auto_ex               # 服务状态
sudo journalctl -u auto_ex -f               # 实时日志(含每轮心跳)
tail -f /opt/auto_ex/data/logs/trader.log   # 交易日志
sudo systemctl restart auto_ex              # 改配置后重启生效
```

## 已知限制 / 重要说明

- **止盈止损靠机器人自己盯盘,不是交易所原生条件单**——这意味着机器人进程必须持续运行,如果崩溃或断网,持仓在那段时间没有止损保护。systemd 配置了崩溃自动重启,但仍建议部署在能长期稳定运行的服务器上,而不是本机电脑。
- **YBRadar 数据本身有滞后**:网站每 3 分钟扫描一次,`MAX_SIGNAL_AGE_SECONDS` 只能过滤"信号窗口开始太久"的情况,无法消除数据源本身的延迟。
- **测试网流动性和实盘不一样**:标记价格(mark price)跟实盘几乎实时同步,但测试网撮合深度更薄,实际成交可能有比实盘更明显的滑点。
- 这是自动交易程序,涉及真实资金操作,使用风险自担。建议先在测试网跑够长时间、确认逻辑符合预期后再切实盘,并从小资金开始。
