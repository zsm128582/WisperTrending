# WhisperTrending

WhisperTrending 是一个面向北邮人论坛“悄悄话”板块的站内趋势索引工具。项目按固定时间间隔采集当天帖子列表与互动指标，写入 SQLite，基于“当前总热度 + 最近增长速度 + 轻微新帖加成”计算今日榜单，并生成可发布到论坛的预览内容。

当前阶段已经实现：

- 采集“悄悄话”帖子列表数据。
- 可选进入帖子页采集赞、踩等互动指标。
- 使用 SQLite 保存帖子、采样批次、采样指标、楼层互动与发布记录。
- 计算今日 Top 10。
- 生成论坛帖子内容 `preview.md`。
- 支持发布/修改论坛帖子，若当天已发布则修改已有文章。
- 已在 `test` 测试板块验证普通发帖、编辑和 `[md][/md]` Markdown 包裹格式。

本项目只做站内趋势索引，不保存或展示匿名帖正文，不展示发帖人信息。

## 目录结构

```text
.
├── config/
│   └── ranking.toml          # 排行榜统计窗口和权重配置
├── scripts/
│   ├── collector.py          # 登录论坛并采集板块数据
│   ├── publisher.py          # 发帖、改帖原型
│   └── publish_trending.py   # 生成榜单并发布或更新论坛帖子
├── tests/
│   └── test_ranking.py       # 排名算法单元测试
├── storage.py                # SQLite 存储层
├── ranking.py                # 排名算法与 preview.md 渲染
├── plan.md                   # 阶段计划
├── telnet_usage.md           # Telnet 访问记录，当前仅作参考
├── publishing_notes.md       # 发帖探索记录
├── requirements.txt          # 依赖说明
└── .env                      # 论坛测试账号，本地私密文件，不提交
```

## 环境配置

项目运行时主要使用 Python 标准库，SQLite 也通过标准库 `sqlite3` 使用。账号密码放在 `.env` 中：

```env
username=你的论坛用户名
password=你的论坛密码
```

`.env`、采集数据和 Cookie 文件已通过 `.gitignore` 排除。

如果需要显式安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 数据流

1. `scripts/collector.py` 登录北邮人论坛。
2. 采集 `IWhisper` 当天帖子列表。
3. 如启用 `--include-reactions`，继续访问帖子页采集赞、踩等互动指标。
4. `storage.py` 将每次采样写入 SQLite。
5. `ranking.py` 从 SQLite 读取今天数据，计算综合分、热度分、增长分。
6. `scripts/publish_trending.py` 生成 `preview.md`。
7. 如果不是 `--dry-run`，脚本会发布或修改论坛帖子。
8. `publication_records` 表记录当天已经发布的文章 ID，避免重复发帖。

## SQLite 存储

默认数据库路径建议使用：

```text
data/wisper_trending.sqlite3
```

初始化数据库：

```powershell
python storage.py --db data\wisper_trending.sqlite3 init
```

主要表：

- `snapshot_runs`：每次采集批次。
- `posts`：帖子基础信息。
- `post_snapshots`：每个帖子在某个采样时刻的统计指标。
- `article_floors`：帖子楼层基础信息。
- `floor_snapshots`：楼层赞、踩等互动快照。
- `publication_records`：每天发布到目标板块的文章 ID 与更新时间。

这些表支持“每天统计”和“固定时间间隔比较增长”的需求。排行榜计算会用今天的多次采样记录估算当前热度和最近窗口内的增长。

## 排名配置

配置文件位于：

```text
config/ranking.toml
```

常见配置项包括：

- `trend_window_minutes`：增长趋势统计窗口，例如近 30 分钟或近 2 小时。
- `top_n`：输出榜单数量，默认 Top 10。
- `weights`：综合分权重，包括当前热度、增长速度、新帖加成等。
- `freshness`：新帖加成衰减参数。

最终对外只展示一个综合榜，但内部会保留：

- `heat_score`：当前总热度。
- `growth_score`：最近增长趋势。
- `freshness_score`：轻微新帖加成。
- `score`：最终综合分。

## 采集脚本

采集并写入 SQLite：

```powershell
python scripts\collector.py --sqlite-db data\wisper_trending.sqlite3
```

同时保存 JSONL：

```powershell
python scripts\collector.py --sqlite-db data\wisper_trending.sqlite3 --out data\snapshots.jsonl
```

采集帖子页互动指标：

```powershell
python scripts\collector.py --include-reactions --article-delay 0.1 --sqlite-db data\wisper_trending.sqlite3 --out data\snapshots.jsonl
```

参数说明：

- `--include-reactions`：除帖子列表外，继续访问每个帖子详情页，提取赞、踩等指标。速度更慢，但榜单信息更完整。
- `--article-delay 0.1`：访问相邻帖子详情页之间等待 0.1 秒，用于降低请求压力。
- `--sqlite-db`：SQLite 数据库路径。
- `--out`：额外追加保存 JSONL 快照，便于排查和离线分析。

## 生成榜单预览

只生成 `preview.md`，不发帖：

```powershell
python ranking.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --preview-out preview.md
```

也可以通过发布脚本 dry run：

```powershell
python scripts\publish_trending.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --publish-board test --dry-run
```

`preview.md` 使用 Markdown 内容，并保留论坛 BBCode 颜色标签。例如：

```text
[color=#1f6feb]回复 12[/color]
[color=#2da44e]赞 34[/color]
[color=#cf222e]踩 5[/color]
```

当前展示策略：

- 不展示浏览量。
- 回复数据使用蓝色。
- 赞数据使用绿色。
- 踩数据使用红色。
- 不复制匿名帖正文。
- 不展示发帖人信息。

## 发布或更新榜单

发布到测试板块：

```powershell
python scripts\publish_trending.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --publish-board test
```

发布到“谈天说地”板块：

```powershell
python scripts\publish_trending.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --publish-board Talking
```

脚本行为：

- 先生成最新 `preview.md`。
- 查询 `publication_records` 是否已有当天记录。
- 如果没有当天记录，调用发帖接口新建主题。
- 如果已有当天文章 ID，调用编辑接口更新原帖。
- 发布内容会包在 `[md]...[/md]` 中，论坛会按 Markdown 渲染。

适合外部定时任务周期性调用：

```powershell
python scripts\collector.py --include-reactions --article-delay 0.1 --sqlite-db data\wisper_trending.sqlite3 --out data\snapshots.jsonl
python scripts\publish_trending.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --publish-board Talking
```

如果只想采集和生成预览，不想触发论坛发帖：

```powershell
python scripts\publish_trending.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --publish-board Talking --dry-run
```

## 单独测试发帖和改帖

测试板块地址：

```text
https://bbs.byr.cn/#!board/test
```

发测试帖：

```powershell
python scripts\publisher.py post-test --subject "WhisperTrending test" --content-file preview.md
```

编辑测试帖：

```powershell
python scripts\publisher.py edit-test ARTICLE_ID --subject "WhisperTrending edited" --content-file preview.md
```

获取编辑页表单，用于排查接口字段：

```powershell
python scripts\publisher.py fetch-edit-form ARTICLE_ID
```

## 可能调用的论坛接口

以下接口基于当前探索结果，属于北邮人论坛 Web 端实际使用的接口。论坛接口可能变动，调用前建议先在测试板块验证。

登录：

```http
POST https://bbs.byr.cn/user/ajax_login.json
```

常用表单字段：

```text
id=用户名
passwd=密码
s-mode=0
CookieDate=2
```

帖子列表：

```http
GET https://bbs.byr.cn/board/{board}
```

例如：

```text
https://bbs.byr.cn/board/IWhisper
https://bbs.byr.cn/board/Talking
https://bbs.byr.cn/board/test
```

帖子详情：

```http
GET https://bbs.byr.cn/article/{board}/{article_id}
```

发帖：

```http
POST https://bbs.byr.cn/article/{board}/ajax_post.json
```

常用表单字段：

```text
subject=标题
content=正文
```

编辑帖子：

```http
POST https://bbs.byr.cn/article/{board}/ajax_edit/{article_id}.json
```

常用表单字段：

```text
subject=标题
content=正文
```

互动指标提取：

- 主帖赞、踩：详情页 HTML 中的 `a-func-support`、`a-func-oppose`。
- 回复赞、踩：详情页 HTML 中的 `a-func-like`、`a-func-cai`。

项目当前通过解析帖子详情页 HTML 获取这些指标，而不是直接调用点赞或点踩动作接口。

## 测试

运行单元测试：

```powershell
python -m unittest discover -s tests -v
```

当前测试覆盖：

- 新帖。
- 老帖。
- 无增长。
- 突然增长。
- 样本缺失。

语法检查：

```powershell
python -m py_compile storage.py ranking.py scripts\collector.py scripts\publisher.py scripts\publish_trending.py tests\test_ranking.py
```

## Telnet 说明

`telnet_usage.md` 记录了 Telnet 访问论坛的方式。当前自动化实现优先使用 Web 接口，因为：

- 登录、采集、发帖、改帖接口更容易结构化调用。
- Web 端可以直接解析赞、踩等页面指标。
- 发帖和编辑已在测试板块验证。

Telnet 可作为后备方案继续保留，但目前不是主路径。

## 注意事项

- 正式发布前先使用 `--publish-board test` 验证。
- 周期任务建议先运行采集，再运行发布脚本。
- 不要提交 `.env`、Cookie、SQLite 数据库或采集原始数据。
- 控制 `--article-delay`，避免对论坛造成过高请求压力。
- 榜单内容应保持克制，只做站内趋势索引，不传播匿名正文。
- 如帖子已删除、锁定或不适合传播，应以原帖状态为准。
