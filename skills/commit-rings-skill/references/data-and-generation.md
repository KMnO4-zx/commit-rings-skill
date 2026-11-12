# 数据与生成约定

## 来源与范围

脚本只需要 Python 3.10+ 标准库和 Git。可选的 GitHub CLI 登录用于 API 作者关联，不是必需依赖。

- 默认通过 `git ls-remote --symref` 找到 GitHub 默认分支，fetch 到独立 bare 缓存。`--filter=blob:none` 省略文件内容，保留完整祖先历史与树对象；不执行 checkout、依赖安装、hook 或目标仓库脚本。
- 默认范围是快照 SHA 可达的全部提交，含分支合入前的祖先和 merge；不是所有远端分支的并集，也不是仅 first-parent。
- `--local-repo` 读取指定 ref 或当前 HEAD，只读。浅克隆明确失败。没有提交的空仓库明确失败，不生成虚构数据。
- 按 committer timestamp（`%ct`）升序回放；同秒按 Git 的 parent-first 遍历顺序稳定排序。异常历史时间可能与拓扑顺序不一致，日期如实保留。
- 默认日期显示 UTC+8，数据含 `timezoneOffsetMinutes`；需要换时区时一起更新该字段和日期说明。
- Git 路径采用 NUL 分隔读取，支持空格、中文、tab 和换行。不可解码的字节用替换字符显示。

## 文件变化和分类

`git diff-tree --stdin --root -r --name-status --no-renames -z` 批量读取非合并提交。首次提交包含所有新增路径；重命名按 D+A，类型变化按 M。merge 保留一个柱子，但不重复累计其 diff。

历史路径数是非合并提交中曾被触及的去重路径数，包含已删除路径；不等于 HEAD 文件数，也不覆盖仅在合并解决时出现的路径。数据另存 `headFiles`，由快照树统计。

通用分类由路径规则推断：测试 → 文档 → 配置 → 资源 → 源码。提交按涉及路径最多的类别着色，平手按类别序号；merge 和空提交进入维护类。不按文件行数或 commit 文案猜测贡献质量。

## 身份、头像与降级

- GitHub REST `commits?sha=<固定 SHA>&per_page=100&page=N` 分页读取，使用每条 commit 的顶层 **author** 账号，不能使用 committer 或只看第一页。
- 同 GitHub account ID 合并，不靠 login 字符串、同名、相似邮箱或 GitHub 搜索猜人；API 返回 null 或没有查到时按原始 `(author name, author email)` 组合分组。没有将未匹配记录推断归入某个已知账号。
- raw author email 仅用于采集内存中的分组，不进入生成 JSON/HTML，也不写入作者 API 缓存。提交说明本身可能包含用户写入的邮箱，脚本不改写提交内容。
- 读取已有 `GH_TOKEN`、`GITHUB_TOKEN` 或 `gh auth token`，只放 API 请求头，不打印、不内嵌、不传给头像域名。Git fetch 独立使用用户现有 Git 凭据；API token 不会自动配置 Git 登录。
- 按 GitHub 数字 ID 下载 64 px 头像，内嵌 raster data URI。失败时使用缩写；不加载远程图片、Gravatar 或 CDN 库。
- 身份请求遇到鉴权、网络或限流错误，保留已得到的结果并继续。JSON 和页面说明显示 queried/matched/total 覆盖率，不能把 Git 身份条数称为全部真实人数。

缓存位于 `<cache-dir>/<owner>--<repo>/`：bare 仓库、`source.json`、按快照 SHA 缓存的作者映射和按 GitHub ID 缓存的头像。完整作者映射会复用，用户名是上次查询的结果；`--refresh-authors` 重新查询。`--offline` 不执行网络请求，已有缓存也可离线重建。

## HTML 数据契约

模板的 `@@HISTORY_JSON@@` 位于 `script#history-data[type=application/json]`。JSON 中的 `<`、U+2028/U+2029 转义，仓库品牌文字 HTML-escape。动态姓名、路径和提交说明使用 `textContent`，不得拼接不可信 HTML。

主要字段：

| 字段 | 含义 |
|---|---|
| `repo`, `branch`, `head`, `generated` | 仓库身份、ref、快照 SHA、采集 UTC 时间 |
| `commits[]` | `h` SHA、`t` 秒、`n` 原署名、`s` 标题、`m` merge、`a` 作者索引、`g` 分类索引、`f` 文件变化 |
| `commits[].f[]` | `[pathIndex, "A"/"M"/"D"]` |
| `authors[]` | `n`, `login`, `github_id`, `aliases`, `first`, `total`, `avatar` |
| `paths[]` | 去重后的历史路径表 |
| `categories[]` | 6 个 `{label,color}`，与 `g` 对应 |
| `identityCoverage` | `queried`, `matched`, `total`，区分查过但无账号和没查到 |
| `gitAuthorIdentities`, `headFiles` | 原始 Git 身份组合数、快照树文件数 |

生成文件保留只读调试入口 `window.commitRingsHistory.state` / `.points`，可检查 index、selected、ranking、tilt、yaw 与投影。它不修改数据。

## 验证重点

提交数量应与 `git rev-list --count <SHA>` 一致，作者总次数之和等于提交数；任意时间位置的榜单总数等于 `index+1`。还要覆盖一个提交、同名不同邮箱、同账号多签名、合并、重命名、删除、含特殊字符的路径，以及提交文本含 `</script>` 的情况。

大仓库目前逐提交绘制，没有静默采样；上万根柱子会有性能成本。更换渲染方式或聚合前应保留可核对的原始统计和清楚的显示口径。

实现依据：[Git diff-tree](https://git-scm.com/docs/git-diff-tree)、[GitHub List commits](https://docs.github.com/en/rest/commits/commits#list-commits)。
