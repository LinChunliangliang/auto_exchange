# auto_ex

YBRadar 信号驱动的币安合约自动交易机器人。

## 它做什么

- 每隔 `SIGNAL_POLL_INTERVAL_SECONDS` 轮询一次 YBRadar 的 `/api/signals`,筛出同时满足以下条件的品种:
  - 强信号(🔥 `signalKey=hot`)
  - 当前处于强信号窗口(`strongState=active`)
  - 方向明确(`recDir=long/short`,排除观望)
  - 信号足够新鲜(`strongSince` 距离现在不超过 `MAX_SIGNAL_AGE_SECONDS`,避免追一个已经走完的行情)
- 命中就开市价单,**仓位数量按目标风险金额反推**(不是固定保证金):目标风险金额 = 账户余额 × `POSITION_SIZE_PCT` × `LEVERAGE` × `STOP_LOSS_PCT`,仓位数量 = 目标风险金额 ÷ (价格 × 这笔交易实际用的止损百分比)——止损空间越宽仓位越小,止损空间越窄仓位越大,不管哪个品种止损空间差多少,真止损时亏掉的绝对金额基本恒定
- 开仓后**不依赖交易所条件单**:实测这个账号的 `STOP_MARKET`/`TAKE_PROFIT_MARKET` 委托会被交易所拒绝(错误码 -4120),所以止盈止损改成机器人自己每隔 `POSITION_MONITOR_INTERVAL_SECONDS` 盯盘一次标记价格,触发阈值就发市价单平仓——盯仓和拉信号是两个独立的节奏,前者查币安自己的 API 可以很快,后者要顾及第三方网站的压力
- 止盈/止损阈值按**币本位涨跌幅**计算(`entry_price` 直接乘百分比),不受杠杆影响
- **可选的 ATR 动态止损**(`ATR_STOP_LOSS_ENABLED`,默认关闭):开仓时拉最近K线算 ATR(平均真实波幅),把止损空间换算成"这个品种最近实际波动有多大",下限是 `ATR_MIN_STOP_PCT`、**没有上限**(真正波动大的品种止损空间可以明显超过 `STOP_LOSS_PCT`,风险交给上面的仓位联动机制兜底,不是死卡止损空间的大小);查不到K线数据自动退回固定的 `STOP_LOSS_PCT`
- **没有"亏损也强平"的超时逻辑**:亏损或持平的仓位不会因为拿久了就被强平,一直等到止盈/止损线——"舔一口就跑"指的是拿到正确收益才走,不是拿够时间就走
- **但持仓超过 `PROFIT_LOCK_AFTER_SECONDS` 且浮盈超过 `PROFIT_LOCK_MIN_PCT` 会锁定收益提前平仓**:持仓太久说明预期的快速突破大概率已经落空,继续拖着只是在赌反转不会发生;必须超过最小浮盈门槛才触发(市价单平仓有真实的手续费+滑点成本,浮盈太薄的话锁盈这个动作本身执行完反而会变成亏损),不影响亏损/持平的仓位
- **可选的阶梯止盈**(`LADDER_TAKE_PROFIT_ENABLED`,默认关闭):碰到止盈线不是一次性全平,而是分批止盈——第1档平掉大部分仓位并把止损上移到保本附近,后面每再往有利方向走一个 `TAKE_PROFIT_PCT` 就把剩余仓位再平一半,最多加到 `LADDER_MAX_LEVELS` 档封顶,剩下的尾巴交给保本止损/超时锁盈处理。每一档都是真实的部分平仓,单独记一笔成交记录

## 架构

```
src/
  main.py              交易主程序循环入口
  dashboard.py         监控面板(独立进程,Flask + Basic Auth),可暂停/恢复开仓和管理币种黑名单
  config.py            .env 配置加载
  signal_client.py     拉取/过滤 YBRadar 信号
  risk.py              开仓前风控检查(并发上限/冷却期/日亏损熔断)+ 仓位数量计算
  trader.py            开仓 + 自监盘止盈止损
  state_store.py       本地持久化状态(data/state.json):信号去重/币种冷却/当前持仓/当日盈亏/成交记录
  status_file.py       轻量心跳状态文件(data/status.json),给面板判断主程序是否存活
  control_store.py     面板可写的运行时开关(data/controls.json):是否允许开新仓、币种黑名单、可覆盖的策略参数(白名单);主程序只读,面板是唯一写入方
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
| `ALLOW_TRADIFI_PERPETUALS` | 是否交易美股/大宗商品代币化合约(NVDA、TSLA、XAU 这类)。默认 `false`;需要先在币安网页/APP 签过 TradFi-Perps 协议才能改成 `true`,而且这套策略的信号打分是针对加密货币调的,美股品种的信号质量没验证过,交易时段也跟随美股,建议先小范围观察 |
| `POSITION_SIZE_PCT` | 跟 `LEVERAGE`、`STOP_LOSS_PCT` 一起决定每笔交易的目标风险金额(账户余额 × 这个百分比 × 杠杆 × `STOP_LOSS_PCT`),仓位数量由此反推,不是固定保证金。每次开仓前实时查余额计算 |
| `DRY_RUN_BALANCE_USDT` | 仅 `DRY_RUN=true` 时用到,模拟账户没有真实余额,用这个固定值当参考余额 |
| `LEVERAGE` | 杠杆倍数 |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | 止盈百分比 / 止损百分比,按币价格涨跌幅计算,不受杠杆影响。开启 ATR 止损后 `STOP_LOSS_PCT` 不再是止损上限,变成"目标风险金额"的校准基准 |
| `ATR_STOP_LOSS_ENABLED` | 是否用 ATR(平均真实波幅)动态计算止损空间,默认 `false` |
| `ATR_PERIOD` / `ATR_INTERVAL` | 算 ATR 用几根K线 / K线的时间粒度(如 `5m`) |
| `ATR_MULTIPLIER` | ATR 乘这个系数作为止损空间,常见取值 1.5 左右 |
| `ATR_MIN_STOP_PCT` | 止损空间的下限,避免波动率异常低时止损贴得太近 |
| `PROFIT_LOCK_AFTER_SECONDS` | 持仓超过这个秒数,且浮盈超过 `PROFIT_LOCK_MIN_PCT` 就平仓锁定收益(哪怕没到止盈线);亏损/持平的仓位不受影响 |
| `PROFIT_LOCK_MIN_PCT` | 触发"超时锁盈"所需的最小浮盈百分比,防止浮盈太薄、锁盈时被手续费+滑点吃成亏损 |
| `LADDER_TAKE_PROFIT_ENABLED` | 是否启用阶梯止盈(分批止盈),默认 `false`(碰到止盈线一次性全平) |
| `LADDER_FIRST_CLOSE_PCT` / `LADDER_STEP_CLOSE_PCT` | 第1档平仓比例 / 第2档及以后每档平掉剩余仓位的比例 |
| `LADDER_MAX_LEVELS` | 阶梯最多加到第几档,超过就封顶,剩余仓位交给保本止损/超时锁盈处理 |
| `LADDER_BREAKEVEN_BUFFER_PCT` | 第1档触发后止损上移到"开仓价 × (1+这个百分比)",留一点缓冲覆盖手续费+滑点 |
| `MAX_CONCURRENT_POSITIONS` | 同时最多持有几个仓位 |
| `SYMBOL_COOLDOWN_SECONDS` | 平仓后同一币种多久内不再重复进场 |
| `MAX_DAILY_LOSS_PCT` | 当日累计亏损达到 账户余额 × 这个百分比 就停止开新仓(熔断),已有仓位的止盈止损不受影响。跟 `POSITION_SIZE_PCT` 一样按余额百分比算,不是写死的美元数 |
| `DASHBOARD_PORT` | 监控面板监听端口 |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | 面板 Basic Auth 账号密码,必须改成自己的强密码,不能留空(留空面板拒绝启动) |

## 监控面板

跟交易主程序是完全独立的进程(`src/dashboard.py`),挂了也不影响交易逻辑。展示内容:

- 运行状态:当前模式(DRY_RUN/测试网/实盘)、心跳是否正常、交易所是否正被限流熔断
- 当前持仓:开仓价/标记价/止盈止损线/持仓时长/浮动盈亏
- 风控状态:当日盈亏 vs 熔断线、并发持仓数、冷却中的币种
- 绩效统计:胜率、平均止盈/止损、净盈亏(基于最近 200 笔结构化成交记录,不依赖解析日志文本)
- 最近成交记录、当前生效的风控参数

可操作的开关(通过 `control_store.py` 写入 `data/controls.json`,主程序每次开仓/盯仓检查都会重新从磁盘读取,不用重启即可生效):

- **暂停/恢复开新仓**:暂停后只影响新信号进场,已有持仓的止盈止损/超时锁盈/阶梯止盈完全不受影响,跟当日亏损熔断"只停新仓不动老仓"是同一个原则。适合手边没有电脑、想临时用手机控制要不要继续开新仓的场景。
- **币种黑名单**:输入币种(`RIF` 或 `RIFUSDT` 都可以,会自动补全 `USDT` 后缀),命中黑名单的信号直接跳过、不会开仓;对已经持有的该币种仓位没有影响,该平仓还是要手动去交易所/面板确认后自己操作。
- **策略参数在线修改**:止盈止损百分比、仓位比例、杠杆、ATR/阶梯止盈/超时锁盈/并发数/冷却时间/日亏损熔断线这些"跟交易策略行为直接相关"的参数,都可以直接在面板上改,改完立刻生效,不需要登录服务器改 `.env` 再重启。修改会先用跟 `.env` 加载时一样的合法性检查校验一遍(比如止损不能比 ATR 下限还小),不合法会直接拒绝并在面板上提示原因。**只影响新开的仓位和阶梯止盈往下一档推进的计算,不会追溯修改已经持有仓位当前这一档的止盈止损价位**——这两者是独立的:仓位一旦开出来,当前档位的止盈止损价格就已经写死在 `open_positions` 里了。
  - `.env` 里的值是"默认值",面板上没改过的参数显示的就是它;改过之后,面板会标"已覆盖",并提供"恢复默认"按钮改回 `.env` 里的值。
  - 出于安全考虑,API 密钥/`YBRADAR_SESSION_COOKIE`/`DRY_RUN`/`BINANCE_TESTNET`/面板自己的账号密码这些字段**不开放**面板修改——一部分是凭证不该经网页改,一部分(`DRY_RUN`/`BINANCE_TESTNET`/`ALLOW_TRADIFI_PERPETUALS`)是只在交易主程序启动那一刻构造具体的交易所客户端时生效一次,不重启进程改了也没用,面板上放出来只会造成"改了却没生效"的误解,所以从白名单里拿掉了。

面板本身**不提供下单或平仓的入口**,以上所有开关都只影响"要不要评估新信号 / 用什么参数评估",不会主动操作任何已有仓位。

本地单独运行:

```bash
PYTHONPATH=src python src/dashboard.py
```

浏览器/手机访问 `http://<地址>:<DASHBOARD_PORT>/`,会弹出账号密码框(HTTP Basic Auth)。

**安全提示**:Basic Auth 在没有配 HTTPS 的情况下是明文传输,不要用你在其他地方也在用的密码。面板不能下单/平仓,密码泄露不会导致直接的资金操作,但攻击者能看到仓位/盈亏等信息,也能暂停你的开仓或把任意币种拉黑——账号密码务必设成强密码,不要用示例值。

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
