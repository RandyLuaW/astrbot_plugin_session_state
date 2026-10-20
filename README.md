# 观望 / 活跃双状态：给群聊加一层节奏

> AstrBot 插件 · 接上话才进入活跃，靠四道刹车主动退场，按群活跃度调制开口概率

## 它解决什么问题

主动回复最难的不是「什么时候开口」，而是**什么时候闭嘴**。

只有概率抽签的插件会出现两个极端：概率高了变成刷屏话痨，概率低了整晚不吭声。而且一旦开口，往往黏着不走——一个人唱独角戏，群里没人接还在继续说。

这个插件把「开口」变成一个**有进入、有停留、有退出的状态机**：

```
观望态 ──抽签/关键词/被点名──> 活跃态 ──四道刹车──> 观望态
```

## 四道刹车

| 刹车 | 默认 | 作用 |
|---|---|---|
| 活跃时长上限 | 300 秒 | 到点强制退场，治「黏着不走」 |
| 冷场退场 | 90 秒无人说话 | 话题结束就收尾，不硬撑 |
| 连续发言轮数 | 6 轮 | 说满就主动收尾，避免独角戏 |
| 进入限流 | 冷却 600 秒 + 每小时 3 次 | 防止频繁插话 |

另有**活跃度调制**：群冷清时更容易开口（避免冷场），群热闹时更克制（避免打扰别人聊天）。

## 功能一览

- 观望 / 活跃双状态机，进入有门槛、停留有时限、退出有理由
- 活跃态内只跟随**本轮参与者**，不理会路过插话的人
- 关键词直接进入（不受抽签概率限制，仍受限流约束）
- `@` 别人或引用别人消息时不插话
- 复读过滤：与 bot 最近发言去掉标点后完全一致的消息不响应
- `[SKIP]` 标记：让模型判断「这条不是对我说的」时只输出标记，插件静默吞掉，群里无感
- 免打扰时段（支持跨零点）
- `dry_run` 演练模式：完整跑判定并打日志，但绝不发送
- 与 `active_reply_ctl` 自动互斥（检测到它在运行就降级为纯观察）

## 安装

### 方式一：插件市场（推荐）

WebUI 插件市场搜索「会话状态机」。

### 方式二：Releases 下载

从 [Releases](../../releases) 下载 zip，WebUI → 插件 → 上传安装。

### 方式三：手动克隆

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_session_state.git
```

无额外依赖。

## 核心配置

### 开关与互斥

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 总开关；关闭后只记录状态，不参与判定 |
| `dry_run` | bool | `false` | 演练模式，只打日志不发送（调参必开几天） |
| `coexist_mode` | string | `auto` | 与 `active_reply_ctl` 的关系：`auto` 检测到就降级 / `on` 强制降级 / `off` 不降级 |

### 四道刹车

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `active_max_duration` | float | `300` | 活跃态最长秒数 |
| `cold_exit_seconds` | float | `90` | 冷场退场阈值，0 关闭 |
| `max_bot_turns` | int | `6` | 活跃态内最多连续发言轮数，0 不限 |
| `enter_cooldown` | float | `600` | 两次进入的最小间隔（秒），被点名不受限 |
| `max_enters_per_hour` | int | `3` | 每小时最多主动进入几次 |

### 进入条件

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enter_probability` | float | `0.02` | 观望态下每条消息的基础触发概率（约 50 条开口 1 次） |
| `activity_modulation` | bool | `true` | 按群活跃度调制进入概率 |
| `activity_gain` | float | `0.5` | 调制幅度：活跃度 0 时 ×1.5、1 时 ×0.5 |
| `activity_window` | int | `300` | 活跃度统计窗口（秒） |
| `enter_on_keyword` | bool | `true` | 命中关键词直接进入 |
| `keyword_list` | list | `["肥鱼","大肥鲸","🐳","哦鲸鲸"]` | 触发关键词 |
| `skip_when_talking_to_others` | bool | `true` | @ 别人/引用别人时不插话 |
| `min_message_len` | int | `2` | 过短消息不接（纯图不受限） |

### 停留与跟随

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `follow_participants` | bool | `true` | 只跟随本轮参与者 |
| `follow_probability` | float | `0.85` | 跟随响应概率（别长期设 1.0） |
| `follow_window` | int | `180` | 参与者最后发言多久内算「仍在对话」 |

### 其他

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `quiet_hours` | list | `[]` | 免打扰时段，如 `23:00-08:00`，支持多条与跨零点 |
| `reread_filter` | bool | `true` | 复读过滤 |
| `skip_marker` | string | `[SKIP]` | 让模型表示「不是对我说」的标记 |
| `prompt_template` | text | 见插件 | 主动接话提示词，占位符 `{sender}` `{message}` `{marker}` |
| `follow_prompt_template` | text | 见插件 | 跟随对话提示词 |
| `debug` | bool | `false` | 输出判定明细（每次进退场与原因） |

## 调参建议

1. **先开 `dry_run: true` 跑两三天**，看日志里的「本会进入/退场」是否符合你的预期
2. **太吵**：降 `enter_probability`（0.02 → 0.01）、降 `max_enters_per_hour`
3. **太安静**：提 `enter_probability`、把 `activity_gain` 调大让冷场时更主动
4. **说话太多轮**：降 `max_bot_turns`（4 轮通常够）
5. **退场太早**：提 `cold_exit_seconds` 到 120~180

## 注意事项

- **与其他主动回复机制会打架**：和 `active_reply_ctl`、iris_memory 的 proactive 功能重叠，同时开会抢决策权。`coexist_mode: auto` 只能检测 `active_reply_ctl`，iris 那套需要自己关
- 状态在内存里，重启后回到观望态
- `[SKIP]` 依赖模型配合输出，提示词别改得太软
- 依赖 `aiocqhttp` 适配器的事件结构

## License

MIT
