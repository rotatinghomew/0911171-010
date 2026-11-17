# 定时指标简报

容器环境固定在仓库 Dockerfile 的 Python 3.11.14 镜像摘要，时区数据包固定为 requirements.txt 的版本。执行 `docker build -t local-analytics .` 后，`docker run --rm --network none local-analytics` 运行内置例子；`docker run --rm --network none local-analytics python -m unittest discover -s tests -v` 运行测试。构建时安装依赖，运行时无需联网；非容器环境使用 Python 3.11，先执行 `python -m pip install -r requirements.txt`，并把 `PYTHONTZPATH` 设为空字符串以使用锁定的时区数据而非宿主机数据库。

本服务完全离线：指标计算、定时调度与投递记录都在本地文件系统完成，不连接任何外部邮件平台，`LocalMailbox` 就是本地邮箱。

## 解决的对账问题

| 对账分歧来源 | 本服务的处理 |
| --- | --- |
| 口径说不清（分子/分母/去重对象/比较期间） | 指标定义为结构化 JSON，随每封消息送达；定义显式表达分子、分母、去重对象、过滤条件与比较期间偏移 |
| 各渠道百分比直接平均 | `evaluate_pivot` 的合计始终在合并明细上按同一口径重算（合并分子/合并分母、跨渠道重新去重），分组比率不参与平均 |
| 补数导致数字变化 | 事件带 `received_at`，发稿绑定数据截止点；补到的数据只在新截止点可见，产生**关联原稿的更正稿**，已送达内容永不覆盖 |
| 地区时区不同 | 每个区域配置 IANA 时区与本地发送时刻，业务日窗口按本地 00:00 计算（DST 日为 23/25 小时），比较期间也按本地业务日平移 |
| 夏令时跳变 | 跳空时刻显式顺延一个跳变量（如 02:30 不存在→03:30），重叠时刻 `fold=0` 取第一次；安排可在 `upcoming_runs` 中预先核对 |
| 缺失数据/零分母被当成 0 | 结果带 `status`：`ok` / `no_data` / `zero_denominator`，后两者 `value=null`；真实 0（有曝光无成交）正常出 0 |
| 重启后说不清哪份是原稿 | 原稿与更正都是不可变 issue 文件，内容指纹相同则幂等返回，不同则生成带 `original_issue_id`/`supersedes` 的更正链 |
| 进程中断、收件人变动 | 每个收件人一条独立投递台账（pending/delivered），重启后续投未完成项；收件人在发稿时快照，名册变动不换旧稿内容、不重发旧稿 |
| 数字无法追溯 | `trace_metric` 从已送达 issue 查回口径定义、窗口、截止点、明细编号范围、更正原因，可附完整明细行 |

## 模块

- `digest/metrics.py`：指标引擎。度量支持字段求和、去重计数、行数计数与等值过滤；`evaluate` / `evaluate_with_comparison` / `evaluate_pivot` 共用同一口径。
- `digest/scheduler.py`：`RegionSchedule` 区域调度、业务日窗口、比较期窗口、`next_run` / `upcoming_runs` 与 DST 安排。
- `digest/warehouse.py`：不可变事件仓，按 `received_at` 与数据截止点控制可见性，支持落盘/重载。
- `digest/dispatch.py`：持久化 `Dispatcher`。issue 与投递台账原子写入 `state/issues`、`state/deliveries`；断点续投、更正链、明细追溯。
- `digest/mailbox.py`：`LocalMailbox` 本地邮箱。同一投递编号+收件人内容必须字节一致，异内容复用编号直接拒绝；原子写入不留半截文件。
- `digest/fixtures.py`：三区域、两渠道、去重转化与补数的合成样例。
- `digest/demo.py`：端到端演示（固定时钟，重复运行逐字节一致），覆盖跨日 DST 调度、原稿、中断续投、名册变动、补数更正、数字追溯、口径状态。

## 运行

```bash
python -m unittest discover -s tests -v   # 44 个测试
python -m digest.demo                      # 端到端演示，落盘在 ./demo-state
```

演示落盘结构：

```
demo-state/
  mailbox/                 # 每个 (issue, 收件人) 一个 JSON 信封，含 content_hash
  dispatcher/issues/       # 不可变原稿与更正稿（更正稿带 original_issue_id 与 correction_reason）
  dispatcher/deliveries/   # 投递台账：pending/delivered、尝试次数、回执
```

## 指标定义示例

```json
{
  "id": "conversion_rate_v1",
  "name": "用户转化率",
  "numerator":   {"distinct": "user_id", "filter": {"ordered": true}},
  "denominator": {"distinct": "user_id"},
  "comparison":  {"offset_days": 7}
}
```

纯量指标（如成交金额）把 `denominator` 设为 `null`，分子用 `{"field": "amount", "filter": {...}}` 求和。
