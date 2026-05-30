# WisperTrending 阶段性计划

## 项目目标

北邮人论坛的“悄悄话”板块匿名、流量高，且不进入“今日十大”统计。项目目标是定时采集“悄悄话”今日帖子流量，按可解释算法生成今日十大，并在“谈天说地”板块发布 `WisperTrending` 榜单。

目标板块：

- 悄悄话：`https://bbs.byr.cn/#!board/IWhisper`
- 谈天说地：`https://bbs.byr.cn/#!board/Talking`

## 阶段 0：边界确认

目标：确认技术、账号和合规边界，避免账号风险。

任务：

- 确认北邮人论坛是否允许自动化访问、发帖，以及是否有频率限制。
- 只统计公开可见的帖子元信息和流量指标，不采集或传播用户隐私。
- 使用专用自动化测试账号，不把账号密码写入代码或日志。
- 明确展示字段，默认只展示标题、链接、回复数、浏览数、最近增量和排名分数。

阶段性验证：

- 能手动访问 `IWhisper` 和 `Talking`。
- 能确认机器人发布内容不会泄露匿名用户身份或正文隐私。
- `.env` 中存在自动化账号配置。

## 阶段 1：调研访问方式

目标：确认采集和发帖分别使用 Web、后端接口还是 Telnet。

任务：

- 探测 Web 页面实际使用的 AJAX/API 路径。
- 验证登录接口、Cookie 和会话有效期。
- 验证能否读取 `IWhisper` 板块列表。
- 评估发帖接口是否可用；若 Web 发帖复杂，再评估 Telnet 自动化。

阶段性验证：

- 能用脚本登录测试账号。
- 能请求 `IWhisper` 板块第一页并拿到帖子列表。
- 能确认帖子列表字段是否包含标题、ID、发表时间、回复数、浏览数、最后回复时间。
- 能确认后续翻页方式。

## 阶段 2：只读爬虫原型

目标：先可靠采集今日“悄悄话”主题数据，不做发帖。

任务：

- 从 `.env` 读取 `username` 和 `password`。
- 登录论坛并维护 Cookie。
- 拉取 `IWhisper` 今日所有主题，必要时翻页直到遇到非今日主题。
- 将原始快照保存到本地，优先使用 JSONL，后续再升级 SQLite。
- 每条快照记录采集时间，保证可用于计算浏览/回复增量。
- 可选抓取单帖页面，解析楼主和每条回复的赞/踩计数。

建议字段：

- `post_id`
- `title`
- `url`
- `board`
- `created_at`
- `last_reply_at`
- `reply_count`
- `view_count`
- `root_like_count`
- `root_dislike_count`
- `reply_like_count`
- `reply_dislike_count`
- `total_like_count`
- `total_dislike_count`
- `reaction_floors`
- `snapshot_at`

阶段性验证：

- 手动运行一次脚本，输出今日帖子数量和前几条摘要。
- 连续运行两次，快照可追加保存且不会覆盖原始数据。
- 跨页采集无明显遗漏。
- 对长帖抓取所有文章页，确认 `reaction_floor_count = reply_count + 1`。
- 登录失败、网络失败、接口结构变化时有清晰错误。

### 已验证的反应指标路径

Web 单帖页已经包含赞/踩计数，不需要调用投票接口：

- 楼主赞：`a-func-support`，接口路径形如 `/article/IWhisper/ajax_support/<id>.json`。
- 楼主踩：`a-func-oppose`，接口路径形如 `/article/IWhisper/ajax_oppose/<id>.json`。
- 回复赞：`a-func-like`，接口路径形如 `/article/IWhisper/ajax_voteup/<id>.json`。
- 回复踩：`a-func-cai`，接口路径形如 `/article/IWhisper/ajax_votedown/<id>.json`。

这些按钮文本里包含当前计数，例如 `(+1)` 或 `(7)`。长帖每页约 10 层楼，需要按 `ceil((reply_count + 1) / 10)` 抓取所有文章页后聚合。

Telnet 端口 `bbs.byr.cn:23` 可连接并返回 ANSI/GBK 首屏，但需要处理 Telnet 协商字节和终端控制序列。当前阶段 Web 单帖页更适合作为赞/踩采集路径。

## 阶段 3：趋势算法 MVP

目标：做一个简单、可解释、可调权重的“今日十大”算法。

初始公式：

```text
score = view_delta_30m * 1
      + reply_delta_30m * 20
      + total_reply_count * 5
      + freshness_bonus
```

说明：

- `view_delta_30m`：最近 30 分钟新增浏览量。
- `reply_delta_30m`：最近 30 分钟新增回复数。
- `total_reply_count`：今日累计回复数。
- `freshness_bonus`：给新帖少量加分，避免早发帖天然霸榜。

阶段性验证：

- 用历史快照生成 Top 10。
- 输出每个帖子的分数构成。
- 人工检查结果是否符合直觉。
- 调整权重后结果稳定。

## 阶段 4：生成帖子内容

目标：生成待发布正文，但不自动发帖。

示例：

```text
WisperTrending 今日悄悄话十大 - 2026-05-30 18:00

1. 标题 A
   热度：1234 | 回复：56 | 近30分钟新增浏览：320 | 新增回复：8
   链接：https://bbs.byr.cn/...
```

阶段性验证：

- 本地生成 `preview.txt`。
- 人工复制到论坛草稿，确认格式可读。
- 确认标题长度和正文长度不超过论坛限制。

## 阶段 5：半自动发布

目标：先由脚本生成内容，人确认后发布。

任务：

- 生成 `preview.txt`。
- 人工确认后复制到 `Talking` 发帖。
- 或由脚本准备发帖页面，最后一步人工确认。

阶段性验证：

- 测试发帖格式正常。
- 中文编码、换行和链接正常。
- 不会重复发布同一份榜单。

## 阶段 6：自动发布

目标：定时在 `Talking` 发布或更新 `WisperTrending`。

可选策略：

- 每天固定时间发一篇。
- 若支持编辑，全天更新同一篇。
- 若只能回复，限制频率，避免刷屏。

阶段性验证：

- 自动发布成功一次。
- 发布失败不会重复发帖。
- 日志记录发帖 ID。
- 再次运行能识别今天已经发布过。

## 阶段 7：定时任务与监控

目标：让系统稳定运行。

建议模块：

- `collector`：每 5-15 分钟采集一次。
- `ranker`：根据快照生成排名。
- `publisher`：生成或发布榜单。
- `storage`：保存 JSONL/SQLite 快照。
- `config`：账号、板块、频率和权重配置。
- `logs`：记录运行和错误。

阶段性验证：

- 本地连续跑一天。
- 登录过期后能重新登录或明确报警。
- 网络或接口异常不会导致高频重试。
- 采集日志和数据快照可追溯。
