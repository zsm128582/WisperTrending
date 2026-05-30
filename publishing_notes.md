# Publishing Exploration Notes

## Scope

Exploration was performed only on the BYR `test` board:

- Board URL: `https://bbs.byr.cn/#!board/test`
- Test article: `https://bbs.byr.cn/#!article/test/76274`

## Web Posting

Posting a new thread works through the AJAX endpoint:

```text
POST /article/{board}/ajax_post.json
```

Required form fields for a plain thread:

```text
subject=<thread title>
content=<thread body>
```

Successful response shape:

```json
{
  "ajax_st": 1,
  "ajax_msg": "发表成功",
  "list": [
    {"text": "主题:...", "url": "/article/test/76274"}
  ]
}
```

The article ID can be extracted from the `list[].url` entry.

## Web Editing

Editing an existing article works through:

```text
POST /article/{board}/ajax_edit/{article_id}.json
```

Required form fields for a plain edit:

```text
subject=<new thread title>
content=<new thread body>
```

Successful response shape:

```json
{
  "ajax_st": 1,
  "ajax_msg": "修改文章成功",
  "list": [
    {"text": "主题:...", "url": "/article/test/76274"}
  ]
}
```

## Prototype

The prototype lives in:

```text
scripts/publisher.py
```

Test commands:

```powershell
python scripts\publisher.py post-test
python scripts\publisher.py edit-test 76274
```

Both commands read credentials from `.env` and use the same AJAX login flow as the collector.

## Result

The test post was created and then edited successfully:

```text
article_id = 76274
final title = WhisperTrending 自动化编辑测试 2026-05-30 21:11:19
```

The edited title was verified by fetching `/article/test/76274`.

Markdown-in-article syntax was also tested successfully on the `test` board:

```text
article_id = 76275
title = WhisperTrending Markdown 格式测试 2026-05-30
body wrapper = [md]...[/md]
```

The test body used a heading, list, quote, link, and color tags inside the Markdown wrapper.

## Notes Before Production Publishing

- Keep automatic posting disabled for `Talking` until a manual approval step is added.
- Store the published article ID in SQLite so reruns edit the same daily post instead of creating duplicates.
- Prefer editing the same daily post over repeated replies or repeated new posts.
- Continue using a board allowlist; automated test commands should default to `test`.

## External Automation Calls

An external scheduler can run collection and publishing separately.

Collect snapshots into SQLite:

```powershell
python scripts\collector.py --include-reactions --article-delay 0.1 --sqlite-db data\wisper_trending.sqlite3 --out data\snapshots.jsonl
```

Generate preview only, without publishing:

```powershell
python scripts\publish_trending.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --publish-board test --dry-run
```

Publish or update today's post:

```powershell
python scripts\publish_trending.py --db data\wisper_trending.sqlite3 --config config\ranking.toml --publish-board Talking
```

`publish_trending.py` checks SQLite table `publication_records`.

- If no record exists for `(source_board, publish_board, run_date, kind)`, it creates a new thread.
- If a record exists, it edits the recorded `article_id`.
- The generated preview is written to `preview.md`.
- The actual forum body is wrapped as `[md]...[/md]`.

For safety, use `--publish-board test` during rehearsals.
